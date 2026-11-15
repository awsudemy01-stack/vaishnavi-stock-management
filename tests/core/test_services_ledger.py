"""services/ledger.py — the sales hash chain, invoice gaps and stock reconciliation (docs/05 §3–4, docs/14 §2a).

Every test name is a promise made by the documents; they were written before the code (docs/13 rule 7).
The store is the stdlib ``SqliteLedgerStore`` running on the real schema with its triggers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace

from vaishnavi.db import seed
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.records import GENESIS_HASH, PaymentRecord, SaleItemRecord, SaleRecord
from vaishnavi.db.schema import apply_schema
from vaishnavi.services.ledger import (
    LedgerService,
    canonical_qty,
    canonical_sale,
    sale_hash,
    verify_sales_chain,
)

NOW = "2026-11-14T04:00:00Z"


def fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    seed.apply_seed(conn, now=NOW)
    conn.execute("INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (2, 'ramesh', 'Ramesh', 2, 'x')")
    conn.execute("INSERT INTO product(id, sku, name, price_paise) VALUES (10, 'CAM-0001', 'Camphor 50g', 1000), (11, 'AGR-0001', 'Agarbatti', 2500)")
    conn.execute("INSERT INTO shift(id, user_id, opened_at) VALUES (1, 2, ?)", (NOW,))
    conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id) VALUES (10, 'OPENING', 100, 100, 1), (11, 'OPENING', 50, 50, 1)")
    conn.commit()
    return conn


def make_sale(seq: int, *, qty: str = "2", total: int = 2000, status: str = "COMPLETED", sold_at: str = NOW, prev_hash: str = GENESIS_HASH, product_id: int = 10) -> SaleRecord:
    item = SaleItemRecord(product_id=product_id, qty=canonical_qty(qty), unit="pcs", shelf_price_paise=1000, offer_id=None, discount_paise=0, unit_price_paise=1000, tax_rate_bp=0, entry_method="SCAN", is_open_item=False, open_item_name=None)
    sale = SaleRecord(
        id=0, invoice_no=f"VPS/26-27/{seq:06d}", client_txn_id=f"txn-{seq}", fin_year="26-27", seq_no=seq, sold_at=sold_at, user_id=2, shift_id=1,
        status=status, original_sale_id=None, subtotal_paise=total, item_discount_paise=0, bill_discount_paise=0, taxable_paise=total, cgst_paise=0,
        sgst_paise=0, roundoff_paise=0, total_paise=total, items=(item,), payments=(PaymentRecord("CASH", total),), prev_hash=prev_hash,
    )
    return replace(sale, hash=sale_hash(sale))


def insert_sale(conn: sqlite3.Connection, sale: SaleRecord, *, with_movement: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO sale(invoice_no, client_txn_id, public_id, fin_year, seq_no, sold_at, user_id, shift_id, status, subtotal_paise, taxable_paise, total_paise, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sale.invoice_no, sale.client_txn_id, f"pub-{sale.seq_no}", sale.fin_year, sale.seq_no, sale.sold_at, sale.user_id, sale.shift_id, sale.status, sale.subtotal_paise, sale.taxable_paise, sale.total_paise, sale.prev_hash, sale.hash),
    )
    sale_id = int(cur.lastrowid)
    for it in sale.items:
        conn.execute(
            "INSERT INTO sale_item(sale_id, product_id, name_snapshot, qty, unit, shelf_price_paise, unit_price_paise, tax_rate_bp, taxable_paise, line_total_paise, entry_method, is_open_item, open_item_name)"
            " VALUES (?, ?, 'x', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sale_id, it.product_id, it.qty, it.unit, it.shelf_price_paise, it.unit_price_paise, it.tax_rate_bp, sale.taxable_paise, sale.total_paise, it.entry_method, 1 if it.is_open_item else 0, it.open_item_name),
        )
        if with_movement and sale.status == "COMPLETED" and not it.is_open_item:
            before = conn.execute("SELECT qty FROM product_stock WHERE product_id = ?", (it.product_id,)).fetchone()[0]
            conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, ref_type, ref_id, user_id, created_at) VALUES (?, 'SALE', ?, ?, 'SALE', ?, 2, ?)", (it.product_id, -float(it.qty), float(before) - float(it.qty), sale_id, sale.sold_at))
    for p in sale.payments:
        conn.execute("INSERT INTO payment(sale_id, mode, amount_paise) VALUES (?, ?, ?)", (sale_id, p.mode, p.amount_paise))
    conn.commit()
    return sale_id


class CanonicalFormTest(unittest.TestCase):
    def test_canonical_is_deterministic_compact_sorted_and_contains_documented_fields(self) -> None:
        sale = make_sale(1)
        text = canonical_sale(sale)
        doc = json.loads(text)
        self.assertEqual(text, json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        for key in ("invoice_no", "client_txn_id", "sold_at", "user_id", "shift_id", "status", "original_sale_id", "items", "payments", "subtotal_paise", "item_discount_paise", "bill_discount_paise", "taxable_paise", "cgst_paise", "sgst_paise", "roundoff_paise", "total_paise", "prev_hash"):
            self.assertIn(key, doc, key)
        self.assertNotIn("id", doc, "database ids are not part of the hash — a restore renumbering must not break it")
        self.assertNotIn("hash", doc)
        self.assertEqual(set(doc["items"][0]), {"product_id", "qty", "unit", "shelf_price_paise", "offer_id", "discount_paise", "unit_price_paise", "tax_rate_bp", "entry_method", "is_open_item", "open_item_name"})
        self.assertEqual(set(doc["payments"][0]), {"mode", "amount_paise"})

    def test_quantities_are_normalised_so_equal_amounts_hash_equally(self) -> None:
        self.assertEqual(canonical_qty("2"), canonical_qty("2.0"))
        self.assertEqual(canonical_qty("0.50"), "0.5")
        self.assertEqual(canonical_qty("100.000"), "100")
        a, b = make_sale(1, qty="2"), make_sale(1, qty="2.00")
        self.assertEqual(a.hash, b.hash)

    def test_hash_is_sha256_of_canonical_and_genesis_is_64_zeros(self) -> None:
        sale = make_sale(1)
        self.assertEqual(sale.prev_hash, "0" * 64)
        self.assertEqual(sale.hash, hashlib.sha256(canonical_sale(sale).encode("utf-8")).hexdigest())
        self.assertNotEqual(sale.hash, make_sale(1, total=2001).hash)
        self.assertNotEqual(sale.hash, make_sale(1, prev_hash="a" * 64).hash, "prev_hash is inside the hash → chain")


class ChainVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh()
        self.store = SqliteLedgerStore()
        self.svc = LedgerService(self.store)
        prev = GENESIS_HASH
        self.ids: list[int] = []
        for seq in range(1, 5):
            sale = make_sale(seq, prev_hash=prev)
            self.ids.append(insert_sale(self.conn, sale))
            prev = sale.hash

    def tearDown(self) -> None:
        self.conn.close()

    def test_intact_chain_verifies_and_reports_head(self) -> None:
        report = self.svc.verify_sales(self.conn)
        self.assertTrue(report.ok)
        self.assertEqual(report.checked, 4)
        self.assertEqual(report.head, self.conn.execute("SELECT hash FROM sale ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(self.svc.chain_head(self.conn), report.head)

    def test_edited_amount_is_detected_with_sale_id_expected_and_found(self) -> None:
        # The append-only trigger blocks UPDATE; simulate an external tool by disabling triggers' effect via a raw copy.
        self.conn.execute("DROP TRIGGER trg_sale_limited_update")
        self.conn.execute("UPDATE sale SET total_paise = 1 WHERE id = ?", (self.ids[2],))
        self.conn.commit()
        with self.assertLogs("vaishnavi.services.ledger", level="ERROR") as logs:
            report = self.svc.verify_sales(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.first_bad_id, self.ids[2])
        self.assertEqual(report.code, "E-LEDGER-001")
        self.assertEqual(len(report.expected), 64)
        self.assertNotEqual(report.expected, report.found)
        self.assertTrue(any("INTEGRITY_FAIL" in line and f"sale_id={self.ids[2]}" in line for line in logs.output))

    def test_deleted_sale_breaks_the_next_rows_prev_hash(self) -> None:
        self.conn.execute("DROP TRIGGER trg_sale_no_delete")
        self.conn.execute("DROP TRIGGER trg_sale_item_no_delete")
        self.conn.execute("DROP TRIGGER trg_payment_no_delete")
        self.conn.execute("DELETE FROM payment WHERE sale_id = ?", (self.ids[1],))
        self.conn.execute("DELETE FROM sale_item WHERE sale_id = ?", (self.ids[1],))
        self.conn.execute("DELETE FROM sale WHERE id = ?", (self.ids[1],))
        self.conn.commit()
        report = self.svc.verify_sales(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.first_bad_id, self.ids[2], "the row after the gap no longer links to its predecessor")
        self.assertIn("prev_hash", report.detail)

    def test_verify_function_works_on_plain_records_without_a_database(self) -> None:
        sales = [replace(make_sale(1), id=1)]
        second = make_sale(2, prev_hash=sales[0].hash)
        sales.append(replace(second, id=2))
        self.assertIsNone(verify_sales_chain(sales))
        tampered = [sales[0], replace(sales[1], total_paise=5)]
        self.assertEqual(verify_sales_chain(tampered), 2)

    def test_next_hash_for_billing_uses_current_head(self) -> None:
        head = self.svc.chain_head(self.conn)
        new = make_sale(5, prev_hash=head)
        self.assertEqual(self.svc.hash_for_new_sale(self.conn, replace(new, prev_hash="", hash="")).prev_hash, head)
        self.assertEqual(self.svc.hash_for_new_sale(self.conn, replace(new, prev_hash="", hash="")).hash, new.hash)

    def test_empty_ledger_is_ok_with_genesis_head(self) -> None:
        conn = fresh()
        report = LedgerService(self.store).verify_sales(conn)
        self.assertTrue(report.ok)
        self.assertEqual(report.checked, 0)
        self.assertEqual(report.head, GENESIS_HASH)
        conn.close()


class InvoiceGapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh()
        self.svc = LedgerService(SqliteLedgerStore())

    def tearDown(self) -> None:
        self.conn.close()

    def test_continuous_numbers_per_financial_year_are_ok(self) -> None:
        prev = GENESIS_HASH
        for seq in (1, 2, 3):
            s = make_sale(seq, prev_hash=prev)
            insert_sale(self.conn, s)
            prev = s.hash
        report = self.svc.verify_gaps(self.conn)
        self.assertTrue(report.ok)
        self.assertEqual(report.gaps, {})
        self.assertEqual(report.counts, {"26-27": (3, 3)}, "(COUNT(*), MAX(seq_no)) per FY")

    def test_missing_number_is_reported_with_before_and_after(self) -> None:
        prev = GENESIS_HASH
        for seq in (1, 2, 4, 7):
            s = make_sale(seq, prev_hash=prev)
            insert_sale(self.conn, s)
            prev = s.hash
        with self.assertLogs("vaishnavi.services.ledger", level="ERROR") as logs:
            report = self.svc.verify_gaps(self.conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.code, "E-LEDGER-002")
        self.assertEqual(report.gaps["26-27"], [(2, 4), (4, 7)], "each gap as (last seen, next seen)")
        self.assertEqual(report.missing["26-27"], [3, 5, 6])
        self.assertTrue(any("E-LEDGER-002" in line for line in logs.output))

    def test_voided_sales_still_count_as_numbers(self) -> None:
        a = make_sale(1)
        b = make_sale(2, prev_hash=a.hash, status="VOIDED")
        insert_sale(self.conn, a)
        insert_sale(self.conn, b, with_movement=False)
        self.assertTrue(self.svc.verify_gaps(self.conn).ok)


class StockReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh()
        self.svc = LedgerService(SqliteLedgerStore())
        a = make_sale(1, qty="2")
        b = make_sale(2, qty="3", prev_hash=a.hash, product_id=11)
        insert_sale(self.conn, a)
        insert_sale(self.conn, b)

    def tearDown(self) -> None:
        self.conn.close()

    def test_sum_of_movements_equals_cached_stock(self) -> None:
        report = self.svc.reconcile_stock(self.conn, day="2026-11-14")
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.products_checked, 2)
        self.assertEqual(self.conn.execute("SELECT qty FROM product_stock WHERE product_id = 10").fetchone()[0], 98)

    def test_cached_stock_edited_outside_the_app_is_flagged(self) -> None:
        for name in ("trg_product_stock_no_direct_update",):
            self.conn.execute(f"DROP TRIGGER {name}")
        self.conn.execute("UPDATE product_stock SET qty = 99 WHERE product_id = 10")
        self.conn.commit()
        with self.assertLogs("vaishnavi.services.ledger", level="ERROR"):
            report = self.svc.reconcile_stock(self.conn, day="2026-11-14")
        self.assertFalse(report.ok)
        self.assertEqual(report.code, "E-LEDGER-003")
        self.assertEqual(report.mismatches[0].product_id, 10)
        self.assertEqual((report.mismatches[0].expected, report.mismatches[0].found), ("98", "99"))

    def test_sale_lines_of_the_day_must_match_sale_movements(self) -> None:
        # A sale recorded without its stock movement (someone bypassed checkout) is a reconciliation failure.
        c = make_sale(3, qty="1", prev_hash=self.svc.chain_head(self.conn))
        insert_sale(self.conn, c, with_movement=False)
        report = self.svc.reconcile_stock(self.conn, day="2026-11-14")
        self.assertFalse(report.ok)
        self.assertEqual(report.code, "E-LEDGER-003")
        self.assertTrue(any(m.kind == "SALE_LINES_VS_MOVEMENTS" and m.product_id == 10 for m in report.mismatches), report.mismatches)

    def test_open_items_are_excluded_from_reconciliation(self) -> None:
        open_item_id = self.conn.execute("SELECT id FROM product WHERE sku = 'OPEN-ITEM'").fetchone()[0]
        head = self.svc.chain_head(self.conn)
        item = SaleItemRecord(product_id=open_item_id, qty="1", unit="pcs", shelf_price_paise=500, offer_id=None, discount_paise=0, unit_price_paise=500, tax_rate_bp=0, entry_method="OPEN_ITEM", is_open_item=True, open_item_name="Loose thread")
        s = replace(make_sale(3, total=500, prev_hash=head), items=(item,))
        s = replace(s, hash=sale_hash(s))
        insert_sale(self.conn, s, with_movement=False)
        self.assertTrue(self.svc.reconcile_stock(self.conn, day="2026-11-14").ok)


class FullCheckTest(unittest.TestCase):
    def test_run_all_writes_integrity_rows_and_logs_ok(self) -> None:
        conn = fresh()
        svc = LedgerService(SqliteLedgerStore())
        insert_sale(conn, make_sale(1))
        with self.assertLogs("vaishnavi.services.ledger", level="INFO") as logs:
            status = svc.run_all(conn, day="2026-11-14")
        self.assertTrue(status.ok)
        self.assertEqual(sorted(status.reports), ["AUDIT", "CHAIN", "GAPS", "STOCK"])
        rows = conn.execute("SELECT kind, ok FROM integrity_check ORDER BY id").fetchall()
        self.assertEqual(sorted(rows), [("CHAIN", 1), ("GAPS", 1), ("STOCK", 1)])
        self.assertTrue(any("INTEGRITY_OK" in line for line in logs.output))
        self.assertEqual(status.head, svc.chain_head(conn))
        conn.close()

    def test_run_all_reports_first_failure_and_keeps_checking_the_rest(self) -> None:
        conn = fresh()
        svc = LedgerService(SqliteLedgerStore())
        a = make_sale(1)
        insert_sale(conn, a)
        insert_sale(conn, make_sale(3, prev_hash=a.hash))  # gap at 2
        with self.assertLogs("vaishnavi.services.ledger", level="ERROR"):
            status = svc.run_all(conn, day="2026-11-14")
        self.assertFalse(status.ok)
        self.assertEqual(status.codes, ["E-LEDGER-002"])
        self.assertTrue(status.reports["CHAIN"].ok)
        self.assertFalse(status.reports["GAPS"].ok)
        rows = dict(conn.execute("SELECT kind, ok FROM integrity_check").fetchall())
        self.assertEqual(rows["GAPS"], 0)
        conn.close()

    def test_audit_chain_is_part_of_the_check(self) -> None:
        conn = fresh()
        svc = LedgerService(SqliteLedgerStore())
        conn.execute("INSERT INTO audit_log(at, action, prev_hash, hash) VALUES (?, 'LOGIN', ?, 'deadbeef')", (NOW, GENESIS_HASH))
        conn.commit()
        status = svc.run_all(conn, day="2026-11-14")
        self.assertFalse(status.ok)
        self.assertFalse(status.reports["AUDIT"].ok)
        self.assertIn("audit_log", status.reports["AUDIT"].detail)
        conn.close()


if __name__ == "__main__":
    unittest.main()
