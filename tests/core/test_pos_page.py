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
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteHardwareStore, SqliteInventoryStore, SqlitePrintQueueStore, SqliteShiftStore, SqliteTxn
from vaishnavi.hardware import receipts as renderers
from vaishnavi.platform.paths import AppPaths
from vaishnavi.platform.printing import PrinterStatus
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, QuantitySpec
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.hardware_guard import HardwareGuard
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferInput, OfferService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.receipt import ReceiptService
from vaishnavi.services.receipt_share import ReceiptShareService
from vaishnavi.services.shift import ShiftService
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
        c.shifts = ShiftService(txn, SqliteShiftStore(), audit, ledger, event_bus=self.bus)
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
        self.posts: list[tuple[str, dict]] = []
        self.secrets: dict[str, str] = {}
        self.post_fails = False

        def fake_post(url: str, *, headers: dict, json_body: dict) -> dict:
            if self.post_fails:
                raise OSError("network down")
            self.posts.append((url, json_body))
            return {"messages": [{"id": "wamid.1"}]}

        c.receipt_share = ReceiptShareService(txn, c.receipts, c.billing, audit, settings=lambda: self.settings, secret=self.secrets.get, post=fake_post, text_renderer=renderers.text_lines)
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

    def test_no_open_shift_shows_a_banner_and_checkout_names_the_code(self) -> None:
        """Live run 3: the cashier met "No shift is open" only at Pay; the screen now says so up front."""
        owner_cart = self.carts.get(self.c.billing, OWNER)
        m = pos.build_pos(self.c, self.texts, OWNER, owner_cart)
        self.assertEqual([b.kind for b in m.banners], ["shift"], "the Owner has no open shift in the fixture")
        self.assertIn("Open your shift", m.banners[0].text)
        self.c.billing.add_scan(OWNER, owner_cart, CAMPHOR)
        o = pos.do_checkout(self.c, OWNER, owner_cart, [pos.cash_payment(1000, tendered_paise=1000)])
        self.assertFalse(o.ok)
        self.assertEqual(o.error_code, "E-BILL-042", "the page uses the code to jump to /shifts?then_pos=1")
        self.assertEqual([b.kind for b in pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF)).banners], [], "staff has shift 1 open")


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


class LiveRun4Test(Base):
    """Observations 3, 4 and 8 of live run 4: weighed items on the pad, the tile/bill layout, UPI."""

    def test_qty_pad_accepts_weights_in_kg_or_grams(self) -> None:
        ghee = QuantitySpec(sale_mode="WEIGHT", base_unit="g", pricing_unit_qty=Decimal("1000"), pricing_unit_label="kg", min_qty=Decimal("50"), qty_step=Decimal("50"), presets=(), allow_decimal=True)
        self.assertEqual(pos.parse_qty_text(ghee, "500"), Decimal("500"), "a bare number on a weighed item means base units (grams)")
        self.assertEqual(pos.parse_qty_text(ghee, "0.5 kg"), Decimal("500"))
        self.assertEqual(pos.parse_qty_text(ghee, "1.5kg"), Decimal("1500"))
        self.assertEqual(pos.parse_qty_text(ghee, "250 g"), Decimal("250"))
        piece = QuantitySpec(sale_mode="PIECE", base_unit="pcs", pricing_unit_qty=Decimal("1"), pricing_unit_label="pc", min_qty=Decimal("1"), qty_step=Decimal("1"), presets=(), allow_decimal=False)
        self.assertEqual(pos.parse_qty_text(piece, "3"), Decimal("3"))
        with self.assertRaises(ValueError):
            pos.parse_qty_text(ghee, "half")

    def test_tiles_show_every_active_item_with_icon_category_and_stock(self) -> None:
        self.c.catalog.create(OWNER, ProductInput(name="Ghee 1kg", sku="GHEE-01", price_paise=100000, cost_paise=80000, unit="kg", category_id=None))
        self.c.catalog.set_quick_pick(OWNER, 10, True)
        m = pos.build_pos(self.c, self.texts, OWNER, self.carts.get(self.c.billing, OWNER))
        self.assertEqual([t.name for t in m.tiles][:2], ["Camphor 50g", "Ghee 1kg"], "quick picks first, then every other active item")
        by_name = {t.name: t for t in m.tiles}
        self.assertEqual(by_name["Camphor 50g"].icon, "\U0001f56f\ufe0f")
        self.assertEqual(by_name["Ghee 1kg"].icon, "\U0001f95b")
        self.assertTrue(by_name["Ghee 1kg"].out, "no opening stock yet")
        self.assertEqual(m.categories[0][0], None, "'All' pill comes first")
        self.assertEqual(pos.icon_for("Something unknown"), "\U0001f6cd\ufe0f")

    def test_bill_discount_tiers_and_totals_row(self) -> None:
        tiers = pos.discount_tiers(self.c)
        self.assertEqual([t.key for t in tiers], ["regular", "premier", "festival", "round"], "defaults when the Owner has not configured tiers")
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("10"))
        self.c.billing.set_bill_discount(STAFF, cart, amount_paise=500, reason="Regular customer")
        with self.assertRaises(Exception, msg="₹20 on ₹100 is 20 % — above the staff cap without the Owner PIN"):
            self.c.billing.set_bill_discount(STAFF, cart, amount_paise=2000, reason="Premier customer")
        m = pos.build_pos(self.c, self.texts, STAFF, cart)
        bill_rows = [r for r in m.total_rows if r.kind == "billdisc"]
        self.assertEqual(len(bill_rows), 1)
        self.assertIn("Regular customer", bill_rows[0].label)
        self.assertEqual(bill_rows[0].amount_text, "−5.00")
        self.assertEqual(m.total_text, "₹95.00")
        self.assertEqual(m.bill_discount_reason, "Regular customer")
        self.assertEqual(m.saved_text, "₹5.00", "the green 'Customer saves' line under TOTAL")
        self.assertEqual(m.saved_rows, [("Saved another, as our Regular customer", "₹5.00")])
        self.assertEqual(bill_rows[0].label, "Extra discount — Regular customer")
        # … and the same story on the printed receipt
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(pos.build_payment(self.c, self.texts, cart), mode="CASH", tendered_paise=10000))
        self.assertTrue(done.ok, done.error_code)
        preview = done.receipt_preview
        self.assertIn("Extra discount - Regular customer", preview)
        self.assertIn("-5.00", preview)
        self.assertIn("You saved Rs5.00 as our Regular customer", preview)
        self.assertIn("Happy shopping!", preview)
        self.assertLess(preview.index("TOTAL"), preview.index("You saved"), "saved line sits right under the total, as on screen")
        self.assertLess(preview.index("Happy shopping"), preview.index("Paid:"))

    def test_f2_opens_the_bill_discount_and_f4_the_line_discount(self) -> None:
        """Live run 5b: the bill discount was only reachable via F4 with no line selected — now it has its own key."""
        calls: list[str] = []

        class Ev:
            def __init__(self, key: str) -> None:
                self.key = key
                self.action = type("A", (), {"keydown": True})()

        handlers = dict(find=lambda: calls.append("find"), discount=lambda: calls.append("line"), hold=lambda: None, held=lambda: None, pay=lambda: None, clear=lambda: None, customer=lambda: None, bill_discount=lambda: calls.append("bill"))
        pos._keys(Ev("F2"), **handlers)
        pos._keys(Ev("F4"), **handlers)
        pos._keys(Ev("F5"), **handlers)
        self.assertEqual(calls, ["bill", "line"])
        m = pos.build_pos(self.c, self.texts, STAFF, self.carts.get(self.c.billing, STAFF))
        self.assertEqual(m.labels["bill_discount_hint"], "whole bill · customer type")
        self.assertIn("F2 bill discount", m.labels["keys"])

    def test_item_and_bill_discounts_together_tell_a_two_line_savings_story(self) -> None:
        self.c.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("10"))  # ₹100 shelf, −1 % offer
        self.c.billing.set_bill_discount(STAFF, cart, amount_paise=900, reason="Premier customer")
        m = pos.build_pos(self.c, self.texts, STAFF, cart)
        self.assertEqual(m.saved_rows, [("Saved on items", "₹1.00"), ("Saved another, as our Premier customer", "₹9.00")])
        self.assertEqual(m.total_text, "₹90.00")
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(pos.build_payment(self.c, self.texts, cart), mode="CASH", tendered_paise=10000))
        self.assertTrue(done.ok, done.error_code)
        lines = done.receipt_preview.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith("TOTAL"))
        self.assertEqual(lines[i + 1 : i + 5], ["You saved Rs1.00 on items", "You saved another Rs9.00 as our Premier customer", "Happy shopping! Aapka din shubh ho - see you", "again soon."])
        # no bill discount → one plain line, no celebration
        cart2 = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart2, CAMPHOR, qty=Decimal("1"))
        plain = pos.do_checkout(self.c, STAFF, cart2, pos.make_payments(pos.build_payment(self.c, self.texts, cart2), mode="CASH", tendered_paise=1000))
        self.assertIn("You saved Rs0.10 today", plain.receipt_preview)
        self.assertNotIn("Happy shopping", plain.receipt_preview)

    def test_tapping_a_weighed_tile_opens_the_pad_instead_of_adding_one_gram(self) -> None:
        """Live run 4: the ghee tile called add_quick_pick(qty=1) → 1 g < 10 g minimum → 'Quantity is below the minimum'."""
        kg = dict(unit="kg", sale_mode="WEIGHT", base_unit="g", pricing_unit_qty=Decimal("1000"), pricing_unit_label="kg", min_qty=Decimal("10"), qty_step=Decimal("10"), allow_decimal_qty=True)
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Desi Ghee", price_paise=50000, **kg))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("10000"))
        scan = pos.scan_for_product(self.c, ghee.id)
        assert scan is not None
        self.assertTrue(scan.needs_qty, "weighed → the POS must open the quantity pad")
        self.assertEqual((scan.spec.base_unit, scan.spec.min_qty), ("g", Decimal("10")))
        self.assertFalse(pos.scan_for_product(self.c, 10).needs_qty, "camphor is counted → straight into the bill")  # type: ignore[union-attr]
        cart = self.carts.get(self.c.billing, OWNER)
        line = pos.add_measured(self.c, OWNER, cart, scan, Decimal("500"), source="PRESET", entry="QUICK_PICK")
        self.assertEqual((line.qty, line.entry_method), (Decimal("500"), "QUICK_PICK"))
        line2 = pos.add_measured(self.c, STAFF, self.carts.get(self.c.billing, STAFF), scan, pos.parse_qty_text(scan.spec, "0.25 kg"), source="TYPED", entry="LOOKUP", reason="label damaged")
        self.assertEqual((line2.qty, line2.entry_method, line2.entry_reason), (Decimal("250"), "LOOKUP", "label damaged"))
        self.assertIsNone(pos.scan_for_product(self.c, 9999))

    def test_f8_customer_is_normalised_remembered_and_linked_to_the_sale(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR)
        bad = pos.set_customer(self.c, STAFF, cart, phone="12345")
        self.assertFalse(bad.ok)
        self.assertIn("10-digit", bad.message)
        ok = pos.set_customer(self.c, STAFF, cart, phone="098765-43210", name="Sharma ji")
        self.assertTrue(ok.ok, ok.message)
        self.assertEqual((cart.customer_phone, cart.customer_name), ("919876543210", "Sharma ji"))
        self.assertEqual(pos.customer_text(cart), "+91 98765 43210 · Sharma ji")
        self.assertEqual(pos.build_pos(self.c, self.texts, STAFF, cart).customer_text, "+91 98765 43210 · Sharma ji")
        held = pos.hold_current(self.c, STAFF, self.carts, label=None)
        cart = pos.resume_held(self.c, STAFF, self.carts, held)
        self.assertEqual(cart.customer_phone, "919876543210", "customer survives hold / resume")
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(pos.build_payment(self.c, self.texts, cart), mode="CASH", tendered_paise=1000))
        self.assertTrue(done.ok, done.error_code)
        self.assertEqual((done.customer_phone, done.customer_name), ("919876543210", "Sharma ji"))
        sale = self.c.receipts.load(done.result.sale_id)  # type: ignore[union-attr]
        record = self.c.billing.customer_of(sale)
        self.assertEqual((record.phone, record.name), ("919876543210", "Sharma ji"))  # type: ignore[union-attr]
        again = self.carts.get(self.c.billing, STAFF)
        pos.set_customer(self.c, STAFF, again, phone="+91 98765 43210")
        self.assertEqual(again.customer_id, record.id, "same number → same customer row; the stored name is kept")  # type: ignore[union-attr]
        self.assertEqual(again.customer_name, "Sharma ji")
        pos.set_customer(self.c, STAFF, again, phone="")
        self.assertIsNone(again.customer_id)

    def test_whatsapp_bill_uses_click_to_chat_link_or_cloud_api(self) -> None:
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("2"))
        pos.set_customer(self.c, STAFF, cart, phone="98765 43210")
        self.assertFalse(pos.build_pos(self.c, self.texts, STAFF, cart).whatsapp_enabled, "optional feature — off by default")
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(pos.build_payment(self.c, self.texts, cart), mode="CASH", tendered_paise=5000))
        sale_id = done.result.sale_id  # type: ignore[union-attr]
        off = pos.share_receipt(self.c, STAFF, sale_id)
        self.assertEqual((off.ok, off.via), (False, "none"))
        self.assertIn("switched off", off.message)
        self.settings.receipt.whatsapp_share = True
        self.assertTrue(self.c.receipt_share.enabled())
        # no API configured → click-to-chat link with the 58 mm receipt inside a monospace block
        out = pos.share_receipt(self.c, STAFF, sale_id)
        self.assertEqual((out.ok, out.via, out.phone_text), (True, "link", "+91 98765 43210"))
        assert out.link is not None
        self.assertTrue(out.link.startswith("https://wa.me/919876543210?text="))
        from urllib.parse import unquote

        text = unquote(out.link.split("text=", 1)[1])
        self.assertTrue(text.startswith("*Vaishnavi Puja Store*\nBill VPS/26-27/000003\n```\n"))
        self.assertIn("Camphor 50g", text)
        self.assertIn("TOTAL", text)
        self.assertIn("Paid: CASH", text)
        self.assertTrue(text.endswith("\n```"))
        self.assertTrue(all(len(ln) <= 32 for ln in text.split("```")[1].splitlines()), "58 mm width so it fits a phone screen")
        self.assertEqual(self.posts, [])
        # phone typed afterwards (customer asked at the end) → used for this send and remembered
        later = pos.share_receipt(self.c, STAFF, sale_id, phone="91234 56789", name="Gupta")
        self.assertEqual((later.via, later.phone_text), ("link", "+91 91234 56789"))
        # Cloud API configured → sent directly; audit row written
        self.settings.publishing.whatsapp_phone_id = "1234567890"
        self.secrets["publish.whatsapp_token"] = "EAAB-token"
        sent = pos.share_receipt(self.c, STAFF, sale_id)
        self.assertEqual((sent.ok, sent.via), (True, "api"))
        url, body = self.posts[-1]
        self.assertEqual(url, "https://graph.facebook.com/v19.0/1234567890/messages")
        self.assertEqual((body["to"], body["type"], body["messaging_product"]), ("919876543210", "text", "whatsapp"))
        self.assertIn("Camphor 50g", body["text"]["body"])
        # API down → falls back to the link, never fails the bill
        self.post_fails = True
        fallback = pos.share_receipt(self.c, STAFF, sale_id)
        self.assertEqual((fallback.ok, fallback.via), (True, "link"))
        # no number anywhere → polite refusal
        cart2 = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart2, CAMPHOR)
        anon = pos.do_checkout(self.c, STAFF, cart2, pos.make_payments(pos.build_payment(self.c, self.texts, cart2), mode="CASH", tendered_paise=1000))
        none = pos.share_receipt(self.c, STAFF, anon.result.sale_id)  # type: ignore[union-attr]
        self.assertEqual((none.ok, none.via), (False, "none"))
        self.assertIn("no mobile number", none.message)

    def test_upi_payload_and_split_shortfall(self) -> None:
        payload = pos.upi_payload("shop@upi", "Vaishnavi Puja Store", 12345, note="VPS/26-27/000003")
        self.assertTrue(payload.startswith("upi://pay?"))
        self.assertIn("pa=shop%40upi", payload)
        self.assertIn("am=123.45", payload)
        self.assertIn("cu=INR", payload)
        self.assertEqual(pos.split_shortfall(10000, cash_paise=5000, upi_paise=3000, card_paise=0), 2000)
        self.assertEqual(pos.split_shortfall(10000, cash_paise=5000, upi_paise=6000, card_paise=0), -1000, "negative = over-assigned")
        svg = pos.qr_svg(payload)
        self.assertTrue(svg is None or svg.lstrip().startswith("<svg"), "QR is optional (needs the qrcode package)")

    def test_weighed_items_price_proportionally_and_deduct_the_exact_weight(self) -> None:
        """Owner's examples: 100 g ghee at ₹500/kg → ₹50; 50 g camphor at ₹5000/kg → ₹250; stock down by the grams sold."""
        kg = dict(unit="kg", sale_mode="WEIGHT", base_unit="g", pricing_unit_qty=Decimal("1000"), pricing_unit_label="kg", min_qty=Decimal("10"), qty_step=Decimal("10"), allow_decimal_qty=True)
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Desi Ghee", price_paise=50000, **kg))
        camphor = self.c.catalog.create(OWNER, ProductInput(name="Camphor loose", price_paise=500000, **kg))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("10000"))
        self.c.inventory.opening_stock(OWNER, camphor.id, Decimal("2000"))
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, ghee.internal_barcode, qty=Decimal("100"))
        self.c.billing.add_scan(STAFF, cart, camphor.internal_barcode, qty=Decimal("50"))
        m = pos.build_pos(self.c, self.texts, STAFF, cart)
        self.assertEqual([(r.qty_text, r.shelf_text, r.amount_text) for r in m.rows], [("100 g", "500.00", "50.00"), ("50 g", "5000.00", "250.00")])
        self.assertEqual(m.total_text, "₹300.00")
        done = pos.do_checkout(self.c, STAFF, cart, pos.make_payments(pos.build_payment(self.c, self.texts, cart), mode="CASH", tendered_paise=50000))
        self.assertTrue(done.ok, done.error_code)
        lines = [ln for ln in done.receipt_preview.splitlines() if "Desi Ghee" in ln or "Camphor loose" in ln]
        self.assertIn("500.00/kg", lines[0], "rate printed per kg, not per 1000 g")
        self.assertIn("5000.00/kg", lines[1])
        self.assertEqual(self.c.inventory.stock_of(ghee.id), Decimal("9900"))
        self.assertEqual(self.c.inventory.stock_of(camphor.id), Decimal("1950"))

    def test_bill_rows_render_like_the_mockup(self) -> None:
        """Mock-up cart cells: mono '#id · code' sub-line, marker, discount chip with its small second line, key chips."""
        self.c.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.carts.get(self.c.billing, STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("2"))
        self.c.billing.add_lookup(STAFF, cart, 10, qty=Decimal("1"), reason="label damaged")
        self.c.billing.set_line_discount(STAFF, cart, cart.lines[1].line_id, amount_paise=100, reason="regular customer")
        self.c.billing.add_open_item(STAFF, cart, name="Garland", detail="fresh", qty=Decimal("1"), unit_price_paise=3500)
        rows = pos.build_pos(self.c, self.texts, STAFF, cart).rows
        offer, adhoc, open_item = rows
        self.assertEqual(pos.row_item_html(offer), "<div class='nm'>Camphor 50g</div><div class='sub'><span class='id'>#10 · CAM-0001</span></div>")
        html = pos.row_item_html(adhoc)
        self.assertIn("<span class='mark'>†</span>", html)
        self.assertIn("<span class='id'>#10 · CAM-0001</span> · found by name (label damaged)", html)
        self.assertNotIn("class='id'", pos.row_item_html(open_item), "open items have no catalogue id")
        self.assertEqual(pos.row_discount_html(offer, locked_text="offer · locked"), "<span class='disc OFFER'>Diwali Offer −1%<small>offer · locked</small></span>")
        self.assertEqual(pos.row_discount_html(adhoc, locked_text="x"), "<span class='disc ADHOC'>−₹1.00<small>regular customer</small></span>")
        self.assertEqual(pos.row_discount_html(open_item, locked_text="x"), "<span class='disc NONE'>—</span>")
        keys = pos.keys_html("Enter add scanned · F9 pay · Esc clear")
        self.assertEqual(keys, "<div class='vsm-keys'><code>Enter</code> add scanned <code>F9</code> pay <code>Esc</code> clear</div>")


if __name__ == "__main__":
    unittest.main()
