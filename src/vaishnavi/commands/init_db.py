"""``python -m vaishnavi init-db [--json]`` — create/seed/upgrade the database and import installer secrets (stdin JSON).

Idempotent: the second run reports ``created: false``. Secrets arrive as ``{"secrets": {...}}`` on stdin
(see ``commands.read_stdin_secrets``); they are stored encrypted or **not at all** — when the secret box is
unavailable the command exits 2 with ``E-SEC-001`` so the installer keeps its copy and tells the owner.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

from vaishnavi.bootstrap.startup import ensure_database, ensure_schema_current, require_primary
from vaishnavi.commands import Env, read_stdin_secrets, register, start_file_logging
from vaishnavi.app_services import AppServices
from vaishnavi.core.logging import with_correlation
from vaishnavi.core.security import NullBox
from vaishnavi.db.schema import current_revision
from vaishnavi.db.seed import is_owner_setup_pending

log = logging.getLogger("vaishnavi.commands.init_db")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")


@register("init-db", "create, seed and upgrade the database; import installer secrets from stdin", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="installer", role="SYSTEM"):
            return _run(ns, env)
    finally:
        if handle:
            handle.shutdown()


def _run(ns: argparse.Namespace, env: Env) -> int:
    pending = read_stdin_secrets()
    if not require_primary(env.paths):
        sys.stderr.write(f"The data folder {env.paths.primary} is not there (unplugged disk?). It is never re-created automatically. (E-SYS-013)\n")
        return 2
    conn = sqlite3.connect(env.paths.database) if env.paths.database.exists() else None
    before = None
    if conn is not None:
        try:
            before = current_revision(conn)
        finally:
            conn.close()
    report = ensure_database(env.paths)
    created = report is not None
    after = ensure_schema_current(env.paths)
    migrated = (not created) and before != after
    seeded_rows = report.total if report else 0

    services = AppServices.build(env.settings, env.paths, env.config_dir)
    stored: list[str] = []
    failed: list[str] = []
    if pending:
        if isinstance(services.secure_settings._box, NullBox):  # noqa: SLF001 - deliberate capability probe
            failed = sorted(pending)
            log.error("SECRET_IMPORT_FAILED keys=%s code=E-SEC-001 (secret box unavailable)", ",".join(failed))
        else:
            with services.txn(label="init_db.secrets") as session:
                stored = services.secure_settings.import_pending(session, pending, source="installer")
            failed = sorted(set(pending) - set(stored))

    conn = sqlite3.connect(env.paths.database)
    try:
        owner_pending = is_owner_setup_pending(conn)
    finally:
        conn.close()

    summary = {"created": created, "seeded_rows": seeded_rows, "migrated": migrated, "revision": after, "secrets_stored": stored, "secrets_failed": failed, "owner_setup_pending": owner_pending, "database": str(env.paths.database)}
    log.info("INIT_DB %s", json.dumps({k: v for k, v in summary.items() if k != "database"}))
    if ns.json:
        sys.stdout.write(json.dumps(summary) + "\n")
    else:
        sys.stdout.write(f"Database {'created' if created else 'ready'} at {env.paths.database} (schema {after}); secrets stored: {len(stored)}; owner setup pending: {owner_pending}\n")
    if failed:
        sys.stderr.write(f"Could not store {len(failed)} secret(s) — the secret store is unavailable. (E-SEC-001)\n")
    sys.stdout.flush()
    return 2 if failed else 0
