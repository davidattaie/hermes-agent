"""Without tested-content identity, root and completion time cannot share proof."""
import sqlite3

import pytest

from agent.verification_evidence import mark_workspace_edited, record_verify_run, verification_status


@pytest.mark.parametrize("prior_proof", [False, True])
def test_a_completion_cannot_certify_b_edit_after_a_started(tmp_path, monkeypatch, prior_proof):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    path = str(tmp_path / "b.py")
    if prior_proof:
        record_verify_run(root=tmp_path, session_id="B", ok=True)
    mark_workspace_edited(session_id="A", cwd=tmp_path, paths=[str(tmp_path / "a.py")])
    # A begins a run here; B edits while that run is in flight. No snapshot is
    # passed to record_verify_run, so even a full pass proves nothing for B.
    mark_workspace_edited(session_id="B", cwd=tmp_path, paths=[path])
    record_verify_run(root=tmp_path, session_id="A", ok=True)
    status = verification_status(session_id="B", cwd=tmp_path)
    assert status["status"] == ("stale" if prior_proof else "unverified")
    assert status["changed_paths"] == [path]
    assert verification_status(session_id="A", cwd=tmp_path)["status"] == "passed"


def test_legacy_foreign_pointer_is_not_accepted_as_current_session_proof(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    event = record_verify_run(root=tmp_path, session_id="A", ok=True)
    assert event is not None
    mark_workspace_edited(session_id="B", cwd=tmp_path, paths=[str(tmp_path / "b.py")])
    with sqlite3.connect(home / "verification_evidence.db") as conn:
        # This is the state written by the previous global-clear implementation.
        conn.execute("UPDATE verification_state SET last_event_id=?, last_edit_at=NULL, changed_paths_json='[]' WHERE session_id='B'", (event["id"],))
    assert verification_status(session_id="B", cwd=tmp_path)["status"] == "unverified"
