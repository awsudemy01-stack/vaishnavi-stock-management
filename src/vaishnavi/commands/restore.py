"""``python -m vaishnavi restore [--list] [--id BACKUP_ID | --path FILE] [--yes] [--json]`` — restore the database
from a verified snapshot (docs/14 §5). Refuses while the app is running (fresh heartbeat → ``E-SYS-016``);
``main.py --restore`` drives this interactively."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from vaishnavi.commands import Env, register, start_file_logging
from vaishnavi.app_services import AppServices
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.logging import with_correlation
from vaishnavi.platform.watchdog import app_is_running
from vaishnavi.services.backup import snapshot_to_dict

log = logging.getLogger("vaishnavi.commands.restore")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--list", action="store_true", help="list restorable snapshots (newest first)")
    p.add_argument("--id", dest="backup_id", default=None, help="backup id to restore")
    p.add_argument("--path", type=Path, default=None, help="snapshot file to restore (when it is not in the known locations)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--json", action="store_true", help="machine-readable result")


@register("restore", "restore the database from a snapshot (--list to choose)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="owner-cli", role="SYSTEM"):
            services = AppServices.build(env.settings, env.paths, env.config_dir)
            snapshots = sorted(services.backup.list_snapshots(), key=lambda s: s.taken_at, reverse=True)
            if ns.list or (not ns.backup_id and not ns.path):
                items = [snapshot_to_dict(s) for s in snapshots]
                sys.stdout.write(json.dumps(items) + "\n" if ns.json else ("".join(f"{i + 1:>2}. {s['backup_id']}  {s['taken_at']}  {s['trigger']:<8} {s['path']}\n" for i, s in enumerate(items)) or "(no snapshots)\n"))
                return 0
            if app_is_running(env.paths.state):
                log.error("RESTORE_REFUSED reason=app-running code=E-SYS-016")
                sys.stderr.write("The application is running. Close it first (Settings → System → Shut down), then restore. (E-SYS-016)\n")
                return 2
            target: Path | None = ns.path
            if ns.backup_id:
                match = next((s for s in snapshots if s.backup_id == ns.backup_id), None)
                if match is None:
                    sys.stderr.write(f"No snapshot with id {ns.backup_id}. Use --list. (E-BKP-002)\n")
                    return 2
                target = Path(match.path)
            assert target is not None
            if not target.exists():
                sys.stderr.write(f"Snapshot file not found: {target} (E-BKP-002)\n")
                return 2
            if not ns.yes:
                sys.stdout.write(f"Restore {target.name} over the current database? The current database is saved first. [y/N] ")
                sys.stdout.flush()
                answer = sys.stdin.readline().strip().lower() if sys.stdin and not sys.stdin.closed else ""
                if answer not in ("y", "yes"):
                    sys.stdout.write("Cancelled.\n")
                    return 3
            try:
                result = services.backup.restore(target)
            except VaishnaviError as exc:
                log.error("RESTORE_CLI_FAILED code=%s detail=%s", exc.code, exc.detail)
                sys.stderr.write(f"{exc.user_message(env.lang)} {exc.what_to_do(env.lang)}  ({exc.code})\n")
                return 2
            data = {"ok": result.ok, "backup_id": result.backup_id, "pre_restore_backup_id": result.pre_restore_backup_id, "detail": result.detail}
            log.info("RESTORE_CLI ok=%s backup_id=%s pre_restore=%s", result.ok, result.backup_id, result.pre_restore_backup_id)
            sys.stdout.write(json.dumps(data) + "\n" if ns.json else f"Restored {result.backup_id}. The previous database was saved as {result.pre_restore_backup_id}. Bills made after the snapshot must be re-entered from paper receipts.\n")
            return 0 if result.ok else 2
    finally:
        if handle:
            handle.shutdown()
