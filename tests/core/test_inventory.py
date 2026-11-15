"""Inventory (docs/04 stock ledger, docs/05 §2a, docs/13 Phase 2 unit 32): InventoryService — movements, purchases,
stock-take, low-stock events, reconciliation. Written first (docs/13 rule 7). Real SQLite; frozen clock.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import InventoryError
from vaishnavi.core.events import EventBus, StockChanged
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteCatalogStore, SqliteInventoryStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.inventory import InventoryService, PurchaseLine
from vaishnavi.services.price_history import PriceHistoryService

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # product 10 "Camphor 50g" with OPENING 100 and 2 sales of 2 → stock 96
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.txn = SqliteTxn(self.paths.database)
        self.audit = AuditService(SqliteAuditStore())
        self.catalog = CatalogService(self.txn, SqliteCatalogStore(), self.audit, event_bus=self.bus)
        self.prices = PriceHistoryService(self.txn, SqliteCatalogStore())
        self.inventory = InventoryService(self.txn, SqliteInventoryStore(), SqliteCatalogStore(), self.audit, event_bus=self.bus)
        self.events: list[StockChanged] = []
        self.bus.subscribe(StockChanged, self.events.append)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class MovementsTest(Base):
    def test_fixture_stock_and_adjustment_with_reason(self) -> None:
        self.assertEqual(self.inventory.stock_of(10), Decimal("96"))
        mv = self.inventory.adjust(OWNER, 10, Decimal("-6"), reason="broken packet")
        self.assertEqual((mv.type, mv.qty_delta, mv.qty_after), ("ADJUSTMENT", Decimal("-6"), Decimal("90")))
        self.assertEqual(self.inventory.stock_of(10), Decimal("90"))
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 90)
        with self.assertRaises(ValueError):
            self.inventory.adjust(OWNER, 10, Decimal("-1"), reason="")  # a reason is mandatory
        with self.assertRaises(ValueError):
            self.inventory.adjust(OWNER, 10, Decimal("0"), reason="nothing")
        with self.assertRaises(PermissionDenied):
            self.inventory.adjust(STAFF, 10, Decimal("-1"), reason="x")
        self.assertEqual(self.events[-1].movement_type, "ADJUSTMENT")
        actions = [r[0] for r in self._q("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("STOCK_ADJUSTED", actions)

    def test_negative_stock_policy_and_damage(self) -> None:
        with self.assertRaises(InventoryError) as cm:
            self.inventory.adjust(OWNER, 10, Decimal("-200"), reason="typo")
        self.assertEqual(cm.exception.code, "E-INV-003", "BLOCK policy: never below zero")
        self.inventory.record_damage(OWNER, 10, Decimal("3"), reason="water damage")
        self.assertEqual(self.inventory.stock_of(10), Decimal("93"))
        self.assertEqual(self._q("SELECT type FROM stock_movement ORDER BY id DESC LIMIT 1")[0][0], "DAMAGE")

    def test_low_stock_event_fires_once_when_crossing_the_minimum(self) -> None:
        p = self.catalog.create(OWNER, ProductInput(name="Agarbatti 20", category_id=None, price_paise=1000, min_stock=Decimal("5")))
        self.inventory.opening_stock(OWNER, p.id, Decimal("8"))
        self.assertFalse(self.events[-1].low_stock)
        self.inventory.adjust(OWNER, p.id, Decimal("-4"), reason="counter sample")
        self.assertTrue(self.events[-1].low_stock, "8 → 4 crosses min 5")
        self.assertEqual(self.events[-1].product_name, "Agarbatti 20")
        low = self.inventory.low_stock_products()
        self.assertEqual([x.id for x in low], [p.id])
        self.assertEqual(self.inventory.opening_stock(OWNER, p.id, Decimal("1")), None, "opening stock can only be set once")

    def test_timeline_and_reconciliation(self) -> None:
        self.inventory.adjust(OWNER, 10, Decimal("-1"), reason="a")
        rows = self.inventory.movements(10, limit=10)
        self.assertEqual([r.type for r in rows][:2], ["ADJUSTMENT", "SALE"], "newest first")
        self.assertEqual(rows[0].user_id, 1)
        report = self.inventory.reconcile()
        self.assertTrue(report.ok)
        self.assertEqual(report.products_checked, 1 + 0)
        self.assertEqual(self.inventory.valuation_paise(), 95 * 1000, "stock value at the catalogue price when cost is unknown")


class PurchaseTest(Base):
    def test_purchase_entry_moves_stock_updates_cost_and_price_history(self) -> None:
        self.assertEqual(self.catalog.get(10).cost_paise, None)  # type: ignore[union-attr]
        purchase = self.inventory.record_purchase(OWNER, supplier_name="Sharma Traders", invoice_no="ST/1201", lines=[PurchaseLine(product_id=10, qty=Decimal("50"), unit_cost_paise=700)])
        self.assertEqual(purchase.total_paise, 50 * 700)
        self.assertEqual(self.inventory.stock_of(10), Decimal("146"))
        mv = self._q("SELECT type, qty_delta, ref_type, ref_id, unit_cost_paise FROM stock_movement ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(mv, ("PURCHASE", 50, "PURCHASE", purchase.id, 700))
        self.assertEqual(self.catalog.get(10).cost_paise, 700, "the product's cost follows the latest purchase")  # type: ignore[union-attr]
        hist = self.prices.history(10, field="COST")
        self.assertEqual((hist[0].new_value, hist[0].source, hist[0].changed_by), (700, "PURCHASE", 1))
        self.assertEqual(purchase.label_count, 50, "the entry offers to print one label per received piece")
        suppliers = self.inventory.suppliers()
        self.assertEqual([s.name for s in suppliers], ["Sharma Traders"])
        again = self.inventory.record_purchase(OWNER, supplier_name="sharma traders", invoice_no="ST/1202", lines=[PurchaseLine(product_id=10, qty=Decimal("1"), unit_cost_paise=700)])
        self.assertEqual(again.supplier_id, purchase.supplier_id, "supplier matched case-insensitively, not duplicated")
        with self.assertRaises(PermissionDenied):
            self.inventory.record_purchase(STAFF, supplier_name="x", invoice_no=None, lines=[PurchaseLine(10, Decimal("1"), 1)])
        with self.assertRaises(ValueError):
            self.inventory.record_purchase(OWNER, supplier_name="x", invoice_no=None, lines=[])


class StockTakeTest(Base):
    def test_count_draft_variance_and_approval_creates_movements(self) -> None:
        p = self.catalog.create(OWNER, ProductInput(name="Diya", category_id=None, price_paise=500, cost_paise=200))
        self.inventory.opening_stock(OWNER, p.id, Decimal("40"))
        count = self.inventory.start_stock_take(OWNER, scope="SELECTED", product_ids=[10, p.id])
        self.assertEqual(count.status, "DRAFT")
        lines = self.inventory.stock_take_lines(count.id)
        self.assertEqual({(l.product_id, l.system_qty) for l in lines}, {(10, Decimal("96")), (p.id, Decimal("40"))})
        self.inventory.record_count(OWNER, count.id, 10, Decimal("94"))
        self.inventory.record_count(OWNER, count.id, p.id, Decimal("40"))
        lines = {l.product_id: l for l in self.inventory.stock_take_lines(count.id)}
        self.assertEqual(lines[10].variance, Decimal("-2"))
        self.assertEqual(lines[10].variance_value_paise, -2 * 1000, "valued at price when no cost is known")
        self.assertEqual(lines[p.id].variance, Decimal("0"))
        summary = self.inventory.approve_stock_take(OWNER, count.id)
        self.assertEqual(summary.movements, 1, "only the line with a variance moves stock")
        self.assertEqual(self.inventory.stock_of(10), Decimal("94"))
        mv = self._q("SELECT type, ref_type, ref_id FROM stock_movement ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(mv, ("STOCK_TAKE", "STOCK_COUNT", count.id))
        self.assertEqual(self._q("SELECT status FROM stock_count WHERE id = ?", count.id)[0][0], "APPROVED")
        with self.assertRaises(InventoryError):
            self.inventory.approve_stock_take(OWNER, count.id)  # not twice
        with self.assertRaises(PermissionDenied):
            self.inventory.start_stock_take(STAFF, scope="FULL")

    def test_cancel_a_draft(self) -> None:
        count = self.inventory.start_stock_take(OWNER, scope="FULL")
        self.assertGreaterEqual(len(self.inventory.stock_take_lines(count.id)), 1)
        self.inventory.cancel_stock_take(OWNER, count.id)
        self.assertEqual(self._q("SELECT status FROM stock_count WHERE id = ?", count.id)[0][0], "CANCELLED")


class DailyBalanceTest(Base):
    def test_daily_balance_job_snapshots_closing_quantities(self) -> None:
        written = self.inventory.write_daily_balance()
        self.assertGreaterEqual(written, 1)
        rows = self._q("SELECT product_id, qty_close FROM stock_daily_balance WHERE product_id = 10")
        self.assertEqual(rows[0][1], 96)
        self.assertEqual(self.inventory.write_daily_balance(), written, "re-running the same day overwrites, never duplicates")


if __name__ == "__main__":
    unittest.main()
