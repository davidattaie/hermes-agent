"""Auto-detected Python commands must never fall back to Hermes's interpreter."""
import json

from pathlib import Path
import subprocess
import sys
import venv

import pytest

from agent.verify.recipes import Recipe, detect_recipe
from agent.verify.runner import run_verify


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("present", [False, True])
def test_detected_python_never_uses_launcher_path(tmp_path, monkeypatch, present):
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / "requirements.txt").write_text("not-installed\n")
    (project / "tests").mkdir()
    ambient = tmp_path / "managed-bin"
    ambient.mkdir()
    marker = tmp_path / "ambient-used"
    for name in ("python", "pip", "pytest"):
        command = ambient / name
        command.write_text(f"#!/bin/sh\nprintf used >> '{marker}'\nexit 0\n")
        command.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient))
    monkeypatch.setenv("VIRTUAL_ENV", str(ambient.parent))
    monkeypatch.setenv("HERMES_PYTHON", sys.executable)
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "shared-site"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shared-site"))
    if present:
        venv.EnvBuilder(with_pip=False).create(project / ".venv")
        # Harmless recorder modules, not real pip or dependency installs.
        python = project / ".venv/bin/python"
        site = subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
        for module in ("pip", "pytest"):
            Path(site, module + ".py").write_text(
                "import json, os, sys\nfrom pathlib import Path\n"
                "Path('record-' + Path(sys.argv[0]).stem + '.json').write_text(json.dumps({'prefix': sys.prefix, 'target': os.getenv('PIP_TARGET'), 'pythonpath': os.getenv('PYTHONPATH')}))\n"
            )
    recipe = detect_recipe(project)
    result = run_verify(project, recipe, skip_start=True)
    assert not marker.exists(), "auto-detection executed a shared-runtime PATH tool"
    if present:
        assert result.ok, result.to_dict()
        for module in ("pip", "pytest"):
            record = json.loads((project / f"record-{module}.json").read_text())
            assert Path(record["prefix"]) == project / ".venv"
            assert record["target"] is None
            assert record["pythonpath"] is None
    else:
        assert not result.ok
        assert "isolated" in result.phases[0].output_tail.lower()
        assert ".venv" in result.phases[0].output_tail


def test_detected_isolation_survives_manifest_roundtrip(tmp_path):
    (tmp_path / "requirements.txt").write_text("x")
    recipe = Recipe.from_dict(detect_recipe(tmp_path).to_dict())
    assert getattr(recipe, "isolated_python", False)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("unsafe", ["symlink", "system-sites", "not-a-venv"])
def test_test_only_refuses_unsafe_environments(tmp_path, unsafe):
    (tmp_path / "requirements.txt").write_text("x")
    environment = tmp_path / ".venv"
    if unsafe == "symlink":
        environment.symlink_to(Path(sys.prefix), target_is_directory=True)
    elif unsafe == "system-sites":
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
    else:
        (environment / "bin").mkdir(parents=True)
        (environment / "bin/python").symlink_to(sys.executable)
    result = run_verify(tmp_path, detect_recipe(tmp_path), phases=["test"])
    assert not result.ok
    assert result.phases[0].phase == "environment"
    assert "isolated" in result.phases[0].output_tail


def test_manual_recipes_remain_deliberate_shell_commands(tmp_path):
    recipe = Recipe(name="manual", test=[f'"{sys.executable}" -c "print(123)"'])
    result = run_verify(tmp_path, recipe, phases=["test"])
    assert result.ok
    assert result.phases[0].output_tail.strip() == "123"
