"""Unit 47 — the owner/staff dashboard (``/``), insight bullets, ``/notifications`` and the SVG charts (docs/09 §2)."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.billing import PaymentInput
from vaishnavi.ui.components import charts
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import dashboard
from vaishnavi.ui.pages import notifications as notifications_page
from vaishnavi.ui.state import ClientState, ClientUser

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")
HI = UiTexts.load("hi")


def _client(role: Role) -> ClientState:
    client = ClientState.anonymous("en")
    client.sign_in(ClientUser(user_id=1 if role is Role.OWNER else 2, username="x", display_name="X", role=role), work_session_id=None)
    return client


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.c = AppServices.build(self.settings, self.paths, root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=root / "config", started_at=T0)
        self.state.extra["services"] = self.c
        self.state.chips["scanner"] = ("green", "Scanner OK")
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})
        self.c.shifts.open(OWNER, opening_float_paise=50000)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _sale(self, actor: Actor = STAFF, *, qty: str = "2", open_item: bool = False):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        self.c.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        if open_item:
            self.c.billing.add_open_item(actor, cart, name="Garland", detail="", qty=Decimal("1"), unit_price_paise=2000)
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput("CASH", total, tendered_paise=total)])

    def _set_min_stock(self, qty: str) -> None:
        self.c.catalog.update(OWNER, 10, row_version=self.c.catalog.get(10).row_version, changes={"min_stock": Decimal(qty)})  # type: ignore[union-attr]


class ChartsTest(unittest.TestCase):
    def test_sparkline_scales_and_survives_flat_or_empty_series(self) -> None:
        svg = charts.sparkline_svg([0, 50, 100], width=100, height=20)
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        self.assertIn("polyline", svg)
        self.assertIn("0.0,19.0 50.0,10.0 100.0,1.0", svg, "x spreads over the width, y is inverted with a 1px pad")
        flat = charts.sparkline_svg([7, 7, 7])
        self.assertIn("polyline", flat, "a flat series must not divide by zero")
        self.assertIn("<svg", charts.sparkline_svg([]))
        self.assertIn("<svg", charts.sparkline_svg([3]))

    def test_bars_render_one_rect_per_value_with_share_widths(self) -> None:
        svg = charts.bars_svg({"CASH": 6000, "UPI": 2000}, width=80)
        self.assertEqual(svg.count("<rect"), 2)
        self.assertIn('width="60.0"', svg)
        self.assertIn('width="20.0"', svg)
        self.assertIn("<svg", charts.bars_svg({}))


class InsightsTest(Base):
    def test_owner_bullets_cover_sales_stock_and_counter_signals(self) -> None:
        self._sale()
        self._sale(open_item=True)
        self._set_min_stock("500")
        bullets = self.c.insights.for_owner(OWNER)
        kinds = {b.kind: b for b in bullets}
        self.assertIn("SALES_TODAY", kinds)
        self.assertEqual(kinds["SALES_TODAY"].params["bills"], 4, "fixture 2 bills + 2 here, all on the same shop day")
        self.assertEqual(kinds["SALES_TODAY"].params["net"], "₹100.00")
        self.assertIn("LOW_STOCK", kinds)
        self.assertEqual(kinds["LOW_STOCK"].params["count"], 1)
        self.assertEqual(kinds["LOW_STOCK"].link, "/reports/low_stock")
        self.assertEqual(kinds["LOW_STOCK"].severity, "WARN")
        self.assertIn("MANUAL_LINES", kinds, "one open item among today's 5 lines is a 20% manual share — above the 15% threshold")
        self.assertTrue(all(b.key.startswith("insights.") for b in bullets))
        self.assertTrue(all(EN.has(b.key) and HI.has(b.key) for b in bullets), "every bullet is translatable in both languages")
        order = [b.severity for b in bullets]
        self.assertEqual(order, sorted(order, key=lambda s: {"HIGH": 0, "WARN": 1, "INFO": 2}[s]), "worst first")

    def test_quiet_shop_has_only_info_bullets_and_staff_see_counter_only(self) -> None:
        bullets = self.c.insights.for_owner(OWNER)
        self.assertNotIn("LOW_STOCK", {b.kind for b in bullets})
        self.assertNotIn("MANUAL_LINES", {b.kind for b in bullets})
        self.assertTrue(all(b.severity == "INFO" for b in bullets), bullets)
        with self.assertRaises(PermissionDenied):
            self.c.insights.for_owner(STAFF)
        self._set_min_stock("500")
        staff = self.c.insights.for_staff(STAFF)
        self.assertEqual([b.kind for b in staff], ["LOW_STOCK"])
        self.assertTrue(all(b.kind not in ("SALES_TODAY", "MANUAL_LINES", "VARIANCE") for b in staff))

    def test_variance_and_print_queue_signals(self) -> None:
        self._sale()
        self.c.shifts.close(OWNER, self.c.shifts.current(1).id, denominations={100: 1}, note="counted twice, still short")  # type: ignore[union-attr]
        kinds = {b.kind: b for b in self.c.insights.for_owner(OWNER)}
        self.assertIn("VARIANCE", kinds)
        self.assertEqual(kinds["VARIANCE"].severity, "WARN")
        self.assertIn("variance", kinds["VARIANCE"].params)


class DashboardTest(Base):
    def test_owner_model_tiles_sparkline_lists_and_setup_card(self) -> None:
        self._sale()
        self.c.sessions.start(user_id=2, client="counter-1")
        m = dashboard.build_dashboard(self.c, self.state, EN, _client(Role.OWNER))
        tiles = {t.key: t for t in m.tiles}
        self.assertEqual(tiles["sales"].value, "₹60.00")
        self.assertEqual(tiles["bills"].value, "3")
        self.assertEqual(tiles["avg_bill"].value, "₹20.00")
        self.assertEqual(tiles["items"].value, "6")
        self.assertEqual(len(m.sparkline), 14)
        self.assertEqual(m.sparkline[-1], 6000)
        self.assertEqual(m.sparkline[:-1], [0] * 13)
        self.assertIn("<svg", m.sparkline_svg)
        self.assertEqual(m.by_mode, {"CASH": 6000})
        self.assertEqual([r.name for r in m.low_stock], [])
        self.assertEqual(m.logged_in, ["Ramesh"])
        self.assertIsNotNone(m.setup_card, "owner still has setup steps")
        self.assertTrue(m.insights)
        self.assertEqual([c.name for c in m.chips][:1], ["scanner"])
        self.assertEqual(m.shift_text, "")  # the owner's own open shift is shown via /shifts; dashboard text only for staff

    def test_staff_model_shows_own_shift_not_shop_sales(self) -> None:
        self._sale()
        self._set_min_stock("500")
        self._sale(STAFF, qty="1")  # fixture shift #1 is already open for ramesh
        x = self.c.shifts.x_report(1)
        self.assertGreaterEqual(x.bills, 2)
        m = dashboard.build_dashboard(self.c, self.state, EN, _client(Role.STAFF))
        keys = [t.key for t in m.tiles]
        self.assertNotIn("sales", keys)
        self.assertIn("shift_bills", keys)
        tiles = {t.key: t for t in m.tiles}
        self.assertEqual(tiles["shift_bills"].value, str(x.bills))
        self.assertEqual(tiles["shift_cash"].value, format_inr(x.by_mode["CASH"]))
        self.assertEqual(m.sparkline, [])
        self.assertEqual(m.reorder, [])
        self.assertIsNone(m.setup_card)
        self.assertEqual([r.name for r in m.low_stock], ["Camphor 50g"])
        self.assertEqual(m.logged_in, [])
        self.assertIn("Shift #", m.shift_text)

    def test_reorder_list_comes_from_the_forecast_plan(self) -> None:
        self._set_min_stock("500")
        m = dashboard.build_dashboard(self.c, self.state, EN, _client(Role.OWNER))
        self.assertEqual([r.name for r in m.reorder], ["Camphor 50g"])
        self.assertEqual(m.reorder[0].qty_text, "404", "min 500 - stock 96; no demand history yet, so the gap to the minimum is the whole suggestion")
        self.assertEqual(m.labels["reorder"], EN.t("dashboard.reorder"))


class NotificationsPageTest(Base):
    def test_list_filters_by_role_marks_read_and_mark_all(self) -> None:
        a = self.c.notifications.notify("WARN", "SCANNER_MISSING", "Scanner gone")
        b = self.c.notifications.notify("INFO", "LOW_STOCK", "Low: Camphor", source_ref="product:10")
        self.c.notifications.notify("INFO", "BACKUP_QUEUED", "Backup queued", user_id=2)
        staff = notifications_page.build_notifications(self.c, EN, user_id=2, role=Role.STAFF)
        self.assertEqual([i.kind for i in staff.items], ["BACKUP_QUEUED", "SCANNER_MISSING"], "newest first; LOW_STOCK is not broadcast to staff")
        self.assertEqual(staff.unread_count, 2)
        owner = notifications_page.build_notifications(self.c, EN, user_id=1, role=Role.OWNER)
        self.assertEqual(owner.unread_count, 2, "a notice addressed to one staff member is not the owner's")
        self.assertTrue(all(not i.read for i in owner.items))
        self.assertTrue(notifications_page.mark_read(self.c, user_id=1, notification_id=a))  # type: ignore[arg-type]
        owner = notifications_page.build_notifications(self.c, EN, user_id=1, role=Role.OWNER)
        self.assertEqual(owner.unread_count, 1)
        self.assertEqual({i.id: i.read for i in owner.items}[a], True, "read rows stay in the history list")  # type: ignore[index]
        self.assertEqual(notifications_page.mark_all_read(self.c, user_id=1, role=Role.OWNER), 1)
        self.assertEqual(notifications_page.build_notifications(self.c, EN, user_id=1, role=Role.OWNER).unread_count, 0)
        self.assertEqual(notifications_page.build_notifications(self.c, HI, user_id=1, role=Role.OWNER).labels["title"], HI.t("notifications.title"))
        self.assertIsNotNone(b)


if __name__ == "__main__":
    unittest.main()
