"""Unit 41 — shifts & cash reconciliation (docs/05 §6): float, cash in/out, expected vs counted, variance, X/Z reports."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import ShiftError
from vaishnavi.core.events import EventBus, ShiftClosed
from vaishnavi.core.permissions import Actor, PinRequired, Role
from vaishnavi.core.settings import Settings
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteInventoryStore, SqliteShiftStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, PaymentInput
from vaishnavi.services.catalog import CatalogService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferService
from vaishnavi.services.shift import ShiftService, count_denominations

from tests.core.test_billing import CAMPHOR, OWNER, STAFF, STAFF_PIN
from tests.core.test_db_guardian_backup import T0, make_live_db


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # shift 1 OPEN for user 2 with two CASH 2000 sales
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.txn = SqliteTxn(self.paths.database)
        self.audit = AuditService(SqliteAuditStore())
        self.settings = Settings()
        catalog_store = SqliteCatalogStore()
        catalog = CatalogService(self.txn, catalog_store, self.audit, event_bus=self.bus)
        self.ledger = LedgerService(SqliteLedgerStore())
        billing_store = SqliteBillingStore()
        offers = OfferService(self.txn, billing_store, catalog_store, self.audit)
        self.billing = BillingService(self.txn, billing_store, catalog_store, SqliteInventoryStore(), catalog, offers, self.ledger, self.audit, settings=lambda: self.settings, event_bus=self.bus)
        self.shifts = ShiftService(self.txn, SqliteShiftStore(), self.audit, self.ledger, event_bus=self.bus)
        self.closed: list[ShiftClosed] = []
        self.bus.subscribe(ShiftClosed, self.closed.append)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, actor: Actor, payments: list[PaymentInput], qty: str = "2") -> None:
        cart = self.billing.new_cart(actor)
        self.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        self.billing.checkout(actor, cart, payments)


class OpenCloseTest(Base):
    def test_open_once_per_user_and_owner_sees_all(self) -> None:
        self.assertEqual(self.shifts.current(2).id, 1)  # type: ignore[union-attr]
        self.assertIsNone(self.shifts.current(1))
        mine = self.shifts.open(OWNER, opening_float_paise=100000)
        self.assertEqual((mine.user_id, mine.opening_float_paise, mine.status), (1, 100000, "OPEN"))
        with self.assertRaises(ShiftError) as cm:
            self.shifts.open(OWNER, opening_float_paise=0)
        self.assertEqual(cm.exception.code, "E-SHIFT-002")
        with self.assertRaises(ShiftError):
            self.shifts.open(STAFF, opening_float_paise=0)
        self.assertEqual([s.user_id for s in self.shifts.open_shifts()], [2, 1])
        with self.assertRaises(ValueError):
            self.shifts.open(Actor(user_id=3, role=Role.STAFF), opening_float_paise=-1)
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action = 'SHIFT_OPENED'")]
        self.assertEqual(len(actions), 1)

    def test_expected_cash_follows_sales_change_and_cash_events(self) -> None:
        self.assertEqual(self.shifts.expected_cash(1), 4000, "float 0 + two cash bills of ₹20")
        self.shifts.cash_in(STAFF, 500, reason="change from owner")
        self.assertEqual(self.shifts.expected_cash(1), 4500)
        with self.assertRaises(PinRequired):
            self.shifts.cash_out(STAFF, 300, reason="tea")
        self.shifts.cash_out(STAFF_PIN, 300, reason="tea", approver_id=1)
        self.assertEqual(self.shifts.expected_cash(1), 4200)
        with self.assertRaises(ShiftError) as cm:
            self.shifts.cash_out(OWNER, 999999, reason="x", shift_id=1)
        self.assertEqual(cm.exception.code, "E-SHIFT-004", "cannot take out more than the drawer holds")
        self._sale(STAFF, [PaymentInput("CASH", 2000, tendered_paise=5000)])
        self.assertEqual(self.shifts.expected_cash(1), 6200, "₹50 tendered, ₹30 change → +₹20")
        self._sale(STAFF, [PaymentInput("UPI", 2000)])
        self.assertEqual(self.shifts.expected_cash(1), 6200, "UPI never touches the drawer")
        with self.assertRaises(ValueError):
            self.shifts.cash_in(STAFF, 0, reason="x")
        with self.assertRaises(ValueError):
            self.shifts.cash_in(STAFF, 10, reason="")
        events = self._q("SELECT type, amount_paise, approved_by_user_id FROM cash_event ORDER BY id")
        self.assertEqual(events, [("CASH_IN", 500, None), ("CASH_OUT", 300, 1)])

    def test_x_report_mid_shift(self) -> None:
        self.shifts.cash_in(STAFF, 500, reason="float top-up")
        self._sale(STAFF, [PaymentInput("CASH", 2000, tendered_paise=2000)])
        self._sale(STAFF, [PaymentInput("UPI", 1000)], qty="1")
        x = self.shifts.x_report(1)
        self.assertEqual((x.bills, x.sales_total_paise), (4, 7000))
        self.assertEqual(x.by_mode, {"CASH": 6000, "UPI": 1000})
        self.assertEqual((x.cash_in_paise, x.cash_out_paise, x.expected_cash_paise), (500, 0, 6500))
        self.assertEqual(x.invoice_range, ("VPS/26-27/000001", "VPS/26-27/000004"))
        self.assertEqual(x.items_sold, Decimal("7"))
        self.assertIsNone(x.variance_paise, "an X report has no count yet")
        self.assertEqual(x.cashier_id, 2)

    def test_close_with_denominations_variance_z_report_and_rules(self) -> None:
        self.assertEqual(count_denominations({500: 12, 100: 2}), 620000, "₹6,200 in paise")
        self.shifts.cash_in(STAFF, 2200, reason="top-up")
        z = self.shifts.close(STAFF, 1, denominations={50: 1, 10: 1, 2: 1}, note="")
        self.assertEqual((z.status, z.expected_cash_paise, z.counted_cash_paise, z.variance_paise, z.closed_by_user_id), ("CLOSED", 6200, 6200, 0, 2))
        self.assertIsNone(self.shifts.current(2))
        self.assertEqual([(e.shift_id, e.variance_paise) for e in self.closed], [(1, 0)])
        report = self.shifts.z_report(1)
        self.assertEqual((report.variance_paise, report.counted_cash_paise), (0, 6200))
        self.assertEqual(report.chain_head, self.ledger_head())
        self.assertEqual(report.denominations, {50: 1, 10: 1, 2: 1})
        lines = self.shifts.report_lines(report)
        self.assertTrue(any("Z REPORT" in ln for ln in lines))
        self.assertTrue(any("62.00" in ln for ln in lines), lines)
        self.assertTrue(any(report.chain_head[:16] in ln for ln in lines), "the chain head goes on paper (off-system anchor)")
        with self.assertRaises(ShiftError) as cm:
            self.shifts.close(STAFF, 1, denominations={}, note="")
        self.assertEqual(cm.exception.code, "E-SHIFT-003")
        with self.assertRaises(ShiftError) as cm:
            self.shifts.cash_in(STAFF, 100, reason="late")
        self.assertEqual(cm.exception.code, "E-SHIFT-001", "no open shift")
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action LIKE 'SHIFT_%' OR action LIKE 'CASH_%' ORDER BY id")]
        self.assertEqual(actions, ["CASH_IN", "SHIFT_CLOSED"])

    def test_negative_variance_needs_a_note_and_owner_can_close_staff_shift(self) -> None:
        with self.assertRaises(ValueError):
            self.shifts.close(STAFF, 1, counted_cash_paise=3800, note="")
        z = self.shifts.close(OWNER, 1, counted_cash_paise=3800, note="₹2 short, probably change error")
        self.assertEqual((z.variance_paise, z.closed_by_user_id, z.note), (-200, 1, "₹2 short, probably change error"))
        hist = self.shifts.variance_history(limit=10)
        self.assertEqual([(h.user_id, h.variance_paise) for h in hist], [(2, -200)])
        self.assertEqual(self.shifts.variance_history(user_id=1), [])
        summary = self.shifts.cashier_variance_summary()
        self.assertEqual(summary[2].shifts, 1)
        self.assertEqual(summary[2].negative_shifts, 1)
        self.assertEqual(summary[2].total_variance_paise, -200)

    def ledger_head(self) -> str:
        with self.txn(label="t") as session:
            return self.ledger.chain_head(session)


if __name__ == "__main__":
    unittest.main()
