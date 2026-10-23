"""Private run-document diagnostics, kept separate from delivery summaries."""

from pathlib import Path
from traceback import format_exception

from agent.redact import redact_sensitive_text

# Enough for a Python traceback's last frames plus the exception line.
_WORKER_STDERR_TAIL_CHARS = 1500


def format_run_error(exc: BaseException) -> str:
    """Retain chained causes without capturing locals or exposing URL credentials."""
    traceback_text = redact_sensitive_text(
        "".join(format_exception(exc)), force=True, redact_url_credentials=True,
    )
    return f"## Error\n\n```\n{traceback_text}\n```\n"


def external_worker_stderr_tail(path: Path) -> str:
    """Redacted tail of a restart-safe cron worker's captured stderr, as a message suffix.

    The gateway used to spawn the worker with ``stderr=DEVNULL``, so a worker that died before
    publishing its acknowledgement (an import error, a missing module in the interpreter it was
    handed) only ever reported ``exit 1`` and the reporter had to guess the cause (#112729).
    Returns an empty string when nothing was captured.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    tail = text[-_WORKER_STDERR_TAIL_CHARS:]
    if len(text) > _WORKER_STDERR_TAIL_CHARS:
        tail = "…" + tail
    tail = redact_sensitive_text(tail, force=True, redact_url_credentials=True)
    return f"; worker stderr: {tail}"


def negative_exit_diagnostics(returncode, *, job_id=None, execution_id=None, process_pid=None) -> str:
    """Evidence for a restart-safe worker killed by a signal (negative exit code).

    Never raises. Returns a message suffix and appends a JSON line to
    ``<cron dir>/worker_crashes.jsonl``. Records job name/script, signal, the
    children's rusage, and where to look for kernel/core evidence (dmesg is often
    unreadable by non-root, so the journal/coredumpctl hints are included).
    """
    if not isinstance(returncode, int) or returncode >= 0:
        return ""
    try:
        import json
        import resource
        import shutil
        import signal as _signal
        import subprocess
        import time
        from hermes_constants import get_hermes_home

        try:
            sig_name = _signal.Signals(-returncode).name
        except ValueError:
            sig_name = f"signal {-returncode}"
        info = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "returncode": returncode,
            "signal": sig_name,
            "job_id": job_id,
            "execution_id": execution_id,
            "worker_pid": process_pid,
        }
        try:
            from cron.jobs import get_job
            job = get_job(str(job_id)) if job_id else None
            if job:
                info["job_name"] = job.get("name")
                info["script"] = job.get("script")
        except Exception:
            pass
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        info["children_rusage"] = {
            "maxrss_kib": ru.ru_maxrss, "utime_s": round(ru.ru_utime, 2),
            "stime_s": round(ru.ru_stime, 2), "minflt": ru.ru_minflt,
            "majflt": ru.ru_majflt,
        }
        hints = []
        if shutil.which("coredumpctl") and process_pid:
            hints.append(f"coredumpctl --user info {process_pid} (or: coredumpctl list)")
        if job_id:
            hints.append(
                f"journalctl --user -g 'hermes-worker-cron-{job_id}|segfault' --since '-10min'")
        try:
            dm = subprocess.run(["dmesg", "--time-format=iso", "-T"], capture_output=True,
                                text=True, timeout=3)
            if dm.returncode == 0:
                lines = [l for l in dm.stdout.splitlines()
                         if "segfault" in l or "traps:" in l or "invalid opcode" in l][-3:]
                info["dmesg_tail"] = lines
            else:
                hints.append("dmesg unreadable (kernel.dmesg_restrict); use journalctl -k")
        except Exception:
            hints.append("dmesg unavailable; use journalctl -k")
        info["hints"] = hints
        try:
            log = get_hermes_home() / "cron" / "worker_crashes.jsonl"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(info) + "\n")
        except OSError:
            pass
        return (f"; killed by {sig_name} (job={info.get('job_name') or job_id}, "
                f"script={info.get('script')}, maxrss_kib={ru.ru_maxrss}); "
                f"diagnostics in cron/worker_crashes.jsonl")
    except Exception:
        return ""
