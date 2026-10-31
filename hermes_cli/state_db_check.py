"""Read-only SQLite watchdog; also installed as scripts/state_db_quick_check.py.

Standard library only so scheduled checks survive broken application imports.
Cron owns failure delivery; successful runs emit only a summary on stderr.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone


def quick_check(path: Path) -> str:
    """Never open writable, create a missing DB, restore, or reindex."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute("PRAGMA quick_check(1)").fetchall()
    finally:
        conn.close()
    return "ok" if rows == [("ok",)] else repr(rows)


def send_alert(root: Path, message: str) -> None:
    """Use the weekly watchdog's existing Hermes delivery target for immediate alerts."""
    try:
        data = json.loads((root / "cron" / "jobs.json").read_text())
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        targets = {job.get("failure_deliver") or job.get("deliver") for job in jobs
                   if Path(job.get("script") or "").name == "state_db_quick_check.py"}
        targets.discard(None)
        targets.discard("local")
        targets.discard("origin")  # no origin context exists in a CLI update
        executable = shutil.which("hermes")
        if not targets or not executable:
            raise RuntimeError("no routable state_db_quick_check.py cron alert target or hermes executable")
        for target in sorted(targets):
            result = subprocess.run([executable, "send", "--to", target, message],
                                    env={**os.environ, "HERMES_HOME": str(root)},
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(f"hermes send exited {result.returncode}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"STATE.DB ALERT DELIVERY FAILED: {exc}", file=sys.stderr)


def check_all(root: Path, *, alert: bool = False, verbose: bool = False) -> int:
    """Check every profile independently, log/alert failures, never alter database contents."""
    homes = [("default", root)]
    profiles = root / "profiles"
    failures = []
    try:
        if profiles.is_dir():
            homes.extend((f"profiles/{p.name}", p) for p in sorted(profiles.iterdir())
                         if not p.name.startswith(".") and not p.is_symlink() and p.is_dir())
    except OSError as exc:
        failures.append(f"profile enumeration: {exc}")
    checked = 0
    for label, home in homes:
        db = home / "state.db"
        if not db.exists() and not db.is_symlink():
            continue
        checked += 1
        try:
            verdict = quick_check(db)
        except (OSError, sqlite3.Error) as exc:
            verdict = str(exc)
        if verdict != "ok":
            failures.append(f"{label}: {verdict}")
        elif verbose:
            print(f"{label}: ok")
    if not failures:
        print(f"state.db quick_check: {checked} databases ok", file=sys.stderr)
        return 0
    message = "STATE.DB CHECK FAILED — no repair attempted\n" + "\n".join(failures)
    print(message, flush=True)
    try:
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "state-db-check.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except OSError as exc:
        print(f"STATE.DB FAILURE LOG WRITE FAILED: {exc}", file=sys.stderr)
    if alert:
        send_alert(root, message)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return check_all(args.root, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
