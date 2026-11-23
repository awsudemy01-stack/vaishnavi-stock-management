"""db/schema.sql applied with the stdlib sqlite3 module: tables, constraints and the anti-theft triggers."""

from __future__ import annotations

import sqlite3
import unittest

from vaishnavi.db.schema import SCHEMA_FILE, apply_schema, load_statements, schema_hash, table_names, trigger_names

EXPECTED_TABLES = {
    "role", "user", "app_run", "work_session", "category", "product", "product_barcode", "tag", "product_tag",
    "price_history", "label_print_log", "offer", "stock_movement", "product_stock", "stock_daily_balance",
    "_write_guard", "shift", "cash_event", "customer", "counter", "sale", "sale_item", "payment",
    "open_item_review", "print_pending", "held_cart", "held_cart_line", "cashier_day_record", "supplier", "purchase", "purchase_item",
    "stock_count", "stock_count_line", "audit_log", "notification", "recovery_key", "setup_step", "setting",
    "secure_setting", "db_identity", "backup_log", "integrity_check", "hardware_override", "publish_queue",
    "chat_session", "chat_message", "ai_usage", "chat_security_event", "doc_chunk", "embedding",
    "prompt_version", "festival", "forecast_snapshot",
}


def fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    conn.execute("INSERT INTO role(id, name) VALUES (1, 'OWNER'), (2, 'STAFF')")
    conn.execute("INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (1, 'owner', 'Owner', 1, 'x'), (2, 'ramesh', 'Ramesh', 2, 'x')")
    conn.execute("INSERT INTO product(id, sku, name, price_paise) VALUES (10, 'CAM-0001', 'Camphor 50g', 1000)")
    conn.execute("INSERT INTO product(id, sku, name, price_paise, is_system) VALUES (99, 'OPEN-ITEM', 'Open item', 0, 1)")
    conn.execute("INSERT INTO shift(id, user_id, opened_at) VALUES (1, 2, '2026-11-14T03:30:00Z')")
    conn.commit()
    return conn


def insert_sale(conn: sqlite3.Connection, sale_id: int = 1, seq: int = 1) -> None:
    conn.execute(
        "INSERT INTO sale(id, invoice_no, client_txn_id, public_id, fin_year, seq_no, sold_at, user_id, shift_id, status, subtotal_paise, taxable_paise, total_paise, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, '25-26', ?, '2026-11-14T04:00:00Z', 2, 1, 'COMPLETED', 1000, 1000, 1000, 'GENESIS', ?)",
        (sale_id, f"VPS/25-26/{seq:06d}", f"txn{sale_id}", f"pub{sale_id}", seq, f"hash{sale_id}"),
    )


def movement(conn: sqlite3.Connection, *, user: int, type_: str, delta: int, after: int, product: int = 10, ref_type: str | None = None, ref_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, ref_type, ref_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (product, type_, delta, after, ref_type, ref_id, user),
    )


class SchemaFileTest(unittest.TestCase):
    def test_statements_split_correctly_including_triggers(self) -> None:
        stmts = load_statements()
        self.assertGreater(len(stmts), 80)
        triggers = [s for s in stmts if s.upper().startswith("CREATE TRIGGER")]
        self.assertTrue(all(s.rstrip().upper().endswith("END;") for s in triggers), "trigger bodies kept whole")
        self.assertFalse(any(s.upper().startswith("PRAGMA") for s in stmts))
        self.assertEqual(len(schema_hash()), 16)
        self.assertTrue(SCHEMA_FILE.exists())

    def test_applies_cleanly_and_creates_every_documented_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        count = apply_schema(conn)
        self.assertGreater(count, 80)
        self.assertEqual(table_names(conn), EXPECTED_TABLES)
        self.assertGreaterEqual(len(trigger_names(conn)), 18)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class ConstraintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh_db()

    def test_enums_and_ranges(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO role(name) VALUES ('ADMIN')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO product(sku, name, price_paise) VALUES ('X', 'neg', -1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO product(sku, name, price_paise, tax_rate_bp) VALUES ('X', 'x', 1, 20000)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE product SET sale_mode = 'VOLUMETRIC' WHERE id = 10")

    def test_one_open_shift_per_user(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO shift(user_id, opened_at) VALUES (2, '2026-11-14T05:00:00Z')")
        self.conn.execute("UPDATE shift SET status = 'CLOSED', closed_at = '2026-11-14T12:00:00Z' WHERE id = 1")
        self.conn.execute("INSERT INTO shift(user_id, opened_at) VALUES (2, '2026-11-15T03:30:00Z')")

    def test_sale_item_needs_product_unless_open_item(self) -> None:
        insert_sale(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO sale_item(sale_id, name_snapshot, qty, unit, shelf_price_paise, unit_price_paise, taxable_paise, line_total_paise, entry_method) VALUES (1, 'x', 1, 'pcs', 10, 10, 10, 10, 'SCAN')")
        self.conn.execute("INSERT INTO sale_item(sale_id, name_snapshot, qty, unit, shelf_price_paise, unit_price_paise, taxable_paise, line_total_paise, entry_method, is_open_item, open_item_name) VALUES (1, 'x', 1, 'pcs', 10, 10, 10, 10, 'OPEN_ITEM', 1, 'loose kumkum')")

    def test_sale_sequence_unique_per_fin_year_and_idempotent_txn(self) -> None:
        insert_sale(self.conn, 1, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_sale(self.conn, 2, 1)  # same seq_no in same FY
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO sale(id, invoice_no, client_txn_id, public_id, fin_year, seq_no, sold_at, user_id, shift_id, status, subtotal_paise, taxable_paise, total_paise, prev_hash, hash) VALUES (3, 'VPS/25-26/000003', 'txn1', 'pub3', '25-26', 3, 'x', 2, 1, 'COMPLETED', 1, 1, 1, 'a', 'h3')")

    def test_one_active_prompt_version(self) -> None:
        self.conn.execute("INSERT INTO prompt_version(version, editable_instructions, core_hash, is_active) VALUES (1, 'a', 'h', 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO prompt_version(version, editable_instructions, core_hash, is_active) VALUES (2, 'b', 'h', 1)")
        self.conn.execute("INSERT INTO prompt_version(version, editable_instructions, core_hash, is_active) VALUES (2, 'b', 'h', 0)")


class StockTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh_db()

    def qty(self, product: int = 10) -> int | None:
        row = self.conn.execute("SELECT qty FROM product_stock WHERE product_id = ?", (product,)).fetchone()
        return None if row is None else int(row[0])

    def test_owner_opening_stock_populates_cache_via_trigger(self) -> None:
        self.assertIsNone(self.qty())
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        self.assertEqual(self.qty(), 50)
        movement(self.conn, user=1, type_="PURCHASE", delta=20, after=70)
        self.assertEqual(self.qty(), 70)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM _write_guard").fetchone()[0], 0, "guard row cleaned up")

    def test_wrong_running_balance_is_refused(self) -> None:
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            movement(self.conn, user=1, type_="ADJUSTMENT", delta=-5, after=40)  # should be 45
        self.assertIn("E-INV-013", str(cm.exception))
        self.assertEqual(self.qty(), 50)

    def test_direct_writes_to_product_stock_are_blocked(self) -> None:
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        for sql in ("UPDATE product_stock SET qty = 999 WHERE product_id = 10", "INSERT INTO product_stock(product_id, qty) VALUES (99, 5)", "DELETE FROM product_stock WHERE product_id = 10"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError) as cm:
                self.conn.execute(sql)
            self.assertIn("E-INV-012", str(cm.exception))
        self.assertEqual(self.qty(), 50)

    def test_staff_can_only_create_sale_movements_and_positive_receipts(self) -> None:
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        for type_ in ("OPENING", "PURCHASE", "ADJUSTMENT", "DAMAGE", "STOCK_TAKE"):
            with self.subTest(type_=type_), self.assertRaises(sqlite3.IntegrityError) as cm:
                movement(self.conn, user=2, type_=type_, delta=-1, after=49)
            self.assertIn("E-INV-010", str(cm.exception))
        for type_ in ("OPENING", "ADJUSTMENT", "STOCK_TAKE"):
            with self.subTest(type_=type_, direction="up"), self.assertRaises(sqlite3.IntegrityError) as cm:
                movement(self.conn, user=2, type_=type_, delta=5, after=55)
            self.assertIn("E-INV-010", str(cm.exception))
        movement(self.conn, user=2, type_="PURCHASE", delta=12, after=62)
        self.assertEqual(self.qty(), 62, "goods received at the counter: staff may add, never remove")
        insert_sale(self.conn)
        movement(self.conn, user=2, type_="SALE", delta=-2, after=60, ref_type="SALE", ref_id=1)
        self.assertEqual(self.qty(), 60)

    def test_statement_for_returns_the_trigger_exactly_as_in_schema_sql(self) -> None:
        from vaishnavi.db.schema import statement_for

        stmt = statement_for("trg_stock_movement_validate")
        self.assertTrue(stmt.startswith("CREATE TRIGGER trg_stock_movement_validate"))
        self.assertIn("NEW.type = 'PURCHASE' AND NEW.qty_delta > 0", stmt)
        self.assertTrue(statement_for("stock_movement").startswith("CREATE TABLE stock_movement"))
        with self.assertRaises(KeyError):
            statement_for("no_such_object")

    def test_sale_movement_must_reference_an_existing_sale(self) -> None:
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            movement(self.conn, user=2, type_="SALE", delta=-1, after=49)
        self.assertIn("E-INV-011", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            movement(self.conn, user=2, type_="SALE", delta=-1, after=49, ref_type="SALE", ref_id=42)
        self.assertIn("E-INV-011", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            movement(self.conn, user=1, type_="SALE_VOID", delta=1, after=51)  # void needs its sale too

    def test_system_open_item_product_never_moves_stock(self) -> None:
        insert_sale(self.conn)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            movement(self.conn, user=2, type_="SALE", delta=-1, after=-1, product=99, ref_type="SALE", ref_id=1)
        self.assertIn("E-INV-011", str(cm.exception))

    def test_movements_are_append_only(self) -> None:
        movement(self.conn, user=1, type_="OPENING", delta=50, after=50)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE stock_movement SET qty_delta = 500")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM stock_movement")

    def test_failed_movement_rolls_back_with_its_transaction(self) -> None:
        """One-sale-one-transaction: a refused movement takes the sale with it (docs/14 §2a)."""
        movement(self.conn, user=1, type_="OPENING", delta=1, after=1)
        self.conn.commit()
        try:
            self.conn.execute("BEGIN")
            insert_sale(self.conn, 7, 7)
            movement(self.conn, user=2, type_="SALE", delta=-2, after=-1, ref_type="SALE", ref_id=7)  # fine for the DB (policy is service-level)
            movement(self.conn, user=2, type_="SALE", delta=-1, after=-5, ref_type="SALE", ref_id=7)  # wrong balance → abort
        except sqlite3.IntegrityError:
            self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sale WHERE id = 7").fetchone()[0], 0)
        self.assertEqual(self.qty(), 1)


class LedgerTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fresh_db()
        insert_sale(self.conn)
        self.conn.execute("INSERT INTO payment(sale_id, mode, amount_paise) VALUES (1, 'CASH', 1000)")
        self.conn.execute("INSERT INTO audit_log(at, user_id, action, prev_hash, hash) VALUES ('x', 2, 'LOGIN', 'GENESIS', 'a1')")

    def test_sale_cannot_be_deleted_or_financially_edited(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.conn.execute("DELETE FROM sale WHERE id = 1")
        self.assertIn("E-LEDGER-001", str(cm.exception))
        for sql in ("UPDATE sale SET total_paise = 1 WHERE id = 1", "UPDATE sale SET hash = 'tampered' WHERE id = 1", "UPDATE sale SET status = 'VOIDED' WHERE id = 1", "UPDATE sale SET user_id = 1 WHERE id = 1"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)

    def test_sale_operational_fields_may_change(self) -> None:
        self.conn.execute("UPDATE sale SET reprint_count = 1, receipt_printed = 1, note = 'printed later' WHERE id = 1")
        self.assertEqual(self.conn.execute("SELECT reprint_count, receipt_printed FROM sale WHERE id = 1").fetchone(), (1, 1))

    def test_items_payments_audit_append_only(self) -> None:
        for sql in ("UPDATE payment SET amount_paise = 1", "DELETE FROM payment", "UPDATE audit_log SET action = 'X'", "DELETE FROM audit_log"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError) as cm:
                self.conn.execute(sql)
            self.assertIn("E-LEDGER-001", str(cm.exception))

    def test_price_history_append_only(self) -> None:
        self.conn.execute("INSERT INTO price_history(product_id, field, old_value_paise, new_value_paise, effective_from) VALUES (10, 'PRICE', 1000, 1100, 'x')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM price_history")


class ProductTouchTest(unittest.TestCase):
    def test_row_version_advances_on_update(self) -> None:
        conn = fresh_db()
        self.assertEqual(conn.execute("SELECT row_version FROM product WHERE id = 10").fetchone()[0], 1)
        conn.execute("UPDATE product SET price_paise = 1200 WHERE id = 10")
        self.assertEqual(conn.execute("SELECT row_version FROM product WHERE id = 10").fetchone()[0], 2)
        conn.execute("UPDATE product SET name = 'Camphor 50 g' WHERE id = 10")
        self.assertEqual(conn.execute("SELECT row_version FROM product WHERE id = 10").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
