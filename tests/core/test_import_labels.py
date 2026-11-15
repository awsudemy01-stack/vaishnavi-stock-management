"""CSV import/export + labels (docs/11 §2, docs/13 Phase 2 unit 33): ImportExportService, EAN-13 module encoding,
ESC/POS label strip, A4 HTML sheet, label print log. Written first (docs/13 rule 7).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteCatalogStore, SqliteInventoryStore, SqliteLabelStore, SqliteTxn
from vaishnavi.hardware import labels
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.import_export import ImportExportService
from vaishnavi.services.inventory import InventoryService
from vaishnavi.services.labels import LabelService
from vaishnavi.services.price_history import PriceHistoryService

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)

CSV = """sku,name,name_hi,category,price,mrp,cost,tax_rate,unit,barcode,opening_stock,min_stock,tags
,Camphor Tablets 100g,कपूर की गोलियाँ,Camphor & Ghee,25.00,28,18.50,5,pkt,,40,5,diwali;daily
AGR-0007,Mogra Agarbatti 20,मोगरा अगरबत्ती,Agarbatti & Dhoop,30,,22,12,pkt,8901030865275,120,10,
,Broken Row,,Camphor & Ghee,abc,,,5,pc,,1,0,
,No Category Item,,Unknown Category,10,,,0,pc,,0,0,
"""


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
        store = SqliteCatalogStore()
        self.catalog = CatalogService(self.txn, store, self.audit, event_bus=self.bus)
        self.prices = PriceHistoryService(self.txn, store)
        self.inventory = InventoryService(self.txn, SqliteInventoryStore(), store, self.audit, event_bus=self.bus)
        self.io = ImportExportService(self.catalog, self.inventory)
        self.labels = LabelService(self.txn, SqliteLabelStore(), self.audit, products=store, renderer=labels)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class ImportTest(Base):
    def test_preview_lists_errors_per_line_and_apply_creates_products_with_history_and_stock(self) -> None:
        preview = self.io.preview_csv(CSV)
        self.assertEqual(preview.total, 4)
        self.assertEqual(preview.ok_count, 2)
        errors = {row.line_no: row.errors for row in preview.rows if row.errors}
        self.assertIn(4, errors)
        self.assertTrue(any("price" in e for e in errors[4]))
        self.assertIn(5, errors)
        self.assertTrue(any("category" in e.lower() for e in errors[5]))
        result = self.io.apply(OWNER, preview)
        self.assertEqual((result.created, result.updated, result.skipped), (2, 0, 2))
        camphor = self.catalog.by_sku("CAM-0002")
        self.assertIsNotNone(camphor, "auto code from the category when the sku column is empty")
        self.assertEqual((camphor.price_paise, camphor.mrp_paise, camphor.cost_paise, camphor.tax_rate_bp), (2500, 2800, 1850, 500))  # type: ignore[union-attr]
        self.assertEqual(camphor.name_hi, "कपूर की गोलियाँ")  # type: ignore[union-attr]
        self.assertEqual(self.inventory.stock_of(camphor.id), Decimal("40"))  # type: ignore[union-attr]
        self.assertEqual(self.catalog.tags_of(camphor.id), ["daily", "diwali"])  # type: ignore[union-attr]
        agar = self.catalog.by_sku("AGR-0007")
        self.assertEqual(self.catalog.resolve_barcode("8901030865275").product.id, agar.id, "manufacturer barcode attached")  # type: ignore[union-attr]
        self.assertTrue(all(h.source == "IMPORT" for h in self.prices.history(camphor.id)))  # type: ignore[union-attr]
        with self.assertRaises(PermissionDenied):
            self.io.apply(STAFF, preview)

    def test_reimport_updates_prices_by_sku_and_records_the_change(self) -> None:
        self.io.apply(OWNER, self.io.preview_csv(CSV))
        before = self.catalog.by_sku("AGR-0007")
        self.clock.advance(days=1)
        again = "sku,name,price,tax_rate\nAGR-0007,Mogra Agarbatti 20,35,12\n"
        result = self.io.apply(OWNER, self.io.preview_csv(again))
        self.assertEqual((result.created, result.updated), (0, 1))
        after = self.catalog.by_sku("AGR-0007")
        self.assertEqual(after.price_paise, 3500)  # type: ignore[union-attr]
        self.assertEqual(self.inventory.stock_of(after.id), Decimal("120"), "a re-import never touches stock")  # type: ignore[union-attr]
        hist = self.prices.history(after.id, field="PRICE")  # type: ignore[union-attr]
        self.assertEqual((hist[0].old_value, hist[0].new_value, hist[0].source), (3000, 3500, "IMPORT"))
        self.assertEqual(before.row_version + 1, after.row_version)  # type: ignore[union-attr]

    def test_export_round_trips(self) -> None:
        self.io.apply(OWNER, self.io.preview_csv(CSV))
        text = self.io.export_csv()
        self.assertTrue(text.startswith("\ufeff"), "UTF-8 BOM so Excel shows Hindi correctly")
        preview = self.io.preview_csv(text)
        self.assertEqual(preview.error_count, 0)
        self.assertIn("कपूर की गोलियाँ", text)
        self.assertIn(self.catalog.by_sku("CAM-0002").internal_barcode, text, "internal barcodes are exported too")  # type: ignore[union-attr]
        header = text.lstrip("\ufeff").splitlines()[0]
        for col in ("sku", "name", "name_hi", "category", "price", "tax_rate", "barcode", "stock"):
            self.assertIn(col, header)


class LabelRenderTest(unittest.TestCase):
    def test_ean13_module_pattern_is_95_modules_with_correct_guards(self) -> None:
        mods = labels.ean13_modules("2900000010420")
        self.assertEqual(len(mods), 95)
        self.assertTrue(mods.startswith("101") and mods.endswith("101"))
        self.assertEqual(mods[45:50], "01010", "centre guard")
        self.assertEqual(labels.ean13_modules("4006381333931")[:10], "1010001101", "after the left guard, the second digit 0 is L-encoded as 0001101 (first digit 4 only sets the parity pattern)")
        with self.assertRaises(ValueError):
            labels.ean13_modules("123")

    def test_escpos_strip_contains_native_barcode_text_and_cut(self) -> None:
        model = labels.LabelModel(name="Camphor Tablets 100g", name_hi="कपूर की गोलियाँ", sku="CAM-0002", product_id=1042, barcode="2900000010420", price_paise=2500, mrp_paise=2800)
        data = labels.render_escpos(model, paper_mm=80)
        self.assertTrue(data.startswith(b"\x1b@"), "initialise")
        self.assertIn(b"\x1dk\x02", data, "GS k 2 = EAN-13 (function A, NUL-terminated)")
        self.assertIn(b"2900000010420\x00", data)
        self.assertIn("CAM-0002 · #1042".encode("utf-8"), data)
        self.assertIn("\u20b925.00".encode("utf-8"), data)
        self.assertIn(b"\x1dV", data, "cut")
        narrow = labels.render_escpos(model, paper_mm=58)
        self.assertLess(len(narrow), len(data) + 64)
        self.assertIn(b"\x1dw\x02", narrow, "narrower bars on 58 mm")

    def test_html_sheet_grid_and_svg_barcodes(self) -> None:
        model = labels.LabelModel(name="Diya", name_hi=None, sku="DEC-0001", product_id=7, barcode="2900000000070", price_paise=500, mrp_paise=None)
        html = labels.render_html_sheet([model] * 70, layout="65-up")
        self.assertEqual(html.count("<svg"), 70)
        self.assertIn("page-break", html, "a second sheet starts after 65 labels")
        self.assertIn("2900000000070", html)
        self.assertIn("@page", html)
        self.assertIn("#7", html)
        forty = labels.render_html_sheet([model], layout="40-up")
        self.assertIn("52.5mm", forty)


class LabelServiceTest(Base):
    def test_labels_for_products_and_print_log(self) -> None:
        p = self.catalog.create(OWNER, ProductInput(name="Rudraksha Mala", name_hi="रुद्राक्ष माला", category_id=None, price_paise=35000, mrp_paise=40000))
        models = self.labels.models_for([p.id, 10], copies=2)
        self.assertEqual(len(models), 4)
        self.assertEqual(models[0].barcode, p.internal_barcode)
        self.assertEqual(models[0].price_paise, 35000)
        job = self.labels.print_job(OWNER, product_ids=[p.id], copies=3, target="ESCPOS", reason="NEW", paper_mm=80)
        self.assertEqual(job.copies, 3)
        self.assertIn(b"\x1dk\x02", job.payload)
        self.assertEqual(self._q("SELECT product_id, copies, target, reason, user_id FROM label_print_log")[0], (p.id, 3, "ESCPOS", "NEW", 1))
        sheet = self.labels.print_job(OWNER, product_ids=[p.id, 10], copies=1, target="PDF", reason="RESTOCK")
        self.assertTrue(sheet.payload.startswith(b"<!DOCTYPE html") or sheet.payload.startswith(b"%PDF"), "PDF via reportlab when available, else the printable HTML sheet")
        replacement = self.labels.print_job(STAFF, product_ids=[10], copies=1, target="ESCPOS", reason="REPLACEMENT")
        self.assertEqual(replacement.copies, 1, "staff may print a replacement label (PRINT_LABEL)")
        with self.assertRaises(PermissionDenied):
            self.labels.print_job(STAFF, product_ids=[10], copies=5, target="ESCPOS", reason="NEW")
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action LIKE 'LABEL%'")]
        self.assertEqual(actions.count("LABEL_PRINTED"), 3, "logged per product per job: 1 + 2")
        self.assertEqual(actions.count("LABEL_REPRINTED"), 1)
        history = self.labels.recent(limit=10)
        self.assertEqual(len(history), 4)


if __name__ == "__main__":
    unittest.main()
