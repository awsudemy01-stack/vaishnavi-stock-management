"""Doctor check: sales + audit hash chains, invoice continuity, stock reconciliation (stdlib sqlite3)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from vaishnavi.bootstrap.checks.database import fix_text
from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.services.ledger import LedgerService


@dataclass
class LedgerCheck:
    """FAIL on a broken chain or a bill-number gap (E-LEDGER-001/002); WARN on a stock mismatch (E-LEDGER-003)."""

    key: str = "ledger"
    group: str = "database"

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Full ledger pass without writing ``integrity_check`` rows (the hourly job does that)."""
        db = ctx.paths.database
        if not db.exists():
            return CheckResult.skip("no database")
        ledger: LedgerService = ctx.runtime.get("ledger") or LedgerService(SqliteLedgerStore())
        conn = sqlite3.connect(db)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sale'").fetchone() is None:
                return CheckResult.skip("no schema")
            status = ledger.run_all(conn, record=False)
        finally:
            conn.close()
        chain, gaps, stock, audit = status.reports["CHAIN"], status.reports["GAPS"], status.reports["STOCK"], status.reports["AUDIT"]
        data = {"sales": getattr(chain, "checked", 0), "head": status.head, "codes": status.codes}
        if not chain.ok or not audit.ok:
            bad = chain if not chain.ok else audit
            return CheckResult.fail(ctx.t("doctor.ledger.chain"), error_code="E-LEDGER-001", hint=fix_text("E-LEDGER-001", ctx.lang, invoice_no=getattr(bad, "first_bad_id", "?")), detail=bad.detail, **data)
        if not gaps.ok:
            return CheckResult.fail(ctx.t("doctor.ledger.gaps", detail=gaps.detail), error_code="E-LEDGER-002", hint=fix_text("E-LEDGER-002", ctx.lang, before="?", after="?"), detail=gaps.detail, **data)
        if not stock.ok:
            return CheckResult.warn(ctx.t("doctor.ledger.stock", count=len(stock.mismatches)), error_code="E-LEDGER-003", hint=ctx.t("doctor.ledger.stock_hint"), detail=stock.detail, **data)
        return CheckResult.ok(ctx.t("doctor.ledger.ok", sales=data["sales"]), **data)
