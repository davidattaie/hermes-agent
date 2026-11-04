"""Intervention triage must not become executable work through spec generation."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def held_task(conn):
    tid = kb.create_task(conn, title="NO-GO: physical result still missing", assignee="dev")
    assert kb.block_task(conn, tid, reason="Needs measured GO", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    assert kb.block_task(conn, tid, reason="Still no measured GO", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_intervention_triage_is_not_an_auto_decomposition_candidate(board):
    held = held_task(board)
    idea = kb.create_task(board, title="New rough idea", triage=True)
    assert held not in decomp.list_triage_ids()
    assert idea in decomp.list_triage_ids()


@pytest.mark.parametrize("fanout", [False, True])
def test_intervention_hold_survives_direct_specification_and_fanout(board, fanout):
    held = held_task(board)
    if fanout:
        result = decompose_triage_task(
            board, held, root_assignee="dev",
            children=[{"title": "Run the unsafe step", "assignee": "dev"}],
            author="auto-decomposer",
        )
        assert result is None
    else:
        assert not kb.specify_triage_task(board, held, title="Ready now", author="auto-decomposer")
    task = kb.get_task(board, held)
    assert task.status == "triage"
    assert task.block_kind == "needs_input"
    assert task.block_recurrences == 2
    assert len(kb.list_tasks(board)) == 1


def test_held_decomposition_does_not_call_the_aux_model(board, monkeypatch):
    held = held_task(board)
    def forbidden(*args, **kwargs):
        pytest.fail("intervention hold must be rejected before any model call")
    monkeypatch.setattr(decomp, "_call_aux", forbidden)
    assert not decomp.decompose_task(held).ok
