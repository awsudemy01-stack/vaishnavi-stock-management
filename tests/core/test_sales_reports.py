"""Unit 45 — sales-side reports (docs/05 §9): sales by day, item history, top sellers / dead stock, low stock, purchases, profit & margin, hourly heatmap, stock as of a day."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import PermissionDenied
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.billing import PaymentInput
from vaishnavi.services.catalog import ProductInput
from vaishnavi.services.inventory import PurchaseLine
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import reports as reports_page

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")
DAY = "2026-11-14"  # T0 in shop time


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # two CASH ₹20 bills on DAY by staff, Camphor stock 96
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.c = AppServices.build(self.settings, self.paths, root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=root / "config", started_at=T0)
        self.state.extra["services"] = self.c
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})
        self.c.shifts.open(OWNER, opening_float_paise=0)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _sale(self, actor=STAFF, *, qty: str = "2", mode: str = "CASH", product_code: str = CAMPHOR):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        self.c.billing.add_scan(actor, cart, product_code, qty=Decimal(qty))
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput(mode, total, tendered_paise=total if mode == "CASH" else None)])


class SalesReportsTest(Base):
    def test_sales_by_day_and_summary_reconcile_with_sql(self) -> None:
        self._sale(mode="UPI")
        self.clock.advance(days=1)
        self._sale(OWNER, qty="1")
        voided = self._sale(OWNER, qty="1")
        self.c.billing.void(OWNER, voided.sale_id, reason="test")
        rows = self.c.reports.sales_by_day(OWNER)
        self.assertEqual([(r.day, r.bills, r.total_paise, r.cash_paise, r.upi_paise, r.refunds_paise) for r in rows], [("2026-11-14", 3, 6000, 4000, 2000, 0), ("2026-11-15", 2, 2000, 2000, 0, 1000)], "credit notes appear as refunds on their own day, bills count completed sales only")
        summary = self.c.reports.sales_summary(OWNER)
        self.assertEqual((summary.bills, summary.total_paise, summary.refunds_paise, summary.net_paise, summary.items), (5, 8000, 1000, 7000, Decimal("8")))
        self.assertEqual(summary.avg_bill_paise, 1600)
        self.assertEqual(summary.by_mode, {"CASH": 6000, "UPI": 2000})
        with self.assertRaises(PermissionDenied):
            self.c.reports.sales_by_day(STAFF)
        only_today = self.c.reports.sales_by_day(OWNER, since="2026-11-15", until="2026-11-15")
        self.assertEqual([r.day for r in only_today], ["2026-11-15"], "date-only bounds are shop days")

    def test_item_history_top_sellers_dead_stock_and_low_stock(self) -> None:
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee 500g", price_paise=30000, min_stock=Decimal("5")))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("6"))
        dead = self.c.catalog.create(OWNER, ProductInput(name="Old Idol", price_paise=50000))
        self.c.inventory.opening_stock(OWNER, dead.id, Decimal("3"))
        from vaishnavi.core.barcode import internal_ean13

        self._sale(product_code=internal_ean13(ghee.id), qty="2")
        self._sale(qty="5")
        hist = self.c.reports.item_sales(OWNER, 10)
        self.assertEqual([(r.day, str(r.qty), r.amount_paise) for r in hist], [(DAY, "9", 9000)], "2 + 2 fixture + 5")
        top_qty = self.c.reports.top_sellers(OWNER, limit=5)
        self.assertEqual([(t.product_id, str(t.qty)) for t in top_qty], [(10, "9"), (ghee.id, "2")])
        top_amount = self.c.reports.top_sellers(OWNER, limit=5, by="amount")
        self.assertEqual([t.product_id for t in top_amount], [ghee.id, 10], "₹600 of ghee beats ₹90 of camphor")
        self.assertEqual(top_amount[0].share_pct, 87, "600 / 690")
        dead_rows = self.c.reports.dead_stock(OWNER, days=30)
        self.assertEqual([(d.product_id, str(d.stock_qty), d.value_paise) for d in dead_rows], [(dead.id, "3", 150000)])
        low = self.c.reports.low_stock(STAFF)
        self.assertEqual([(lo.product_id, str(lo.stock_qty), str(lo.min_stock)) for lo in low], [(ghee.id, "4", "5")], "staff may see low stock")

    def test_purchases_profit_margin_heatmap_and_stock_as_of(self) -> None:
        self.c.inventory.record_purchase(OWNER, supplier_name="Sharma Traders", invoice_no="ST/1", lines=[PurchaseLine(10, Decimal("50"), 600)])
        self.c.inventory.record_purchase(OWNER, supplier_name="Gupta & Sons", invoice_no="GS/9", lines=[PurchaseLine(10, Decimal("10"), 650)])
        purchases = self.c.reports.purchases(OWNER)
        self.assertEqual([(p.supplier, p.purchases, p.total_paise) for p in purchases], [("Sharma Traders", 1, 30000), ("Gupta & Sons", 1, 6500)])
        self.assertEqual(self.c.reports.purchases_by_day(OWNER), [(DAY, 2, 36500)])
        self._sale(qty="4")  # cost snapshot is now 650 per unit
        profit = self.c.reports.profit(OWNER)
        row = next(p for p in profit.rows if p.product_id == 10)
        self.assertEqual((str(row.qty), row.revenue_paise, row.cost_paise, row.profit_paise), ("4", 4000, 2600, 1400), "fixture lines have no cost snapshot and are excluded from the margin")
        self.assertEqual(row.margin_pct, 35)
        self.assertEqual(profit.unknown_cost_lines, 2)
        self.assertEqual((profit.revenue_paise, profit.profit_paise), (4000, 1400))
        with self.assertRaises(PermissionDenied):
            self.c.reports.profit(STAFF)
        heat = self.c.reports.hourly_heatmap(OWNER)
        self.assertEqual(sum(heat.values()), 3, "three completed bills")
        self.assertEqual(heat[(5, 21)], 3, "Saturday 14-Nov-2026 21:xx IST (weekday 5 = Sat)")
        self.c.inventory.write_daily_balance()
        asof = self.c.reports.stock_as_of(OWNER, DAY)
        self.assertEqual([(s.product_id, str(s.qty_close)) for s in asof], [(10, "152")], "96 + 50 + 10 − 4")
        self.assertEqual(self.c.reports.stock_as_of(OWNER, "2026-11-01"), [], "no balance row for a day before the data starts")

    def test_basket_pairs_and_export_everything_zip(self) -> None:
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee 500g", price_paise=30000))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("6"))
        wick = self.c.catalog.create(OWNER, ProductInput(name="Cotton wicks", price_paise=1500))
        self.c.inventory.opening_stock(OWNER, wick.id, Decimal("20"))
        from vaishnavi.core.barcode import internal_ean13

        for _ in range(2):  # two baskets with camphor + ghee + wicks, one with camphor + wicks
            cart = self.c.billing.new_cart(STAFF)
            self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal(1))
            self.c.billing.add_scan(STAFF, cart, internal_ean13(ghee.id), qty=Decimal(1))
            self.c.billing.add_scan(STAFF, cart, internal_ean13(wick.id), qty=Decimal(1))
            total = self.c.billing.totals(cart).total_paise
            self.c.billing.checkout(STAFF, cart, [PaymentInput("CASH", total, tendered_paise=total)])
        cart = self.c.billing.new_cart(STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal(1))
        self.c.billing.add_scan(STAFF, cart, internal_ean13(wick.id), qty=Decimal(3))
        total = self.c.billing.totals(cart).total_paise
        self.c.billing.checkout(STAFF, cart, [PaymentInput("CASH", total, tendered_paise=total)])
        pairs = self.c.reports.basket_pairs(OWNER, limit=10)
        self.assertEqual([(p.name_a, p.name_b, p.baskets) for p in pairs], [("Camphor 50g", "Cotton wicks", 3), ("Camphor 50g", "Ghee 500g", 2), ("Ghee 500g", "Cotton wicks", 2)], "pairs counted once per bill regardless of quantity; lower product id first so a pair never appears twice")
        self.assertEqual(pairs[0].share_pct, 60, "3 of 5 completed bills (2 fixture bills had camphor only)")
        with self.assertRaises(PermissionDenied):
            self.c.reports.basket_pairs(STAFF)
        self.assertIn("basket", [r.key for r in reports_page.build_index(self.c, EN, OWNER).reports])
        import io
        import zipfile

        blob = reports_page.export_all_zip(self.c, OWNER)
        assert blob is not None
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            self.assertIn("sales.csv", names)
            self.assertIn("basket.csv", names)
            self.assertIn("forecast.csv", names)
            self.assertIn("README.txt", names)
            sales_csv = zf.read("sales.csv").decode("utf-8-sig")
            self.assertTrue(sales_csv.startswith("Vaishnavi Puja Store"), "every export opens with the shop letterhead")
            self.assertIn("\nday,", sales_csv, "the column header follows the letterhead and a blank row")
            self.assertTrue(zf.read("README.txt").decode("utf-8").startswith("Vaishnavi Puja Store"))
        self.assertIsNone(reports_page.export_all_zip(self.c, STAFF), "staff cannot take the shop's figures home")

    def test_report_pages_registry_gained_sales_reports(self) -> None:
        self._sale()
        keys = [r.key for r in reports_page.build_index(self.c, EN, OWNER).reports]
        for k in ("sales", "items", "top", "dead", "low_stock", "purchases", "profit", "heatmap"):
            self.assertIn(k, keys)
        self.assertIn("low_stock", [r.key for r in reports_page.build_index(self.c, EN, STAFF).reports])
        table = reports_page.build_table(self.c, EN, OWNER, "sales")
        assert table is not None
        self.assertEqual(table.title, "Sales by month — Nov-2026", "defaults to the current month")
        self.assertEqual(table.rows[0]["day"], DAY)
        self.assertEqual(table.rows[0]["total"], "₹60.00")
        self.assertEqual((table.rows[-1]["day"], table.rows[-1]["total"], table.rows[-1]["bills"]), ("TOTAL · Nov-2026", "₹60.00", 3), "the month closes with a total row")
        other = reports_page.build_table(self.c, EN, OWNER, "sales", {"month": "2026-10"})
        assert other is not None
        self.assertEqual((other.title, other.rows), ("Sales by month — Oct-2026", []))
        self.assertEqual(reports_page.month_window({"month": "2026-02"}), ("2026-02-01", "2026-02-28", "Feb-2026"))
        self.assertIsNone(reports_page.build_table(self.c, EN, STAFF, "profit"))
        self.assertTrue(reports_page.export_csv(self.c, OWNER, "top").startswith("\ufeff"))
        # Phase 5 forecast entries resolve through the real services; too little history => NEW/low row, never an error.
        plan = reports_page.build_table(self.c, EN, OWNER, "forecast")
        assert plan is not None
        camphor = next(r for r in plan.rows if r["product"].startswith("#10 "))
        self.assertEqual((camphor["reorder"], camphor["confidence"], camphor["rate"]), ("0", "low", ""))
        self.assertEqual(plan.column_labels["cover"], "Days of cover")
        demand = reports_page.build_table(self.c, EN, OWNER, "demand")
        assert demand is not None
        self.assertEqual(next(r for r in demand.rows if r["product"].startswith("#10 "))["class"], "NEW")
        self.assertIsNone(reports_page.build_table(self.c, EN, STAFF, "forecast"))

    def test_sales_by_date_lists_quantity_per_item_in_its_unit_then_the_days_money(self) -> None:
        """Owner's example: Item · Quantity (Match Box 10 · Ghee 200 g · Oil 100 ml), then Total sale / Cash / UPI — for any calendar day."""
        from vaishnavi.core.barcode import internal_ean13

        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee", price_paise=50000, sale_mode="WEIGHT", base_unit="g", pricing_unit_qty=Decimal("1000"), pricing_unit_label="kg", min_qty=Decimal("10"), qty_step=Decimal("10"), allow_decimal_qty=True))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("5000"))
        self._sale(product_code=internal_ean13(ghee.id), qty="200", mode="UPI")  # 200 g at ₹500/kg = ₹100
        self._sale(qty="3")  # 3 camphor, cash ₹30
        self._sale(qty="7", mode="UPI")  # 7 camphor, UPI ₹70
        rep = self.c.reports.day_sales(OWNER, DAY)
        by_name = {r.name: r for r in rep.items}
        self.assertEqual((by_name["Ghee"].qty_text, by_name["Ghee"].amount_paise, by_name["Ghee"].bills), ("200 g", 10000, 1))
        self.assertEqual((by_name["Camphor 50g"].qty_text, by_name["Camphor 50g"].amount_paise, by_name["Camphor 50g"].bills), ("14 pcs", 14000, 4), "2 + 2 fixture bills + 3 + 7")
        self.assertEqual((rep.summary.total_paise, rep.summary.by_mode), (24000, {"CASH": 7000, "UPI": 17000}))
        self.assertEqual(self.c.reports.day_sales(OWNER, "2026-11-13").items, [])
        with self.assertRaises(PermissionDenied):
            self.c.reports.day_sales(STAFF, DAY)
        table = reports_page.build_table(self.c, EN, OWNER, "sales_date", {"day": DAY})
        assert table is not None
        self.assertEqual(table.title, "Day Sales Report — 14-Nov-2026")
        self.assertEqual(table.columns, ["item", "qty", "bills", "amount"])
        self.assertEqual([(r["item"], r["qty"]) for r in table.rows[:2]], [("Camphor 50g", "14 pcs"), ("Ghee", "200 g")], "biggest rupees first")
        self.assertEqual([(r["item"], r["amount"]) for r in table.rows[2:]], [("TOTAL SALE", "₹240.00"), ("Cash", "₹70.00"), ("UPI", "₹170.00")])
        self.assertEqual(reports_page.build_table(self.c, EN, OWNER, "sales_date", {"day": "2026-11-13"}).rows[0]["item"], "TOTAL SALE")  # type: ignore[union-attr]
        self.assertEqual(reports_page.date_window({"day": "not a date"}), DAY, "a bad date falls back to today")
        self.assertIsNone(reports_page.build_table(self.c, EN, STAFF, "sales_date", {"day": DAY}))
        self.assertIn("sales_date", [r.key for r in reports_page.build_index(self.c, EN, OWNER).reports])

    def test_investment_counts_every_unit_that_came_in_at_the_cost_of_that_day(self) -> None:
        """"How much have I put into the shop so far" — opening stock + purchases + corrections × cost, from day one."""
        ghee = self.c.catalog.create(OWNER, ProductInput(name="Ghee 500g", price_paise=30000, cost_paise=24000, category_id=1))
        self.c.inventory.opening_stock(OWNER, ghee.id, Decimal("10"))  # 10 × ₹240 = ₹2,400 at today's cost
        self.assertEqual(self._q_movement_cost(ghee.id), 24000, "the opening movement now snapshots the purchase price")
        self.c.inventory.record_purchase(OWNER, supplier_name="Sharma Traders", invoice_no="ST/1", lines=[PurchaseLine(ghee.id, Decimal("5"), 25000)])  # ₹1,250; product cost becomes ₹250
        self.c.inventory.adjust(OWNER, ghee.id, Decimal("2"), reason="found in store room")  # 2 × ₹250 = ₹500 correction
        self.c.inventory.record_damage(OWNER, ghee.id, Decimal("1"), reason="broken")  # outgoing: not an investment
        from vaishnavi.core.barcode import internal_ean13

        self._sale(product_code=internal_ean13(ghee.id), qty="4")
        rep = self.c.reports.investment(OWNER)
        ghee_row = next(r for r in rep.rows if r.product_id == ghee.id)
        self.assertEqual((str(ghee_row.qty_in), ghee_row.opening_paise, ghee_row.purchases_paise, ghee_row.corrections_paise, ghee_row.invested_paise), ("17", 240000, 125000, 50000, 415000))
        self.assertFalse(ghee_row.estimated)
        self.assertEqual((str(ghee_row.stock_qty), ghee_row.stock_value_paise), ("12", 300000), "10 + 5 + 2 − 1 − 4 on the shelf at the current cost ₹250")
        camphor = next(r for r in rep.rows if r.product_id == 10)
        self.assertEqual((str(camphor.qty_in), camphor.opening_paise, camphor.estimated), ("100", 100000, True), "the fixture camphor has no purchase price: 100 × selling price ₹10, flagged")
        self.assertEqual((rep.items, rep.estimated_items, rep.invested_paise), (2, 1, 515000))
        self.assertEqual(rep.opening_paise + rep.purchases_paise + rep.corrections_paise, rep.invested_paise)
        self.assertEqual([(c.category, c.items, c.invested_paise) for c in rep.categories][0][2], 415000, "categories subtotal, biggest first")
        self.assertEqual(rep.since, "2026-11-14T16:00:00Z", "from the first stock entry — not clamped to 90 days")
        with self.assertRaises(PermissionDenied):
            self.c.reports.investment(STAFF)
        # the assistant's tool and the report page read the same figures
        tool = self.c.tools.run(OWNER, "investment_summary", {})
        self.assertEqual((tool["total"], tool["opening_stock"], tool["purchases"], tool["corrections"], tool["items_count"], tool["estimated_items"]), ("₹5,150.00", "₹3,400.00", "₹1,250.00", "₹500.00", 2, 1))
        self.assertEqual(tool["since"], "14-Nov-2026")
        table = reports_page.build_table(self.c, EN, OWNER, "investment")
        assert table is not None
        self.assertEqual(table.rows[-1]["product"], "TOTAL")
        self.assertEqual(table.rows[-1]["invested"], "₹5,150.00")
        self.assertTrue(any(r["product"].startswith("SUBTOTAL") for r in table.rows))
        self.assertIsNone(reports_page.build_table(self.c, EN, STAFF, "investment"))

    def _q_movement_cost(self, product_id: int) -> int | None:
        import sqlite3

        conn = sqlite3.connect(self.paths.database)
        try:
            row = conn.execute("SELECT unit_cost_paise FROM stock_movement WHERE product_id = ? AND type = 'OPENING'", (product_id,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None


if __name__ == "__main__":
    unittest.main()
