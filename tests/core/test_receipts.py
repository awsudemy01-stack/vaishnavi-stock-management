"""Unit 38 — receipt view-model, ESC/POS 80/58 mm + HTML renderers, archive, print-or-queue (docs/05 §8, §10)."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.barcode import parse_receipt_ean13, receipt_ean13
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import AuthError
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.settings import HardwareSettings, Settings
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteBillingStore, SqliteCatalogStore, SqliteInventoryStore, SqlitePrintQueueStore, SqliteTxn
from vaishnavi.hardware import receipts as renderers
from vaishnavi.platform.paths import AppPaths
from vaishnavi.platform.printing import PrinterStatus
from vaishnavi.services.audit import AuditService
from vaishnavi.services.billing import BillingService, PaymentInput
from vaishnavi.services.catalog import CatalogService
from vaishnavi.services.ledger import LedgerService
from vaishnavi.services.offers import OfferInput, OfferService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.receipt import ReceiptService

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db

GOLDEN = Path(__file__).parent / "golden"


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.jobs: list[bytes] = []
        self.fail = fail

    def send(self, data: bytes) -> None:
        if self.fail:
            raise OSError("printer unplugged")
        self.jobs.append(data)


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
        self.settings.shop.address_line1 = "12, Mandir Road, Varanasi"
        self.settings.shop.phone = "9800000001"
        catalog_store = SqliteCatalogStore()
        self.catalog = CatalogService(self.txn, catalog_store, self.audit, event_bus=self.bus)
        self.ledger = LedgerService(SqliteLedgerStore())
        billing_store = SqliteBillingStore()
        self.offers = OfferService(self.txn, billing_store, catalog_store, self.audit)
        self.billing = BillingService(self.txn, billing_store, catalog_store, SqliteInventoryStore(), self.catalog, self.offers, self.ledger, self.audit, settings=lambda: self.settings, event_bus=self.bus)
        self.upi = "shop@upi"
        self.receipts = ReceiptService(self.txn, billing_store, self.paths, settings=lambda: self.settings, cashier_name=lambda uid: {1: "Owner", 2: "Ramesh"}.get(uid, "?"), upi_id=lambda: self.upi, renderer=renderers)
        self.transport = FakeTransport()
        self.status = PrinterStatus("none", "", False, "not set up")
        self.queue = PrintQueueService(self.txn, SqlitePrintQueueStore(), self.receipts, self.audit, settings=lambda: self.settings, status=lambda hw: self.status, transport_factory=lambda hw: self.transport if hw.printer_kind != "none" else None, event_bus=self.bus)
        self.queue.subscribe(self.bus)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, *, with_offer: bool = True, open_item: bool = True, lookup: bool = False):  # type: ignore[no-untyped-def]
        if with_offer:
            self.offers.create(OWNER, OfferInput(name="Diwali Offer", type="PCT", value=Decimal("1"), scope="PRODUCT", scope_id=10))
        cart = self.billing.new_cart(STAFF)
        self.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal("2"))
        if lookup:
            self.billing.add_lookup(STAFF, cart, 10, qty=Decimal("1"), reason="label damaged")
        if open_item:
            self.billing.add_open_item(STAFF, cart, name="Marigold garland", detail="fresh", qty=Decimal("1"), unit_price_paise=3500)
        total = self.billing.totals(cart).total_paise
        return self.billing.checkout(STAFF, cart, [PaymentInput(mode="CASH", amount_paise=total, tendered_paise=10000)])


# ============================================================================ view-model
class ModelTest(Base):
    def test_receipt_barcode_round_trips(self) -> None:
        code = receipt_ean13("26-27", 123)
        self.assertEqual(len(code), 13)
        self.assertTrue(code.startswith("2826"))
        self.assertEqual(parse_receipt_ean13(code), ("26-27", 123))
        self.assertIsNone(parse_receipt_ean13("2900000000100"))

    def test_model_lines_totals_markers_and_payloads(self) -> None:
        self.settings.receipt.show_qr_upi = True
        self.settings.receipt.show_hindi_name = True
        self.catalog.update(OWNER, 10, row_version=self.catalog.get(10).row_version, changes={"name_hi": "कपूर"})  # type: ignore[union-attr]
        result = self._sale(lookup=True)
        model = self.receipts.build(result.sale)
        self.assertEqual(model.shop_name, "Vaishnavi Puja Store")
        self.assertEqual(model.invoice_no, "VPS/26-27/000003")
        self.assertEqual(model.cashier, "Ramesh")
        self.assertEqual(model.shift_label, "#1")
        self.assertEqual([ln.marker for ln in model.lines], ["", "", ""], "lookup / open-item flags are never printed for the customer (live run 5b)")
        camphor = model.lines[0]
        self.assertEqual((camphor.name, camphor.qty_text, camphor.price_text, camphor.discount_text, camphor.amount_text), ("Camphor 50g (कपूर)", "2 pcs", "10.00", "-1%", "20.00"), "item row shows the shelf amount …")
        self.assertEqual(camphor.discount_row, ("discount 1% (Diwali Offer)", "-0.20", "19.80"), "… the row beneath shows the discount and what was charged")
        self.assertEqual(camphor.note, "")
        self.assertEqual(model.lines[1].discount_row, ("discount 1% (Diwali Offer)", "-0.10", "9.90"))
        self.assertEqual(model.lines[2].name, "Marigold garland")
        self.assertEqual(model.lines[2].discount_text, "—")
        self.assertIsNone(model.lines[2].discount_row)
        text = renderers.text_lines(self.receipts.escpos(model))
        joined = "\n".join(text)
        self.assertRegex(joined, r"Camphor 50g.*20\.00\n\s+discount 1% \(Diwali Offer\)\s+-0\.20\s+= 19\.80", "thermal print: shelf amount, then discount row with the net")
        html = self.receipts.html(model)
        self.assertIn("<tr class='disc'><td colspan='3'>&nbsp;&nbsp;discount 1% (Diwali Offer)</td><td class='n'>-0.20</td><td class='n'>= 19.80</td></tr>", html)
        rows = {r.label: r.amount_text for r in model.total_rows}
        self.assertEqual(rows["Subtotal (shelf price)"], "65.00", "20 + 10 + 35")
        self.assertEqual(rows["Item discounts"], "-0.30")
        self.assertIn("Round-off", rows)
        self.assertEqual(model.total_text, "₹65.00", "64.70 rounds to 65")
        self.assertEqual(model.saved_text, "₹0.30")
        self.assertEqual(model.payment_lines, ["Paid: CASH ₹100.00  Change ₹35.00"])
        self.assertEqual(model.legend, "", "no '† label damaged – looked up' footnote on the customer's bill")
        self.assertNotIn("looked up", joined)
        self.assertNotIn("open item", joined)
        self.assertEqual(model.upi_payload, "upi://pay?pa=shop%40upi&pn=Vaishnavi+Puja+Store&am=65.00&cu=INR")
        self.assertEqual(model.barcode, receipt_ean13("26-27", 3))
        self.assertEqual(model.tax_rows, [], "unregistered shop → no tax lines")
        self.assertIsNone(model.copy_label)

    def test_tax_rows_when_registered_and_no_legend(self) -> None:
        self.settings.tax.registered = True
        self.settings.shop.gstin = "09ABCDE1234F1Z5"
        self.catalog.update(OWNER, 10, row_version=self.catalog.get(10).row_version, changes={"tax_rate_bp": 500})  # type: ignore[union-attr]
        result = self._sale(with_offer=False, open_item=False)
        model = self.receipts.build(result.sale, copy_label="DUPLICATE")
        self.assertEqual([(r.label, r.amount_text) for r in model.tax_rows], [("CGST @ 2.5%", "0.48"), ("SGST @ 2.5%", "0.47")])
        self.assertIn("GSTIN: 09ABCDE1234F1Z5", model.header_lines)
        self.assertEqual(model.legend, "")
        self.assertEqual(model.copy_label, "DUPLICATE")
        self.assertIsNone(model.upi_payload, "QR off by default")


# ============================================================================ renderers
class RenderTest(Base):
    def _model(self):  # type: ignore[no-untyped-def]
        self.settings.receipt.show_qr_upi = True
        return self.receipts.build(self._sale(lookup=True).sale)

    def test_escpos_80_and_58_are_well_formed_and_deterministic(self) -> None:
        model = self._model()
        for paper, width in ((80, 48), (58, 32)):
            data = self.receipts.escpos(model, paper_mm=paper)
            self.assertTrue(data.startswith(b"\x1b@"), "ESC @ initialise")
            self.assertTrue(data.endswith(b"\x1dV\x41\x03"), "partial cut with feed last")
            self.assertIn(b"VPS/26-27/000003", data)
            self.assertIn(b"\x1dk\x02" + model.barcode.encode() + b"\x00", data, "native EAN-13 of the receipt code")
            self.assertIn(b"\x1d(k" + bytes([len(model.upi_payload or "") + 3, 0]) + b"\x31\x50\x30" + (model.upi_payload or "").encode(), data, "QR store command with the UPI payload")
            self.assertNotIn(b"label damaged", data, "lookup flag is back-office information — never on the customer's paper")
            self.assertNotIn("†".encode("utf-8"), data)
            self.assertNotIn("\u20b9".encode("utf-8"), data, "rupee sign becomes Rs on thermal paper")
            self.assertEqual(data, self.receipts.escpos(model, paper_mm=paper), "deterministic")
            text_lines = renderers.text_lines(data)
            self.assertTrue(text_lines, "has printable lines")
            self.assertTrue(all(len(line) <= width for line in text_lines), f"{paper} mm: {[l for l in text_lines if len(l) > width]}")
            self.assertNotIn("कपूर", "".join(text_lines), "Devanagari is not sent as text without a raster hook")

    def test_escpos_golden_snapshot(self) -> None:
        model = self._model()
        GOLDEN.mkdir(exist_ok=True)
        for paper in (80, 58):
            data = self.receipts.escpos(model, paper_mm=paper)
            path = GOLDEN / f"receipt_{paper}.sha256"
            digest = hashlib.sha256(data).hexdigest()
            if not path.exists():
                path.write_text(digest + "\n", encoding="ascii")
                self.skipTest(f"golden {path.name} recorded")
            self.assertEqual(path.read_text(encoding="ascii").strip(), digest, f"{paper} mm byte stream changed — review the receipt layout, then update the golden file")

    def test_html_contains_everything_and_archive_writes_year_month_folder(self) -> None:
        model = self._model()
        html = self.receipts.html(model)
        for needle in ("Vaishnavi Puja Store", "VPS/26-27/000003", "Camphor 50g", "Marigold garland", "You saved", "<svg", "upi://pay?pa=shop%40upi", "@page"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("†", html)
        self.assertNotIn("open item", html)
        path = self.receipts.archive(model)
        self.assertEqual(path, self.paths.receipts / "2026" / "11" / "VPS-26-27-000003.html")
        self.assertTrue(path.exists())
        self.assertIn("Camphor 50g", path.read_text(encoding="utf-8"))
        self.assertEqual(self.receipts.archive(model), path, "idempotent")


# ============================================================================ print queue
class QueueTest(Base):
    def test_no_printer_archives_and_queues_then_prints_when_one_appears(self) -> None:
        result = self._sale()
        self.assertEqual(self.queue.pending_count(), 1, "SaleCommitted → after_sale → queued (no printer yet)")
        self.assertEqual(self._q("SELECT receipt_printed FROM sale WHERE id = ?", result.sale_id), [(0,)])
        self.assertTrue((self.paths.receipts / "2026" / "11" / "VPS-26-27-000003.html").exists(), "the PDF/HTML archive always happens")
        chip = self.queue.chip()
        self.assertEqual((chip.configured, chip.ready, chip.pending), (False, False, 1))
        self.settings.hardware = HardwareSettings(printer_kind="usb", printer_address="04B8:0202")
        self.status = PrinterStatus("usb", "04B8:0202", True, "ready")
        report = self.queue.print_pending(OWNER)
        self.assertEqual((report.printed, report.failed), (1, 0))
        self.assertEqual(len(self.transport.jobs), 1)
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self._q("SELECT receipt_printed, reprint_count FROM sale WHERE id = ?", result.sale_id), [(1, 0)])
        self.assertEqual(self.queue.chip().ready, True)

    def test_ready_printer_prints_on_commit_and_failure_queues(self) -> None:
        self.settings.hardware = HardwareSettings(printer_kind="network", printer_address="192.168.1.50:9100")
        self.status = PrinterStatus("network", "192.168.1.50:9100", True, "reachable")
        result = self._sale()
        self.assertEqual(len(self.transport.jobs), 1)
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self._q("SELECT receipt_printed FROM sale WHERE id = ?", result.sale_id), [(1,)])
        self.transport.fail = True
        cart = self.billing.new_cart(STAFF)
        self.billing.add_scan(STAFF, cart, CAMPHOR)
        second = self.billing.checkout(STAFF, cart, [PaymentInput(mode="UPI", amount_paise=1000)])
        self.assertEqual(self.queue.pending_count(), 1, "transport error → E-PRN-001 → queued, sale still committed")
        self.assertEqual(self._q("SELECT attempts FROM print_pending WHERE sale_id = ?", second.sale_id), [(1,)])
        self.transport.fail = False
        self.assertEqual(self.queue.print_pending(OWNER).printed, 1)

    def test_reprint_counts_marks_duplicate_and_limits_staff(self) -> None:
        self.settings.hardware = HardwareSettings(printer_kind="usb", printer_address="04B8:0202")
        self.status = PrinterStatus("usb", "04B8:0202", True, "ready")
        result = self._sale()
        self.queue.reprint(STAFF, result.sale_id)
        self.queue.reprint(STAFF, result.sale_id)
        self.assertEqual(self._q("SELECT reprint_count FROM sale WHERE id = ?", result.sale_id), [(2,)])
        self.assertIn(b"DUPLICATE", self.transport.jobs[-1])
        with self.assertRaises(AuthError) as cm:
            self.queue.reprint(STAFF, result.sale_id)
        self.assertEqual(cm.exception.code, "E-AUTH-011", "above reprint_limit → Owner PIN")
        self.queue.reprint(OWNER, result.sale_id)
        self.assertEqual(self._q("SELECT reprint_count FROM sale WHERE id = ?", result.sale_id), [(3,)])
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action = 'REPRINT'")]
        self.assertEqual(len(actions), 3)

    def test_find_sale_by_receipt_barcode(self) -> None:
        result = self._sale()
        found = self.billing.find_sale(receipt_ean13("26-27", 3))
        assert found is not None
        self.assertEqual(found.id, result.sale_id)
        self.assertIsNone(self.billing.find_sale(receipt_ean13("26-27", 99)))


if __name__ == "__main__":
    unittest.main()
