"""Block recurrence routing: intervention triage is not an executable idea."""
from __future__ import annotations


def route_block(kind, reason, source_status, *, prev_kind, prev_recurrences):
    """Return the landing state, event and SQL fields for a block transition.

    A dependency waits in todo; repeated unchanged prerequisites go to triage
    for intervention. Counts survive unblocking, so a missing human result
    cannot acquire a fresh retry budget just because an automation wakes it.
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload
