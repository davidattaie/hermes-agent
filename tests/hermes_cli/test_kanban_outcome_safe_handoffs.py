"""Declared negative/unknown stage outcomes must never satisfy downstream gates."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize("outcome", ["no_go", "hold", "needs_input", "unknown"])
def test_negative_outcome_cannot_complete_or_release_children(board, outcome):
    parent = kb.create_task(board, title="Measured safety gate")
    child = kb.create_task(board, title="Enable live writes", parents=[parent])
    with pytest.raises(ValueError, match="handoff"):
        kb.complete_task(board, parent, summary="Assessment finished", force=True,
                         metadata={"handoff": {"outcome": outcome, "evidence": ["report.json"]}})
    assert kb.get_task(board, parent).status == "ready"
    assert kb.get_task(board, child).status == "todo"


def test_reviewer_cannot_approve_away_persisted_no_go_by_omitting_metadata(board):
    tid = kb.create_task(board, title="Measured gate", assignee="dev")
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="Measurement complete; NO-GO",
                             metadata={"handoff": {"outcome": "no_go", "evidence": ["gate.json"]}},
                             reviewer="reviewer", expected_run_id=run.current_run_id)
    with pytest.raises(ValueError, match="handoff"):
        kb.complete_task(board, tid)
    assert kb.get_task(board, tid).status == "review"


@pytest.mark.parametrize("handoff", [{}, {"outcome": "accepted"}, {"outcome": "accepted", "evidence": [" "]}])
def test_declared_handoff_requires_accepted_outcome_and_nonblank_evidence(board, handoff):
    tid = kb.create_task(board, title="Outcome")
    with pytest.raises(ValueError, match="handoff"):
        kb.complete_task(board, tid, summary="Done", metadata={"handoff": handoff})


def test_accepted_outcome_receipt_releases_dependency(board):
    tid = kb.create_task(board, title="Accepted gate")
    child = kb.create_task(board, title="Allowed next stage", parents=[tid])
    assert kb.complete_task(board, tid, summary="Measured GO",
                            metadata={"handoff": {"outcome": "accepted", "evidence": ["gate.json#sha256=abc"]}})
    assert kb.get_task(board, child).status == "ready"


def test_rework_cannot_erase_outcome_contract_by_omission(board):
    tid = kb.create_task(board, title="Safety outcome", assignee="dev")
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="Measured NO-GO", reviewer="reviewer",
                             metadata={"handoff": {"outcome": "no_go", "evidence": ["gate.json"]}},
                             expected_run_id=run.current_run_id)
    assert kb.reopen_review_task(board, tid)
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="Assessment reformatted", reviewer="reviewer",
                             expected_run_id=run.current_run_id)
    with pytest.raises(ValueError, match="handoff"):
        kb.complete_task(board, tid)
