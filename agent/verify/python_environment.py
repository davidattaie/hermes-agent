"""Bind inferred Python commands to a validated, project-owned virtualenv.

Verification never provisions an environment implicitly. Prepare .venv first;
if a manager/module is absent there, fail rather than use an ambient executable.
Explicit user-authored recipes retain their shell-command semantics.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from agent.verify.recipes import Recipe


def isolated_recipe(root: Path, recipe: Recipe) -> tuple[Recipe, dict[str, str]]:
    root = root.resolve()
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    reason = (
        f"Refusing ambient Python execution: prepare an isolated project .venv at {environment} "
        "(without system site-packages), including its verification tools, then retry. "
        "For deliberate custom commands, supply a user-authored .hermes/environment.json recipe."
    )
    if (environment.is_symlink() or not (environment / "pyvenv.cfg").is_file()
            or environment.resolve() == Path(sys.prefix).resolve()):
        raise ValueError(reason)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTHON", "PIP_", "UV_", "POETRY_", "PIPENV_"))
           and k != "VIRTUAL_ENV"}
    env.update(VIRTUAL_ENV=str(environment), PYTHONNOUSERSITE="1", PIP_CONFIG_FILE=os.devnull,
               PIP_REQUIRE_VIRTUALENV="true", UV_PROJECT_ENVIRONMENT=str(environment),
               POETRY_VIRTUALENVS_CREATE="false", PIPENV_IGNORE_VIRTUALENVS="0")
    try:
        probe = subprocess.run(
            [str(python), "-I", "-c", "import json,sys; print(json.dumps([sys.prefix,sys.base_prefix]))"],
            env=env, capture_output=True, text=True, timeout=15, check=False,
        )
        prefix, base = json.loads(probe.stdout) if probe.returncode == 0 else (None, None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValueError(reason) from exc
    config = (environment / "pyvenv.cfg").read_text(encoding="utf-8-sig").lower().replace(" ", "")
    if prefix != str(environment) or prefix == base or "include-system-site-packages=true" in config:
        raise ValueError(reason)

    def bind(command: str) -> str:
        words = shlex.split(command)
        if not words:
            return command
        tool, *args = words
        if tool in ("python", "python3"):
            argv = [str(python), *args]
        elif tool in ("pip", "pytest", "uvicorn", "flask", "uv", "poetry", "pipenv", "ruff"):
            argv = [str(python), "-m", tool, *args]
        else:
            raise ValueError(f"{reason} Cannot safely infer Python command: {command}")
        return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

    return replace(recipe, bootstrap=[bind(c) for c in recipe.bootstrap],
                   build=[bind(c) for c in recipe.build], test=[bind(c) for c in recipe.test],
                   start=bind(recipe.start) if recipe.start else None), env
