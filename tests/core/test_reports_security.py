"""Unit 43 — anti-theft reports (docs/05 §9), the integrity banner, Settings → Users / Payments tabs."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.billing import PaymentInput
from vaishnavi.ui import layout
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import reports as reports_page
from vaishnavi.ui.pages import settings_payments, settings_shop, settings_users
from vaishnavi.ui.state import ClientState, ClientUser

from tests.core.test_billing import CAMPHOR, OWNER, STAFF, STAFF_PIN
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
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})  # no paired scanner in this fixture
        self.c.shifts.open(OWNER, opening_float_paise=0)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, actor: Actor = STAFF, *, qty: str = "2", discount_pct: str | None = None, open_item: bool = False, lookup: bool = False):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        line = self.c.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        if discount_pct:
            self.c.billing.set_line_discount(actor, cart, line.line_id, percent=Decimal(discount_pct), reason="regular customer")
        if lookup:
            self.c.billing.add_lookup(actor, cart, 10, qty=Decimal("1"), reason="label damaged")
        if open_item:
            self.c.billing.add_open_item(actor, cart, name="Garland", detail="", qty=Decimal("1"), unit_price_paise=2000)
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput("CASH", total, tendered_paise=total)])


class ReportsTest(Base):
    def test_discount_manual_void_and_override_reports(self) -> None:
        self._sale(STAFF, discount_pct="5")
        self._sale(STAFF, lookup=True, open_item=True)
        sale = self._sale(STAFF)
        self.c.billing.void(STAFF_PIN, sale.sale_id, reason="wrong item", approver_id=1)
        self.c.hardware.start_override(OWNER, "PRINTER", minutes=10, reason="no paper")
        r = self.c.reports
        with self.assertRaises(PermissionDenied):
            r.discount_audit(STAFF)
        disc = r.discount_audit(OWNER)
        self.assertEqual([(d.user_id, d.lines, d.adhoc_paise) for d in disc], [(2, 1, 100)], "one ad-hoc line, 5 % of ₹20")
        self.assertEqual(disc[0].reasons, {"regular customer": 1})
        manual = r.manual_lines(OWNER)
        self.assertEqual([(m.user_id, m.lookup_lines, m.open_item_lines, m.open_item_paise) for m in manual], [(2, 1, 1, 2000)])
        self.assertAlmostEqual(manual[0].manual_ratio, 2 / 7, places=3, msg="2 manual lines out of 7 completed lines (2 fixture + 2 + 3)")
        voids = r.voids(OWNER)
        self.assertEqual([(v.original_invoice, v.status, v.user_id, v.approved_by, v.total_paise) for v in voids], [("VPS/26-27/000005", "VOIDED", 2, 1, 2000)])
        self.assertIn("wrong item", voids[0].reason)
        overrides = r.overrides(OWNER)
        self.assertEqual([o.action for o in overrides], ["HARDWARE_OVERRIDE", "SALE_VOIDED"], "newest first: every PIN / owner override in one list")
        self.assertEqual((overrides[1].user_id, overrides[1].actor_user_id), (2, 1))

    def test_shrinkage_cashier_performance_tax_summary_and_csv(self) -> None:
        self.c.inventory.record_damage(OWNER, 10, Decimal("3"), reason="water damage")
        self.c.inventory.adjust(OWNER, 10, Decimal("-2"), reason="count correction")
        self.c.inventory.adjust(OWNER, 10, Decimal("+5"), reason="found in store room")
        shrink = self.c.reports.shrinkage(OWNER)
        self.assertEqual([(s.product_id, str(s.damage_qty), str(s.adjustment_qty), s.value_paise) for s in shrink], [(10, "3", "2", 5000)], "5 units gone at ₹10 shelf price")
        self._sale(STAFF, discount_pct="5")
        self._sale(OWNER)
        self.c.shifts.close(STAFF, 1, counted_cash_paise=self.c.shifts.expected_cash(1) - 100, note="short")
        perf = self.c.reports.cashier_performance(OWNER)
        by_user = {p.user_id: p for p in perf}
        self.assertEqual((by_user[2].bills, by_user[2].sales_paise, by_user[2].discount_paise, by_user[2].variance_paise), (3, 5900, 100, -100))
        self.assertEqual(by_user[2].avg_bill_paise, 1966)
        self.assertEqual((by_user[1].bills, by_user[1].sales_paise), (1, 2000))
        self.c.settings_service.set_many(OWNER, {"tax.registered": True, "shop.gstin": "09ABCDE1234F1Z5"})
        self.c.catalog.update(OWNER, 10, row_version=self.c.catalog.get(10).row_version, changes={"tax_rate_bp": 500})  # type: ignore[union-attr]
        self._sale(OWNER)
        tax = self.c.reports.tax_summary(OWNER)
        self.assertEqual([t.rate_bp for t in tax], [0, 500])
        five = next(t for t in tax if t.rate_bp == 500)
        self.assertEqual((five.taxable_paise, five.cgst_paise + five.sgst_paise), (1905, 95))
        csv = self.c.reports.to_csv(tax)
        self.assertTrue(csv.startswith("\ufeffrate_bp,"), "UTF-8 BOM for Excel + header from the dataclass")
        self.assertEqual(len(csv.strip().splitlines()), 3)

    def test_integrity_and_audit_trail(self) -> None:
        status = self.c.reports.integrity(OWNER)
        self.assertTrue(status.ok)
        self.assertEqual(status.chain_head, self._q("SELECT hash FROM sale ORDER BY id DESC LIMIT 1")[0][0])
        trail = self.c.reports.audit_trail(OWNER, limit=5)
        self.assertTrue(trail)
        self.assertTrue(all(e.action for e in trail))
        only = self.c.reports.audit_trail(OWNER, action="SHIFT_OPENED")
        self.assertEqual([e.action for e in only], ["SHIFT_OPENED"])
        with self.assertRaises(PermissionDenied):
            self.c.reports.audit_trail(STAFF)


class BannerAndPagesTest(Base):
    def _client(self, role: Role) -> ClientState:
        client = ClientState.anonymous("en")
        client.sign_in(ClientUser(user_id=1 if role is Role.OWNER else 2, username="x", display_name="X", role=role), work_session_id=None)
        return client

    def test_integrity_banner_only_for_owner_when_chain_is_red(self) -> None:
        self.assertIsNone(layout.integrity_banner(self.state, self._client(Role.OWNER), EN))
        self.state.chips["ledger"] = ("red", "Integrity: E-LEDGER-001")
        banner = layout.integrity_banner(self.state, self._client(Role.OWNER), EN)
        assert banner is not None
        self.assertIn("E-LEDGER-001", banner.text)
        self.assertEqual(banner.link, "/reports/integrity")
        self.assertIsNone(layout.integrity_banner(self.state, self._client(Role.STAFF), EN), "staff never see integrity detail")

    def test_reports_page_models(self) -> None:
        self._sale(STAFF, discount_pct="5")
        index = reports_page.build_index(self.c, EN, OWNER)
        keys = [r.key for r in index.reports]
        for k in ("discounts", "manual_lines", "voids", "overrides", "shrinkage", "cashiers", "tax", "variance", "attendance", "integrity", "audit"):
            self.assertIn(k, keys)
        staff_index = reports_page.build_index(self.c, EN, STAFF)
        self.assertEqual([r.key for r in staff_index.reports], ["low_stock", "attendance"], "staff see low stock (shelf duty) and their own work history only")
        self.assertNotIn("forecast", [r.key for r in staff_index.reports], "reorder plan / demand classes are owner-only")
        self.assertIn("forecast", keys)
        self.assertIn("demand", keys)
        table = reports_page.build_table(self.c, EN, OWNER, "discounts")
        self.assertEqual(table.columns[0], "cashier")
        self.assertEqual(table.rows[0]["cashier"], "Ramesh")
        self.assertEqual(table.rows[0]["adhoc"], "₹1.00")
        integ = reports_page.build_table(self.c, EN, OWNER, "integrity")
        self.assertTrue(integ.rows and integ.rows[0]["check"] == "CHAIN")
        self.assertTrue(reports_page.export_csv(self.c, OWNER, "discounts").startswith("\ufeff"))
        self.assertIsNone(reports_page.build_table(self.c, EN, STAFF, "discounts"))

    def test_settings_users_tab_models_and_actions(self) -> None:
        m = settings_users.build_users_model(self.state, EN)
        self.assertEqual([(u.username, u.role) for u in m.users], [("owner", "OWNER"), ("ramesh", "STAFF")])
        created = settings_users.create_staff(self.state, OWNER, username="sita", display_name="Sita", password="sita#2026")
        self.assertTrue(created.ok, created.message)
        m = settings_users.build_users_model(self.state, EN)
        self.assertEqual([u.username for u in m.users], ["owner", "ramesh", "sita"])
        self.assertTrue(next(u for u in m.users if u.username == "sita").must_change_password)
        self.assertFalse(settings_users.create_staff(self.state, OWNER, username="sita", display_name="Dup", password="sita#2026").ok)
        self.assertFalse(settings_users.create_staff(self.state, STAFF, username="x", display_name="X", password="sita#2026").ok)
        sita = next(u for u in m.users if u.username == "sita")
        self.assertTrue(settings_users.set_active(self.state, OWNER, sita.id, active=False).ok)
        self.assertFalse(next(u for u in settings_users.build_users_model(self.state, EN).users if u.id == sita.id).active)
        self.assertTrue(settings_users.reset_password(self.state, OWNER, sita.id, new_password="temp#9087").ok)
        self.assertEqual(settings_users.set_owner_pin(self.state, OWNER, password="wrong", new_pin="2468").ok, False)

    def test_settings_payments_tab_masks_and_requires_reauth_to_reveal(self) -> None:
        self.c.users.complete_owner_setup("Owner@123", "7391")
        owner = Actor(user_id=1, role=Role.OWNER)
        saved = settings_payments.save_payments(self.state, owner, {"upi_id": "shop@upi", "payee_name": "Vaishnavi Puja Store", "account_no": "001122334455", "ifsc": "SBIN0001234", "bank_name": "SBI"})
        self.assertTrue(saved.ok, saved.message)
        m = settings_payments.build_payments_model(self.state, EN)
        previews = {f.key: f.preview for f in m.fields}
        self.assertEqual(previews["account_no"], "•••• 4455")
        self.assertNotIn("001122334455", str(m))
        self.assertTrue(m.qr_enabled, "saving a UPI id switches the receipt QR on")
        self.assertEqual(settings_payments.reveal(self.state, owner, "account_no", password="nope").value, None)
        shown = settings_payments.reveal(self.state, owner, "account_no", password="Owner@123")
        self.assertEqual(shown.value, "001122334455")
        self.assertFalse(settings_payments.save_payments(self.state, STAFF, {"upi_id": "x@y"}).ok)
        self.assertTrue(settings_payments.clear_field(self.state, owner, "account_no").ok)
        self.assertEqual({f.key: f.preview for f in settings_payments.build_payments_model(self.state, EN).fields}["account_no"], "")

    def test_settings_shop_tab_saves_identity_and_gst_rules_with_validation(self) -> None:
        owner = Actor(user_id=1, role=Role.OWNER)
        base = {"name": "Vaishnavi Puja Store", "address_line1": "12 Mandir Road", "address_line2": "", "phone": "98765 43210", "email": "", "state_code": "09"}
        bad = settings_shop.save_shop(self.state, owner, {**base, "registered": True, "gstin": "short", "default_rate": "5", "prices_include_tax": True})
        self.assertFalse(bad.ok)
        self.assertIn("gstin", bad.problems)
        missing = settings_shop.save_shop(self.state, owner, {**base, "registered": True, "gstin": "", "default_rate": "5", "prices_include_tax": True})
        self.assertEqual((missing.ok, missing.error_code), (False, "E-CFG-003"), "registered without a GSTIN is refused")
        ok = settings_shop.save_shop(self.state, owner, {**base, "registered": True, "gstin": "09abcde1234f1z5", "default_rate": "5", "prices_include_tax": True})
        self.assertTrue(ok.ok, ok.message)
        m = settings_shop.build_shop_model(self.state, EN)
        self.assertEqual((m.gstin, m.registered, m.default_rate, m.prices_include_tax), ("09ABCDE1234F1Z5", True, "5", True))
        self.assertEqual(m.receipt_preview, ["Vaishnavi Puja Store", "12 Mandir Road", "Ph: 98765 43210", "GSTIN: 09ABCDE1234F1Z5"])
        self.assertFalse(m.whatsapp_share, "WhatsApp bills are optional and off by default")
        on = settings_shop.save_shop(self.state, owner, {**base, "registered": True, "gstin": "09abcde1234f1z5", "default_rate": "5", "prices_include_tax": True, "whatsapp_share": True})
        self.assertTrue(on.ok, on.message)
        self.assertTrue(settings_shop.build_shop_model(self.state, EN).whatsapp_share)
        self.assertTrue(self.c.settings_service.effective().receipt.whatsapp_share)
        self.assertFalse(settings_shop.save_shop(self.state, STAFF, {**base, "registered": False, "gstin": "", "default_rate": "0"}).ok, "staff cannot change shop settings")


if __name__ == "__main__":
    unittest.main()
