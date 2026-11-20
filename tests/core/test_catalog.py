"""Catalogue core (docs/05 §"identity triangle", ADR-023, docs/13 Phase 2 unit 31): EAN-13, CatalogService,
PriceHistoryService. Written first (docs/13 rule 7). Real SQLite files; frozen clock.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from vaishnavi.core import barcode
from vaishnavi.core.clock import FrozenClock, iso_utc, set_clock
from vaishnavi.core.errors import CatalogError
from vaishnavi.core.events import EventBus, ProductChanged
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteCatalogStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.price_history import PriceHistoryService

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)


def _ean(prefix12: str) -> str:
    return prefix12 + str(barcode.ean13_check_digit(prefix12))


# ============================================================================ EAN-13
class Ean13Test(unittest.TestCase):
    def test_check_digit_and_internal_code_follow_the_documented_example(self) -> None:
        self.assertEqual(barcode.ean13_check_digit("290000001042"), 0)
        self.assertEqual(barcode.ean13_check_digit("400638133393"), 1, "a real-world EAN (4006381333931) confirms the GS1 weights")
        self.assertEqual(barcode.internal_ean13(1042), "2900000010420")
        self.assertTrue(barcode.is_valid_ean13("2900000010420"))
        self.assertFalse(barcode.is_valid_ean13("2900000010421"))
        self.assertFalse(barcode.is_valid_ean13("29000000104"))
        self.assertEqual(barcode.product_id_from_internal("2900000010420"), 1042)
        self.assertIsNone(barcode.product_id_from_internal("8901030865278"), "manufacturer codes are not ours")
        self.assertIsNone(barcode.product_id_from_internal("2900000010421"), "bad check digit")

    def test_symbology_detection(self) -> None:
        self.assertEqual(barcode.detect_symbology("8901030865278"), "EAN13")
        self.assertEqual(barcode.detect_symbology("96385074"), "EAN8")
        self.assertEqual(barcode.detect_symbology("036000291452"), "UPCA")
        self.assertEqual(barcode.detect_symbology("ABC-123"), "CODE128")
        self.assertEqual(barcode.normalise("  2900000010420\r\n"), "2900000010420")


# ============================================================================ catalogue service
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
        self.catalog = CatalogService(self.txn, SqliteCatalogStore(), self.audit, event_bus=self.bus)
        self.prices = PriceHistoryService(self.txn, SqliteCatalogStore())
        self.events: list[ProductChanged] = []
        self.bus.subscribe(ProductChanged, self.events.append)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _camphor_category(self) -> int:
        return next(c.id for c in self.catalog.categories() if c.name.startswith("Camphor"))


class CatalogServiceTest(Base):
    def test_create_generates_sku_internal_ean13_and_price_history_seed_rows(self) -> None:
        cat = self._camphor_category()
        p = self.catalog.create(OWNER, ProductInput(name="Camphor 100g", name_hi="कपूर 100 ग्राम", category_id=cat, price_paise=2000, mrp_paise=2200, cost_paise=1500, tax_rate_bp=500, min_stock=Decimal("5")))
        self.assertEqual(p.sku, "CAM-0002", "prefix from the category, next free sequence (CAM-0001 exists in the fixture)")
        self.assertEqual(p.internal_barcode, barcode.internal_ean13(p.id))
        self.assertTrue(barcode.is_valid_ean13(p.internal_barcode))
        codes = self.catalog.barcodes(p.id)
        self.assertEqual([(b.kind, b.is_primary) for b in codes], [("INTERNAL", True)])
        history = self.prices.history(p.id)
        self.assertEqual({h.field for h in history}, {"PRICE", "MRP", "COST", "TAX_RATE"}, "every price-like field starts its history at creation")
        self.assertTrue(all(h.source == "EDITOR" and h.changed_by == 1 for h in history))
        self.assertEqual([e.change for e in self.events], ["created"])
        with self.assertRaises(PermissionDenied):
            self.catalog.create(STAFF, ProductInput(name="x", category_id=cat, price_paise=100))

    def test_resolve_barcode_internal_manufacturer_and_pack(self) -> None:
        mfr, pack = _ean("890103086527"), _ean("890103086528")
        bad = mfr[:-1] + str((int(mfr[-1]) + 1) % 10)
        p = self.catalog.create(OWNER, ProductInput(name="Agarbatti 20", category_id=None, price_paise=1000))
        self.catalog.add_barcode(OWNER, p.id, mfr, kind="MANUFACTURER")
        self.catalog.add_barcode(OWNER, p.id, pack, kind="PACK", pack_qty=Decimal("12"))
        hit = self.catalog.resolve_barcode(p.internal_barcode)
        self.assertEqual((hit.product.id, hit.pack_qty), (p.id, Decimal("1")))  # type: ignore[union-attr]
        self.assertEqual(self.catalog.resolve_barcode(mfr).product.id, p.id, "either code resolves the same product")  # type: ignore[union-attr]
        self.assertEqual(self.catalog.resolve_barcode(pack).pack_qty, Decimal("12"))  # type: ignore[union-attr]
        self.assertIsNone(self.catalog.resolve_barcode(_ean("890123456789")), "unknown → None (POS raises E-SCN-002)")
        with self.assertRaises(CatalogError) as cm:
            self.catalog.add_barcode(OWNER, 10, mfr, kind="MANUFACTURER")
        self.assertEqual(cm.exception.code, "E-CAT-004", "a barcode belongs to one product")
        with self.assertRaises(CatalogError) as cm:
            self.catalog.add_barcode(OWNER, p.id, bad, kind="MANUFACTURER")
        self.assertEqual(cm.exception.code, "E-CAT-020", "bad check digit")
        self.catalog.remove_barcode(OWNER, p.id, pack)
        self.assertIsNone(self.catalog.resolve_barcode(pack))
        with self.assertRaises(CatalogError):
            self.catalog.remove_barcode(OWNER, p.id, p.internal_barcode)  # the internal code is permanent

    def test_search_by_name_hindi_code_and_id(self) -> None:
        cat = self._camphor_category()
        a = self.catalog.create(OWNER, ProductInput(name="Camphor Tablets 50g", name_hi="कपूर की गोलियाँ", category_id=cat, price_paise=1500))
        self.catalog.create(OWNER, ProductInput(name="Pure Cow Ghee 200ml", name_hi="शुद्ध गाय का घी", category_id=cat, price_paise=12000))
        self.assertEqual([h.product.id for h in self.catalog.search("camphor tab")][:1], [a.id])
        self.assertEqual(self.catalog.search("कपूर")[0].product.id, a.id, "Hindi name")
        self.assertIn(a.id, [h.product.id for h in self.catalog.search("camfor")], "fuzzy: one typo still finds it")
        self.assertEqual(self.catalog.search("#%d" % a.id)[0].product.id, a.id, "#ID")
        self.assertEqual(self.catalog.search(str(a.id))[0].product.id, a.id, "bare ID")
        self.assertEqual(self.catalog.search(a.sku.lower())[0].product.id, a.id, "code, case-insensitive")
        self.assertEqual(self.catalog.search(a.internal_barcode)[0].product.id, a.id, "typed barcode")
        self.assertEqual(self.catalog.search("zzzz-nothing"), [])
        self.assertTrue(all(not h.product.is_system for h in self.catalog.search("open")), "the OPEN-ITEM system product is never offered")

    def test_update_is_optimistic_and_records_price_changes(self) -> None:
        cat = self._camphor_category()
        p = self.catalog.create(OWNER, ProductInput(name="Camphor 100g", category_id=cat, price_paise=2000, cost_paise=1500, tax_rate_bp=500))
        self.clock.advance(days=1)
        updated = self.catalog.update(OWNER, p.id, row_version=p.row_version, changes={"price_paise": 2200, "name": "Camphor 100 g", "tax_rate_bp": 1200}, reason="festival price")
        self.assertEqual(updated.row_version, p.row_version + 1)
        self.assertEqual(updated.price_paise, 2200)
        with self.assertRaises(CatalogError) as cm:
            self.catalog.update(OWNER, p.id, row_version=p.row_version, changes={"price_paise": 1})
        self.assertEqual(cm.exception.code, "E-CAT-010", "stale editor is rejected")
        rows = [h for h in self.prices.history(p.id) if h.effective_from == iso_utc(self.clock.now())]
        self.assertEqual({(h.field, h.old_value, h.new_value, h.reason) for h in rows}, {("PRICE", 2000, 2200, "festival price"), ("TAX_RATE", 500, 1200, "festival price")})
        self.assertEqual([e.change for e in self.events][-1], "price")
        self.assertEqual(self._q("SELECT COUNT(*) FROM audit_log WHERE action = 'PRODUCT_UPDATED'")[0][0], 1)
        with self.assertRaises(PermissionDenied):
            self.catalog.update(STAFF, p.id, row_version=updated.row_version, changes={"price_paise": 1})
        with self.assertRaises(ValueError):
            self.catalog.update(OWNER, p.id, row_version=updated.row_version, changes={"internal_barcode": "x"})  # permanent identity

    def test_archive_hides_from_search_and_scan_but_keeps_history(self) -> None:
        p = self.catalog.create(OWNER, ProductInput(name="Old Diya", category_id=None, price_paise=500))
        self.catalog.archive(OWNER, p.id)
        self.assertIsNone(self.catalog.resolve_barcode(p.internal_barcode))
        self.assertEqual(self.catalog.search("old diya"), [])
        self.assertIsNotNone(self.catalog.get(p.id), "the row stays for history and reports")
        self.assertFalse(self.catalog.get(p.id).is_active)  # type: ignore[union-attr]
        self.assertEqual(self.events[-1].change, "archived")
        self.catalog.restore(OWNER, p.id)
        self.assertIsNotNone(self.catalog.resolve_barcode(p.internal_barcode))

    def test_categories_tags_and_quick_picks(self) -> None:
        cats = self.catalog.categories()
        from vaishnavi.db.seed import CATEGORIES

        self.assertEqual(len(cats), len(CATEGORIES), "seeded puja categories")
        self.assertIn("Oil & Wicks", {c.name for c in cats})
        new = self.catalog.create_category(OWNER, name="Rudraksha", name_hi="रुद्राक्ष")
        self.assertGreater(new.sort_order, max(c.sort_order for c in cats), "appended at the end of the order")
        p = self.catalog.create(OWNER, ProductInput(name="Rudraksha Mala 108", category_id=new.id, price_paise=35000))
        self.assertEqual(p.sku, "RUD-0001")
        self.catalog.set_tags(OWNER, p.id, ["Diwali", "gift"])
        self.assertEqual(self.catalog.tags_of(p.id), ["diwali", "gift"])
        self.catalog.set_quick_pick(OWNER, p.id, True)
        self.assertEqual([q.id for q in self.catalog.quick_picks()], [p.id])
        with self.assertRaises(PermissionDenied):
            self.catalog.create_category(STAFF, name="x")

    def test_list_for_grid_filters_and_counts(self) -> None:
        cat = self._camphor_category()
        for i in range(3):
            self.catalog.create(OWNER, ProductInput(name=f"Item {i}", category_id=cat, price_paise=100 * (i + 1)))
        page = self.catalog.list_products(category_id=cat, query="item", limit=2, offset=0)
        self.assertEqual(page.total, 3)
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.items[0].stock_qty, Decimal("0"), "stock comes along for the grid")
        everything = self.catalog.list_products()
        self.assertTrue(all(not p.is_system for p in everything.items))


# ============================================================================ price history queries
class PriceHistoryTest(Base):
    def test_windows_and_chart_points(self) -> None:
        p = self.catalog.create(OWNER, ProductInput(name="Camphor 100g", category_id=self._camphor_category(), price_paise=2000))
        version = p.row_version
        for days, price in ((20, 2100), (100, 2200), (300, 2300)):
            self.clock.advance(days=days - (self.clock.now() - T0).days)
            version = self.catalog.update(OWNER, p.id, row_version=version, changes={"price_paise": price}).row_version
        self.clock.advance(days=30)  # now = T0 + 330 d
        one_month = self.prices.history(p.id, field="PRICE", window="1m")
        self.assertEqual([h.new_value for h in one_month], [2300])
        six_months = self.prices.history(p.id, field="PRICE", window="6m")
        self.assertEqual([h.new_value for h in six_months], [2300], "the change 230 days ago is outside 6 months")
        one_year = self.prices.history(p.id, field="PRICE", window="1y")
        self.assertEqual([h.new_value for h in one_year], [2300, 2200, 2100, 2000])
        custom = self.prices.history(p.id, field="PRICE", since=T0 + timedelta(days=50), until=T0 + timedelta(days=250))
        self.assertEqual([h.new_value for h in custom], [2200])
        points = self.prices.chart_points(p.id, field="PRICE", window="1y")
        self.assertEqual([v for _, v in points], [2000, 2100, 2200, 2300, 2300], "step chart: one point per change plus 'now' carrying the last price")
        self.assertEqual(points[-1][0], iso_utc(self.clock.now()))
        self.assertEqual(self.prices.current(p.id, "PRICE"), 2300)


if __name__ == "__main__":
    unittest.main()
