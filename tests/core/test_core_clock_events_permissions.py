"""core.clock, core.events, core.permissions (stdlib only)."""

from __future__ import annotations

import logging
import unittest
from datetime import UTC, date, datetime, timedelta

from vaishnavi.core import clock as clk
from vaishnavi.core.clock import IST, FrozenClock, SystemClock, business_day_bounds, format_ist, to_ist, to_utc
from vaishnavi.core.events import Alert, Event, EventBus, ProductChanged, SaleCommitted, StockChanged
from vaishnavi.core.permissions import (
    MATRIX,
    Actor,
    Grant,
    OpenItemPolicy,
    Permission,
    PermissionDenied,
    PinRequired,
    Role,
    check,
    grant_for,
    permissions_of,
    require,
)


class ClockTest(unittest.TestCase):
    def tearDown(self) -> None:
        clk.set_clock(SystemClock())

    def test_system_clock_is_aware_utc_and_monotonic(self) -> None:
        c = SystemClock()
        self.assertEqual(c.now().tzinfo, UTC)
        a, b = c.monotonic(), c.monotonic()
        self.assertLessEqual(a, b)

    def test_frozen_clock_advance_and_set(self) -> None:
        f = FrozenClock()
        clk.set_clock(f)
        t0 = clk.now_utc()
        f.advance(minutes=5)
        self.assertEqual(clk.now_utc() - t0, timedelta(minutes=5))
        self.assertEqual(f.monotonic(), 1000.0 + 300)
        f.set(datetime(2026, 11, 14, 9, 30))  # naive → IST
        self.assertEqual(clk.now_ist().hour, 9)
        self.assertEqual(clk.now_utc().hour, 4)  # 09:30 IST = 04:00 UTC
        self.assertEqual(clk.today_ist(), date(2026, 11, 14))

    def test_midnight_boundary_uses_shop_day(self) -> None:
        f = FrozenClock()
        clk.set_clock(f)
        f.set(datetime(2026, 11, 15, 0, 30))  # 00:30 IST = 19:00 UTC previous day
        self.assertEqual(clk.now_utc().date(), date(2026, 11, 14))
        self.assertEqual(clk.today_ist(), date(2026, 11, 15), "sales after midnight belong to the IST day")

    def test_conversions_and_formatting(self) -> None:
        utc = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
        self.assertEqual(to_ist(utc).strftime("%H:%M"), "09:30")
        self.assertEqual(to_ist(datetime(2026, 11, 14, 4, 0)).hour, 9, "naive assumed UTC")
        self.assertEqual(to_utc(datetime(2026, 11, 14, 9, 30)).hour, 4, "naive assumed IST")
        self.assertEqual(format_ist(utc), "14-Nov-2026 09:30")
        start, end = business_day_bounds(date(2026, 11, 14))
        self.assertEqual(start, datetime(2026, 11, 13, 18, 30, tzinfo=UTC))
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual(IST.utcoffset(None), timedelta(hours=5, minutes=30))

    def test_set_clock_returns_previous(self) -> None:
        prev = clk.set_clock(FrozenClock())
        self.assertIsInstance(prev, SystemClock)


class EventBusTest(unittest.TestCase):
    def test_publish_subscribe_and_unsubscribe(self) -> None:
        bus = EventBus()
        got: list[Event] = []
        unsub = bus.subscribe(SaleCommitted, got.append)
        ev = SaleCommitted(sale_id=1, invoice_no="VPS/25-26/000001", total_paise=1000, user_id=2, line_count=3, correlation_id="c1")
        self.assertEqual(bus.publish(ev), 1)
        self.assertEqual(got, [ev])
        self.assertEqual(bus.publish(ProductChanged(product_id=1, change="price")), 0, "no handler for this type")
        unsub()
        bus.publish(ev)
        self.assertEqual(len(got), 1)
        unsub()  # idempotent

    def test_base_class_subscription_receives_all_events(self) -> None:
        bus = EventBus()
        got: list[str] = []
        bus.subscribe(Event, lambda e: got.append(type(e).__name__))
        bus.publish(StockChanged(product_id=1, movement_type="SALE", qty_delta=-1, qty_after=9))
        bus.publish(Alert(kind="LOW_STOCK", severity="WARN", message="x"))
        self.assertEqual(got, ["StockChanged", "Alert"])

    def test_failing_subscriber_is_isolated_and_logged(self) -> None:
        bus = EventBus()
        order: list[str] = []

        def bad(e: Event) -> None:
            raise RuntimeError("dashboard glitch")

        bus.subscribe(SaleCommitted, bad)
        bus.subscribe(SaleCommitted, lambda e: order.append("good"))
        stream: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: stream.append(record.getMessage())  # type: ignore[method-assign]
        logging.getLogger("vaishnavi.core.events").addHandler(handler)
        try:
            ok = bus.publish(SaleCommitted(sale_id=1, invoice_no="x", total_paise=1, user_id=1, line_count=1))
        finally:
            logging.getLogger("vaishnavi.core.events").removeHandler(handler)
        self.assertEqual(ok, 1)
        self.assertEqual(order, ["good"], "the good handler still runs after the bad one")
        self.assertEqual(bus.failed, 1)
        self.assertTrue(any("dashboard glitch" in m for m in stream))

    def test_events_are_frozen_and_stamped(self) -> None:
        ev = ProductChanged(product_id=5, change="created")
        self.assertIsNotNone(ev.at.tzinfo)
        with self.assertRaises(Exception):
            ev.product_id = 6  # type: ignore[misc]


class PermissionsTest(unittest.TestCase):
    def test_matrix_covers_every_permission_and_role(self) -> None:
        self.assertEqual(set(MATRIX), set(Permission))
        for perm, grants in MATRIX.items():
            self.assertEqual(set(grants), set(Role), perm)
            self.assertIs(grants[Role.OWNER], Grant.ALLOW, f"owner can do everything: {perm}")

    def test_staff_grants_match_docs_05_section_5(self) -> None:
        expected_allow = {Permission.SELL, Permission.DISCOUNT_WITHIN_CAP, Permission.HOLD_RESUME_REPRINT, Permission.SHIFT_OWN, Permission.VIEW_STOCK_LEVEL, Permission.PRINT_LABEL, Permission.RECEIVE_STOCK, Permission.REPORT_OWN_SHIFT, Permission.AI_STOCK_HELP}
        expected_pin = {Permission.DISCOUNT_OVER_CAP, Permission.VOID_REFUND, Permission.CASH_OUT, Permission.SHIFT_CLOSE_VARIANCE}
        expected_policy = {Permission.OPEN_ITEM}
        staff = {p: g[Role.STAFF] for p, g in MATRIX.items()}
        self.assertEqual({p for p, g in staff.items() if g is Grant.ALLOW}, expected_allow)
        self.assertEqual({p for p, g in staff.items() if g is Grant.PIN}, expected_pin)
        self.assertEqual({p for p, g in staff.items() if g is Grant.POLICY}, expected_policy)
        denied = {p for p, g in staff.items() if g is Grant.DENY}
        for must_deny in (Permission.EDIT_PRICES, Permission.VIEW_BANK_DETAILS, Permission.STOCK_MOVEMENTS, Permission.REPORT_SALES, Permission.REPORT_PROFIT, Permission.MANAGE_SYSTEM, Permission.AI_BUSINESS, Permission.AI_CONFIGURE):
            self.assertIn(must_deny, denied)

    def test_open_item_policy_resolution(self) -> None:
        self.assertIs(grant_for(Permission.OPEN_ITEM, Role.STAFF, OpenItemPolicy.ALLOW), Grant.ALLOW)
        self.assertIs(grant_for(Permission.OPEN_ITEM, Role.STAFF, OpenItemPolicy.PIN), Grant.PIN)
        self.assertIs(grant_for(Permission.OPEN_ITEM, Role.STAFF, OpenItemPolicy.BLOCK), Grant.DENY)
        self.assertIs(grant_for(Permission.OPEN_ITEM, Role.OWNER, OpenItemPolicy.BLOCK), Grant.ALLOW, "policy never restricts the owner")

    def test_require_and_check(self) -> None:
        staff = Actor(user_id=7, role=Role.STAFF)
        owner = Actor(user_id=1, role=Role.OWNER)
        require(staff, Permission.SELL)
        require(owner, Permission.REPORT_PROFIT)
        with self.assertRaises(PermissionDenied) as cm:
            require(staff, Permission.REPORT_PROFIT)
        self.assertEqual(cm.exception.code, "E-AUTH-010")
        with self.assertRaises(PinRequired) as pin:
            require(staff, Permission.VOID_REFUND)
        self.assertEqual(pin.exception.code, "E-AUTH-011")
        self.assertIs(check(Actor(7, Role.STAFF, pin_verified=True), Permission.VOID_REFUND), Grant.ALLOW)
        require(Actor(7, Role.STAFF, pin_verified=True), Permission.VOID_REFUND)
        with self.assertRaises(PermissionDenied):
            require(Actor(7, Role.STAFF, pin_verified=True), Permission.EDIT_PRICES)  # PIN never unlocks DENY

    def test_permissions_of(self) -> None:
        staff_map = permissions_of(Role.STAFF, OpenItemPolicy.BLOCK)
        self.assertEqual(len(staff_map), len(Permission))
        self.assertIs(staff_map[Permission.OPEN_ITEM], Grant.DENY)
        self.assertTrue(all(g is Grant.ALLOW for g in permissions_of(Role.OWNER).values()))


if __name__ == "__main__":
    unittest.main()
