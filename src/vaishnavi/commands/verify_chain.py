"""``python -m vaishnavi verify-chain [--json]`` — sales hash chain, invoice gaps, stock reconciliation, audit chain (docs/05 §4).

Exit 0 when every check passes, 2 otherwise (with the first failing code on stderr, e.g. ``E-LEDGER-001``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict

from vaishnavi.commands import Env, register, start_file_logging
from vaishnavi.core.logging import with_correlation
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.commands.verify_chain")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable result")
    p.add_argument("--no-record", action="store_true", help="do not write an integrity_check row")


@register("verify-chain", "verify the sales ledger (hash chain, invoice gaps, stock, audit chain)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring."""
    handle = start_file_logging(env)
    try:
        with with_correlation(user="owner-cli", role="SYSTEM"):
            if not env.paths.database.exists():
                sys.stderr.write(f"No database at {env.paths.database}. (E-DB-002)\n")
                return 2
            conn = sqlite3.connect(env.paths.database)
            try:
                status = LedgerService(SqliteLedgerStore()).run_all(conn, record=not ns.no_record)
            finally:
                conn.close()
            payload = {"ok": status.ok, "at": status.at, "head": status.head, "codes": status.codes, "reports": {k: _jsonable(asdict(r)) for k, r in status.reports.items()}}
            if ns.json:
                sys.stdout.write(json.dumps(payload) + "\n")
            else:
                sys.stdout.write(("OK  " if status.ok else "FAIL  ") + status.summary() + "\n")
                for kind, report in status.reports.items():
                    if not report.ok:
                        sys.stdout.write(f"  - {kind}: {report.detail}  ({report.code})\n")
            sys.stdout.flush()
            log.info("VERIFY_CHAIN_CLI ok=%s codes=%s", status.ok, ",".join(status.codes) or "-")
            return 0 if status.ok else 2
    finally:
        if handle:
            handle.shutdown()


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
