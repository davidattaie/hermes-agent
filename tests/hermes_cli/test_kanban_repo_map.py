"""Kanban workers get the canonical repo map at dispatch (env + worker context)."""

from __future__ import annotations

import json
import os
import subprocess

from tests.hermes_cli.test_kanban_worker_terminal_cwd import _capture_spawn_env


def _fake_dev(tmp_path):
    dev = tmp_path / "dev"
    for name in ("Hermesify", "authify", "Healthify"):
        (dev / name / ".git").mkdir(parents=True)
    (dev / "authify-wt").mkdir()                       # task worktree folder: excluded
    (dev / "authify-wt" / "pr3").mkdir()
    (dev / "plain").mkdir()                            # not a git clone: excluded
    (dev / "linked").mkdir()
    (dev / "linked" / ".git").write_text("gitdir: /x\n")  # linked worktree: excluded
    (dev / ".hidden" / ".git").mkdir(parents=True)
    return dev


def test_discover_repos_only_canonical_clones(tmp_path):
    from hermes_cli.kanban_repo_map import discover_repos

    dev = _fake_dev(tmp_path)
    assert discover_repos(dev) == {
        "Healthify": str(dev / "Healthify"),
        "Hermesify": str(dev / "Hermesify"),
        "authify": str(dev / "authify"),
    }
    assert discover_repos(tmp_path / "missing") == {}


def test_spawn_env_carries_repo_map(monkeypatch, tmp_path):
    dev = _fake_dev(tmp_path)
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REPOS_ROOT", str(dev))
    monkeypatch.setenv("HEALTHIFY_REPO", "/operator/override")

    from hermes_cli import kanban_db as kb

    ws = tmp_path / "ws"
    ws.mkdir()
    env = _capture_spawn_env(kb, monkeypatch, str(ws))["env"]
    assert json.loads(env["HERMES_REPO_MAP"])["Hermesify"] == str(dev / "Hermesify")
    assert env["HERMES_REPOS_ROOT"] == str(dev)
    assert env["AUTHIFY_REPO"] == str(dev / "authify")
    assert env["HERMESIFY_REPO"] == str(dev / "Hermesify")
    assert env["HEALTHIFY_REPO"] == "/operator/override"   # operator value wins


def test_worker_context_lists_repos_and_marks_named(monkeypatch, tmp_path):
    dev = _fake_dev(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_REPOS_ROOT", str(dev))
    from hermes_cli.kanban_repo_map import render_repo_section

    lines: list[str] = []
    render_repo_section(lines, "Review authify PR 3", "also touches Hermesify")
    text = "\n".join(lines)
    assert "## Canonical repos" in text and "Never guess a repo path" in text
    assert f"- authify: {dev / 'authify'}  <- named in this task" in text
    assert f"- Hermesify: {dev / 'Hermesify'}  <- named in this task" in text
    assert f"- Healthify: {dev / 'Healthify'}\n" in text + "\n"
    assert "plain" not in text and "authify-wt" not in text


def test_no_section_without_repos(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_REPOS_ROOT", str(tmp_path / "none"))
    from hermes_cli.kanban_repo_map import apply_repo_env, render_repo_section

    lines: list[str] = []
    render_repo_section(lines, "t", None)
    env: dict = {}
    apply_repo_env(env)
    assert lines == [] and env == {}
