"""Unit 40 — POS page view-models: cart table with Discount column, scan box, F3 Find/Open item, quantity pad, pay dialog (docs/09 §3, docs/05 §8–10)."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.barcode import internal_ean13, receipt_ean13
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.settings import Settings
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteHardwareStore, SqliteInventoryStore, SqlitePrintQueueStore, SqliteTxn
from vaishnavi.hardware import receipts as renderers
from vaishnavi.platform.paths import AppPaths
from vaishnavi.platform.printing import PrinterStatus
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.hardware_guard import HardwareGuard
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferInput, OfferService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.receipt import ReceiptService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import pos

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)
CAMPHOR = internal_ean13(10)


class _Shop:
    """Just enough of AppServices for the POS view-models."""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        txn = SqliteTxn(self.paths.database)
        audit = AuditService(SqliteAuditStore())
        self.settings = Settings()
        self.settings.hardware.scanner_vid_pid = "0C2E:0B61"
        catalog_store = SqliteCatalogStore()
        c = _Shop()
        c.settings_service = type("S", (), {"effective": staticmethod(lambda: self.settings)})()
        c.catalog = CatalogService(txn, catalog_store, audit, event_bus=self.bus)
        c.inventory = InventoryService(txn, SqliteInventoryStore(), catalog_store, audit, event_bus=self.bus)
        ledger = LedgerService(SqliteLedgerStore())
        billing_store = SqliteBillingStore()
        c.offers = OfferService(txn, billing_store, catalog_store, audit)
        c.billing = BillingService(txn, billing_store, catalog_store, SqliteInventoryStore(), c.catalog, c.offers, ledger, audit, settings=lambda: self.settings, event_bus=self.bus)
        c.receipts = ReceiptService(txn, billing_store, self.paths, settings=lambda: self.settings, cashier_name=lambda uid: "Ramesh", upi_id=lambda: None, renderer=renderers)
        self.printer = PrinterStatus("none", "", False, "not set up — bills are saved as PDF")
        c.print_queue = PrintQueueService(txn, SqlitePrintQueueStore(), c.receipts, audit, settings=lambda: self.settings, status=lambda hw: self.printer, transport_factory=lambda hw: None, event_bus=self.bus)
        c.print_queue.subscribe(self.bus)
        self.scanner_present = True
        c.hardware = HardwareGuard(txn, SqliteHardwareStore(), audit, settings=lambda: self.settings, scanner_present=lambda hw: self.scanner_present, printer_status=lambda hw: self.printer, event_bus=self.bus)
        c.billing.add_checkout_hook(c.hardware.can_checkout)
        c.users = type("U", (), {"display_names": staticmethod(lambda: {1: "Owner", 2: "Ramesh"})})()
        self.c = c
        self.texts = UiTexts.load("en")
        self.carts = pos.CartRegistry()

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()


class ModelTest(Base):
    def test_cart_registry_keeps_one_bill_per_cashier(self) -> None:
        a = self.carts.get(self.c.billing, STAFF)
        self.assertIs(self.carts.get(self.c.billing, STAFF), a)
        b = self.carts.get(self.c.billing, OWNER)
        self.assertNotEqual(a.client_txn_id, b.client_txn_id)
        self.carts.reset(STAFF)
        self.assertIsNot(self.carts.get(self.c.billing, STAFF), a)

    def test_pos_model_rows_totals_chips_and_tiles(self) -> None:
        self.c.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        self.c.catalog.set_quick_pick(OWNER, 10, True)
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("2"))
        self.c.billing.add_lookup(STAFF, cart, 10, qty=Decimal("1"), reason="label damaged")
        self.c.billing.set_line_discount(STAFF, cart, cart.lines[1].line_id, amount_paise=100, reason="regular customer")
        self.c.billing.add_open_item(STAFF, cart, name="Garland", detail="", qty=Decimal("1"), unit_price_paise=3500)
        m = pos.build_pos(self.c, self.texts, STAFF, cart)
        self.assertEqual([r.marker for r in m.rows], ["", "†", "*"])
        self.assertEqual((m.rows[0].name, m.rows[0].sub, m.rows[0].qty_text, m.rows[0].shelf_text), ("Camphor 50g", "#10 · CAM-0001", "2 pcs", "10.00"))
        self.assertEqual((m.rows[0].discount_text, m.rows[0].discount_kind, m.rows[0].amount_text), ("Diwali Offer −1%", "OFFER", "19.80"))
        self.assertEqual((m.rows[1].discount_text, m.rows[1].discount_kind), ("−₹1.00 · regular customer", "ADHOC"), "ad-hoc beats the 1 % offer, reason shown")
        self.assertIn("found by name", m.rows[1].sub)
        self.assertEqual(m.rows[2].discount_text, "—")
        labels = [r.label for r in m.total_rows]
        self.assertEqual(labels[:2], ["Subtotal (shelf price)", "Item discounts"])
        self.assertEqual(m.total_text, "₹64.00", "20 + 10 + 35 − 0.20 − 1.00 = 63.80 → 64")
        self.assertEqual(m.saved_text, "₹1.20")
        self.assertEqual(m.pay_label, "F9 Pay ₹64")
        self.assertEqual(m.line_count, 3)
        self.assertEqual(m.next_invoice, "VPS/26-27/000003")
        self.assertEqual((m.scanner_chip.tone, m.printer_chip.tone), ("green", "grey"))
        self.assertEqual(m.scan_hint, self.texts.t("pos.scan_strict"))
        self.assertFalse(m.tiles_enabled, "staff tiles are locked in strict mode")
        self.assertEqual([t.product_id for t in m.tiles], [10])
        self.assertEqual(m.tiles[0].stock_text, "96 left")
        owner = pos.build_pos(self.c, self.texts, OWNER, self.carts.get(self.c.billing, OWNER))
        self.assertTrue(owner.tiles_enabled)
        self.assertEqual(owner.scan_hint, self.texts.t("pos.scan_free"))

    def test_banners_follow_hardware_state(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        m = pos.build_pos(self.c, self.texts, STAFF, cart)
        self.assertEqual([b.kind for b in m.banners], [], "no printer configured and nothing queued → quiet")
        self.c.billing.add_scan(STAFF, cart, CAMPHOR)
        self.c.billing.checkout(STAFF, cart, [pos.cash_payment(1000, tendered_paise=1000)])
        self.carts.reset(STAFF)
        m = pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF))
        self.assertEqual([b.kind for b in m.banners], ["printer"])
        self.assertIn("1", m.banners[0].text)
        self.assertEqual(m.printer_chip.tone, "amber")
        self.scanner_present = False
        self.c.hardware.refresh()
        m = pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF))
        self.assertEqual([b.kind for b in m.banners], ["scanner", "printer"])
        self.assertEqual(m.scanner_chip.tone, "red")
        self.assertTrue(m.billing_paused)


class ScanTest(Base):
    def test_classify_entry_by_typing_speed(self) -> None:
        self.assertTrue(pos.classify_entry("2900000000100", elapsed_ms=120), "13 chars in 120 ms is a scanner")
        self.assertFalse(pos.classify_entry("2900000000100", elapsed_ms=3000), "a human typed that")
        self.assertTrue(pos.classify_entry("2900000000100", elapsed_ms=None), "no timing info (e.g. paste event from a wedge) → trust the scanner suffix")

    def test_scan_input_outcomes(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        added = pos.handle_scan_input(self.c, STAFF, cart, CAMPHOR, elapsed_ms=100)
        self.assertEqual((added.kind, added.line.qty), ("ADDED", Decimal("1")))  # type: ignore[union-attr]
        typed = pos.handle_scan_input(self.c, STAFF, cart, CAMPHOR, elapsed_ms=4000)
        self.assertEqual(typed.kind, "REJECTED")
        self.assertIn("F3", typed.message)
        unknown = pos.handle_scan_input(self.c, STAFF, cart, "8901234567890", elapsed_ms=100)
        self.assertEqual(unknown.kind, "UNKNOWN")
        self.assertIn("Ask Owner", unknown.message)
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee", price_paise=6000, sale_mode="WEIGHT", base_unit="g", unit="g", pricing_unit_qty=Decimal("100"), pricing_unit_label="100 g", min_qty=Decimal("50"), qty_step=Decimal("10"), qty_presets="[50, 100, 250]", allow_decimal_qty=True))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("5000"))
        needs = pos.handle_scan_input(self.c, STAFF, cart, internal_ean13(ghee.id), elapsed_ms=90)
        self.assertEqual(needs.kind, "NEEDS_QTY")
        pad = pos.build_qty_pad(needs.scan, self.texts)  # type: ignore[arg-type]
        self.assertEqual(pad.presets, [("50 g", Decimal("50")), ("100 g", Decimal("100")), ("250 g", Decimal("250"))])
        self.assertEqual(pad.unit, "g")
        self.assertEqual(pad.price_hint, "₹60.00 / 100 g")
        self.assertEqual(pos.qty_from_pad(needs.scan, qty_text="125"), Decimal("130"), "typed 125 g snaps to the 10 g step")  # type: ignore[arg-type]
        self.assertEqual(pos.qty_from_pad(needs.scan, amount_text="50"), Decimal("80"), "₹50 by amount")  # type: ignore[arg-type]
        self.assertEqual(pos.pad_amount_text(needs.scan, Decimal("130")), "₹78.00")  # type: ignore[arg-type]
        line = pos.add_measured(self.c, STAFF, cart, needs.scan, Decimal("130"), source="TYPED")  # type: ignore[arg-type]
        self.assertEqual((line.qty, line.unit), (Decimal("130"), "g"))
        self.c.billing.checkout(STAFF, cart, [pos.cash_payment(10000, tendered_paise=10000)])
        receipt = pos.handle_scan_input(self.c, STAFF, self.carts.get(self.c.billing, STAFF), receipt_ean13("26-27", 3), elapsed_ms=100)
        self.assertEqual((receipt.kind, receipt.sale.invoice_no), ("RECEIPT", "VPS/26-27/000003"))  # type: ignore[union-attr]
        self.assertTrue(receipt.message)

    def test_scanner_absent_pauses_staff_not_owner(self) -> None:
        self.scanner_present = False
        self.c.hardware.refresh()
        paused = pos.handle_scan_input(self.c, STAFF, self.carts.get(self.c.billing, STAFF), CAMPHOR, elapsed_ms=100)
        self.assertEqual(paused.kind, "PAUSED")
        self.assertIn("scanner", paused.message.lower())
        ok = pos.handle_scan_input(self.c, OWNER, self.carts.get(self.c.billing, OWNER), CAMPHOR, elapsed_ms=5000)
        self.assertEqual(ok.kind, "ADDED")


class FindAndPayTest(Base):
    def test_find_model_and_actions(self) -> None:
        m = pos.build_find(self.c, self.texts, STAFF, "camph")
        self.assertEqual([h.product_id for h in m.hits], [10])
        hit = m.hits[0]
        self.assertEqual((hit.name, hit.price_text, hit.stock_text, hit.barcode), ("Camphor 50g", "₹10.00", "96 pcs", CAMPHOR))
        self.assertEqual(m.reasons[0][0], "label damaged")
        self.assertEqual(m.open_item_policy, "ALLOW")
        self.assertTrue(m.can_open_item)
        cart = self.carts.get(self.c.billing, STAFF)
        line = pos.find_add(self.c, STAFF, cart, 10, qty_text="2", reason="label missing")
        self.assertEqual((line.entry_method, line.entry_reason, line.qty), ("LOOKUP", "label missing", Decimal("2")))
        result = pos.open_item_add(self.c, STAFF, cart, name="Marigold", detail="loose", qty_text="1", price_text="20", discount_text="", reason="")
        self.assertEqual((result.ok, result.line.is_open_item), (True, True))  # type: ignore[union-attr]
        low = pos.open_item_add(self.c, STAFF, cart, name="Camphor", detail="", qty_text="1", price_text="3", discount_text="", reason="")
        self.assertEqual((low.ok, low.error_code, low.needs_confirmation), (False, "E-BILL-072", True))
        confirmed = pos.open_item_add(self.c, STAFF, cart, name="Camphor", detail="", qty_text="1", price_text="3", discount_text="", reason="", confirm_low_price=True)
        self.assertTrue(confirmed.ok)
        self.settings.billing.manual_add_policy = "BLOCK"
        self.assertFalse(pos.build_find(self.c, self.texts, STAFF, "").can_open_item)
        self.assertTrue(pos.build_find(self.c, self.texts, OWNER, "").can_open_item)

    def test_discount_dialog_and_outcomes(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("10"))
        d = pos.build_discount(self.c, self.texts, STAFF, cart, cart.lines[0].line_id)
        self.assertEqual(d.cap_text, "10 %")
        self.assertIn("regular customer", [r[0] for r in d.reasons])
        ok = pos.apply_discount(self.c, STAFF, cart, cart.lines[0].line_id, percent_text="5", amount_text="", reason="regular customer")
        self.assertTrue(ok.ok)
        over = pos.apply_discount(self.c, STAFF, cart, cart.lines[0].line_id, percent_text="20", amount_text="", reason="bargaining")
        self.assertEqual((over.ok, over.error_code, over.needs_pin), (False, "E-BILL-060", True))
        bill = pos.apply_discount(self.c, STAFF, cart, None, percent_text="", amount_text="5", reason="round figure")
        self.assertTrue(bill.ok)
        self.assertEqual(self.c.billing.totals(cart).bill_discount_paise, 500)

    def test_payment_model_and_checkout(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("3"))
        self.c.billing.add_open_item(STAFF, cart, name="Garland", detail="", qty=Decimal("1"), unit_price_paise=2900)
        p = pos.build_payment(self.c, self.texts, cart)
        self.assertEqual(p.total_paise, 5900)
        self.assertEqual(p.quick_cash, [5900, 6000, 10000, 50000], "exact, then the next notes that cover the bill")
        self.assertEqual(pos.change_text(p, tendered_paise=10000), "₹41.00")
        payments = pos.make_payments(p, mode="SPLIT", cash_paise=2000, upi_paise=3900, tendered_paise=2000)
        self.assertEqual([(x.mode, x.amount_paise) for x in payments], [("CASH", 2000), ("UPI", 3900)])
        short = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(p, mode="CASH", tendered_paise=5000))
        self.assertEqual((short.ok, short.error_code), (False, "E-BILL-050"))
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(p, mode="CASH", tendered_paise=10000))
        self.assertTrue(done.ok)
        self.assertEqual((done.result.invoice_no, done.result.change_paise), ("VPS/26-27/000003", 4100))  # type: ignore[union-attr]
        self.assertIn("VPS/26-27/000003", done.receipt_preview)
        self.assertEqual(done.print_status, "queued", "no printer → archived and queued")
        self.assertEqual(pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF)).line_count, 0, "a new bill starts after checkout")

    def test_hold_and_resume_models(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR)
        held_id = pos.hold_current(self.c, STAFF, self.carts, label="Sharma ji")
        self.assertEqual(self.carts.get(self.c.billing, STAFF).lines, [], "the counter is free for the next customer")
        m = pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF))
        self.assertEqual(m.held_count, 1)
        held = pos.held_list(self.c, self.texts, STAFF)
        self.assertEqual([(h.id, h.label, h.lines) for h in held], [(held_id, "Sharma ji", 1)])
        back = pos.resume_held(self.c, STAFF, self.carts, held_id)
        self.assertEqual(len(back.lines), 1)
        self.assertIs(self.carts.get(self.c.billing, STAFF), back)


if __name__ == "__main__":
    unittest.main()
