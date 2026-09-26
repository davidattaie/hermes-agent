"""Optional pre-update hook that keeps a carried-patch branch rebased on upstream.

A checkout that carries local patches on a branch (``local/patched``) points
``sync-patches.sh`` (next to the checkout, or ``HERMES_PATCH_SYNC``) at the
rebase/test/publish work. ``hermes update`` — and so the dashboard "Update
Hermes" button and ``/update`` — runs it first with ``--apply`` and then updates
against that branch. No script, no effect. ``HERMES_PATCH_SYNC=0`` disables it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

SYNC_TIMEOUT_SECONDS = 30 * 60


def find_patch_sync_script(project_root: Path) -> Optional[Path]:
    override = os.environ.get("HERMES_PATCH_SYNC")
    if override == "0":
        return None
    script = Path(override) if override else Path(project_root).parent / "sync-patches.sh"
    return script if script.is_file() and os.access(script, os.X_OK) else None


def run_patch_sync(args, project_root: Path) -> None:
    """Run the sync script and point ``args.branch`` at the patch branch (unless the user chose one)."""
    if getattr(args, "branch", None) or getattr(args, "check", False):
        return
    script = find_patch_sync_script(project_root)
    if script is None:
        return
    print("→ Syncing carried patches onto upstream...")
    try:
        branch = subprocess.run([str(script), "--print-branch"], capture_output=True, text=True,
                                timeout=30, check=True).stdout.strip()
        # Output streams straight to the update log.
        rc = subprocess.run([str(script), "--apply"], timeout=SYNC_TIMEOUT_SECONDS).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ⚠ Patch sync could not run ({exc}); updating without it.")
        return
    if rc == 4 or not branch:
        print("  ⚠ Patch sync did not finish; updating without switching branches.")
        return
    if rc:
        print(f"  ⚠ Patch sync exited {rc}; updating onto the last published {branch}.")
    args.branch = branch
