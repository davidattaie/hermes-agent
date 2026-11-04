"""Active scratch is excluded from ancestor workspaces, not treated as suite proof."""
import tempfile

from agent.verification_evidence import classify_verification_command, mark_workspace_edited, record_terminal_result, verification_status
from agent.verification_stop import build_verify_on_stop_nudge


def test_ancestor_workspace_accepts_its_prescribed_scratch_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "package.json").write_text("{}")
    scratch = tmp_path / "cache" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(scratch))
    changed = str(tmp_path / "feature.py")
    script = scratch / "hermes-verify-feature.py"
    mark_workspace_edited(session_id="A", cwd=tmp_path, paths=[changed])
    nudge = build_verify_on_stop_nudge(session_id="A", changed_paths=[changed])
    assert nudge is not None and str(scratch) in nudge
    event = record_terminal_result(command=f"python {script}", cwd=tmp_path, session_id="A", exit_code=0)
    assert event is not None
    assert event["kind"] == "ad_hoc" and event["scope"] == "targeted"
    assert build_verify_on_stop_nudge(session_id="A", changed_paths=[changed]) is None
    # Cleaning/revising the excluded helper must not invalidate the code's receipt.
    mark_workspace_edited(session_id="A", cwd=tmp_path, paths=[str(script)])
    assert verification_status(session_id="A", cwd=tmp_path)["status"] == "passed"
    assert build_verify_on_stop_nudge(session_id="fresh", changed_paths=[str(script)]) is None


def test_scratch_helper_does_not_add_an_unverified_ancestor_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "package.json").write_text("{}")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    scratch = tmp_path / "cache" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(scratch))
    changed = str(project / "feature.py")
    record_terminal_result(command="pytest", cwd=project, session_id="A", exit_code=0)
    helper = str(scratch / "hermes-verify-feature.py")
    assert build_verify_on_stop_nudge(session_id="A", changed_paths=[changed, helper]) is None


def test_workspace_scripts_are_not_promoted_to_ad_hoc_proof(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    script = tmp_path / "hermes-verify-feature.py"
    assert classify_verification_command(f"python {script}", cwd=tmp_path) is None


def test_approved_scratch_does_not_replace_canonical_suite(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(scratch))
    assert classify_verification_command(f"python {scratch / 'hermes-verify-feature.py'}", cwd=tmp_path) is None
