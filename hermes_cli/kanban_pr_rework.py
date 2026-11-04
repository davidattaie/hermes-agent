"""PR duplicate-work guards and bounded, receipt-ordered rework handoffs."""
from __future__ import annotations


def handoff_receipt(conn, task_id, payload):
    """Bind a handoff to the comments it actually observed, not clock precision."""
    row = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id = ?", (task_id,)).fetchone()
    return {**(payload or {}), "pr_comment_id": row[0] or 0}


def _pr_comments(conn, task_id, now):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE

    comments = []
    for row in conn.execute("SELECT id, body, created_at FROM task_comments WHERE task_id = ?", (task_id,)):
        urls = set(_RESPAWN_GUARD_PR_URL_RE.findall(kb._lossy_text(row["body"]) or ""))
        stamp = kb._to_epoch(row["created_at"])
        if urls:
            comments.append((now if stamp is None else stamp, row, urls))
    return comments


def _observed_comments(comments, payload, event, kb):
    cutoff = payload.get("pr_comment_id")
    if isinstance(cutoff, int) and not isinstance(cutoff, bool):
        return [(t, c, urls) for t, c, urls in comments if c["id"] <= cutoff]
    # Legacy handoffs lack receipt identity. Never guess same-second order.
    stamp = kb._to_epoch(event["created_at"])
    return [(t, c, urls) for t, c, urls in comments if stamp is not None and t < stamp]


def active_pr_guard(conn, task_id, now, window):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _is_handoff_event

    comments = _pr_comments(conn, task_id, now)
    recent = [(t, c, urls) for t, c, urls in comments if t >= now - window]
    if not recent:
        return None
    events = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'claimed', 'completed', 'review_requested') "
        "ORDER BY id", (task_id,),
    ).fetchall()
    events = _current_assignment_events(events, kb)
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    for event in reversed(events):
        if event["kind"] not in ("assigned", "changes_requested", "review_reopened"):
            continue
        if not _is_handoff_event(event["kind"], event["payload"]) or _permit_consumed(events, event["id"], kb):
            continue
        payload = kb._json_dict(event["payload"])
        recipient = payload.get("assignee" if event["kind"] == "assigned" else "implementer")
        if not recipient or assignee != recipient:
            continue
        observed = _observed_comments(comments, payload, event, kb)
        if event["kind"] == "assigned":
            if {c["id"] for _, c, _ in recent} <= {c["id"] for _, c, _ in observed}:
                return None
            continue
        permitted = set().union(*(urls for _, _, urls in observed))
        required = set().union(*(urls for _, _, urls in recent))
        if required <= permitted:
            return None
    return "active_pr"


def _current_assignment_events(events, kb):
    """Ownership transitions revoke older permits; proven no-ops do neither."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event["kind"] != "assigned":
            continue
        payload = kb._json_dict(event["payload"])
        # Default routing and legacy/unknown writes are barriers, not permits.
        if (payload.get("source") != "kanban.default_assignee"
                and "from" in payload and payload["from"] == payload.get("assignee")):
            continue
        return events[index:]
    return events


def _permit_consumed(events, receipt_id, kb):
    """A permit authorizes one implementation attempt, not crash/reclaim loops."""
    for event in events:
        if event["id"] <= receipt_id:
            continue
        if event["kind"] in ("completed", "review_requested"):
            return True
        if event["kind"] == "claimed" and kb._json_dict(event["payload"]).get("source_status", "ready") == "ready":
            return True
    return False
