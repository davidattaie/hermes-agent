"""Database-only update safety; independent of full filesystem rollback snapshots."""
import sys

from hermes_cli.backup_sqlite import preflight_all_state_dbs


def before_update(args, *, root=None) -> None:
    """Run under the update lock, before fetching or mutating the checkout."""
    if getattr(args, "no_db_backup", False):
        print("WARNING: state.db update backups explicitly disabled (--no-db-backup).")
        return
    if root is None:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    try:
        results = preflight_all_state_dbs(root)
    except OSError as exc:
        results = {"profile enumeration": {"error": str(exc)}}
    failures = []
    for label, result in results.items():
        if result.get("error"):
            failures.append(label)
            print(f"WARNING: state.db backup failed for {label}: {result['error']}")
        elif result.get("path"):
            print(f"  state.db backup {label}: {result['path']} (quick_check ok)")
    if not failures:
        return
    if getattr(args, "yes", False):
        print("WARNING: --yes acknowledged failed state.db backups; continuing without those snapshots.")
        return
    if sys.stdin.isatty():
        try:
            if input("Continue update without all state.db backups? [y/N] ").strip().lower() in {"y", "yes"}:
                return
        except (EOFError, OSError):
            pass
    print("Update stopped before checkout changes. Re-run with --yes to acknowledge, or --no-db-backup to opt out.")
    raise SystemExit(1)
