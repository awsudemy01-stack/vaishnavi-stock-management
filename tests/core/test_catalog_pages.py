"""Catalogue & stock pages (docs/09 §5–6, docs/13 Phase 2 unit 34): view-models for Products (grid, editor, price
history pop-up), Stock (overview, movements, adjust, purchase, stock-take), Labels and Settings → Hardware.
Written first (docs/13 rule 7). NiceGUI untouched.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.catalog import ProductInput
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import labels as labels_page
from vaishnavi.ui.pages import products as products_page
from vaishnavi.ui.pages import settings_hardware, stock as stock_page

from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)
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
        self.services = AppServices.build(self.settings, self.paths, root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=root / "config", started_at=T0)
        self.state.extra["services"] = self.services
        cat = next(c.id for c in self.services.catalog.categories() if c.name.startswith("Camphor"))
        self.p = self.services.catalog.create(OWNER, ProductInput(name="Camphor Tablets 100g", name_hi="कपूर की गोलियाँ", category_id=cat, price_paise=2500, mrp_paise=2800, cost_paise=1800, tax_rate_bp=500, min_stock=Decimal("5")))
        self.services.inventory.opening_stock(OWNER, self.p.id, Decimal("3"))

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()


class ProductsPageTest(Base):
    def test_grid_rows_carry_money_and_stock_texts_and_low_flag(self) -> None:
        model = products_page.build_grid(self.services, EN, query="", category_id=None, page=1, page_size=50)
        self.assertEqual(model.total, 2)
        row = next(r for r in model.rows if r.id == self.p.id)
        self.assertEqual(row.price_text, "₹25.00")
        self.assertEqual(row.stock_text, "3 pcs")
        self.assertTrue(row.low_stock)
        self.assertEqual(row.category, "Camphor & Ghee")
        self.assertEqual([c.name for c in model.categories][:1], ["Agarbatti & Dhoop"])
        filtered = products_page.build_grid(self.services, EN, query="tablets", category_id=None, page=1, page_size=50)
        self.assertEqual([r.id for r in filtered.rows], [self.p.id])

    def test_editor_model_and_save_create_update_with_validation(self) -> None:
        empty = products_page.build_editor(self.services, EN, product_id=None)
        self.assertIsNone(empty.product)
        self.assertEqual(empty.form["price"], "")
        self.assertIn("PIECE", [o[0] for o in empty.sale_modes])
        self.assertTrue(any(u["label"] == "100 g" for u in empty.units))
        problems = products_page.validate_form({**empty.form, "name": "", "price": "abc"})
        self.assertEqual(set(problems), {"name", "price"})
        saved = products_page.save_editor(self.services, OWNER, product_id=None, row_version=None, form={**empty.form, "name": "Ghee 200ml", "price": "120", "category_id": str(self.p.category_id), "sale_mode": "VOLUME", "unit": "ml", "min_stock": "2", "tags": "daily, puja"})
        self.assertTrue(saved.ok, saved.problems)
        created = self.services.catalog.get(saved.product_id)  # type: ignore[arg-type]
        self.assertEqual((created.price_paise, created.sale_mode, created.base_unit, created.allow_decimal_qty), (12000, "VOLUME", "ml", True))  # type: ignore[union-attr]
        editor = products_page.build_editor(self.services, EN, product_id=self.p.id)
        self.assertEqual(editor.form["price"], "25.00")
        self.assertEqual(editor.barcodes[0].kind, "INTERNAL")
        self.assertEqual(editor.history_points[-1][1], 2500)
        updated = products_page.save_editor(self.services, OWNER, product_id=self.p.id, row_version=self.p.row_version, form={**editor.form, "price": "27.50", "reason": "festival"})
        self.assertTrue(updated.ok)
        self.assertEqual(self.services.catalog.get(self.p.id).price_paise, 2750)  # type: ignore[union-attr]
        stale = products_page.save_editor(self.services, OWNER, product_id=self.p.id, row_version=self.p.row_version, form={**editor.form, "price": "1"})
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error_code, "E-CAT-010")

    def test_price_history_popup_windows(self) -> None:
        version = self.p.row_version
        for days, price in ((10, 2600), (200, 2700)):
            self.clock.advance(days=days - (self.clock.now() - T0).days)
            version = self.services.catalog.update(OWNER, self.p.id, row_version=version, changes={"price_paise": price}).row_version
        popup = products_page.build_price_history(self.services, EN, self.p.id, window="1m")
        self.assertEqual([r.new_text for r in popup.rows], ["₹27.00"])
        popup_year = products_page.build_price_history(self.services, EN, self.p.id, window="1y")
        self.assertEqual([r.new_text for r in popup_year.rows], ["₹27.00", "₹26.00", "₹25.00"])
        self.assertEqual(popup_year.chart["series"][0]["step"], "end")
        self.assertEqual(len(popup_year.chart["series"][0]["data"]), 3, "three changes; the closing point coincides with the last change made 'now'")
        custom = products_page.build_price_history(self.services, EN, self.p.id, since=T0 + timedelta(days=5), until=T0 + timedelta(days=50))
        self.assertEqual([r.new_text for r in custom.rows], ["₹26.00"])
        self.assertEqual(custom.window_label, "custom")


class StockPageTest(Base):
    def test_overview_movements_adjust_and_purchase_forms(self) -> None:
        overview = stock_page.build_overview(self.services, EN)
        self.assertEqual([r.id for r in overview.low_stock], [self.p.id])
        self.assertGreater(overview.valuation_paise, 0)
        self.assertEqual(overview.valuation_text[:1], "₹")
        timeline = stock_page.build_movements(self.services, EN, product_id=self.p.id)
        self.assertEqual([m.type for m in timeline.rows], ["OPENING"])
        self.assertEqual(timeline.rows[0].user_name, "Owner")
        result = stock_page.submit_adjustment(self.services, OWNER, product_id=self.p.id, qty_text="+4", reason="found in store room")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.services.inventory.stock_of(self.p.id), Decimal("7"))
        bad = stock_page.submit_adjustment(self.services, OWNER, product_id=self.p.id, qty_text="-100", reason="x")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error_code, "E-INV-003")
        denied = stock_page.submit_adjustment(self.services, STAFF, product_id=self.p.id, qty_text="-1", reason="x")
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error_code, "E-AUTH-010")
        purchase = stock_page.submit_purchase(self.services, OWNER, supplier="Sharma Traders", invoice_no="ST/7", lines=[{"product_id": self.p.id, "qty": "20", "unit_cost": "17.50"}])
        self.assertTrue(purchase.ok, purchase.message)
        self.assertEqual(purchase.label_count, 20)
        self.assertEqual(self.services.inventory.stock_of(self.p.id), Decimal("27"))
        self.assertEqual(self.services.catalog.get(self.p.id).cost_paise, 1750)  # type: ignore[union-attr]

    def test_stock_take_flow_model(self) -> None:
        count = stock_page.start_stock_take(self.services, OWNER, scope="FULL")
        model = stock_page.build_stock_take(self.services, EN, count_id=count.id)
        self.assertEqual(model.status, "DRAFT")
        self.assertEqual({l.product_id for l in model.lines}, {10, self.p.id})
        stock_page.submit_count(self.services, OWNER, count_id=count.id, product_id=self.p.id, counted_text="2")
        model = stock_page.build_stock_take(self.services, EN, count_id=count.id)
        line = next(l for l in model.lines if l.product_id == self.p.id)
        self.assertEqual(line.variance_text, "-1")
        self.assertEqual(line.variance_value_text, "-₹18.00")
        summary = stock_page.approve_stock_take(self.services, OWNER, count_id=count.id)
        self.assertTrue(summary.ok)
        self.assertEqual(self.services.inventory.stock_of(self.p.id), Decimal("2"))


class LabelsPageTest(Base):
    def test_labels_page_builds_job_and_history(self) -> None:
        model = labels_page.build_labels_model(self.services, EN, selected=[self.p.id, 10], copies=2, target="ESCPOS")
        self.assertEqual(len(model.preview), 2)
        self.assertEqual(model.preview[0].barcode, self.p.internal_barcode)
        self.assertEqual(model.total_labels, 4)
        self.assertTrue(model.printer_ready is False or model.printer_ready is True)
        job = labels_page.print_selected(self.services, OWNER, selected=[self.p.id], copies=1, target="PDF", reason="NEW")
        self.assertTrue(job.ok)
        self.assertIn(job.content_type.split(";")[0], ("text/html", "application/pdf"))
        history = labels_page.build_labels_model(self.services, EN, selected=[], copies=1, target="ESCPOS").history
        self.assertEqual(history[0]["sku"], self.p.sku)


class HardwarePageTest(Base):
    def test_printer_list_selection_and_test_print_payloads(self) -> None:
        fake = [settings_hardware.PrinterOption(kind="system", address="EPSON TM-T82", label="EPSON TM-T82 (system)", ready=True, detail="")]
        with mock.patch.object(settings_hardware, "discover_printers", lambda settings: fake):
            model = settings_hardware.build_hardware_model(self.state, EN)
        self.assertEqual(model.printers[0].address, "EPSON TM-T82")
        self.assertEqual(model.selected_printer.kind, "none")
        self.assertFalse(model.scanner.paired)
        result = settings_hardware.select_printer(self.state, OWNER, kind="system", address="EPSON TM-T82", paper_mm=80)
        self.assertTrue(result.ok, result.message)
        effective = self.services.settings_service.effective()
        self.assertEqual((effective.hardware.printer_kind, effective.hardware.printer_address), ("system", "EPSON TM-T82"))
        sample = settings_hardware.test_print_payload(self.state, paper_mm=80)
        self.assertTrue(sample.startswith(b"\x1b@"))
        self.assertIn(b"Vaishnavi Puja Store", sample)
        self.assertIn(b"\x1dk\x02", sample, "the test print includes a sample label with a barcode")
        self.assertIn(b"\x1dV", sample)
        denied = settings_hardware.select_printer(self.state, STAFF, kind="none", address="", paper_mm=80)
        self.assertFalse(denied.ok)

    def test_scanner_pairing_model(self) -> None:
        with mock.patch.object(settings_hardware, "list_usb_devices", lambda: [settings_hardware.UsbOption(vid_pid="0C2E:0B61", name="Honeywell Voyager")]):
            model = settings_hardware.build_hardware_model(self.state, EN)
        self.assertEqual(model.usb_devices[0].vid_pid, "0C2E:0B61")
        result = settings_hardware.pair_scanner(self.state, OWNER, vid_pid="0c2e:0b61")
        self.assertTrue(result.ok)
        self.assertEqual(self.services.settings_service.effective().hardware.scanner_vid_pid, "0C2E:0B61")
        with mock.patch.object(settings_hardware, "list_usb_devices", lambda: []):
            model = settings_hardware.build_hardware_model(self.state, EN)
        self.assertTrue(model.scanner.paired)
        self.assertFalse(model.scanner.present)

    def test_require_printer_toggle_and_overrides(self) -> None:
        from vaishnavi.platform.printing import PrinterStatus

        self.assertFalse(self.services.settings_service.effective().billing.require_printer)
        refused = settings_hardware.set_require_printer(self.state, OWNER, on=True)
        self.assertEqual((refused.ok, refused.error_code), (False, "E-PRN-002"), "no ready printer → the switch stays off")
        settings_hardware.select_printer(self.state, OWNER, kind="file", address=str(self.paths.primary / "printer.bin"), paper_mm=80)
        with mock.patch.object(self.services.hardware, "_printer_status", lambda hw: PrinterStatus("file", hw.printer_address, True, "ok")):
            allowed = settings_hardware.set_require_printer(self.state, OWNER, on=True)
        self.assertTrue(allowed.ok)
        self.assertTrue(self.services.settings_service.effective().billing.require_printer)
        self.assertTrue(settings_hardware.set_require_printer(self.state, OWNER, on=False).ok, "switching off never needs a printer")
        model = settings_hardware.build_override_model(self.state, EN)
        self.assertEqual([o.kind for o in model.active], [])
        started = settings_hardware.start_override(self.state, OWNER, kind="PRINTER", minutes=30, reason="paper roll finished")
        self.assertTrue(started.ok)
        model = settings_hardware.build_override_model(self.state, EN)
        self.assertEqual([(o.kind, o.reason) for o in model.active], [("PRINTER", "paper roll finished")])
        self.assertTrue(model.active[0].ends_text)
        self.assertFalse(settings_hardware.start_override(self.state, STAFF, kind="SCANNER", minutes=10, reason="x").ok)
        self.assertTrue(settings_hardware.end_override(self.state, OWNER, kind="PRINTER").ok)
        self.assertEqual(settings_hardware.build_override_model(self.state, EN).active, [])


if __name__ == "__main__":
    unittest.main()
