"""A structured same-PR rework permit survives commentary, but not execution."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch

PR = "https://github.com/example/project/pull/42"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 1800000000)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def rework(conn):
    tid = kb.create_task(conn, title="Ship existing PR", assignee="dev")
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    assert kb.request_review(conn, tid, summary="Review exact head", reviewer="reviewer",
                             expected_run_id=claimed.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert kb.request_changes(conn, tid, reason="Fix acceptance", expected_run_id=review.current_run_id) == (True, "dev")
    return tid


def test_same_second_structured_rework_survives_later_same_pr_comment(board):
    tid = rework(board)
    assert dispatch.check_respawn_guard(board, tid) is None
    kb.add_comment(board, tid, author="reviewer", body=f"CI details for {PR}; changes still required")
    assert dispatch.check_respawn_guard(board, tid) is None
    # A new PR identity is not covered by permission to repair the old one.
    kb.add_comment(board, tid, author="dev", body="https://github.com/example/project/pull/43")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_rework_permit_is_consumed_by_a_ready_claim_even_in_same_second(board):
    tid = rework(board)
    assert dispatch.check_respawn_guard(board, tid) is None
    assert kb.claim_task(board, tid) is not None
    # Model a crash/reclaim: it is not a fresh authorization to work the PR.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_same_second_assignment_orders_by_comment_receipt_not_wall_clock(board):
    tid = kb.create_task(board, title="Closer handoff", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    assert kb.assign_task(board, tid, "closer")
    assert dispatch.check_respawn_guard(board, tid) is None
    kb.add_comment(board, tid, author="closer", body=f"Pushed {PR}")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_comment_only_changes_is_not_a_rework_permit(board):
    tid = kb.create_task(board, title="No structured handoff", assignee="dev")
    kb.add_comment(board, tid, author="reviewer", body=f"review: changes {PR}")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_consumed_assignment_cannot_resurrect_an_old_permit(board):
    tid = kb.create_task(board, title="One closer attempt", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    assert kb.assign_task(board, tid, "closer")
    assert kb.claim_task(board, tid) is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_unconsumed_rework_survives_original_comment_window(board, monkeypatch):
    tid = rework(board)
    monkeypatch.setattr(kb.time, "time", lambda: 1800000000 + 86401)
    kb.add_comment(board, tid, author="reviewer", body=f"Still awaiting fixes to {PR}")
    assert dispatch.check_respawn_guard(board, tid) is None


@pytest.mark.parametrize("default_profile", ["dev", "closer"])
def test_default_assignment_cannot_reuse_superseded_permit(board, default_profile):
    tid = kb.create_task(board, title="Existing PR", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    assert kb.assign_task(board, tid, "closer")
    assert dispatch.check_respawn_guard(board, tid) is None
    assert kb.assign_task(board, tid, None)
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
    assert dispatch._apply_default_assignee(board, tid, default_profile, dry_run=False)
    task = kb.get_task(board, tid)
    assert task is not None and task.assignee == default_profile
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


@pytest.mark.parametrize("structured", [False, True])
def test_noop_preserves_unused_permit_without_refreshing_it(board, structured):
    if structured:
        tid = rework(board)
        profile = "dev"
    else:
        tid = kb.create_task(board, title="Existing PR", assignee="dev")
        kb.add_comment(board, tid, author="dev", body=PR)
        assert kb.assign_task(board, tid, "closer")
        profile = "closer"
    assert kb.assign_task(board, tid, profile)
    assert dispatch.check_respawn_guard(board, tid) is None
    kb.add_comment(board, tid, author=profile, body="https://github.com/example/project/pull/43")
    assert kb.assign_task(board, tid, profile)
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_structured_permit_cannot_survive_unassignment_and_default_return(board):
    tid = rework(board)
    assert kb.assign_task(board, tid, None)
    assert dispatch._apply_default_assignee(board, tid, "dev", dry_run=False)
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_new_assignment_cannot_fall_back_to_older_structured_permit(board):
    tid = rework(board)
    assert kb.assign_task(board, tid, "closer")
    kb.add_comment(board, tid, author="reviewer", body=f"Later details for {PR}")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
    assert kb.assign_task(board, tid, "dev")
    assert dispatch.check_respawn_guard(board, tid) is None
    kb.add_comment(board, tid, author="reviewer", body=f"More details for {PR}")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_legacy_tied_assignment_remains_guarded(board):
    tid = kb.create_task(board, title="Existing PR", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    assert kb.assign_task(board, tid, "closer")
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='assigned'",
                      ('{"assignee":"closer","from":"dev"}', tid))
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"


def test_noop_and_default_without_older_permit_remain_guarded(board):
    tid = kb.create_task(board, title="Existing PR", assignee="dev")
    kb.add_comment(board, tid, author="dev", body=PR)
    assert kb.assign_task(board, tid, "dev")
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
    assert kb.assign_task(board, tid, None)
    assert dispatch._apply_default_assignee(board, tid, "dev", dry_run=False)
    assert dispatch.check_respawn_guard(board, tid) == "active_pr"
