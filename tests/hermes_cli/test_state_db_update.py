"""Read-only update guards and per-profile recovery snapshots."""
import sqlite3
import pytest
from pathlib import Path

from hermes_cli import backup_sqlite as backup
from hermes_cli import main, update_cmd, update_lock, update_owning_install


def make_db(home):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (body TEXT)")
        conn.execute("INSERT INTO messages VALUES ('saved')")


def test_unreadable_profile_roster_is_alerted(tmp_path, monkeypatch, capsys):
    from hermes_cli import state_db_check as check
    make_db(tmp_path)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    original = Path.iterdir
    def iterdir(path):
        if path == profiles:
            raise PermissionError("profile roster inaccessible")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    alerts = []
    monkeypatch.setattr(check, "send_alert", lambda root, message: alerts.append(message))
    assert check.check_all(tmp_path, alert=True) == 1
    assert "profile roster inaccessible" in alerts[0]


def test_check_closes_read_only_connection(tmp_path, monkeypatch):
    from hermes_cli import state_db_check as check
    make_db(tmp_path)
    real_connect = sqlite3.connect
    connections = []
    class Connection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            return super().close()
    def connect(database, **kwargs):
        assert database.endswith("?mode=ro")
        conn = real_connect(database, factory=Connection, **kwargs)
        connections.append(conn)
        return conn
    monkeypatch.setattr(check.sqlite3, "connect", connect)
    assert check.quick_check(tmp_path / "state.db") == "ok"
    assert connections[0].closed


def test_no_db_backup_parser():
    import argparse
    from hermes_cli.subcommands.update import build_update_parser
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    assert parser.parse_args(["update", "--no-db-backup"]).no_db_backup


def test_finish_update_runs_read_only_check(tmp_path, monkeypatch):
    from hermes_cli import state_db_check, update_finish
    make_db(tmp_path)
    calls = []
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(state_db_check, "check_all", lambda root, **kw: calls.append((root, kw)) or 1)
    monkeypatch.setattr(update_cmd, "_run_post_update_maintenance", lambda **kw: False)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *a: None)
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *a: None)
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda *a, **kw: None)
    update_finish.finish_update(root=tmp_path, assume_yes=True, gateway_mode=False,
                                pre_update_snapshot_id=None, had_desktop_app_before_update=False,
                                pre_update_version=None, plan=None, windows_resume=None)
    assert calls == [(tmp_path, {"alert": True})]


def test_cmd_update_snapshots_under_lock_before_impl(tmp_path, monkeypatch):
    from types import SimpleNamespace
    make_db(tmp_path)
    events = []
    class Lock:
        holder = None
        def acquire(self):
            events.append("lock")
            return True
        def release(self):
            events.append("unlock")
    monkeypatch.setattr(update_owning_install, "retarget_to_owning_install", lambda root: None)
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda *args: None)
    monkeypatch.setattr(update_lock, "UpdateLock", Lock)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: tmp_path)
    def impl(*args, **kwargs):
        assert list(tmp_path.glob("state.db.pre-update-[0-9]*.bak"))
        events.append("impl")
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", impl)
    main.cmd_update(SimpleNamespace())
    assert events == ["lock", "impl", "unlock"]


def test_backup_failure_requires_confirmation(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli import update_state_db as guard
    make_db(tmp_path)
    (tmp_path / "state.db").write_bytes(b"corrupt")
    monkeypatch.setattr(guard.sys.stdin, "isatty", lambda: False)
    import pytest
    with pytest.raises(SystemExit) as exc:
        guard.before_update(SimpleNamespace(), root=tmp_path)
    assert exc.value.code == 1
    guard.before_update(SimpleNamespace(yes=True), root=tmp_path)
    assert "WARNING" in capsys.readouterr().out
    guard.before_update(SimpleNamespace(no_db_backup=True), root=tmp_path)


def test_post_check_reports_corruption_without_repair(tmp_path, monkeypatch, capsys):
    from hermes_cli import state_db_check as check
    make_db(tmp_path)
    broken = tmp_path / "profiles" / "broken"
    broken.mkdir(parents=True)
    db = broken / "state.db"
    db.write_bytes(b"broken database")
    original = db.read_bytes()
    alerts = []
    monkeypatch.setattr(check, "send_alert", lambda root, message: alerts.append(message))
    assert check.check_all(tmp_path, alert=True) == 1
    assert db.read_bytes() == original
    assert len(alerts) == 1
    assert "profiles/broken" in alerts[0]
    assert "STATE.DB CHECK FAILED" in capsys.readouterr().out
    assert "profiles/broken" in (tmp_path / "logs" / "state-db-check.log").read_text()


def test_sweep_continues_after_corrupt_profile(tmp_path):
    make_db(tmp_path)
    broken = tmp_path / "profiles" / "broken"
    broken.mkdir(parents=True)
    (broken / "state.db").write_bytes(b"not sqlite")
    make_db(tmp_path / "profiles" / "healthy")
    results = backup.preflight_all_state_dbs(tmp_path)
    assert results["default"]["path"]
    assert results["profiles/broken"]["error"]
    assert results["profiles/healthy"]["path"]


def test_update_backups_keep_three_without_pruning_desktop(tmp_path):
    make_db(tmp_path)
    desktop = backup.preflight_state_db(tmp_path)["path"]
    paths = [backup.preflight_state_db(tmp_path, keep=3, prefix="state.db.pre-update-")["path"] for _ in range(4)]
    assert not Path(paths[0]).exists()
    assert all(Path(p).exists() for p in paths[1:])
    assert Path(desktop).exists()
    with sqlite3.connect(paths[-1]) as conn:
        assert conn.execute("SELECT * FROM messages").fetchall() == [("saved",)]
        assert conn.execute("PRAGMA quick_check(1)").fetchall() == [("ok",)]


def test_failed_backup_prunes_nothing(tmp_path, monkeypatch):
    import pytest
    make_db(tmp_path)
    previous = []
    for i in range(4):
        path = tmp_path / f"state.db.pre-update-2000-01-0{i + 1}.bak"
        path.write_bytes(b"retain even on failure")
        previous.append(path)
    monkeypatch.setattr(backup, "_safe_copy_db", lambda *a: False)
    with pytest.raises(RuntimeError, match="safe copy failed"):
        backup.preflight_state_db(tmp_path, keep=3, prefix="state.db.pre-update-")
    assert all(p.exists() for p in previous)
    assert not list(tmp_path.glob("*.partial"))


def test_corrupt_copy_is_not_published(tmp_path, monkeypatch):
    import pytest
    make_db(tmp_path)
    def corrupt_copy(source, destination):
        destination.write_bytes(b"not sqlite")
        return True
    monkeypatch.setattr(backup, "_safe_copy_db", corrupt_copy)
    with pytest.raises(sqlite3.DatabaseError):
        backup.preflight_state_db(tmp_path, keep=3, prefix="state.db.pre-update-")
    assert not list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*.partial"))


def test_weekly_script_stdlib_only_silent_success_and_failure_exit(tmp_path):
    import subprocess
    import sys
    from hermes_cli import state_db_check
    make_db(tmp_path)
    script = state_db_check.__file__
    command = [sys.executable, "-I", "-S", script, "--root", str(tmp_path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "1 databases ok" in result.stderr
    verbose = subprocess.run(command + ["--verbose"], capture_output=True, text=True, timeout=20)
    assert verbose.returncode == 0
    assert "default: ok" in verbose.stdout
    (tmp_path / "state.db").write_bytes(b"broken")
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "STATE.DB CHECK FAILED" in result.stdout


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_immediate_alert_uses_watchdog_delivery_target(tmp_path, monkeypatch, encoding):
    import json
    import subprocess
    from hermes_cli import state_db_check as check
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(json.dumps({"jobs": [
        {"script": "state_db_quick_check.py", "deliver": "discord:#ops"}]}), encoding=encoding)
    calls = []
    monkeypatch.setattr(check.shutil, "which", lambda name: "/bin/hermes")
    def send(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(check.subprocess, "run", send)
    check.send_alert(tmp_path, "failure message")
    assert calls[0][0] == ["/bin/hermes", "send", "--to", "discord:#ops", "failure message"]
    assert calls[0][1]["env"]["HERMES_HOME"] == str(tmp_path)


def test_interactive_failure_confirmation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from hermes_cli import update_state_db as guard
    make_db(tmp_path)
    (tmp_path / "state.db").write_bytes(b"broken")
    monkeypatch.setattr(guard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(SystemExit):
        guard.before_update(SimpleNamespace(), root=tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    guard.before_update(SimpleNamespace(), root=tmp_path)


def test_fresh_home_does_not_create_database(tmp_path):
    from hermes_cli import state_db_check
    assert backup.preflight_all_state_dbs(tmp_path)["default"]["path"] is None
    assert state_db_check.check_all(tmp_path) == 0
    assert not (tmp_path / "state.db").exists()
