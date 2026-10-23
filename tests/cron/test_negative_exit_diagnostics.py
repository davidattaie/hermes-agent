import json
from cron.scheduler_diagnostics import negative_exit_diagnostics


def test_negative_exit_records_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    out = negative_exit_diagnostics(-11, job_id="abc", execution_id="e1", process_pid=1)
    assert "SIGSEGV" in out
    rec = json.loads((tmp_path / "cron" / "worker_crashes.jsonl").read_text().splitlines()[-1])
    assert rec["signal"] == "SIGSEGV" and rec["job_id"] == "abc"
    assert "children_rusage" in rec


def test_nonnegative_exit_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert negative_exit_diagnostics(1) == ""
    assert negative_exit_diagnostics(None) == ""
    assert not (tmp_path / "cron" / "worker_crashes.jsonl").exists()
