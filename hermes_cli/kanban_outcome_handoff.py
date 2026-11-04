"""Completion gate for explicit business-outcome handoff receipts.

Legacy local-only cards remain compatible. A declared handoff is a contract:
finishing an assessment is not acceptance of the assessed operation. Evidence
references are required, not independently verified here; PR acceptance remains
owned by kanban_pr_acceptance_store.
"""
from __future__ import annotations


def gate_completion_handoff(conn, task_id, metadata):
    from hermes_cli import kanban_db as kb

    if isinstance(metadata, dict) and "handoff" in metadata:
        handoff = metadata["handoff"]
    else:
        # Review approval must not silently discard the implementer's outcome.
        rows = conn.execute(
            "SELECT r.metadata FROM task_events e JOIN task_runs r ON r.id = e.run_id "
            "WHERE e.task_id = ? AND e.kind = 'review_requested' ORDER BY e.id DESC",
            (task_id,),
        )
        prior = next((data for row in rows if "handoff" in (data := kb._json_dict(row[0]))), {})
        if "handoff" not in prior:
            return
        handoff = prior["handoff"]
    if not isinstance(handoff, dict) or handoff.get("outcome") != "accepted":
        raise ValueError("handoff outcome is not accepted; keep the operation held or request changes")
    evidence = handoff.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise ValueError("accepted handoff requires nonblank evidence references")
