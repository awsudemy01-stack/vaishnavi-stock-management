"""Fresh start — a new, empty database that keeps the shop's setup (live run 4: "delete all the entries and start
from scratch, I mean entering all the item values").

Why not ``DELETE FROM …``: sales, stock movements and the audit log are **append-only by trigger** (docs/05 §5a),
and the sale hash chain would be broken by a hole. The honest way to start over is a new file:

1. the current database (and its WAL/SHM side files) is copied to ``<secondary>/fresh-start-<stamp>/``
   together with the receipts archive, so nothing is lost;
2. a new database is created, migrated and seeded;
3. the rows worth keeping are carried across by name: users and passwords, the Owner PIN, settings (shop,
   GST, billing rules …), encrypted secrets (UPI, tokens), categories, units, assistant instructions and
   the onboarding progress — so the Owner does not redo the wizard;
4. the old file is replaced and the guardian fingerprint removed (a new ``instance_id`` must not look FOREIGN).

Everything else — products, barcodes, stock, purchases, suppliers, sales, payments, shifts, held bills,
customers, chat history, forecasts, publish queue — starts empty. Owner password required; the app must be
stopped first (the CLI does that).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from vaishnavi.core.clock import now_ist
from vaishnavi.db import seed
from vaishnavi.db.schema import apply_schema, stamp_head
from vaishnavi.platform.paths import AppPaths

log = logging.getLogger("vaishnavi.services.fresh_start")

KEEP_TABLES: Final[tuple[str, ...]] = ("role", "user", "setting", "secure_setting", "category", "setup_step", "prompt_version")
"""Carried into the new database, in this order (parents before children). Everything else starts empty."""


def copy_database(src: Path, dest: Path) -> None:
    """Consistent copy through SQLite's backup API (stdlib — ``db.migrate`` needs Alembic, which this command must not)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(dest) + suffix)
        if side.exists():
            side.unlink()


@dataclass(frozen=True)
class FreshStartReport:
    """What happened."""

    archive_dir: Path
    kept: dict[str, int] = field(default_factory=dict)
    new_instance_id: str = ""


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()]


def fresh_start(paths: AppPaths, *, keep: tuple[str, ...] = KEEP_TABLES, stamp: str | None = None) -> FreshStartReport:
    """Archive the current database, build a new one, carry the setup rows across, swap. Raises on any failure
    *before* the swap; the live file is only replaced once the new one is complete."""
    db = paths.database
    if not db.exists():
        raise FileNotFoundError(f"no database at {db}")
    stamp = stamp or now_ist().strftime("%Y-%m-%d_%H%M%S")
    archive_dir = paths.snapshots.parent / f"fresh-start-{stamp}"
    archive_dir.mkdir(parents=True, exist_ok=False)
    archived = archive_dir / db.name
    copy_database(db, archived)  # consistent copy even if a stray handle is open
    receipts = paths.receipts
    if receipts.exists():
        shutil.copytree(receipts, archive_dir / "receipts")
    log.info("FRESH_START_ARCHIVED db=%s receipts=%s", archived, receipts.exists())

    fresh = db.with_name(db.stem + ".fresh.db")
    for side in (fresh, Path(str(fresh) + "-wal"), Path(str(fresh) + "-shm")):
        if side.exists():
            side.unlink()
    conn = sqlite3.connect(fresh)
    kept: dict[str, int] = {}
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        apply_schema(conn)
        stamp_head(conn)
        report = seed.apply_seed(conn)
        conn.execute("ATTACH DATABASE ? AS old", (str(archived),))
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in keep:
            cols = [c for c in _columns(conn, "main", table) if c in _columns(conn, "old", table)]
            if not cols:
                continue
            col_list = ", ".join(cols)
            conn.execute(f"DELETE FROM main.{table}")  # seed rows give way to the owner's own (same ids where they match)  # noqa: S608
            cur = conn.execute(f"INSERT OR REPLACE INTO main.{table}({col_list}) SELECT {col_list} FROM old.{table}")  # noqa: S608
            kept[table] = int(cur.rowcount)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        conn.execute("DETACH DATABASE old")
        instance_id = str(conn.execute("SELECT instance_id FROM db_identity LIMIT 1").fetchone()[0])
    finally:
        conn.close()
    log.info("FRESH_START_BUILT kept=%s seeded=%d instance=%s", kept, report.total, instance_id)

    # Swap: old side files away, new file in. The fingerprint of the old instance must go or the guardian refuses (E-DB-021).
    for side in ("-wal", "-shm"):
        p = Path(str(db) + side)
        if p.exists():
            p.unlink()
    db.unlink()
    fresh.replace(db)
    for side in ("-wal", "-shm"):
        p = Path(str(fresh) + side)
        if p.exists():
            p.unlink()
    if receipts.exists():
        shutil.rmtree(receipts)
    if paths.fingerprint.exists():
        paths.fingerprint.unlink()
    log.warning("FRESH_START_DONE archive=%s new_instance=%s — products, stock, sales, shifts cleared; setup kept", archive_dir, instance_id)
    return FreshStartReport(archive_dir, kept, instance_id)
