"""SqliteLedgerStore — ``services.ledger.LedgerStore`` on a raw ``sqlite3`` connection (stdlib only).

Used by the doctor and the restore wizard to verify a database **file** without the ORM, and by the
core test tier on this PC. The ORM-based ``SaleRepo`` (Phase 2 billing) satisfies the same protocol
for the running application; both must return byte-identical canonical records, which
``tests/core/test_services_ledger.py`` pins down.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import date

from vaishnavi.core.clock import business_day_bounds, iso_utc
from vaishnavi.db.records import GENESIS_HASH, AuditEntry, PaymentRecord, SaleItemRecord, SaleRecord

log = logging.getLogger("vaishnavi.db.ledger_store")

_SALE_COLUMNS = (
    "id, invoice_no, client_txn_id, fin_year, seq_no, sold_at, user_id, shift_id, status, original_sale_id, "
    "subtotal_paise, item_discount_paise, bill_discount_paise, taxable_paise, cgst_paise, sgst_paise, roundoff_paise, total_paise, prev_hash, hash"
)
_ITEM_COLUMNS = "product_id, qty, unit, shelf_price_paise, offer_id, discount_paise, unit_price_paise, tax_rate_bp, entry_method, is_open_item, open_item_name"


def _day_bounds(day: str) -> tuple[str, str]:
    start, end = business_day_bounds(date.fromisoformat(day))
    return iso_utc(start), iso_utc(end)


def _qty_text(value: object) -> str:
    return str(value) if value is not None else "0"


class SqliteLedgerStore:
    """Reads sales/items/payments/movements/audit rows for the ledger checks."""

    def iter_sales(self, conn: sqlite3.Connection) -> Iterator[SaleRecord]:
        """Sales ``ORDER BY id`` with lines (by item id) and payments (by payment id)."""
        cur = conn.execute(f"SELECT {_SALE_COLUMNS} FROM sale ORDER BY id")
        for row in cur.fetchall():
            sale_id = int(row[0])
            items = tuple(
                SaleItemRecord(
                    product_id=r[0],
                    qty=_qty_text(r[1]),
                    unit=r[2],
                    shelf_price_paise=int(r[3]),
                    offer_id=r[4],
                    discount_paise=int(r[5]),
                    unit_price_paise=int(r[6]),
                    tax_rate_bp=int(r[7]),
                    entry_method=r[8],
                    is_open_item=bool(r[9]),
                    open_item_name=r[10],
                )
                for r in conn.execute(f"SELECT {_ITEM_COLUMNS} FROM sale_item WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall()
            )
            payments = tuple(PaymentRecord(mode=r[0], amount_paise=int(r[1])) for r in conn.execute("SELECT mode, amount_paise FROM payment WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall())
            yield SaleRecord(
                id=sale_id,
                invoice_no=row[1],
                client_txn_id=row[2],
                fin_year=row[3],
                seq_no=int(row[4]),
                sold_at=row[5],
                user_id=int(row[6]),
                shift_id=int(row[7]),
                status=row[8],
                original_sale_id=row[9],
                subtotal_paise=int(row[10]),
                item_discount_paise=int(row[11]),
                bill_discount_paise=int(row[12]),
                taxable_paise=int(row[13]),
                cgst_paise=int(row[14]),
                sgst_paise=int(row[15]),
                roundoff_paise=int(row[16]),
                total_paise=int(row[17]),
                items=items,
                payments=payments,
                prev_hash=row[18],
                hash=row[19],
            )

    def last_sale_hash(self, conn: sqlite3.Connection) -> str:
        """Newest sale hash by id, or genesis."""
        row = conn.execute("SELECT hash FROM sale ORDER BY id DESC LIMIT 1").fetchone()
        return str(row[0]) if row else GENESIS_HASH

    def invoice_sequences(self, conn: sqlite3.Connection) -> dict[str, list[int]]:
        """All ``seq_no`` per financial year (every status)."""
        out: dict[str, list[int]] = {}
        for fy, seq in conn.execute("SELECT fin_year, seq_no FROM sale ORDER BY fin_year, seq_no").fetchall():
            out.setdefault(str(fy), []).append(int(seq))
        return out

    def stock_sums(self, conn: sqlite3.Connection) -> dict[int, tuple[str, str | None]]:
        """Σ movements vs cached qty for every non-system product that has either."""
        rows = conn.execute(
            """
            SELECT p.id,
                   (SELECT SUM(m.qty_delta) FROM stock_movement m WHERE m.product_id = p.id) AS moved,
                   (SELECT s.qty FROM product_stock s WHERE s.product_id = p.id) AS cached
            FROM product p
            WHERE p.is_system = 0
              AND (EXISTS (SELECT 1 FROM stock_movement m WHERE m.product_id = p.id) OR EXISTS (SELECT 1 FROM product_stock s WHERE s.product_id = p.id))
            ORDER BY p.id
            """
        ).fetchall()
        return {int(pid): (_qty_text(moved), None if cached is None else str(cached)) for pid, moved, cached in rows}

    def day_sale_lines(self, conn: sqlite3.Connection, day: str) -> dict[int, str]:
        """Completed, non-open-item sale lines sold on ``day`` (IST), summed per product."""
        start, end = _day_bounds(day)
        rows = conn.execute(
            """
            SELECT i.product_id, SUM(i.qty)
            FROM sale_item i JOIN sale s ON s.id = i.sale_id
            WHERE s.status = 'COMPLETED' AND i.is_open_item = 0 AND i.product_id IS NOT NULL
              AND s.sold_at >= ? AND s.sold_at < ?
            GROUP BY i.product_id
            """,
            (start, end),
        ).fetchall()
        return {int(pid): _qty_text(qty) for pid, qty in rows}

    def day_sale_movements(self, conn: sqlite3.Connection, day: str) -> dict[int, str]:
        """SALE movements created on ``day`` (IST) as positive sold quantities per product."""
        start, end = _day_bounds(day)
        rows = conn.execute(
            "SELECT product_id, -SUM(qty_delta) FROM stock_movement WHERE type = 'SALE' AND created_at >= ? AND created_at < ? GROUP BY product_id",
            (start, end),
        ).fetchall()
        return {int(pid): _qty_text(qty) for pid, qty in rows}

    def iter_audit(self, conn: sqlite3.Connection) -> Iterator[AuditEntry]:
        """Audit rows ``ORDER BY id``."""
        cur = conn.execute("SELECT id, at, user_id, actor_user_id, action, entity, entity_id, before, after, correlation_id, ip, prev_hash, hash FROM audit_log ORDER BY id")
        for r in cur.fetchall():
            yield AuditEntry(id=int(r[0]), at=r[1], user_id=r[2], actor_user_id=r[3], action=r[4], entity=r[5], entity_id=r[6], before=r[7], after=r[8], correlation_id=r[9], ip=r[10], prev_hash=r[11], hash=r[12])

    def record_check(self, conn: sqlite3.Connection, kind: str, ok: bool, details: str, at: str) -> None:
        """Append an ``integrity_check`` row and commit."""
        conn.execute("INSERT INTO integrity_check(at, kind, ok, details) VALUES (?, ?, ?, ?)", (at, kind, 1 if ok else 0, details))
        conn.commit()
        log.debug("INTEGRITY_ROW kind=%s ok=%s", kind, ok)
