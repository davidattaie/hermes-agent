"""Authoritative project-to-repo path map for Kanban workers.

Workers used to guess repo paths (``/home/da/dev/hermezify``, ``.../buildup``,
``.../worktrees/authify``) and loop on tool failures. The dispatcher now hands every
worker the canonical clones that actually exist on the host: as env vars
(``HERMES_REPO_MAP`` JSON, ``HERMES_REPOS_ROOT`` and one ``<NAME>_REPO`` per clone) and as a
"Canonical repos" section of the worker context.

The map is discovered, never hand-maintained: every non-hidden directory directly under the
repos root (``HERMES_KANBAN_REPOS_ROOT``, default ``~/dev``) that is a git clone (``.git`` is a
directory, so linked worktrees and ``*-wt`` task folders are excluded) is a canonical repo.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

REPOS_ROOT_ENV = "HERMES_KANBAN_REPOS_ROOT"
MAP_ENV = "HERMES_REPO_MAP"
ROOT_ENV = "HERMES_REPOS_ROOT"
_MAX_REPOS = 64


def repos_root() -> Path:
    override = os.environ.get(REPOS_ROOT_ENV, "").strip()
    return Path(override).expanduser() if override else Path(os.path.expanduser("~")) / "dev"


def discover_repos(root: Optional[Path] = None) -> dict[str, str]:
    """``{directory name: absolute path}`` of canonical clones under ``root`` (sorted)."""
    root = root or repos_root()
    found: dict[str, str] = {}
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        if len(found) >= _MAX_REPOS:
            break
        name = entry.name
        if name.startswith(".") or name.endswith("-wt"):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not os.path.isdir(os.path.join(entry.path, ".git")):
                continue
        except OSError:
            continue
        found[name] = os.path.abspath(entry.path)
    return found


def _env_name(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", repo).strip("_").upper() + "_REPO"


def apply_repo_env(env: MutableMapping[str, str], repos: Optional[Mapping[str, str]] = None) -> None:
    """Export the map into a worker env. An operator-set ``<NAME>_REPO`` is never overwritten."""
    repos = discover_repos() if repos is None else repos
    if not repos:
        return
    env[MAP_ENV] = json.dumps(dict(repos), sort_keys=True)
    env[ROOT_ENV] = str(repos_root())
    for name, path in repos.items():
        var = _env_name(name)
        if var != "_REPO" and not env.get(var):
            env[var] = path


def render_repo_section(lines: list[str], title: str = "", body: Optional[str] = None,
                        repos: Optional[Mapping[str, str]] = None) -> None:
    """Append a ``## Canonical repos`` section to worker-context ``lines``."""
    repos = discover_repos() if repos is None else repos
    if not repos:
        return
    text = f"{title}\n{body or ''}".lower()
    lines.append("## Canonical repos")
    lines.append(
        "Use only these paths (case-sensitive; also in $HERMES_REPO_MAP and $<NAME>_REPO). "
        "Never guess a repo path. If the repo you need is not listed, stop and say so "
        "(kanban_block) instead of probing the filesystem. Work in a git worktree of the clone."
    )
    for name, path in repos.items():
        mentioned = re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", text)
        lines.append(f"- {name}: {path}" + ("  <- named in this task" if mentioned else ""))
    lines.append("")
