"""Regression coverage for needs_input parking and auto-decomposer admission."""
import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()


def twice(conn, kind="needs_input"):
    tid = kb.create_task(conn, title="await answer", assignee="worker")
    assert kb.block_task(conn, tid, reason="answer required", kind=kind)
    assert kb.unblock_task(conn, tid)
    assert kb.block_task(conn, tid, reason="answer required", kind=kind)
    return tid


def test_parking_events_and_auto_tick(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = twice(conn)
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_kind, task.block_recurrences) == ("blocked", "needs_input", 2)
        events = kb.list_events(conn, tid)
        loop = [e for e in events if e.kind == "block_loop_detected"]
        blocked = [e for e in events if e.kind == "blocked"]
        assert len(loop) == 1
        assert blocked[-1].id > loop[0].id
        assert blocked[-1].payload["loop_detected"] is True
        assert blocked[-1].payload["recurrences"] == 2
        before = [e.id for e in events]
        kb.recompute_ready(conn)
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **k: pytest.fail("parked card reached LLM"))
    assert tid not in decomp.list_triage_ids()
    for candidate in decomp.list_triage_ids():
        decomp.decompose_task(candidate, author="auto-decomposer")
    assert not decomp.decompose_task(tid, author="auto-decomposer").ok
    with kbc.connect_closing() as conn:
        assert [e.id for e in kb.list_events(conn, tid)] == before
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("kind", ["transient", "capability", None])
def test_other_kinds_still_triage(board, kind):
    with kbc.connect_closing() as conn:
        tid = twice(conn, kind)
        assert kb.get_task(conn, tid).status == "triage"
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "blocked"]) == 1
    assert tid in decomp.list_triage_ids()


def legacy(conn):
    tid = twice(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
    return tid


@pytest.mark.parametrize("author", ["dashboard", "operator", "default", "discord-relay"])
def test_new_operator_reply_is_eligible(board, author):
    with kbc.connect_closing() as conn:
        tid = legacy(conn)
        kb.add_comment(conn, tid, author, "Proceed with the agreed route")
    assert tid in decomp.list_triage_ids()


@pytest.mark.parametrize("author,body,when", [
    ("auto-decomposer", "Specified", 101),
    ("system", "Proceed", 101),
    ("worker", "Proceed", 101),
    ("operator", "Old answer", 99),
    ("operator", "Same-second answer", 100),
    *[("default", p + " generated note", 101) for p in decomp._SYSTEM_PREFIXES],
])
def test_guard_rejects_non_operator_or_stale_comments(board, monkeypatch, author, body, when):
    with kbc.connect_closing() as conn:
        tid = legacy(conn)
        cid = kb.add_comment(conn, tid, author, body)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at=? WHERE id=?", (when, cid))
        before = [e.id for e in kb.list_events(conn, tid)]
    assert tid not in decomp.list_triage_ids()
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **k: pytest.fail("guard reached LLM"))
    outcome = decomp.decompose_task(tid, author="auto-decomposer")
    assert not outcome.ok and outcome.reason == "needs_input awaiting operator"
    with kbc.connect_closing() as conn:
        assert [e.id for e in kb.list_events(conn, tid)] == before


@pytest.mark.parametrize("fanout", [False, True])
def test_recheck_after_llm_before_either_writer(board, monkeypatch, fanout):
    with kbc.connect_closing() as conn:
        tid = legacy(conn)
        kb.add_comment(conn, tid, "operator", "Proceed")
    routing = decomp._Routing("worker", "worker", True, [], {"worker"})
    monkeypatch.setattr(decomp, "_load_routing", lambda **k: routing)

    def aux(*args, **kwargs):
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=9999999999 WHERE task_id=?", (tid,))
        return json.dumps({"fanout": fanout, "title": "updated", "body": "updated",
                           "tasks": [{"title": "child"}]}), ""

    monkeypatch.setattr(decomp, "_call_aux", aux)
    outcome = decomp.decompose_task(tid, author="auto-decomposer")
    assert not outcome.ok and outcome.reason == "needs_input awaiting operator"
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert not [e for e in kb.list_events(conn, tid) if e.kind in {"specified", "decomposed"}]


@pytest.mark.parametrize("author", ["auto-decomposer", "operator"])
def test_single_promotion_with_operator_authorization(board, monkeypatch, author):
    with kbc.connect_closing() as conn:
        tid = legacy(conn)
        if author == "auto-decomposer":
            kb.add_comment(conn, tid, "operator", "Proceed")
    monkeypatch.setattr(decomp, "_load_routing", lambda **k: decomp._Routing("worker", "worker", True, [], {"worker"}))
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **k: ('{"fanout":false,"body":"updated"}', ""))
    outcome = decomp.decompose_task(tid, author=author)
    assert outcome.ok, outcome.reason
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status in {"todo", "ready"}


def test_missing_block_event_fails_closed(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy no audit", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
        kb.add_comment(conn, tid, "operator", "unanchored reply")
    assert tid not in decomp.list_triage_ids()


def test_manual_specify_remains_explicit_action(board):
    with kbc.connect_closing() as conn:
        tid = legacy(conn)
        assert kb.specify_triage_task(conn, tid, body="operator clarified", author="operator")


def test_reply_query_uses_fresh_block_and_unblocks_once(board):
    # Same latest-block query and task:block_event_id key as reply-unblock;
    # no real cron, CLI, Discord, or production database touched.
    with kbc.connect_closing() as conn:
        tid = twice(conn)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=100 WHERE id=?", (events[0].id,))
            conn.execute("UPDATE task_events SET created_at=200 WHERE id=?", (events[-1].id,))
        old = kb.add_comment(conn, tid, "operator", "old answer")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at=150 WHERE id=?", (old,))
        handled = set()

        def tick():
            rows = conn.execute(
                "SELECT t.id, e.id, e.created_at FROM tasks t JOIN task_events e ON e.id = "
                "(SELECT max(id) FROM task_events WHERE task_id=t.id AND kind='blocked') "
                "WHERE t.status='blocked'"
            ).fetchall()
            count = 0
            for row in rows:
                key = f"{row[0]}:{row[1]}"
                reply = conn.execute(
                    "SELECT 1 FROM task_comments WHERE task_id=? AND created_at>=? "
                    "AND author='operator' LIMIT 1", (row[0], row[2]),
                ).fetchone()
                if key not in handled and reply:
                    handled.add(key)
                    assert kb.unblock_task(conn, row[0])
                    count += 1
            return count

        assert tick() == 0  # old reply cannot answer the fresh block
        kb.add_comment(conn, tid, "operator", "new answer")
        assert tick() == 1
        assert tick() == 0
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, tid).block_recurrences == 2
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, kind="needs_input", reason="still unanswered")
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_recurrences == 3


def _cli_unblock(tid, *extra):
    from hermes_cli import kanban as kcli
    ns = argparse.Namespace(task_ids=[tid], task_id=tid, reason=None, decision=None)
    for k, v in zip(extra[::2], extra[1::2]):
        setattr(ns, k, v)
    return kcli._cmd_unblock(ns)


def test_cli_unblock_refuses_parked_card_without_reply(board, capsys):
    with kbc.connect_closing() as conn:
        tid = twice(conn)
    _cli_unblock(tid)
    assert "awaiting-go" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_cli_unblock_allows_operator_reply_or_decision(board):
    with kbc.connect_closing() as conn:
        a, b = twice(conn), twice(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=100")
        kb.add_comment(conn, a, "discord-relay", "GO")
    _cli_unblock(a)
    _cli_unblock(b, "decision", "yolo: proceed with option 1")
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "ready"
        assert kb.get_task(conn, b).status == "ready"
        assert any(c.body.startswith("DECISION:") for c in kb.list_comments(conn, b))


def test_cli_unblock_unparked_card_unaffected(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.block_task(conn, tid, reason="q", kind="needs_input")
    _cli_unblock(tid)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
