import argparse
import stat

from hermes_cli.patch_sync import find_patch_sync_script, run_patch_sync


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_no_script_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PATCH_SYNC", raising=False)
    args = argparse.Namespace(branch=None, check=False)
    run_patch_sync(args, tmp_path / "hermes-agent")
    assert args.branch is None


def test_disabled_by_env(tmp_path, monkeypatch):
    _script(tmp_path / "sync-patches.sh", "exit 0\n")
    monkeypatch.setenv("HERMES_PATCH_SYNC", "0")
    assert find_patch_sync_script(tmp_path / "hermes-agent") is None


def test_sync_sets_branch_and_tolerates_conflict(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PATCH_SYNC", raising=False)
    script = _script(tmp_path / "sync-patches.sh",
                     '[ "$1" = --print-branch ] && echo local/patched && exit 0\nexit 2\n')
    args = argparse.Namespace(branch=None, check=False)
    run_patch_sync(args, tmp_path / "hermes-agent")
    assert script.is_file() and args.branch == "local/patched"


def test_hard_failure_leaves_branch_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PATCH_SYNC", raising=False)
    _script(tmp_path / "sync-patches.sh",
            '[ "$1" = --print-branch ] && echo local/patched && exit 0\nexit 4\n')
    args = argparse.Namespace(branch=None, check=False)
    run_patch_sync(args, tmp_path / "hermes-agent")
    assert args.branch is None


def test_explicit_branch_and_check_skip_sync(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PATCH_SYNC", raising=False)
    marker = tmp_path / "ran"
    _script(tmp_path / "sync-patches.sh", f"touch {marker}\necho x\n")
    for args in (argparse.Namespace(branch="main", check=False), argparse.Namespace(branch=None, check=True)):
        run_patch_sync(args, tmp_path / "hermes-agent")
    assert not marker.exists()
