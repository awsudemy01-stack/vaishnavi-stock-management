"""Unit 44 — Owner-PIN dialog component, /shifts page models, login → open-shift redirect, and the permission property test
(Phase 4 accept: a STAFF account cannot reach any Owner capability through the services)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import MATRIX, Actor, Grant, Permission, PermissionDenied, PinRequired, Role, require
from vaishnavi.core.security import hash_secret
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.billing import PaymentInput
from vaishnavi.services.catalog import ProductInput
from vaishnavi.services.inventory import PurchaseLine
from vaishnavi.services.offers import OfferInput
from vaishnavi.ui.components import pin_dialog
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import shifts as shifts_page
from vaishnavi.ui.pages.login import sign_in
from vaishnavi.ui.state import ClientState, ClientUser

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")


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
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})
        self.c.users.complete_owner_setup("Owner@123", "7391")
        self.client = ClientState.anonymous("en")

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _staff_password(self) -> None:
        conn = sqlite3.connect(self.paths.database)
        conn.execute("UPDATE user SET password_hash = ?, must_change_password = 0 WHERE id = 2", (hash_secret("ramesh#2026"),))
        conn.commit()
        conn.close()


class PinDialogTest(Base):
    def test_pin_verification_returns_pin_actor_and_owner_id(self) -> None:
        self.client.sign_in(ClientUser(user_id=2, username="ramesh", display_name="Ramesh", role=Role.STAFF), work_session_id=None)
        prompt = pin_dialog.build_prompt(EN, Permission.VOID_REFUND)
        self.assertEqual(prompt.title, EN.t("pin.title"))
        self.assertIn("VOID_REFUND", prompt.permission_text)
        bad = pin_dialog.verify(self.c, self.client, "0000", Permission.VOID_REFUND)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error_code, "E-AUTH-002")
        good = pin_dialog.verify(self.c, self.client, "7391", Permission.VOID_REFUND)
        self.assertTrue(good.ok)
        assert good.actor is not None
        self.assertEqual((good.actor.user_id, good.actor.role, good.actor.pin_verified, good.approver_id), (2, Role.STAFF, True, 1))
        self.assertEqual(self._q("SELECT COUNT(*) FROM audit_log WHERE action IN ('OVERRIDE', 'OVERRIDE_FAILED')")[0][0], 2)
        for _ in range(3):
            pin_dialog.verify(self.c, self.client, "1111", Permission.VOID_REFUND)
        locked = pin_dialog.verify(self.c, self.client, "7391", Permission.VOID_REFUND)
        self.assertEqual(locked.error_code, "E-AUTH-030", "three wrong PINs lock the override for a while")


class ShiftsPageTest(Base):
    def test_staff_login_lands_on_open_shift_when_none_is_open(self) -> None:
        self._staff_password()
        self.c.shifts.close(OWNER, 1, counted_cash_paise=4000, note="")
        result = sign_in(self.c.users, self.c.sessions, self.client, username="ramesh", password="ramesh#2026", client_label="test")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.next_path, "/shifts?then_pos=1", "no open shift → open one first (docs/05 §6)")
        self.c.shifts.open(STAFF, opening_float_paise=50000)
        self.client = ClientState.anonymous("en")
        again = sign_in(self.c.users, self.c.sessions, self.client, username="ramesh", password="ramesh#2026", client_label="test")
        self.assertEqual(again.next_path, "/pos")
        self.assertEqual(self.client.shift_id, self.c.shifts.current(2).id)  # type: ignore[union-attr]

    def test_shift_page_models_open_cash_x_close_z(self) -> None:
        self.c.shifts.close(OWNER, 1, counted_cash_paise=4000, note="")
        m = shifts_page.build_shift_model(self.c, EN, STAFF)
        self.assertIsNone(m.current)
        self.assertTrue(m.can_open)
        self.assertEqual(list(m.denominations), [500, 200, 100, 50, 20, 10, 5, 2, 1], "₹2000 was withdrawn in 2023")
        blind = shifts_page.open_shift(self.c, STAFF, float_text="500", denominations={"500": "", "100": ""})
        self.assertFalse(blind.ok, "staff must count the drawer note by note — no typed total")
        self.assertEqual(shifts_page.open_preview_text({"500": "1", "100": "", "20": "x"}), "₹0.00", "invalid grid → ₹0 preview, never a crash")
        self.assertEqual(shifts_page.open_preview_text({"200": "2", "100": "1"}), "₹500.00")
        opened = shifts_page.open_shift(self.c, STAFF, denominations={"200": "2", "100": "1", "50": "0"})
        self.assertTrue(opened.ok, opened.message)
        self.assertEqual(json.loads(self.c.shifts.current(2).opening_denominations), {"200": 2, "100": 1}, "zero rows dropped, count kept for the Owner")  # type: ignore[union-attr]
        m = shifts_page.build_shift_model(self.c, EN, STAFF)
        assert m.current is not None
        self.assertEqual((m.current.opening_float_text, m.current.bills, m.current.cash_sales_text), ("₹500.00", 0, "₹0.00"))
        self.assertFalse(m.show_expected, "staff count blind — expected cash appears after the count")
        self.assertTrue(shifts_page.build_shift_model(self.c, EN, OWNER, shift_id=m.current.id).show_expected)
        cart = self.c.billing.new_cart(STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("2"))
        self.c.billing.checkout(STAFF, cart, [PaymentInput("CASH", 2000, tendered_paise=5000)])
        self.assertTrue(shifts_page.cash_in(self.c, STAFF, amount_text="1", reason="change").ok)
        pinless = shifts_page.cash_out(self.c, STAFF, amount_text="0.50", reason="tea")
        self.assertEqual((pinless.ok, pinless.needs_pin), (False, True))
        pin_actor = Actor(user_id=2, role=Role.STAFF, pin_verified=True)
        self.assertTrue(shifts_page.cash_out(self.c, pin_actor, amount_text="0.50", reason="tea", approver_id=1).ok)
        x = shifts_page.x_report_lines(self.c, STAFF)
        self.assertTrue(any("X REPORT" in ln for ln in x))
        self.assertTrue(any("520.50" in ln for ln in x), "500 float + 20 sale + 1 in − 0.50 out")
        preview = shifts_page.close_preview(self.c, STAFF, {"500": "1", "20": "1", "1": "0"})
        self.assertEqual(preview.counted_text, "₹520.00")
        self.assertEqual(preview.variance_text, "\u2212₹0.50")
        self.assertTrue(preview.needs_note)
        blocked = shifts_page.close_shift(self.c, STAFF, {"500": "1", "20": "1"}, note="")
        self.assertFalse(blocked.ok)
        done = shifts_page.close_shift(self.c, STAFF, {"500": "1", "20": "1"}, note="50p short, coin tray")
        self.assertTrue(done.ok, done.message)
        self.assertEqual(done.variance_paise, -50)
        self.assertTrue(any("Z REPORT" in ln for ln in done.report_lines))
        self.assertIsNone(shifts_page.build_shift_model(self.c, EN, STAFF).current)
        hist = shifts_page.build_shift_model(self.c, EN, OWNER).history
        self.assertEqual([(h.cashier, h.variance_text) for h in hist][:2], [("Ramesh", "\u2212₹0.50"), ("Ramesh", "₹0.00")])


class PermissionPropertyTest(Base):
    """Every Owner-only capability refuses a STAFF actor at the service layer — not just in the UI."""

    def test_matrix_staff_never_gets_allow_on_owner_only_permissions(self) -> None:
        owner_only = {p for p, grants in MATRIX.items() if grants[Role.STAFF] is Grant.DENY}
        self.assertTrue({Permission.EDIT_PRICES, Permission.MANAGE_OFFERS, Permission.MANAGE_PRODUCTS, Permission.STOCK_MOVEMENTS, Permission.MANAGE_SYSTEM, Permission.REPORT_SALES, Permission.REPORT_AUDIT, Permission.AI_BUSINESS} <= owner_only)
        for p in owner_only:
            with self.assertRaises(PermissionDenied, msg=p.value):
                require(STAFF, p)

    def test_every_owner_service_call_refuses_staff(self) -> None:
        c = self.c
        calls = {
            "catalog.create": lambda: c.catalog.create(STAFF, ProductInput(name="x", price_paise=100)),
            "catalog.update": lambda: c.catalog.update(STAFF, 10, row_version=1, changes={"price_paise": 1}),
            "catalog.archive": lambda: c.catalog.archive(STAFF, 10),
            "offers.create": lambda: c.offers.create(STAFF, OfferInput(name="x", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10)),
            "inventory.opening": lambda: c.inventory.opening_stock(STAFF, 10, Decimal("1")),
            "inventory.adjust": lambda: c.inventory.adjust(STAFF, 10, Decimal("-1"), reason="x"),
            "inventory.damage": lambda: c.inventory.record_damage(STAFF, 10, Decimal("1"), reason="x"),
            "inventory.purchase": lambda: c.inventory.record_purchase(STAFF, supplier_name="s", invoice_no="1", lines=[PurchaseLine(10, Decimal("1"), 100)]),
            "inventory.stock_take": lambda: c.inventory.start_stock_take(STAFF),
            "users.create": lambda: c.users.create_user(STAFF, "x", "X", "xyz#2026"),
            "users.set_active": lambda: c.users.set_active(STAFF, 1, False),
            "settings.set": lambda: c.settings_service.set_many(STAFF, {"billing.require_scan": False}),
            "secure.set": lambda: c.secure_settings.set(STAFF, "payments.upi_id", "a@b"),
            "secure.reveal": lambda: c.secure_settings.reveal(STAFF, "payments.upi_id"),
            "reports.discounts": lambda: c.reports.discount_audit(STAFF),
            "reports.audit": lambda: c.reports.audit_trail(STAFF),
            "reports.tax": lambda: c.reports.tax_summary(STAFF),
            "review.convert": lambda: c.review.convert(STAFF, [1], name="x", price_paise=1, category_id=None),
            "hardware.end_override": lambda: c.hardware.end_override(STAFF, "PRINTER"),
            "sessions.force_end": lambda: c.sessions.force_end(STAFF, 1, note="x"),
        }
        for name, call in calls.items():
            with self.assertRaises(PermissionDenied, msg=name):
                call()
        for name, call in {"billing.void": lambda: c.billing.void(STAFF, 1, reason="x"), "shifts.cash_out": lambda: c.shifts.cash_out(STAFF, 1, reason="x"), "hardware.override": lambda: c.hardware.start_override(STAFF, "PRINTER", minutes=1, reason="x")}.items():
            with self.assertRaises(PinRequired, msg=name):
                call()

    def test_staff_cannot_move_stock_except_through_a_sale_even_with_raw_sql(self) -> None:
        conn = sqlite3.connect(self.paths.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id, created_at) VALUES (10, 'ADJUSTMENT', 5, 101, 2, '2026-11-15T00:00:00Z')")
            self.assertIn("E-INV-010", str(cm.exception))
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                conn.execute("UPDATE sale SET total_paise = 1 WHERE id = 1")
            self.assertIn("E-LEDGER-001", str(cm.exception))
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                conn.execute("DELETE FROM stock_movement WHERE id = 1")
            self.assertIn("E-INV-012", str(cm.exception))
        finally:
            conn.close()
        cart = self.c.billing.new_cart(STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("1"))
        self.c.billing.checkout(STAFF, cart, [PaymentInput("CASH", 1000, tendered_paise=1000)])
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 95, "a committed sale deducts exactly once")


if __name__ == "__main__":
    unittest.main()
