"""Auto-decompose must not re-dispatch a human gate the block-loop breaker parked.

A worker blocks ``needs_input``/``capability``; after ``BLOCK_RECURRENCE_LIMIT``
same-cause re-blocks ``_route_block`` sends the card to ``triage`` for a human.
The gateway's auto-decompose sweep then re-specified the unchanged gate,
promoted it to ``ready``, the dispatcher spawned a worker, the worker re-blocked,
and the cycle repeated every tick (100+ recurrences observed on a live board).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: ["default"])
    profiles = [SimpleNamespace(name=n, description=f"{n} profile") for n in ("worker", "orchestrator")]
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: profiles)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    kb.init_db()

    aux_calls: list[str] = []

    def fake_call_llm(**kwargs):
        aux_calls.append(kwargs["messages"][1]["content"])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps(
            {"fanout": False, "title": None, "body": "same gate, rewritten", "assignee": "worker"})
        return resp

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    spawns: list[str] = []

    def spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    dispatcher = kwd._KanbanDispatcher(
        SimpleNamespace(DEFAULT_BOARD="default"),
        kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None),
    )
    return SimpleNamespace(aux_calls=aux_calls, spawns=spawns, spawn=spawn, dispatcher=dispatcher)


def _run(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _escalated_gate(conn, kind: str) -> str:
    """Drive a card through the real block -> unblock -> re-block loop into triage."""
    tid = kb.create_task(conn, title="human must approve handoff", assignee="worker")
    _run(conn, tid)
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT - 1):
        assert kb.block_task(conn, tid, reason="needs operator", kind=kind)
        assert kb.unblock_task(conn, tid)
        _run(conn, tid)
    assert kb.block_task(conn, tid, reason="needs operator", kind=kind)
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_kind) == ("triage", kind)
    return tid


def _tick(b, conn):
    b.dispatcher.auto_decompose_tick(3)
    kbd.dispatch_once(conn, spawn_fn=b.spawn)


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_escalated_gate_is_not_auto_decomposed_or_dispatched(board, kind):
    with kbc.connect() as conn:
        gate = _escalated_gate(conn, kind)
        recurrences = kb.get_task(conn, gate).block_recurrences
        for _ in range(5):
            _tick(board, conn)
        task = kb.get_task(conn, gate)
        assert board.aux_calls == []
        assert gate not in board.spawns
        # Still visible in triage for a human; loop evidence intact.
        assert (task.status, task.block_kind, task.block_recurrences) == ("triage", kind, recurrences)


def test_fresh_triage_still_auto_decomposes_beside_parked_gate(board):
    with kbc.connect() as conn:
        gate = _escalated_gate(conn, "needs_input")
        fresh = kb.create_task(conn, title="rough idea", assignee="worker", triage=True)
        _tick(board, conn)
        assert len(board.aux_calls) == 1 and fresh in board.aux_calls[0]
        assert board.spawns == [fresh]
        assert kb.get_task(conn, gate).status == "triage"


def test_dependency_wait_still_auto_resumes(board):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="worker")
        child = kb.create_task(conn, title="downstream", assignee="worker", parents=[parent])
        _tick(board, conn)
        assert board.spawns == [parent] and kb.get_task(conn, child).status == "todo"
        kb.complete_task(conn, parent, result="ok", force=True)
        _tick(board, conn)
        assert board.spawns == [parent, child]
        assert board.aux_calls == []


def test_explicit_operator_resolution_resumes_and_a_repeat_reparks(board):
    with kbc.connect() as conn:
        gate = _escalated_gate(conn, "needs_input")
        # Explicit per-task decompose (``hermes kanban decompose <id>`` / dashboard) is honoured.
        assert decomp.decompose_task(gate, author="operator").ok
        kbd.dispatch_once(conn, spawn_fn=board.spawn)
        assert board.spawns == [gate]
        # Worker hits the same unresolved gate again: back to triage, still parked.
        assert kb.get_task(conn, gate).status == "running"
        assert kb.block_task(conn, gate, reason="still needs operator", kind="needs_input")
        assert kb.get_task(conn, gate).status == "triage"
        calls = len(board.aux_calls)
        for _ in range(3):
            _tick(board, conn)
        assert len(board.aux_calls) == calls and board.spawns == [gate]


def test_operator_drag_out_of_triage_resumes(board):
    """A human moving the card to todo (dashboard drag) resumes it without the aux LLM."""
    from plugins.kanban.dashboard import plugin_api

    with kbc.connect() as conn:
        gate = _escalated_gate(conn, "capability")
        assert plugin_api._drag_to(conn, gate, "todo")
        kb.recompute_ready(conn)
        kbd.dispatch_once(conn, spawn_fn=board.spawn)
        assert board.spawns == [gate] and board.aux_calls == []


def test_unit_is_escalated_block_gate():
    from hermes_cli.kanban_specify import is_escalated_block_gate as gate

    def t(status="triage", kind="needs_input", n=kb.BLOCK_RECURRENCE_LIMIT):
        return SimpleNamespace(status=status, block_kind=kind, block_recurrences=n)

    assert gate(t()) and gate(t(kind=None)) and gate(t(kind="capability"))
    assert not gate(t(n=0)) and not gate(t(n=1)) and not gate(t(status="todo"))
    assert not gate(t(kind="dependency"))
