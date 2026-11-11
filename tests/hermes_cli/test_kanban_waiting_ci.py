"""waiting_ci hold: park on pending exact-head CI, auto re-queue to review.

The gh API layer is mocked (``_gh_get`` for the verb, ``fetch`` for the sweep).
"""
from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_waiting_ci as wci
from hermes_cli.kanban_pr_acceptance import _GateAuthError

A, B = "a" * 40, "b" * 40
REPO, PR = "acme/repo", 7


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def make_running(conn, assignee=None) -> str:
    tid = kb.create_task(conn, title="review PR", assignee=assignee)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()
    return tid


def run(name="build", status="in_progress", conclusion=None, sha=A, i=1, started=None):
    return {"id": i, "name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "started_at": started}


def ci(head=A, runs=(), statuses=(), state="open", merged=False):
    return {"head_sha": head, "state": state, "merged": merged,
            "runs": list(runs), "statuses": list(statuses)}


def gh_get_for(head=A, runs=(), state="open", statuses=()):
    """Fake ``_gh_get`` speaking the three endpoints fetch_ci_state reads."""
    def get(endpoint, *, paginate=False, profile_home=None):
        if "/check-runs" in endpoint:
            rs = [{"id": r["id"], "name": r["name"], "status": r["status"],
                   "conclusion": r["conclusion"], "head_sha": head, "started_at": r["started_at"]}
                  for r in runs]
            return [{"total_count": len(rs), "check_runs": rs}]
        if "/status" in endpoint:
            return {"statuses": list(statuses)}
        return {"head": {"sha": head}, "state": state, "merged": False}
    return get


def park(conn, tid, **kw):
    return wci.wait_ci(conn, tid, REPO, PR, A,
                       get=gh_get_for(runs=[run()]), **kw)


def status(conn, tid):
    return kb.get_task(conn, tid).status


def events(conn, tid, kind):
    return conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()


# 1 ---------------------------------------------------------------------------
def test_queued_checks_create_hold(conn):
    tid = make_running(conn)
    rec = park(conn, tid)
    assert status(conn, tid) == "scheduled"
    hold = wci.get_hold(conn, tid)
    assert (hold["repo"], hold["pr"], hold["head_sha"], hold["state"]) == (REPO, PR, A, "active")
    assert hold["max_wait_s"] == wci.DEFAULT_MAX_WAIT_S and rec["pending"] == ["build"]
    ev = kb._json_dict(events(conn, tid, "scheduled")[0]["payload"])
    assert ev["kind"] == "waiting_ci" and ev["resume_status"] == "review"
    assert ev["waiting_ci"]["head_sha"] == A


def test_legacy_pending_status_counts_as_pending(conn):
    tid = make_running(conn)
    done = run(status="completed", conclusion="success")
    wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(
        runs=[done], statuses=[{"context": "ci/x", "state": "pending", "id": 1}]))
    assert status(conn, tid) == "scheduled"


@pytest.mark.parametrize("final", ["success", "failure", "error"])
def test_legacy_status_only_requeues_when_completed(conn, final):
    tid = make_running(conn)
    wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(
        statuses=[{"context": "ci/x", "state": "pending", "id": 1}]))
    assert status(conn, tid) == "scheduled"
    done = [{"context": "ci/x", "state": final, "id": 2}]
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=(
        lambda r, p, h: wci.fetch_ci_state(r, p, h, get=gh_get_for(statuses=done))))
    assert out == {tid: "requeued"} and status(conn, tid) == "review"
    ev = kb._json_dict(events(conn, tid, "waiting_ci_requeued")[0]["payload"])
    assert ev["checks"]["statuses"] == [{"context": "ci/x", "state": final}]


def test_no_checks_at_all_still_hits_budget_guard(conn):
    tid = make_running(conn)
    park(conn, tid, max_attempts=1)
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci())
    assert out == {tid: "blocked"} and status(conn, tid) == "blocked"
    ev = events(conn, tid, "blocked")[0]["payload"]
    assert "no check runs appeared" in ev


# 2 ---------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_all_completed_requeues_to_review_not_done(conn, conclusion):
    tid = make_running(conn)
    park(conn, tid)
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(
        runs=[run(status="completed", conclusion=conclusion)]))
    assert out == {tid: "requeued"}
    assert status(conn, tid) == "review"
    ev = kb._json_dict(events(conn, tid, "waiting_ci_requeued")[0]["payload"])
    assert ev["head_sha"] == A and ev["checks"]["runs"][0]["conclusion"] == conclusion
    assert wci.get_hold(conn, tid)["state"] == "requeued"
    # idempotent: a second sweep touches nothing
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: 1 / 0) == {}
    assert status(conn, tid) == "review"


def test_pending_stays_and_bumps_attempts(conn):
    tid = make_running(conn)
    park(conn, tid)
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(runs=[run()]))
    assert out == {tid: "waiting"} and status(conn, tid) == "scheduled"
    assert wci.get_hold(conn, tid)["attempts"] == 1


def test_poll_interval_rate_limits(conn):
    tid = make_running(conn)
    park(conn, tid)
    assert wci.kanban_waiting_ci_sweep(conn, fetch=lambda *a: 1 / 0) == {}  # just parked


# 3 ---------------------------------------------------------------------------
def test_new_head_rebinds_and_ignores_old_sha_completion(conn):
    tid = make_running(conn)
    park(conn, tid)
    conn.execute("UPDATE kanban_waiting_ci SET attempts=9 WHERE task_id=?", (tid,))
    conn.commit()
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(
        head=B, runs=[run(status="completed", conclusion="success", sha=A)]))
    assert out == {tid: "rebound"} and status(conn, tid) == "scheduled"
    hold = wci.get_hold(conn, tid)
    assert hold["head_sha"] == B and hold["attempts"] == 0 and hold["rebinds"] == 1
    ev = kb._json_dict(events(conn, tid, "waiting_ci_rebound")[0]["payload"])
    assert ev["old_head_sha"] == A and ev["head_sha"] == B
    # the old-SHA completion never requeues; stale-sha runs on the new head are ignored too
    out = wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(
        head=B, runs=[run(status="completed", conclusion="success", sha=A)]))
    assert status(conn, tid) == "scheduled" and out == {tid: "waiting"}


def test_head_moving_mid_read_is_reported_as_new_head():
    calls = {"n": 0}

    def get(endpoint, *, paginate=False, profile_home=None):
        if "/check-runs" in endpoint:
            return [{"total_count": 0, "check_runs": []}]
        if "/status" in endpoint:
            return {"statuses": []}
        calls["n"] += 1
        return {"head": {"sha": A if calls["n"] == 1 else B}, "state": "open"}
    state = wci.fetch_ci_state(REPO, PR, get=get)
    assert state["head_sha"] == B and state["runs"] == []


# 4 ---------------------------------------------------------------------------
def test_plain_blocked_card_is_never_touched(conn):
    tid = make_running(conn)
    assert kb.block_task(conn, tid, reason="need a human", kind="needs_input")
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: 1 / 0) == {}
    assert status(conn, tid) == "blocked"
    assert kb.get_task(conn, tid).block_kind == "needs_input"


@pytest.mark.parametrize("exc,reason,kind", [
    (_GateAuthError("HTTP 404 on x"), wci.REASON_AUTH, "capability"),
    (wci._HttpError(422), wci.REASON_UNPROCESSABLE, "needs_input"),
    (wci._HttpError(403, "actions disabled"), wci.REASON_ACTIONS_OFF, "capability"),
])
def test_non_queue_conditions_become_named_block(conn, exc, reason, kind):
    tid = make_running(conn)
    park(conn, tid)

    def boom(*a):
        raise exc
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=boom) == {tid: "blocked"}
    t = kb.get_task(conn, tid)
    assert t.status == "blocked" and t.block_kind == kind
    ev = kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])
    assert ev["waiting_ci_reason"] == reason and ev["resume_status"] == "review"
    # idempotent and no re-park: sweep ignores it, wait_ci refuses the same SHA
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: 1 / 0) == {}
    with pytest.raises(wci.WaitCiRefused, match="already ended"):
        wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(runs=[run()]))
    assert status(conn, tid) == "blocked"


def test_blocked_card_unblocks_back_to_review(conn):
    tid = make_running(conn)
    park(conn, tid)
    wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(state="closed"))
    assert kb.unblock_task(conn, tid) and status(conn, tid) == "review"


def test_pr_closed_or_merged_blocks(conn):
    tid = make_running(conn)
    park(conn, tid)
    wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(state="closed", merged=True))
    ev = kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])
    assert ev["waiting_ci_reason"] == wci.REASON_PR_CLOSED


def test_infra_errors_retry_then_block(conn):
    tid = make_running(conn)
    park(conn, tid)

    def boom(*a):
        raise OSError("net down")
    for _ in range(wci.INFRA_ERROR_LIMIT - 1):
        assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=boom) == {tid: "infra_error"}
        assert status(conn, tid) == "scheduled"
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=boom) == {tid: "blocked"}
    assert kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])["waiting_ci_reason"] == wci.REASON_API


def test_cancelled_with_no_rerun_blocks(conn):
    tid = make_running(conn)
    park(conn, tid)
    wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(
        runs=[run(status="completed", conclusion="cancelled")]))
    assert status(conn, tid) == "blocked"
    assert kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])["waiting_ci_reason"] == wci.REASON_CANCELLED


def test_manual_unblock_voids_hold(conn):
    tid = make_running(conn)
    park(conn, tid)
    assert kb.unblock_task(conn, tid)
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: 1 / 0) == {tid: "released"}
    assert wci.get_hold(conn, tid)["state"] == "released"


# 5 ---------------------------------------------------------------------------
def test_time_budget_exceeded_blocks_with_named_reason(conn):
    tid = make_running(conn)
    park(conn, tid, max_wait_s=60)
    since = wci.get_hold(conn, tid)["since"]
    out = wci.kanban_waiting_ci_sweep(conn, now=since + 61, force=True, fetch=lambda *a: ci(runs=[run()]))
    assert out == {tid: "blocked"}
    t = kb.get_task(conn, tid)
    assert t.status == "blocked" and t.block_kind == "needs_input"
    ev = kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])
    assert ev["waiting_ci_reason"] == wci.REASON_BUDGET and ev["reason"].startswith(wci.REASON_BUDGET)


def test_attempt_budget_exceeded_blocks(conn):
    tid = make_running(conn)
    park(conn, tid, max_attempts=2)
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(runs=[run()])) == {tid: "waiting"}
    assert wci.kanban_waiting_ci_sweep(conn, force=True, fetch=lambda *a: ci(runs=[run()])) == {tid: "blocked"}


def test_runs_stuck_queued_means_no_runner(conn):
    tid = make_running(conn)
    park(conn, tid)
    since = wci.get_hold(conn, tid)["since"]
    out = wci.kanban_waiting_ci_sweep(conn, now=since + wci.QUEUED_STUCK_S + 1, force=True,
                                      fetch=lambda *a: ci(runs=[run(status="queued")]))
    assert out == {tid: "blocked"}
    assert kb._json_dict(events(conn, tid, "blocked")[-1]["payload"])["waiting_ci_reason"] == wci.REASON_NO_RUNNER


# 6 ---------------------------------------------------------------------------
def test_wait_ci_refused_when_nothing_pending(conn):
    tid = make_running(conn)
    with pytest.raises(wci.WaitCiRefused, match="no check run is pending"):
        wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(runs=[run(status="completed", conclusion="success")]))
    assert status(conn, tid) == "running" and wci.get_hold(conn, tid) is None


def test_wait_ci_refused_on_head_mismatch(conn):
    tid = make_running(conn)
    with pytest.raises(wci.WaitCiRefused, match="PR head is"):
        wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(head=B, runs=[run()]))
    assert status(conn, tid) == "running"


@pytest.mark.parametrize("repo,pr,sha", [("nope", 1, A), (REPO, 0, A), (REPO, 1, "abc")])
def test_wait_ci_validates_arguments(conn, repo, pr, sha):
    tid = make_running(conn)
    with pytest.raises(wci.WaitCiRefused):
        wci.wait_ci(conn, tid, repo, pr, sha, get=gh_get_for(runs=[run()]))


def test_wait_ci_refused_when_pr_closed_or_auth(conn):
    tid = make_running(conn)
    with pytest.raises(wci.WaitCiRefused, match="not open"):
        wci.wait_ci(conn, tid, REPO, PR, A, get=gh_get_for(runs=[run()], state="closed"))

    def denied(*a, **k):
        raise _GateAuthError("HTTP 404")
    with pytest.raises(wci.WaitCiRefused, match="capability"):
        wci.wait_ci(conn, tid, REPO, PR, A, get=denied)
    assert status(conn, tid) == "running"


def test_cli_wait_ci_refusal_exit_code(conn, monkeypatch, capsys):
    from hermes_cli import kanban as cli
    tid = make_running(conn)
    live = importlib.import_module("hermes_cli.kanban_waiting_ci")  # other suites may reload modules
    monkeypatch.setattr(live, "_gh_get", gh_get_for(head=B, runs=[run()]))
    rc = cli._cmd_wait_ci(argparse.Namespace(task_id=tid, repo=REPO, pr=PR, head_sha=A, max_wait_hours=6.0))
    assert rc != 0 and "wait-ci refused" in capsys.readouterr().err


def test_cli_wait_ci_parks(conn, monkeypatch, capsys):
    from hermes_cli import kanban as cli
    tid = make_running(conn)
    live = importlib.import_module("hermes_cli.kanban_waiting_ci")
    monkeypatch.setattr(live, "_gh_get", gh_get_for(runs=[run()]))
    rc = cli._cmd_wait_ci(argparse.Namespace(task_id=tid, repo=REPO, pr=PR, head_sha=A, max_wait_hours=2.0))
    assert rc == 0 and status(conn, tid) == "scheduled"
    assert wci.get_hold(conn, tid)["max_wait_s"] == 7200


def test_dispatch_tick_runs_sweep(conn, monkeypatch):
    tid = make_running(conn)
    park(conn, tid)
    live = importlib.import_module("hermes_cli.kanban_waiting_ci")
    monkeypatch.setattr(live, "fetch_ci_state", lambda *a, **k: ci(
        runs=[run(status="completed", conclusion="success")]))
    conn.execute("UPDATE kanban_waiting_ci SET last_checked = 0 WHERE task_id=?", (tid,))
    conn.commit()
    from hermes_cli import kanban_db_dispatch as kd
    kd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)
    assert status(conn, tid) == "review"
