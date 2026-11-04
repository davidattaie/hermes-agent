"""Supported pytest executable spellings retain scope and safe shell attribution."""
import shlex
import subprocess
import sys

import pytest

from agent.verification_evidence import classify_verification_command, record_terminal_result, mark_workspace_edited
from agent.verification_stop import build_verify_on_stop_nudge


@pytest.mark.parametrize("interpreter", ["/project/.venv/bin/python", "/project/.venv/bin/python3.12", "python3.14"])
@pytest.mark.parametrize("selection", ["tests/test_widget.py", "-k widget", "-m unit", "--lf"])
def test_absolute_pytest_receipt_satisfies_gate_without_full_suite_claim(tmp_path, monkeypatch, interpreter, selection):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    changed = str(tmp_path / "widget.py")
    mark_workspace_edited(session_id="A", cwd=tmp_path, paths=[changed])
    event = record_terminal_result(
        command=f"{interpreter} -m pytest {selection}", cwd=tmp_path,
        session_id="A", exit_code=0, output="EXPLICIT receipt fixture; not product tests",
    )
    assert event is not None
    assert event["kind"] == "test"
    assert event["scope"] == "targeted"
    assert build_verify_on_stop_nudge(session_id="A", changed_paths=[changed]) is None


def test_real_absolute_pytest_output_enters_terminal_receipt_adapter(tmp_path, monkeypatch):
    from tools.terminal_tool_result import _verification_evidence

    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "test_fixture.py").write_text("def test_fixture():\n    assert 'receipt'.startswith('re')\n")
    changed = str(tmp_path / "fixture.py")
    mark_workspace_edited(session_id="A", cwd=tmp_path, paths=[changed])
    argv = [sys.executable, "-m", "pytest", "test_fixture.py", "-q"]
    completed = subprocess.run(argv, cwd=tmp_path, text=True, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    receipt = _verification_evidence(shlex.join(argv), str(tmp_path), "A", completed.returncode, completed.stdout)
    assert receipt == {"status": "passed", "kind": "test", "scope": "targeted", "canonical_command": "pytest"}
    assert build_verify_on_stop_nudge(session_id="A", changed_paths=[changed]) is None


@pytest.mark.parametrize("command", [
    "/venv/bin/python -m pytest || true",
    "/venv/bin/python -m pytest; true",
    "/venv/bin/python -m pytest | tee log",
    "echo /venv/bin/python -m pytest",
    "/venv/bin/bash -m pytest",
])
def test_absolute_spelling_never_bypasses_shell_attribution(tmp_path, command):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert classify_verification_command(command, cwd=tmp_path, exit_code=0) is None


@pytest.mark.parametrize("command, exit_code, expected", [
    ("prepare && /venv/bin/python -m pytest", 0, "passed"),
    ("prepare && /venv/bin/python -m pytest", 1, None),
    ("prepare; /venv/bin/python -m pytest", 1, "failed"),
])
def test_absolute_pytest_safe_compound_exit(tmp_path, command, exit_code, expected):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    evidence = classify_verification_command(command, cwd=tmp_path, exit_code=exit_code)
    assert (evidence.status if evidence else None) == expected
