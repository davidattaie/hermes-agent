"""Replay audit hold shapes, not live rows or live tasks.

The four cases are the unchanged-precondition loops documented in coordination.md.
All writes are to a fixture DB under tmp_path. No production board is opened.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_decompose as decomp


@pytest.mark.parametrize("case,kind,reason", [
    ("Emailify t_ed2e6d5c", "needs_input", "Owner will test manually; physical iPhone result not received"),
    ("Healthify t_41e6265d", "needs_input", "Executor offline; coordinator lacks execution authority"),
    ("Healthify t_a85a9e28", "needs_input", "Bundle received, but Docker GO explicitly not granted"),
    ("Muse t_b606de13", None, "Measured NO-GO remains; NOT unblocking by fiat"),
])
def test_unchanged_historical_precondition_never_becomes_a_new_idea(tmp_path, monkeypatch, case, kind, reason):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=case, assignee="dev")
        assert kb.block_task(conn, tid, kind=kind, reason=reason)
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(conn, tid, kind=kind, reason=reason)
        before = len(kb.list_events(conn, tid))
        runs_before = [run.id for run in kb.list_runs(conn, tid)]
        # Repeated ticks find no executable triage candidate and start no worker.
        for _ in range(4):
            assert tid not in decomp.list_triage_ids()
            assert not dispatch.dispatch_once(conn, dry_run=True).spawned
        assert kb.get_task(conn, tid).status == "triage"
        assert len(kb.list_events(conn, tid)) == before
        assert [run.id for run in kb.list_runs(conn, tid)] == runs_before
