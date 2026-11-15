"""Unit 53 — Day-close wizard (docs/05 §12), purchase-order document, XLSX / PDF export of any report."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services import export as export_svc
from vaishnavi.services.billing import PaymentInput
from vaishnavi.services.catalog import ProductInput
from vaishnavi.services.inventory import PurchaseLine
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import day_close
from vaishnavi.ui.pages import reports as reports_page

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = AppPaths(self.root / "p", self.root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.c = AppServices.build(self.settings, self.paths, self.root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=self.root / "config", started_at=T0)
        self.state.extra["services"] = self.c
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _sale(self, actor=STAFF, qty: str = "2"):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        self.c.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput("CASH", total, tendered_paise=total)])


class ExportTest(unittest.TestCase):
    def test_xlsx_is_a_valid_minimal_workbook(self) -> None:
        blob = export_svc.to_xlsx("Sales", ["day", "bills", "total"], [["2026-11-14", 3, "₹60.00"], ["2026-11-15", 0, "₹0.00"]])
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            self.assertTrue({"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml", "_rels/.rels", "xl/_rels/workbook.xml.rels"} <= names)
            sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("2026-11-14", sheet)
            self.assertIn("₹60.00", sheet)
            self.assertIn('<c r="B2"', sheet)
            self.assertIn('t="n"', sheet, "integers are numeric cells")
            self.assertIn("<sheet name=\"Sales\"", zf.read("xl/workbook.xml").decode("utf-8"))
        self.assertEqual(export_svc.safe_sheet_name("Sales / by day: 2026?"), "Sales  by day 2026")


class ReportExportsTest(Base):
    def test_xlsx_and_document_export_of_a_report_and_send(self) -> None:
        self.c.shifts.open(OWNER, opening_float_paise=0)
        self._sale()
        blob = reports_page.export_xlsx(self.c, EN, OWNER, "sales")
        assert blob is not None
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertIn("₹60.00", zf.read("xl/worksheets/sheet1.xml").decode("utf-8"))
        self.assertIsNone(reports_page.export_xlsx(self.c, EN, STAFF, "sales"))
        path = reports_page.export_document(self.c, EN, OWNER, "sales")
        assert path is not None
        self.assertTrue(path.exists())
        self.assertIn("Sales by day", path.read_text(encoding="utf-8"))
        self.c.settings_service.set_many(OWNER, {"publishing.enabled": True, "publishing.channels": ["folder"], "publishing.folder": str(self.root / "sync")})
        result = reports_page.send_document(self.c, EN, OWNER, "sales")
        assert result is not None
        self.assertEqual(result.sent, ["folder"])
        self.assertTrue(list((self.root / "sync" / "2026-11").glob("report-sales-*.html")))


class PurchaseOrderTest(Base):
    def test_po_groups_reorder_lines_by_last_supplier(self) -> None:
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee 500g", price_paise=30000, min_stock=Decimal("10")))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("2"))
        self.c.inventory.record_purchase(OWNER, supplier_name="Sharma Traders", invoice_no="ST/1", lines=[PurchaseLine(ghee.id, Decimal("1"), 25000)])
        self.c.catalog.update(OWNER, 10, row_version=self.c.catalog.get(10).row_version, changes={"min_stock": Decimal("500")})  # type: ignore[union-attr]
        po = self.c.purchase_orders.build(OWNER)
        suppliers = {g.supplier: [(line.name, line.qty_text) for line in g.lines] for g in po.groups}
        self.assertEqual(suppliers["Sharma Traders"], [("Ghee 500g", "7")], "min 10 − stock 3 = 7, last bought from Sharma")
        self.assertEqual(suppliers["Unassigned"], [("Camphor 50g", "404")])
        self.assertEqual(po.total_lines, 2)
        rows = po.rows()
        self.assertEqual(rows[0][:2], ("Sharma Traders", "Ghee 500g"))
        self.assertIn("₹", rows[0][-1], "estimated cost from the last purchase price")
        path = self.c.purchase_orders.document(OWNER)
        self.assertTrue(path.exists())
        self.assertIn("Purchase order", path.read_text(encoding="utf-8"))
        self.assertIn("Sharma Traders", path.read_text(encoding="utf-8"))


class DayCloseTest(Base):
    def test_wizard_steps_close_backup_digest_and_finish(self) -> None:
        self.c.shifts.open(STAFF, opening_float_paise=0) if self.c.shifts.current(2) is None else None
        self._sale()
        m = day_close.build_day_close(self.c, self.state, EN, OWNER)
        self.assertEqual([s.key for s in m.steps], ["shifts", "zreport", "backup", "digest", "finish"])
        shifts_step = m.steps[0]
        self.assertFalse(shifts_step.done)
        self.assertEqual([s.id for s in m.open_shifts], [1])
        self.assertIn("Ramesh", m.open_shifts[0].cashier)
        out = day_close.close_shift(self.c, OWNER, 1, counted_text="20.00", note="day close")
        self.assertTrue(out.ok, out.message)
        m = day_close.build_day_close(self.c, self.state, EN, OWNER)
        self.assertTrue(m.steps[0].done)
        self.assertTrue(m.z_reports)
        self.assertTrue(any("CHAIN" in line.upper() or "HASH" in line.upper() for line in m.z_reports[0].lines), "Z report carries the chain head")
        self.assertFalse(m.steps[2].done, "no backup yet today")
        out = day_close.run_backup(self.c, OWNER)
        self.assertTrue(out.ok, out.message)
        m = day_close.build_day_close(self.c, self.state, EN, OWNER)
        self.assertTrue(m.steps[2].done)
        self.assertFalse(m.steps[3].done)
        out = day_close.send_digest(self.c, OWNER, self.state)
        self.assertTrue(out.ok, out.message)
        m = day_close.build_day_close(self.c, self.state, EN, OWNER)
        self.assertTrue(m.steps[3].done, "digest written (publishing off → local file only)")
        self.assertTrue(m.digest_path and Path(m.digest_path).exists())
        out = day_close.finish(self.c, self.state, OWNER, shutdown=True)
        self.assertTrue(out.ok)
        self.assertTrue(self.state.extra.get("shutdown_requested"))
        self.assertEqual(self.c.reports.audit_trail(OWNER, limit=5)[0].action, "DAY_CLOSED")

    def test_staff_cannot_run_the_wizard(self) -> None:
        out = day_close.run_backup(self.c, STAFF)
        self.assertFalse(out.ok)
        self.assertEqual(out.error_code, "E-AUTH-010")


if __name__ == "__main__":
    unittest.main()
