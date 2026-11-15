"""``python -m vaishnavi backup [--json] [--trigger manual|start|nightly] [--verify ID] [--list]`` — snapshots from the command line (docs/14)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from vaishnavi.commands import Env, register, start_file_logging
from vaishnavi.app_services import AppServices
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.logging import with_correlation
from vaishnavi.services.backup import snapshot_to_dict

log = logging.getLogger("vaishnavi.commands.backup")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable result")
    p.add_argument("--trigger", default="manual", help="why the snapshot is taken (recorded in the manifest)")
    p.add_argument("--list", action="store_true", help="list snapshots instead of taking one")
    p.add_argument("--flush", action="store_true", help="move queued snapshots to the backup drive and exit")


@register("backup", "take a verified snapshot of the database (or --list / --flush)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="owner-cli", role="SYSTEM"):
            if not env.paths.database.exists():
                sys.stderr.write(f"No database at {env.paths.database}. (E-DB-002)\n")
                return 2
            services = AppServices.build(env.settings, env.paths, env.config_dir)
            if ns.list:
                items = [snapshot_to_dict(s) for s in services.backup.list_snapshots()]
                sys.stdout.write(json.dumps(items) + "\n" if ns.json else "".join(f"{s['backup_id']}  {s['taken_at']}  {s['path']}\n" for s in items) or "(no snapshots)\n")
                return 0
            if ns.flush:
                moved = services.backup.flush_queue()
                sys.stdout.write(json.dumps({"moved": moved}) + "\n" if ns.json else f"Moved {moved} queued snapshot(s).\n")
                return 0
            try:
                snap = services.backup.snapshot(ns.trigger)
            except VaishnaviError as exc:
                log.error("BACKUP_CLI_FAILED code=%s", exc.code)
                sys.stderr.write(f"{exc.user_message(env.lang)}  ({exc.code})\n")
                return 2
            data = snapshot_to_dict(snap)
            sys.stdout.write(json.dumps(data) + "\n" if ns.json else f"Snapshot {snap.backup_id} written to {snap.path}{' (queued — backup drive not present)' if snap.queued else ''}\n")
            sys.stdout.flush()
            return 0
    finally:
        if handle:
            handle.shutdown()
