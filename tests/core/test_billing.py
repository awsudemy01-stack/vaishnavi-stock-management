"""Unit 37 — Offers, the cart and the idempotent checkout (docs/05 §1, §3, §4, §8a–8d, §10)."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.barcode import internal_ean13
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import BillingError, InventoryError, MeasureError, ScannerError
from vaishnavi.core.events import EventBus, SaleCommitted
from vaishnavi.core.permissions import Actor, PermissionDenied, PinRequired, Role
from vaishnavi.core.settings import Settings
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteInventoryStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, Cart, PaymentInput
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferInput, OfferService

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)
STAFF_PIN = Actor(user_id=2, role=Role.STAFF, pin_verified=True)
CAMPHOR = internal_ean13(10)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # product 10 "Camphor 50g" ₹10, stock 96; shift 1 open for user 2; two sales in FY 26-27
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.txn = SqliteTxn(self.paths.database)
        self.audit = AuditService(SqliteAuditStore())
        self.settings = Settings()
        catalog_store = SqliteCatalogStore()
        self.catalog = CatalogService(self.txn, catalog_store, self.audit, event_bus=self.bus)
        self.inventory = InventoryService(self.txn, SqliteInventoryStore(), catalog_store, self.audit, event_bus=self.bus)
        self.ledger = LedgerService(SqliteLedgerStore())
        self.offers = OfferService(self.txn, SqliteBillingStore(), catalog_store, self.audit)
        self.billing = BillingService(self.txn, SqliteBillingStore(), catalog_store, SqliteInventoryStore(), self.catalog, self.offers, self.ledger, self.audit, settings=lambda: self.settings, event_bus=self.bus)
        self.committed: list[SaleCommitted] = []
        self.bus.subscribe(SaleCommitted, self.committed.append)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _ghee(self) -> int:
        p = self.catalog.create(OWNER, ProductInput(name="Ghee (loose)", price_paise=6000, sale_mode="WEIGHT", base_unit="g", unit="g", pricing_unit_qty=Decimal("100"), pricing_unit_label="100 g", min_qty=Decimal("50"), qty_step=Decimal("10"), qty_presets="[50, 100, 250, 500, 1000]", allow_decimal_qty=True))
        self.inventory.opening_stock(OWNER, p.id, Decimal("5000"))
        return p.id


# ============================================================================ offers
class OfferTest(Base):
    def test_owner_creates_offers_staff_cannot_and_matching_respects_scope_and_dates(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.offers.create(STAFF, OfferInput(name="x", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        diwali = self.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10, valid_from="2026-11-01", valid_to="2026-11-30"))
        self.offers.create(OWNER, OfferInput(name="Old", type="PCT", value=Decimal("50"), scope="PRODUCT", scope_id=10, valid_from="2025-01-01", valid_to="2025-01-31"))
        product = self.catalog.get(10)
        assert product is not None
        match = self.offers.best_for(product, Decimal("1"))
        assert match is not None
        self.assertEqual((match.offer.id, match.offer.name, match.discount_bp, match.discount_paise), (diwali.id, "Diwali Offer", 100, 0))
        self.assertEqual([o.name for o in self.offers.list_offers(active_only=True)], ["Diwali Offer", "Old"])
        self.offers.deactivate(OWNER, diwali.id)
        self.assertIsNone(self.offers.best_for(product, Decimal("1")), "deactivated and the other one expired")
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action LIKE 'OFFER_%' ORDER BY id")]
        self.assertEqual(actions, ["OFFER_CREATED", "OFFER_CREATED", "OFFER_DEACTIVATED"])

    def test_buy_x_get_y_and_category_scope_and_min_qty(self) -> None:
        cat = self.catalog.create_category(OWNER, name="Camphor")
        self.catalog.update(OWNER, 10, row_version=self.catalog.get(10).row_version, changes={"category_id": cat.id})  # type: ignore[union-attr]
        self.offers.create(OWNER, OfferInput(name="Buy 2 get 1", type="BUY_X_GET_Y", value=Decimal("1"), scope="CATEGORY", scope_id=cat.id, min_qty=Decimal("2")))
        product = self.catalog.get(10)
        assert product is not None
        self.assertIsNone(self.offers.best_for(product, Decimal("1")), "below min qty")
        three = self.offers.best_for(product, Decimal("3"))
        assert three is not None
        self.assertEqual(three.discount_paise, 1000, "one free piece per 2+1 group")
        six = self.offers.best_for(product, Decimal("7"))
        assert six is not None
        self.assertEqual(six.discount_paise, 2000, "two groups of three in 7")


# ============================================================================ cart
class CartTest(Base):
    def test_scan_adds_line_with_offer_and_totals(self) -> None:
        self.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.billing.new_cart(STAFF)
        self.assertTrue(cart.client_txn_id)
        scan = self.billing.lookup_scan(CAMPHOR)
        assert scan is not None
        self.assertEqual((scan.product.id, scan.needs_qty), (10, False))
        line = self.billing.add_scan(STAFF, cart, CAMPHOR)
        self.assertEqual((line.product_id, line.qty, line.entry_method, line.qty_source), (10, Decimal("1"), "SCAN", "SCAN"))
        self.assertEqual(line.offer.offer.name, "Diwali Offer")  # type: ignore[union-attr]
        line = self.billing.add_scan(STAFF, cart, CAMPHOR)
        self.assertEqual((len(cart.lines), line.qty), (1, Decimal("2")), "scanning the same item again bumps the quantity")
        totals = self.billing.totals(cart)
        self.assertEqual((totals.subtotal_paise, totals.item_discount_paise, totals.roundoff_paise, totals.total_paise), (2000, 20, 20, 2000), "19.80 rounds to ₹20 with a +0.20 round-off line")
        self.billing.set_qty(STAFF, cart, line.line_id, Decimal("3"))
        self.assertEqual(self.billing.totals(cart).subtotal_paise, 3000)
        self.billing.remove_line(STAFF, cart, line.line_id)
        self.assertEqual(cart.lines, [])
        with self.assertRaises(ScannerError) as cm:
            self.billing.add_scan(STAFF, cart, "8901234567890")
        self.assertEqual(cm.exception.code, "E-SCN-002")
        self.assertIsNone(self.billing.lookup_scan("8901234567890"))

    def test_weighed_item_needs_a_quantity_and_scale_codes_fill_it(self) -> None:
        ghee = self._ghee()
        code = internal_ean13(ghee)
        scan = self.billing.lookup_scan(code)
        assert scan is not None
        self.assertTrue(scan.needs_qty)
        self.assertEqual(scan.spec.presets[:2], (Decimal("50"), Decimal("100")))
        cart = self.billing.new_cart(STAFF)
        with self.assertRaises(MeasureError):
            self.billing.add_scan(STAFF, cart, code)
        line = self.billing.add_scan(STAFF, cart, code, qty=Decimal("100"), qty_source="PRESET")
        self.assertEqual((line.qty, line.unit, line.qty_source), (Decimal("100"), "g", "PRESET"))
        self.assertEqual(self.billing.totals(cart).total_paise, 6000, "100 g at ₹60/100 g")
        with self.assertRaises(MeasureError):
            self.billing.set_qty(STAFF, cart, line.line_id, Decimal("125"))
        self.settings.hardware.scale_barcode_format = "21PPPPPWWWWWC"
        plu = f"{ghee:05d}"
        body = f"21{plu}01250"
        from vaishnavi.core.barcode import ean13_check_digit

        scale_code = body + str(ean13_check_digit(body))
        scan2 = self.billing.lookup_scan(scale_code)
        assert scan2 is not None
        self.assertEqual((scan2.product.id, scan2.needs_qty, scan2.suggested_qty), (ghee, False, Decimal("1250")))
        line2 = self.billing.add_scan(STAFF, cart, scale_code)
        self.assertEqual((line2.qty, line2.qty_source), (Decimal("1350"), "EMBEDDED_BARCODE"), "same product → quantity accumulates")

    def test_discount_cap_reason_pin_and_offer_removal(self) -> None:
        self.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.billing.new_cart(STAFF)
        line = self.billing.add_scan(STAFF, cart, CAMPHOR)
        with self.assertRaises(ValueError):
            self.billing.set_line_discount(STAFF, cart, line.line_id, percent=Decimal("5"), reason="")
        self.billing.set_line_discount(STAFF, cart, line.line_id, percent=Decimal("5"), reason="regular customer")
        with self.assertRaises(BillingError) as cm:
            self.billing.set_line_discount(STAFF, cart, line.line_id, percent=Decimal("15"), reason="bargaining")
        self.assertEqual(cm.exception.code, "E-BILL-060")
        self.billing.set_line_discount(STAFF_PIN, cart, line.line_id, percent=Decimal("15"), reason="bargaining", approver_id=1)
        self.assertEqual((cart.lines[0].adhoc_discount_bp, cart.approved_by_user_id), (1500, 1))
        totals = self.billing.totals(cart)
        self.assertEqual((totals.lines[0].applied, totals.item_discount_paise), ("ADHOC", 150), "offer 1 % not stackable → the bigger ad-hoc wins and is named")
        with self.assertRaises(BillingError) as cm:
            self.billing.remove_offer(STAFF, cart, line.line_id)
        self.assertEqual(cm.exception.code, "E-BILL-061")
        self.billing.remove_offer(OWNER, cart, line.line_id)
        self.assertTrue(cart.lines[0].offer_removed)
        with self.assertRaises(BillingError) as cm:
            self.billing.set_bill_discount(STAFF, cart, percent=Decimal("20"), reason="festival")
        self.assertEqual(cm.exception.code, "E-BILL-060")
        self.billing.set_bill_discount(STAFF, cart, amount_paise=50, reason="round figure")
        self.assertEqual(self.billing.totals(cart).bill_discount_paise, 50)

    def test_open_items_policy_cap_and_low_price_check(self) -> None:
        cart = self.billing.new_cart(STAFF)
        line = self.billing.add_open_item(STAFF, cart, name="Marigold garland", detail="loose", qty=Decimal("2"), unit_price_paise=2000)
        self.assertEqual((line.entry_method, line.is_open_item, line.open_item_name), ("OPEN_ITEM", True, "Marigold garland"))
        self.assertEqual(line.product_id, self.billing.open_item_product_id)
        with self.assertRaises(BillingError) as cm:
            self.billing.add_open_item(STAFF, cart, name="Camphor", detail="", qty=Decimal("1"), unit_price_paise=300)
        self.assertEqual(cm.exception.code, "E-BILL-072", "₹3 for something that looks like Camphor 50g (₹10) is a classic under-ring")
        self.billing.add_open_item(STAFF, cart, name="Camphor", detail="", qty=Decimal("1"), unit_price_paise=300, confirm_low_price=True)
        self.billing.add_open_item(STAFF, cart, name="Idol", detail="", qty=Decimal("1"), unit_price_paise=50000)
        with self.assertRaises(BillingError) as cm:
            self.billing.add_open_item(STAFF, cart, name="Fourth", detail="", qty=Decimal("1"), unit_price_paise=100)
        self.assertEqual(cm.exception.code, "E-BILL-071", "cap 3 per bill for staff")
        self.billing.add_open_item(STAFF_PIN, cart, name="Fourth", detail="", qty=Decimal("1"), unit_price_paise=100)
        self.settings.billing.manual_add_policy = "PIN"
        with self.assertRaises(PinRequired):
            self.billing.add_open_item(STAFF, self.billing.new_cart(STAFF), name="x", detail="", qty=Decimal("1"), unit_price_paise=100)
        self.settings.billing.manual_add_policy = "BLOCK"
        with self.assertRaises(PermissionDenied):
            self.billing.add_open_item(STAFF, self.billing.new_cart(STAFF), name="x", detail="", qty=Decimal("1"), unit_price_paise=100)
        self.billing.add_open_item(OWNER, self.billing.new_cart(OWNER), name="x", detail="", qty=Decimal("1"), unit_price_paise=100)

    def test_lookup_and_quick_pick_paths_are_marked_and_strict_for_staff(self) -> None:
        cart = self.billing.new_cart(STAFF)
        line = self.billing.add_lookup(STAFF, cart, 10, qty=Decimal("1"), reason="label damaged")
        self.assertEqual((line.entry_method, line.entry_reason), ("LOOKUP", "label damaged"))
        with self.assertRaises(BillingError) as cm:
            self.billing.add_quick_pick(STAFF, cart, 10)
        self.assertEqual(cm.exception.code, "E-BILL-070", "tiles are disabled for staff in strict mode")
        owner_cart = self.billing.new_cart(OWNER)
        self.assertEqual(self.billing.add_quick_pick(OWNER, owner_cart, 10).entry_method, "QUICK_PICK")
        self.settings.billing.require_scan = False
        self.assertEqual(self.billing.add_quick_pick(STAFF, cart, 10).entry_method, "QUICK_PICK")

    def test_hold_and_resume_round_trip(self) -> None:
        self.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.billing.new_cart(STAFF)
        self.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("3"))
        self.billing.add_open_item(STAFF, cart, name="Garland", detail="", qty=Decimal("1"), unit_price_paise=2000)
        self.billing.set_bill_discount(STAFF, cart, amount_paise=100, reason="x")
        held_id = self.billing.hold(STAFF, cart, label="Sharma ji")
        held = self.billing.held_carts(STAFF)
        self.assertEqual([(h.id, h.label, h.line_count) for h in held], [(held_id, "Sharma ji", 2)])
        back = self.billing.resume(STAFF, held_id)
        self.assertEqual(back.client_txn_id, cart.client_txn_id)
        self.assertEqual(self.billing.totals(back).total_paise, self.billing.totals(cart).total_paise)
        self.assertEqual(back.lines[0].offer.offer.name, "Diwali Offer")  # type: ignore[union-attr]
        self.assertEqual(self.billing.held_carts(STAFF), [])


# ============================================================================ checkout
class CheckoutTest(Base):
    def _cart(self, actor: Actor = STAFF, qty: Decimal = Decimal("2")) -> Cart:
        cart = self.billing.new_cart(actor)
        self.billing.add_scan(actor, cart, CAMPHOR, qty=qty)
        return cart

    def test_checkout_writes_sale_items_payments_movements_and_hash_chain(self) -> None:
        cart = self._cart()
        self.billing.add_open_item(STAFF, cart, name="Garland", detail="fresh", qty=Decimal("1"), unit_price_paise=2000)
        result = self.billing.checkout(STAFF, cart, [PaymentInput(mode="CASH", amount_paise=4000, tendered_paise=5000)])
        self.assertFalse(result.replayed)
        self.assertEqual(result.invoice_no, "VPS/26-27/000003", "continues after the two fixture bills")
        self.assertEqual((result.total_paise, result.change_paise), (4000, 1000))
        sale = result.sale
        self.assertEqual((sale.status, sale.shift_id, sale.user_id, len(sale.items), len(sale.payments)), ("COMPLETED", 1, 2, 2, 1))
        self.assertEqual(sale.items[1].is_open_item, True)
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 94)
        moves = self._q("SELECT product_id, type, ref_type, ref_id FROM stock_movement WHERE ref_id = ?", result.sale_id)
        self.assertEqual(moves, [(10, "SALE", "SALE", result.sale_id)], "exactly one SALE movement; the open item never moves stock")
        self.assertEqual(self._q("SELECT receipt_printed, reprint_count FROM sale WHERE id = ?", result.sale_id), [(0, 0)])
        with self.txn(label="t") as session:
            report = self.ledger.verify_sales(session)
        self.assertTrue(report.ok, report)
        self.assertEqual([e.invoice_no for e in self.committed], ["VPS/26-27/000003"])
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE entity = 'sale' ORDER BY id")]
        self.assertIn("SALE_CREATED", actions)
        self.assertIn("OPEN_ITEM_LINE", actions)
        self.assertEqual(self._q("SELECT value FROM counter WHERE name = 'invoice:26-27'"), [(3,)])

    def test_replaying_the_same_cart_returns_the_same_invoice_once(self) -> None:
        cart = self._cart()
        first = self.billing.checkout(STAFF, cart, [PaymentInput(mode="UPI", amount_paise=2000)])
        again = self.billing.checkout(STAFF, cart, [PaymentInput(mode="UPI", amount_paise=2000)])
        self.assertTrue(again.replayed)
        self.assertEqual((again.sale_id, again.invoice_no), (first.sale_id, first.invoice_no))
        self.assertEqual(self._q("SELECT COUNT(*) FROM sale")[0][0], 3)
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 94, "stock moved once")
        self.assertEqual(len(self.committed), 1)

    def test_short_payment_no_shift_and_negative_stock_roll_back_everything(self) -> None:
        cart = self._cart()
        with self.assertRaises(BillingError) as cm:
            self.billing.checkout(STAFF, cart, [PaymentInput(mode="CASH", amount_paise=1500)])
        self.assertEqual(cm.exception.code, "E-BILL-050")
        with self.assertRaises(BillingError) as cm:
            self.billing.checkout(OWNER, self._cart(OWNER), [PaymentInput(mode="CASH", amount_paise=2000)])
        self.assertEqual(cm.exception.code, "E-BILL-042", "the owner has no open shift in the fixture")
        with self.assertRaises(BillingError):
            self.billing.checkout(STAFF, self.billing.new_cart(STAFF), [])
        big = self._cart(qty=Decimal("1000"))
        with self.assertRaises(InventoryError) as cm:
            self.billing.checkout(STAFF, big, [PaymentInput(mode="CASH", amount_paise=1_000_000)])
        self.assertEqual(cm.exception.code, "E-INV-003")
        self.assertEqual(self._q("SELECT COUNT(*) FROM sale")[0][0], 2, "nothing written when the movement fails")
        self.assertEqual(self._q("SELECT value FROM counter WHERE name = 'invoice:26-27'"), [], "counter not consumed either")
        self.assertEqual(self.committed, [])

    def test_split_payment_and_warn_policy(self) -> None:
        cart = self._cart()
        result = self.billing.checkout(STAFF, cart, [PaymentInput(mode="CASH", amount_paise=500, tendered_paise=500), PaymentInput(mode="UPI", amount_paise=1500, reference="UPI/1234")])
        self.assertEqual([(p.mode, p.amount_paise, p.reference) for p in result.sale.payments], [("CASH", 500, None), ("UPI", 1500, "UPI/1234")])
        self.settings.billing.negative_stock_policy = "WARN"
        big = self._cart(qty=Decimal("1000"))
        warned = self.billing.checkout(STAFF, big, [PaymentInput(mode="CASH", amount_paise=1_000_000)])
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 94 - 1000)
        self.assertTrue(warned.warnings and "E-INV-003" in warned.warnings[0])

    def test_concurrent_checkouts_get_gap_free_unique_invoice_numbers(self) -> None:
        carts = [self._cart(qty=Decimal("1")) for _ in range(8)]
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def go(cart: Cart) -> None:
            try:
                r = self.billing.checkout(STAFF, cart, [PaymentInput(mode="CASH", amount_paise=1000)])
                with lock:
                    results.append(r.invoice_no)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=go, args=(c,)) for c in carts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [f"VPS/26-27/{n:06d}" for n in range(3, 11)])
        with self.txn(label="t") as session:
            self.assertTrue(self.ledger.verify_sales(session).ok)
            self.assertTrue(self.ledger.verify_gaps(session).ok)

    def test_reads_sale_find_and_today_summary(self) -> None:
        result = self.billing.checkout(STAFF, self._cart(), [PaymentInput(mode="CASH", amount_paise=2000)])
        view = self.billing.get_sale(result.sale_id)
        assert view is not None
        self.assertEqual((view.invoice_no, view.items[0].name_snapshot, view.items[0].qty), ("VPS/26-27/000003", "Camphor 50g", Decimal("2")))
        self.assertEqual(self.billing.find_sale("VPS/26-27/000003").id, result.sale_id)  # type: ignore[union-attr]
        self.assertEqual(self.billing.find_sale(view.public_id).id, result.sale_id)  # type: ignore[union-attr]
        self.assertIsNone(self.billing.find_sale("nope"))
        summary = self.billing.today_summary(user_id=2)
        self.assertEqual((summary.bills, summary.total_paise), (3, 6000), "two fixture bills + this one, all today")
        recent = self.billing.recent_sales(limit=2)
        self.assertEqual([s.invoice_no for s in recent], ["VPS/26-27/000003", "VPS/26-27/000002"])
        rebuilt = replace(view, note="x")
        self.assertEqual(rebuilt.total_paise, view.total_paise)


if __name__ == "__main__":
    unittest.main()
