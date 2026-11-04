"""Forward-port seam: a supervised verify app keeps the isolated child environment."""
import json
import os
from pathlib import Path
import socket
import subprocess
import venv

import psutil
import pytest

from agent.verify.recipes import Recipe
from agent.verify.runner import run_verify
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
def test_owned_python_start_keeps_isolation_and_tears_down(tmp_path, monkeypatch):
    project = tmp_path / "project with spaces"
    project.mkdir()
    environment = project / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin/python"
    site = subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip()
    # Harmless server/recorder module: no pip or project dependencies installed.
    Path(site, "uvicorn.py").write_text(
        "import http.server, json, os, sys\nfrom pathlib import Path\n"
        "server = http.server.HTTPServer(('127.0.0.1', int(sys.argv[-1])), http.server.BaseHTTPRequestHandler)\n"
        "Path('start-record.json').write_text(json.dumps({'prefix': sys.prefix, 'target': os.getenv('PIP_TARGET'), "
        "'pythonpath': os.getenv('PYTHONPATH'), 'virtual_env': os.getenv('VIRTUAL_ENV'), 'pid': os.getpid(), "
        "'pgid': os.getpgid(0)}))\nserver.serve_forever()\n"
    )
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "shared-site"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shared-site"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "managed-runtime"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    recipe = Recipe(name="isolated owned app", isolated_python=True, start=f"uvicorn fixture:app --port {port}", port=port)
    result = run_verify(project, recipe, phases=["start"], ready_timeout=10)
    assert result.ok, result.to_dict()
    record = json.loads((project / "start-record.json").read_text())
    assert record["prefix"] == str(environment)
    assert record["target"] is None
    assert record["pythonpath"] is None
    assert record["virtual_env"] == str(environment)

    def app_stopped():
        try:
            return psutil.Process(record["pid"]).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    assert record["pgid"] != os.getpgid(0)
    assert wait_until(app_stopped, timeout=5), "verify app survived normal teardown"
