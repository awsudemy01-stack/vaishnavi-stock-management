"""Doctor check: the shop database — presence, soundness, identity, schema revision, journal mode, Owner (stdlib sqlite3).

``fix()`` performs the two safe repairs: create + seed a missing database (stamped at the Alembic head
so the ORM layer agrees later) and switch the journal mode to WAL. Upgrading an older schema is **not**
done here — that is the migration path with its pre-migrate snapshot and dry-run (``db/migrate.py``).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.core.errors import describe
from vaishnavi.db import seed
from vaishnavi.db.guardian import DbGuardian, TriageKind
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.schema import HEAD_REVISION, apply_schema, current_revision, stamp_head
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.bootstrap.checks.database")


def fix_text(code: str, lang: str, **params: object) -> str:
    """The catalogue's "what to do" line for an error code (shared by the checks)."""
    return describe(code, lang, dict(params))["what_to_do"]


def _guardian(ctx: DoctorContext) -> DbGuardian:
    guardian = ctx.runtime.get("guardian")
    if guardian is None:
        guardian = DbGuardian(ctx.paths, ctx.runtime.get("ledger") or LedgerService(SqliteLedgerStore()))
        ctx.runtime["guardian"] = guardian
    return guardian


@dataclass
class DatabaseCheck:
    """FAIL: missing (E-DB-002), damaged (E-DB-003), foreign (E-DB-021), schema behind (E-DB-010).
    WARN: restored copy (E-DB-020), unclean close (E-DB-022), Owner setup pending (E-AUTH-005)."""

    key: str = "database"
    group: str = "database"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Triage first (quarantines a damaged file), then the inside."""
        triage = _guardian(ctx).triage()
        ctx.runtime["triage"] = triage
        db = ctx.paths.database
        if triage.kind is TriageKind.MISSING:
            return CheckResult.fail(ctx.t("doctor.database.missing"), error_code="E-DB-002", hint=ctx.t("doctor.database.missing_hint", path=str(db)), detail=str(db))
        if triage.kind is TriageKind.CORRUPT:
            return CheckResult.fail(ctx.t("doctor.database.damaged"), error_code="E-DB-003", hint=fix_text("E-DB-003", ctx.lang), detail=triage.detail)
        if triage.kind is TriageKind.FOREIGN:
            return CheckResult.fail(ctx.t("doctor.database.foreign"), error_code="E-DB-021", detail=triage.detail)

        conn = sqlite3.connect(db)
        try:
            revision = current_revision(conn)
            if triage.kind is TriageKind.NEW and revision is None and not _has_table(conn, "db_identity"):
                return CheckResult.fail(ctx.t("doctor.database.missing"), error_code="E-DB-002", hint=ctx.t("doctor.database.missing_hint", path=str(db)), detail="file exists but has no schema")
            if revision != HEAD_REVISION:
                return CheckResult.fail(ctx.t("doctor.database.behind", current=revision or "-", head=HEAD_REVISION), error_code="E-DB-010", hint=ctx.t("doctor.database.behind_hint"), detail=f"alembic_version={revision}")
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            sales = int(conn.execute("SELECT COUNT(*) FROM sale").fetchone()[0])
            products = int(conn.execute("SELECT COUNT(*) FROM product WHERE is_system = 0").fetchone()[0])
            owner_pending = seed.is_owner_setup_pending(conn) or conn.execute("SELECT COUNT(*) FROM user WHERE role_id = (SELECT id FROM role WHERE name = 'OWNER') AND is_active = 1").fetchone()[0] == 0
        finally:
            conn.close()
        data = {"sales": sales, "products": products, "journal_mode": mode, "revision": revision, "triage": triage.kind.value}
        if owner_pending:
            return CheckResult.warn(ctx.t("doctor.database.owner_pending"), error_code="E-AUTH-005", hint=ctx.t("doctor.database.owner_pending_hint"), **data)
        if triage.kind is TriageKind.RESTORED:
            return CheckResult.warn(ctx.t("doctor.database.restored", taken_at=triage.params.get("taken_at", "?")), error_code="E-DB-020", hint=fix_text("E-DB-020", ctx.lang, **triage.params), detail=triage.detail, **data)
        if triage.kind is TriageKind.UNCLEAN:
            return CheckResult.warn(ctx.t("doctor.database.unclean"), error_code="E-DB-022", detail=f"last_open={triage.params.get('last_open')}", **data)
        if mode != "wal":
            return CheckResult.warn(f"journal_mode={mode}", hint=ctx.t("doctor.database.wal_fixed"), **data)
        return CheckResult.ok(ctx.t("doctor.database.ok", sales=sales, products=products, mode=mode.upper()), **data)

    def fix(self, ctx: DoctorContext) -> CheckResult | None:
        """Create + seed a missing database; set WAL. Everything else needs the owner."""
        db = ctx.paths.database
        if not db.exists():
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                apply_schema(conn)
                stamp_head(conn)
                report = seed.apply_seed(conn)
            finally:
                conn.close()
            log.info("DOCTOR_FIX database created+seeded rows=%d path=%s", report.total, db)
            return self.run(ctx)
        conn = sqlite3.connect(db)
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
                log.info("DOCTOR_FIX journal_mode %s→wal", mode)
        finally:
            conn.close()
        return self.run(ctx)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
