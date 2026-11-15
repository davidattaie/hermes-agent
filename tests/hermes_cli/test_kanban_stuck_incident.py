"""Durable, one-per-episode dispatcher escalation (t_6b710339)."""
import asyncio
from argparse import Namespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


def incidents():
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key LIKE 'dispatcher-stuck:%'"
        ).fetchall()


def tick(**kwargs):
    return dispatch.note_stuck_tick(
        board="default", ready_pending=True, spawned=False,
        results=[dispatch.DispatchResult(respawn_guarded=[("t_wait", "active_pr")])],
        **kwargs,
    )


def test_threshold_and_no_duplicate():
    from datetime import UTC, datetime
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="waiting", assignee="olly")
    for _ in range(11):
        tick()
    assert incidents() == []
    tick()
    rows = incidents()
    assert len(rows) == 1
    assert rows[0]["assignee"] == "olly"
    assert rows[0]["status"] == "ready"
    assert tid in rows[0]["body"]
    timestamp = rows[0]["body"].split("First stuck (UTC): ")[1].splitlines()[0]
    assert datetime.fromisoformat(timestamp).tzinfo == UTC
    assert "active_pr=1" in rows[0]["body"]
    assert "12 consecutive ticks" in rows[0]["body"]
    for _ in range(20):
        tick()
    assert len(incidents()) == 1


@pytest.mark.parametrize("reset", [dict(spawned=True), dict(ready_pending=False), dict(paused=True)])
def test_reset_then_new_episode(reset, monkeypatch):
    monkeypatch.setattr(dispatch.time, "time", lambda: 1000.0)
    for _ in range(12):
        tick()
    dispatch.note_stuck_tick(board="default", ready_pending=reset.get("ready_pending", True),
                             spawned=reset.get("spawned", False), paused=reset.get("paused", False))
    for _ in range(11):
        tick()
    assert len(incidents()) == 1
    tick()
    rows = incidents()
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


def test_restart_mid_episode_reuses_key(monkeypatch):
    import os
    import subprocess
    import sys
    for _ in range(6):
        tick()
    with kbc.connect_closing() as conn:
        started = conn.execute("SELECT started FROM dispatcher_stuck_episodes").fetchone()[0]
    env = dict(os.environ, HERMES_KANBAN_HOME=str(kb.kanban_home()))
    command = [sys.executable, "-c",
               "from hermes_cli.kanban_db_dispatch import note_stuck_tick; "
               "[note_stuck_tick(board='default', ready_pending=True, spawned=False) for _ in range(6)]"]
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    row = incidents()[0]
    assert row["idempotency_key"] == f"dispatcher-stuck:default:{started}"
    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
    assert len(incidents()) == 1


def test_creation_failure_retries_same_episode(monkeypatch, caplog):
    for _ in range(11):
        tick()
    create = kb.create_task
    def fail(*args, **kwargs):
        raise OSError("incident write unavailable")
    monkeypatch.setattr(kb, "create_task", fail)
    tick()
    assert incidents() == []
    assert "stuck incident update failed" in caplog.text
    monkeypatch.setattr(kb, "create_task", create)
    tick()
    assert len(incidents()) == 1


def test_cli_callback_creates_incident(monkeypatch):
    from hermes_cli import kanban_ops
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="waiting", assignee="olly")
    monkeypatch.setattr(dispatch, "has_spawnable_ready", lambda conn: True)
    def daemon(**kwargs):
        for _ in range(12):
            kwargs["on_tick"](dispatch.DispatchResult())
    monkeypatch.setattr(dispatch, "run_daemon", daemon)
    args = Namespace(force=True, verbose=False, interval=60, max=None)
    assert kanban_ops._cmd_daemon(args) == 0
    assert len(incidents()) == 1
    assert tid in incidents()[0]["body"]


@pytest.mark.parametrize("value,threshold", [("3", 3), ("0", 12), ("-1", 12), ("oops", 12)])
def test_threshold_override(monkeypatch, value, threshold):
    monkeypatch.setenv("HERMES_KANBAN_STUCK_INCIDENT_TICKS", value)
    for _ in range(threshold - 1):
        tick()
    assert incidents() == []
    tick()
    assert len(incidents()) == 1


def test_project_incident_is_on_main_board(monkeypatch):
    from hermes_cli.kanban_db_boards import create_board
    create_board("project")
    with kbc.connect_closing(board="project") as conn:
        tid = kb.create_task(conn, title="project waiting", assignee="olly", board="project")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path("project")))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "project")
    for _ in range(12):
        dispatch.note_stuck_tick(board="project", ready_pending=True, spawned=False)
    rows = incidents()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"].startswith("dispatcher-stuck:project:")
    assert tid in rows[0]["body"]
    with kbc.connect_closing(board="project") as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_concurrent_ticks_create_one_incident():
    from concurrent.futures import ThreadPoolExecutor
    tick()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tick(), range(24)))
    assert len(incidents()) == 1


def test_archived_incident_is_not_recreated():
    for _ in range(12):
        tick()
    with kbc.connect_closing() as conn, kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived'")
    for _ in range(12):
        tick()
    assert len(incidents()) == 1


def test_gateway_loop_creates_incident(monkeypatch):
    from gateway import kanban_watchers as watchers
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    class Runner(watchers.GatewayKanbanWatchersMixin):
        _running = True
        ticks = 0
        def _kanban_dispatcher_boot(self):
            return dict, kb, {}
        async def _sleep_between_ticks(self, interval):
            self.ticks += 1
            self._running = self.ticks < 12
    async def no_delay(*args):
        return None
    monkeypatch.setattr(watchers.asyncio, "sleep", no_delay)
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(watchers, "_resolve_auto_decompose_settings", lambda load: (False, 0))
    monkeypatch.setattr(dispatch, "reap_worker_zombies", list)
    monkeypatch.setattr(dispatch, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(_KanbanDispatcher, "_board_slugs", lambda self: ["default"])
    monkeypatch.setattr(_KanbanDispatcher, "tick_once_for_board", lambda self, slug: dispatch.DispatchResult())
    kb.init_db()
    asyncio.run(Runner()._kanban_dispatcher_watcher())
    assert len(incidents()) == 1
