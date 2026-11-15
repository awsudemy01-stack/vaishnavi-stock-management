"""Unit 42 — void / refund with credit note (docs/05 §7), open-item review queue (§8c), the /sales history page."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus, SaleCommitted
from vaishnavi.core.errors import BillingError
from vaishnavi.core.permissions import Actor, PinRequired, Role
from vaishnavi.core.settings import Settings
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteInventoryStore, SqlitePrintQueueStore, SqliteShiftStore, SqliteTxn
from vaishnavi.hardware import receipts as renderers
from vaishnavi.platform.paths import AppPaths
from vaishnavi.platform.printing import PrinterStatus
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, PaymentInput, RefundLine
from vaishnavi.services.catalog import CatalogService
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferService
from vaishnavi.services.open_item_review import OpenItemReviewService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.receipt import ReceiptService
from vaishnavi.services.shift import ShiftService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import sales_history as page

from tests.core.test_billing import CAMPHOR, OWNER, STAFF, STAFF_PIN
from tests.core.test_db_guardian_backup import T0, make_live_db


class _Shop:
    pass


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
        self.txn = SqliteTxn(self.paths.database)
        self.audit = AuditService(SqliteAuditStore())
        self.settings = Settings()
        catalog_store = SqliteCatalogStore()
        c = _Shop()
        c.settings_service = type("S", (), {"effective": staticmethod(lambda: self.settings)})()
        c.catalog = CatalogService(self.txn, catalog_store, self.audit, event_bus=self.bus)
        c.inventory = InventoryService(self.txn, SqliteInventoryStore(), catalog_store, self.audit, event_bus=self.bus)
        self.ledger = LedgerService(SqliteLedgerStore())
        billing_store = SqliteBillingStore()
        c.offers = OfferService(self.txn, billing_store, catalog_store, self.audit)
        c.billing = BillingService(self.txn, billing_store, catalog_store, SqliteInventoryStore(), c.catalog, c.offers, self.ledger, self.audit, settings=lambda: self.settings, event_bus=self.bus)
        c.shifts = ShiftService(self.txn, SqliteShiftStore(), self.audit, self.ledger, event_bus=self.bus)
        c.receipts = ReceiptService(self.txn, billing_store, self.paths, settings=lambda: self.settings, cashier_name=lambda uid: {1: "Owner", 2: "Ramesh"}.get(uid, "?"), upi_id=lambda: None, renderer=renderers)
        c.print_queue = PrintQueueService(self.txn, SqlitePrintQueueStore(), c.receipts, self.audit, settings=lambda: self.settings, status=lambda hw: PrinterStatus("none", "", False, ""), transport_factory=lambda hw: None, event_bus=self.bus)
        c.print_queue.subscribe(self.bus)
        c.review = OpenItemReviewService(self.txn, billing_store, c.catalog, self.audit)
        c.users = type("U", (), {"display_names": staticmethod(lambda: {1: "Owner", 2: "Ramesh"})})()
        self.c = c
        self.committed: list[SaleCommitted] = []
        self.bus.subscribe(SaleCommitted, self.committed.append)
        self.texts = UiTexts.load("en")

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, *, qty: str = "2", open_item: bool = False, actor: Actor = STAFF):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        self.c.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        if open_item:
            self.c.billing.add_open_item(actor, cart, name="Marigold garland", detail="", qty=Decimal("1"), unit_price_paise=2000)
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput("CASH", total, tendered_paise=total)])

    def _chain_ok(self) -> bool:
        with self.txn(label="t") as session:
            return self.ledger.verify_sales(session).ok


class VoidTest(Base):
    def test_owner_full_void_reverses_stock_cash_and_chains(self) -> None:
        self.c.shifts.open(OWNER, opening_float_paise=0)
        sale = self._sale()
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 94)
        info = self.c.billing.void_status(sale.sale_id)
        self.assertTrue(info.can_void)
        result = self.c.billing.void(OWNER, sale.sale_id, reason="customer returned everything")
        self.assertEqual((result.sale.status, result.sale.original_sale_id, result.invoice_no), ("VOIDED", sale.sale_id, "VPS/26-27/000004"))
        self.assertEqual(result.sale.total_paise, 2000, "a credit note carries the amount returned as a positive figure; status gives the direction")
        self.assertEqual([(p.mode, p.amount_paise) for p in result.sale.payments], [("CASH", 2000)])
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 96, "stock is back")
        moves = self._q("SELECT type, qty_delta, user_id, ref_id FROM stock_movement WHERE ref_id = ? AND type <> 'SALE'", result.sale_id)
        self.assertEqual(moves, [("SALE_VOID", 2, 1, result.sale_id)], "reversing movement carries the Owner's id (trigger E-INV-010)")
        self.assertTrue(self._chain_ok())
        self.assertEqual(self.c.shifts.expected_cash(1), 4000 + 2000, "the staff drawer still holds the sale's cash…")
        self.assertEqual(self.c.shifts.expected_cash(self.c.shifts.current(1).id), -2000, "…and the Owner's drawer paid the refund out")  # type: ignore[union-attr]
        row = self._q("SELECT user_id, actor_user_id, action FROM audit_log WHERE action = 'SALE_VOIDED'")
        self.assertEqual(row, [(1, None, "SALE_VOIDED")], "audit convention: actor is only recorded when it differs from the user")
        self.assertEqual([e.invoice_no for e in self.committed][-1], "VPS/26-27/000004", "the credit note is a committed sale → printed/queued like any receipt")
        model = self.c.receipts.build(result.sale)
        self.assertEqual(model.copy_label, "CREDIT NOTE")
        self.assertIn(f"Against bill {sale.invoice_no}", model.header_lines)
        after = self.c.billing.void_status(sale.sale_id)
        self.assertFalse(after.can_void)
        with self.assertRaises(BillingError) as cm:
            self.c.billing.void(OWNER, sale.sale_id, reason="again")
        self.assertEqual(cm.exception.code, "E-BILL-092", "already voided")
        with self.assertRaises(BillingError):
            self.c.billing.void(OWNER, result.sale_id, reason="void the void")

    def test_staff_void_needs_pin_and_the_owner_is_recorded(self) -> None:
        sale = self._sale()
        with self.assertRaises(PinRequired):
            self.c.billing.void(STAFF, sale.sale_id, reason="wrong item")
        with self.assertRaises(ValueError):
            self.c.billing.void(STAFF_PIN, sale.sale_id, reason="wrong item")  # approver missing
        with self.assertRaises(ValueError):
            self.c.billing.void(STAFF_PIN, sale.sale_id, reason="", approver_id=1)
        result = self.c.billing.void(STAFF_PIN, sale.sale_id, reason="wrong item", approver_id=1)
        self.assertEqual((result.sale.user_id, result.sale.shift_id, result.sale.approved_by_user_id), (2, 1, 1))
        moves = self._q("SELECT user_id FROM stock_movement WHERE ref_id = ? AND type = 'SALE_VOID'", result.sale_id)
        self.assertEqual(moves, [(1,)])
        self.assertEqual(self._q("SELECT user_id, actor_user_id FROM audit_log WHERE action = 'SALE_VOIDED'"), [(2, 1)], "both ids: who did it, who approved")
        self.assertEqual(self.c.shifts.expected_cash(1), 4000, "sale +2000, refund −2000 on the same drawer")

    def test_partial_refund_tracks_quantities(self) -> None:
        self.c.shifts.open(OWNER, opening_float_paise=0)
        sale = self._sale(qty="3", open_item=True)
        camphor_item = next(i for i in sale.sale.items if not i.is_open_item)
        garland_item = next(i for i in sale.sale.items if i.is_open_item)
        r1 = self.c.billing.refund(OWNER, sale.sale_id, [RefundLine(camphor_item.id, Decimal("1"))], reason="one broken")
        self.assertEqual((r1.sale.status, r1.sale.total_paise, len(r1.sale.items)), ("REFUND", 1000, 1))
        self.assertEqual(self._q("SELECT type, qty_delta FROM stock_movement WHERE ref_id = ? AND type = 'RETURN'", r1.sale_id), [("RETURN", 1)])
        info = self.c.billing.void_status(sale.sale_id)
        self.assertFalse(info.can_void, "after a partial refund only further refunds are possible")
        self.assertEqual(info.refundable[camphor_item.id], Decimal("2"))
        self.assertEqual(info.refundable[garland_item.id], Decimal("1"))
        with self.assertRaises(BillingError) as cm:
            self.c.billing.refund(OWNER, sale.sale_id, [RefundLine(camphor_item.id, Decimal("3"))], reason="too much")
        self.assertEqual(cm.exception.code, "E-BILL-091")
        r2 = self.c.billing.refund(OWNER, sale.sale_id, [RefundLine(camphor_item.id, Decimal("2")), RefundLine(garland_item.id, Decimal("1"))], reason="rest")
        self.assertEqual(r2.sale.total_paise, 4000)
        self.assertEqual(self._q("SELECT COUNT(*) FROM stock_movement WHERE ref_id = ? AND type = 'RETURN'", r2.sale_id)[0][0], 1, "open items never move stock")
        self.assertEqual(self.c.billing.void_status(sale.sale_id).refundable, {camphor_item.id: Decimal("0"), garland_item.id: Decimal("0")})
        self.assertTrue(self._chain_ok())
        self.assertEqual(self._q("SELECT qty FROM product_stock WHERE product_id = 10")[0][0], 96)

    def test_void_window_binds_staff_not_owner(self) -> None:
        sale = self._sale()
        self.clock.advance(hours=25)
        with self.assertRaises(BillingError) as cm:
            self.c.billing.void(STAFF_PIN, sale.sale_id, reason="late", approver_id=1)
        self.assertEqual(cm.exception.code, "E-BILL-090")
        self.assertFalse(self.c.billing.void_status(sale.sale_id, actor=STAFF).can_void)
        self.assertTrue(self.c.billing.void_status(sale.sale_id, actor=OWNER).can_void)
        self.c.shifts.open(OWNER, opening_float_paise=0)
        self.assertEqual(self.c.billing.void(OWNER, sale.sale_id, reason="owner decides").sale.status, "VOIDED")


class ReviewTest(Base):
    def test_open_item_queue_groups_converts_and_dismisses(self) -> None:
        self._sale(open_item=True)
        self._sale(open_item=True)
        groups = self.c.review.pending()
        self.assertEqual([(g.name, g.count, g.total_paise, len(g.sale_item_ids)) for g in groups], [("Marigold garland", 2, 4000, 2)])
        product = self.c.review.convert(OWNER, groups[0].sale_item_ids, name="Marigold Garland", price_paise=2000, category_id=None)
        self.assertEqual((product.name, product.price_paise), ("Marigold Garland", 2000))
        self.assertEqual(self.c.review.pending(), [])
        rows = self._q("SELECT status, converted_product_id FROM open_item_review ORDER BY id")
        self.assertEqual(rows, [("CONVERTED", product.id), ("CONVERTED", product.id)])
        self._sale(open_item=True)
        again = self.c.review.pending()
        self.c.review.dismiss(OWNER, again[0].sale_item_ids)
        self.assertEqual(self.c.review.pending(), [])
        self.assertEqual(self._q("SELECT COUNT(*) FROM open_item_review WHERE status = 'DISMISSED'")[0][0], 1)
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action LIKE 'OPEN_ITEM_%' AND action <> 'OPEN_ITEM_LINE'")]
        self.assertEqual(actions, ["OPEN_ITEM_CONVERTED", "OPEN_ITEM_DISMISSED"])


class HistoryPageTest(Base):
    def test_history_rows_role_scoped_and_detail_actions(self) -> None:
        self.c.shifts.open(OWNER, opening_float_paise=0)
        staff_sale = self._sale(open_item=True)
        owner_sale = self._sale(actor=OWNER)
        mine = page.build_history(self.c, self.texts, STAFF)
        self.assertEqual([r.invoice_no for r in mine.rows], ["VPS/26-27/000003", "VPS/26-27/000002", "VPS/26-27/000001"], "staff see their own bills, newest first")
        self.assertFalse(mine.can_filter_cashier)
        everything = page.build_history(self.c, self.texts, OWNER)
        self.assertEqual(everything.rows[0].invoice_no, "VPS/26-27/000004")
        self.assertEqual((everything.rows[0].cashier, everything.rows[1].markers), ("Owner", "*"))
        self.assertEqual(everything.rows[1].total_text, "₹40.00")
        self.assertEqual(everything.rows[0].status, "COMPLETED")
        found = page.build_history(self.c, self.texts, OWNER, query="000003")
        self.assertEqual([r.invoice_no for r in found.rows], ["VPS/26-27/000003"])
        detail = page.build_detail(self.c, self.texts, STAFF, staff_sale.sale_id)
        assert detail is not None
        self.assertEqual(detail.receipt.invoice_no, "VPS/26-27/000003")
        self.assertTrue(detail.can_reprint)
        self.assertTrue(detail.void_needs_pin, "staff may void only with the Owner PIN")
        self.assertEqual([(ln.name, str(ln.max_qty)) for ln in detail.refund_lines], [("Camphor 50g", "2"), ("Marigold garland", "1")])
        self.assertEqual(detail.reasons[0][1], "wrong_item")
        self.assertIsNone(page.build_detail(self.c, self.texts, STAFF, owner_sale.sale_id), "another cashier's bill is not shown to staff")
        outcome = page.do_void(self.c, OWNER, staff_sale.sale_id, reason_key="wrong_item", note="")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result.sale.status, "VOIDED")  # type: ignore[union-attr]
        detail2 = page.build_detail(self.c, self.texts, OWNER, staff_sale.sale_id)
        assert detail2 is not None
        self.assertFalse(detail2.can_void)
        self.assertEqual(detail2.related[0].invoice_no, "VPS/26-27/000005")
        refund = page.do_refund(self.c, OWNER, owner_sale.sale_id, {str(owner_sale.sale.items[0].id): "1"}, reason_key="damaged", note="torn")
        self.assertTrue(refund.ok)
        self.assertEqual(refund.result.sale.status, "REFUND")  # type: ignore[union-attr]
        bad = page.do_refund(self.c, OWNER, owner_sale.sale_id, {str(owner_sale.sale.items[0].id): "9"}, reason_key="damaged", note="")
        self.assertEqual((bad.ok, bad.error_code), (False, "E-BILL-091"))
        review = page.build_review(self.c, self.texts)
        self.assertEqual([g.name for g in review.groups], ["Marigold garland"])


if __name__ == "__main__":
    unittest.main()
