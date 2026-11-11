"""``waiting_ci`` hold: park a card while the exact PR head's CI is unfinished.

A hold is the existing ``scheduled`` status plus a row in ``kanban_waiting_ci``
(repo, PR, held head SHA, budget, attempts) and a ``scheduled`` event whose
payload carries ``kind: waiting_ci`` / ``resume_status: review``. No new status.

The deterministic dispatcher tick calls :func:`kanban_waiting_ci_sweep`. For
each active hold it reads the PR head and ALL check runs/statuses of the held
SHA (not only branch-protection "required" ones: private repos without Pro have
none). Outcomes:

* head moved            -> ``waiting_ci_rebound`` (hold follows the new SHA)
* every run completed   -> ``scheduled`` -> ``review`` + ``waiting_ci_requeued``.
                           Never ``done``; the reviewer judges green itself.
* still pending         -> stay (attempts/last_checked bumped)
* budget/PR closed/any non-queue condition -> real ``blocked`` with a NAMED
  reason (``waiting_ci_*``). A converted hold is never re-parked for that SHA.

Network I/O happens outside SQLite transactions.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import _GateAuthError, _assignee_profile_home, _gh_env

logger = logging.getLogger(__name__)

DEFAULT_MAX_WAIT_S = 6 * 3600
DEFAULT_MAX_ATTEMPTS = 400
CHECK_INTERVAL_S = 60          # min seconds between polls of one hold
QUEUED_STUCK_S = 3600          # only-queued runs for this long => no runner
INFRA_ERROR_LIMIT = 5          # consecutive API infra errors before blocking
MAX_HOLDS_PER_TICK = 5
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")

REASON_BUDGET = "waiting_ci_budget_exceeded"
REASON_PR_CLOSED = "waiting_ci_pr_closed"
REASON_AUTH = "waiting_ci_auth"
REASON_ACTIONS_OFF = "waiting_ci_actions_disabled"
REASON_UNPROCESSABLE = "waiting_ci_http_422"
REASON_NO_RUNNER = "waiting_ci_no_runner"
REASON_CANCELLED = "waiting_ci_cancelled_no_rerun"
REASON_API = "waiting_ci_api_error"


class WaitCiRefused(ValueError):
    """The caller should proceed (or block) normally instead of parking."""


class _HttpError(RuntimeError):
    def __init__(self, status: int, text: str = ""):
        super().__init__(f"HTTP {status}")
        self.status, self.text = status, text


# ---- GitHub read layer (the single seam tests mock) -------------------------

def _gh_get(endpoint: str, *, paginate: bool = False, profile_home: Optional[str] = None):
    """``gh api`` GET as the card's profile. 401/403/404 -> ``_GateAuthError``;
    other HTTP errors -> ``_HttpError``; anything else propagates (infra)."""
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        out = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30, check=True,
                             env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        err = exc.stderr or ""
        m = re.search(r"HTTP (\d{3})", err)
        if m and m[1] in ("401", "403", "404"):
            low = err.lower()
            if m[1] == "403" and "actions" in low and "disabled" in low:
                raise _HttpError(403, "actions disabled") from None
            raise _GateAuthError(f"HTTP {m[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        if m:
            raise _HttpError(int(m[1]), "actions disabled" if "actions" in err.lower() and "disabled" in err.lower() else "") from None
        raise
    return json.loads(out.stdout)


def fetch_ci_state(repo: str, pr: int, profile_home: Optional[str] = None,
                   *, get: Optional[Callable] = None) -> dict:
    """PR head/state plus ALL check runs and legacy statuses for that head.

    Returns ``{head_sha, state, merged, runs:[{name,status,conclusion,
    head_sha,started_at,id}], statuses:[{context,state}]}``. Check runs are
    paginated with ``filter=latest`` and ``total_count`` is verified; the PR
    head is re-read after the runs so a push mid-read is reported as the NEW
    head with ``runs`` discarded.
    """
    get = get or _gh_get
    pull = get(f"repos/{repo}/pulls/{pr}", profile_home=profile_home)
    sha = pull["head"]["sha"]
    state = {"head_sha": sha, "state": pull.get("state"), "merged": bool(pull.get("merged")),
             "runs": [], "statuses": []}
    if state["state"] != "open":
        return state
    pages = get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                paginate=True, profile_home=profile_home)
    runs = [r for page in pages for r in page["check_runs"]]
    if pages and len({r["id"] for r in runs}) != pages[0]["total_count"]:
        raise ValueError("incomplete check-run pagination")
    spages = get(f"repos/{repo}/commits/{sha}/status?per_page=100", profile_home=profile_home)
    latest: dict[str, dict] = {}
    for s in (spages.get("statuses") or []):
        if s["context"] not in latest or s.get("id", 0) > latest[s["context"]].get("id", 0):
            latest[s["context"]] = s
    state["runs"] = [{"id": r["id"], "name": r["name"], "status": r.get("status"),
                      "conclusion": r.get("conclusion"), "started_at": r.get("started_at"),
                      "head_sha": r.get("head_sha", sha)} for r in runs]
    state["statuses"] = [{"context": c, "state": s["state"]} for c, s in sorted(latest.items())]
    again = get(f"repos/{repo}/pulls/{pr}", profile_home=profile_home)["head"]["sha"]
    if again != sha:
        return {**state, "head_sha": again, "runs": [], "statuses": []}
    return state


def _pending(state: dict) -> list[str]:
    names = [r["name"] for r in state["runs"] if r["status"] != "completed"]
    names += [s["context"] for s in state["statuses"] if s["state"] == "pending"]
    return names


def _summary(state: dict) -> dict:
    return {"runs": [{"name": r["name"], "status": r["status"], "conclusion": r["conclusion"]}
                     for r in state["runs"]],
            "statuses": state["statuses"]}


# ---- store ------------------------------------------------------------------

def get_hold(conn, task_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM kanban_waiting_ci WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


# ---- wait-ci verb -----------------------------------------------------------

def wait_ci(conn, task_id: str, repo: str, pr: int, head_sha: str, *,
            expected_run_id: Optional[int] = None, max_wait_s: int = DEFAULT_MAX_WAIT_S,
            max_attempts: int = DEFAULT_MAX_ATTEMPTS, get: Optional[Callable] = None) -> dict:
    """Park *task_id* as a waiting_ci hold. Raises :class:`WaitCiRefused` when
    the PR head differs from *head_sha*, nothing is pending, the PR is closed,
    or a hold for this exact SHA was already converted to blocked."""
    if not _REPO.fullmatch(repo or ""):
        raise WaitCiRefused("repo must be OWNER/REPO")
    if not isinstance(pr, int) or pr < 1:
        raise WaitCiRefused("pr must be a positive integer")
    if not _SHA.fullmatch(head_sha or ""):
        raise WaitCiRefused("head_sha must be a full 40-char lowercase SHA")
    task = kb.get_task(conn, task_id)
    if task is None:
        raise WaitCiRefused(f"unknown task {task_id}")
    prior = get_hold(conn, task_id)
    if prior and prior["state"] == "converted" and prior["head_sha"] == head_sha:
        raise WaitCiRefused(
            f"a waiting_ci hold for {head_sha[:12]} already ended as {prior['outcome']}; "
            "do not re-park the same SHA - proceed or kanban_block with the named reason")
    try:
        state = fetch_ci_state(repo, pr, _assignee_profile_home(task.assignee), get=get)
    except _GateAuthError as exc:
        raise WaitCiRefused(f"cannot read CI state ({exc}); use kanban_block kind=capability") from None
    except (_HttpError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise WaitCiRefused(f"CI state unavailable ({type(exc).__name__}); use kanban_block") from None
    if state["state"] != "open":
        raise WaitCiRefused("PR is not open; proceed or block normally")
    if state["head_sha"] != head_sha:
        raise WaitCiRefused(f"PR head is {state['head_sha']}, not {head_sha}; review the current head instead")
    pending = _pending(state)
    if not pending:
        raise WaitCiRefused("no check run is pending on this head; proceed with review (or kanban_block for a non-CI reason)")
    now = int(time.time())
    reason = f"waiting_ci {repo}#{pr} @ {head_sha[:12]}: pending {', '.join(pending[:5])}"
    record = {"repo": repo, "pr": pr, "head_sha": head_sha, "since": now,
              "max_wait_s": max_wait_s, "max_attempts": max_attempts}
    with write_txn(conn):
        ok = kb.schedule_task(
            conn, task_id, reason=reason, expected_run_id=expected_run_id, in_txn=True,
            event_extra={"kind": "waiting_ci", "resume_status": "review", "waiting_ci": record})
        if not ok:
            raise WaitCiRefused(f"could not park {task_id} (unknown id or not in todo/ready/running/blocked)")
        conn.execute(
            "INSERT OR REPLACE INTO kanban_waiting_ci (task_id, repo, pr, head_sha, since, attempts,"
            " max_wait_s, max_attempts, last_checked, infra_errors, rebinds, state, outcome)"
            " VALUES (?,?,?,?,?,0,?,?,?,0,0,'active',NULL)",
            (task_id, repo, pr, head_sha, now, max_wait_s, max_attempts, now))
    return {**record, "pending": pending, "reason": reason}


# ---- sweep ------------------------------------------------------------------

def _block(conn, hold: dict, reason_name: str, detail: str, kind: str = "needs_input") -> str:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', block_kind=?, block_recurrences=1, claim_lock=NULL,"
            " claim_expires=NULL, worker_pid=NULL WHERE id=? AND status='scheduled'",
            (kind, hold["task_id"]))
        conn.execute("UPDATE kanban_waiting_ci SET state='converted', outcome=? WHERE task_id=?",
                     (reason_name, hold["task_id"]))
        if cur.rowcount == 1:
            kb._append_event(conn, hold["task_id"], "blocked", {
                "reason": f"{reason_name}: {detail}", "kind": kind, "recurrences": 1,
                "source_status": "review", "resume_status": "review",
                "waiting_ci_reason": reason_name, "head_sha": hold["head_sha"]})
    return "blocked"


def _requeue(conn, hold: dict, summary: dict) -> str:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='review', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL,"
            " worker_pid=NULL WHERE id=? AND status='scheduled'", (hold["task_id"],))
        conn.execute("UPDATE kanban_waiting_ci SET state='requeued', outcome='requeued' WHERE task_id=?",
                     (hold["task_id"],))
        if cur.rowcount == 1:
            kb._append_event(conn, hold["task_id"], "waiting_ci_requeued", {
                "status": "review", "head_sha": hold["head_sha"], "repo": hold["repo"],
                "pr": hold["pr"], "checks": summary})
    return "requeued"


def _rebind(conn, hold: dict, new_sha: str, now: int) -> str:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_waiting_ci SET head_sha=?, since=?, attempts=0, infra_errors=0,"
            " rebinds=rebinds+1, last_checked=? WHERE task_id=? AND state='active'",
            (new_sha, now, now, hold["task_id"]))
        kb._append_event(conn, hold["task_id"], "waiting_ci_rebound", {
            "old_head_sha": hold["head_sha"], "head_sha": new_sha, "repo": hold["repo"], "pr": hold["pr"]})
    return "rebound"


def _touch(conn, hold: dict, now: int, *, infra: bool) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_waiting_ci SET attempts=attempts+1, last_checked=?,"
            " infra_errors = CASE WHEN ? THEN infra_errors+1 ELSE 0 END WHERE task_id=?",
            (now, 1 if infra else 0, hold["task_id"]))


def _parse_ts(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


def _evaluate(conn, hold: dict, now: int, fetch: Callable) -> str:
    try:
        task = kb.get_task(conn, hold["task_id"])
        profile_home = _assignee_profile_home(task.assignee if task else None)
        state = fetch(hold["repo"], hold["pr"], profile_home)
    except _GateAuthError as exc:
        return _block(conn, hold, REASON_AUTH, f"GitHub refused the read ({exc}); fix the reviewer gh login/access", "capability")
    except _HttpError as exc:
        if exc.text == "actions disabled":
            return _block(conn, hold, REASON_ACTIONS_OFF, "GitHub Actions appears disabled for this repo", "capability")
        if exc.status == 422:
            return _block(conn, hold, REASON_UNPROCESSABLE, "GitHub returned HTTP 422 for the CI read")
        _touch(conn, hold, now, infra=True)
        return _infra_or_block(conn, hold)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        _touch(conn, hold, now, infra=True)
        return _infra_or_block(conn, hold)

    if state["state"] != "open":
        return _block(conn, hold, REASON_PR_CLOSED,
                      "PR is " + ("merged" if state.get("merged") else "closed"))
    if state["head_sha"] != hold["head_sha"]:
        return _rebind(conn, hold, state["head_sha"], now)   # old-SHA results ignored
    runs = [r for r in state["runs"] if r["head_sha"] == hold["head_sha"]]
    state = {**state, "runs": runs}
    pending = _pending(state)
    if not pending and (runs or state["statuses"]):
        bad = [r["name"] for r in runs if r["conclusion"] in ("cancelled", "stale")]
        if bad:
            return _block(conn, hold, REASON_CANCELLED,
                          "latest check run(s) cancelled/stale with no rerun: " + ", ".join(bad[:5]))
        return _requeue(conn, hold, _summary(state))
    attempts = hold["attempts"] + 1
    if now - hold["since"] > hold["max_wait_s"] or attempts >= hold["max_attempts"]:
        return _block(conn, hold, REASON_BUDGET,
                      f"CI still not complete after {now - hold['since']}s / {attempts} checks: "
                      + (", ".join(pending[:5]) or "no check runs appeared"))
    queued = [r for r in runs if r["status"] != "completed"]
    if queued and all(r["status"] in ("queued", "waiting", "pending", "requested") for r in queued):
        starts = [t for t in (_parse_ts(r.get("started_at")) for r in queued) if t]
        if now - hold["since"] > QUEUED_STUCK_S and (not starts or now - min(starts) > QUEUED_STUCK_S):
            return _block(conn, hold, REASON_NO_RUNNER,
                          "runs stuck queued (no runner picked them up): " + ", ".join(pending[:5]))
    _touch(conn, hold, now, infra=False)
    return "waiting"


def _infra_or_block(conn, hold: dict) -> str:
    fresh = get_hold(conn, hold["task_id"]) or hold
    if fresh["infra_errors"] >= INFRA_ERROR_LIMIT:
        return _block(conn, fresh, REASON_API,
                      f"GitHub API failed {fresh['infra_errors']} consecutive reads")
    return "infra_error"


def kanban_waiting_ci_sweep(conn, *, now: Optional[int] = None, fetch: Optional[Callable] = None,
                            force: bool = False) -> dict[str, str]:
    """Advance every active waiting_ci hold. Returns ``{task_id: outcome}``.

    Idempotent: only ``scheduled`` cards with an ``active`` hold are touched, so
    a plain blocked card, a converted hold or a requeued card is never re-run.
    Holds are polled at most every ``CHECK_INTERVAL_S`` (``force`` ignores it).
    """
    now = int(now if now is not None else time.time())
    fetch = fetch or fetch_ci_state
    results: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT w.*, t.status AS task_status FROM kanban_waiting_ci w "
            "JOIN tasks t ON t.id = w.task_id WHERE w.state = 'active' ORDER BY w.last_checked").fetchall()
    except Exception:  # table absent on a not-yet-migrated board: nothing to do
        return results
    for row in rows[:MAX_HOLDS_PER_TICK * 4]:
        hold = dict(row)
        status = hold.pop("task_status")
        if status != "scheduled":
            # Released by hand (unblock/reclaim/archive): the hold is void, never act on it.
            with write_txn(conn):
                conn.execute("UPDATE kanban_waiting_ci SET state='released', outcome=? WHERE task_id=?",
                             (f"task_{status}", hold["task_id"]))
            results[hold["task_id"]] = "released"
            continue
        if not force and hold["last_checked"] and now - hold["last_checked"] < CHECK_INTERVAL_S:
            continue
        if len(results) >= MAX_HOLDS_PER_TICK:
            break
        try:
            results[hold["task_id"]] = _evaluate(conn, hold, now, fetch)
        except Exception:
            logger.exception("waiting_ci sweep failed for %s", hold["task_id"])
            results[hold["task_id"]] = "error"
    return results
