"""Run Alembic migrations from inside the application, with a dry run on a copy first (docs/14 §3).

The owner never runs the ``alembic`` CLI. ``upgrade_to_head(db_path)``:

1. reads the current revision;
2. if not at head, **copies** the live database with the SQLite backup API to ``tmp/`` and upgrades
   the copy — any failure there costs nothing (``E-DB-010`` with the detail, live file untouched);
3. then upgrades the live file inside one transaction per migration.

Pre-migration snapshots to the secondary location are the backup service's job (unit 22); it is
called by the doctor before this function when the database is behind.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from vaishnavi.core.errors import DatabaseError
from vaishnavi.db.engine import make_engine

log = logging.getLogger("vaishnavi.db.migrate")

ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"


def alembic_config(engine: Engine | None = None) -> Config:
    """Config pointing at the packaged migration scripts; the connection is passed via attributes."""
    cfg = Config(str(ALEMBIC_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    if engine is not None:
        cfg.attributes["engine"] = engine
    return cfg


def head_revision() -> str:
    """Newest revision shipped with this release."""
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    return head or ""


def current_revision(engine: Engine) -> str | None:
    """Revision stamped in ``alembic_version`` (None for an empty/unstamped database)."""
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


@dataclass(frozen=True)
class MigrationReport:
    """What ``upgrade_to_head`` did."""

    before: str | None
    after: str
    dry_run_ms: int
    applied_ms: int
    changed: bool


def _upgrade(engine: Engine) -> None:
    cfg = alembic_config(engine)
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        connection.commit()


def copy_database(src: Path, dest: Path) -> None:
    """Consistent copy via the SQLite backup API (safe while the app holds the file).

    Connections are closed explicitly: ``with sqlite3.connect()`` only commits, and an open handle
    would keep the copy locked on Windows.
    """
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


def upgrade_to_head(db_path: Path, tmp_dir: Path, *, dry_run: bool = True) -> MigrationReport:
    """Bring ``db_path`` to the shipped head, dry-running on a copy first. Raises ``DatabaseError``."""
    head = head_revision()
    engine = make_engine(db_path, writer=True)
    try:
        before = current_revision(engine)
    finally:
        engine.dispose()
    if before == head:
        log.info("MIGRATION_UP_TO_DATE revision=%s", head)
        return MigrationReport(before, head, 0, 0, changed=False)

    dry_ms = 0
    if dry_run and db_path.exists():
        started = time.perf_counter()
        trial = tmp_dir / f"migrate-trial-{int(started)}.db"
        try:
            copy_database(db_path, trial)
            trial_engine = make_engine(trial, writer=True)
            try:
                _upgrade(trial_engine)
            finally:
                trial_engine.dispose()
        except Exception as exc:
            log.exception("MIGRATION_DRY_RUN_FAILED from=%s to=%s", before, head)
            raise DatabaseError("E-DB-010", detail=f"dry run on a copy failed: {exc!r}", cause=exc) from exc
        finally:
            for p in (trial, Path(str(trial) + "-wal"), Path(str(trial) + "-shm")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        dry_ms = int((time.perf_counter() - started) * 1000)
        log.info("MIGRATION_DRY_RUN_OK from=%s to=%s ms=%d", before, head, dry_ms)

    started = time.perf_counter()
    engine = make_engine(db_path, writer=True)
    try:
        _upgrade(engine)
        after = current_revision(engine) or ""
    except Exception as exc:
        log.exception("MIGRATION_FAILED from=%s to=%s", before, head)
        raise DatabaseError("E-DB-010", detail=repr(exc), cause=exc) from exc
    finally:
        engine.dispose()
    applied_ms = int((time.perf_counter() - started) * 1000)
    log.info("MIGRATION_APPLIED from=%s to=%s ms=%d", before, after, applied_ms)
    return MigrationReport(before, after, dry_ms, applied_ms, changed=True)


def verify_schema_objects(db_path: Path) -> list[str]:
    """Names of expected tables that are missing (doctor check D3 detail) — stdlib only."""
    from vaishnavi.db.schema import load_statements  # noqa: PLC0415

    expected = {s.split()[2] for s in load_statements() if s.upper().startswith("CREATE TABLE")}
    conn = sqlite3.connect(db_path)
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    finally:
        conn.close()
    missing = sorted(expected - present)
    if missing:
        log.warning("SCHEMA_TABLES_MISSING %s", missing)
    return missing
