"""Unit 46 — forecast engine: series building (censored stock-outs), SMA / EWMA / last-year ensemble with bands,
festival uplift, planner (days of cover, reorder, classification), ForecastService with snapshots and E-FCST-001."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, iso_utc, set_clock
from vaishnavi.core.errors import ForecastError
from vaishnavi.core.permissions import PermissionDenied
from vaishnavi.core.settings import Settings
from vaishnavi.db.sqlite_stores import SqliteCatalogStore, SqliteForecastStore, SqliteTxn
from vaishnavi.forecast import festivals, methods, planner, series
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.forecast import ForecastService

from tests.core.test_billing import OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_ledger import insert_sale, make_sale

TODAY = date(2026, 11, 14)


def _series(values: list[float], *, censored: list[bool] | None = None) -> series.DailySeries:
    start = TODAY - timedelta(days=len(values) - 1)
    return series.DailySeries(start=start, values=[Decimal(str(v)) for v in values], censored=censored or [False] * len(values))


class SeriesTest(unittest.TestCase):
    def test_build_fills_gaps_and_marks_stockouts(self) -> None:
        rows = [("2026-11-10", Decimal("3")), ("2026-11-12", Decimal("5"))]
        s = series.build_series(rows, start=date(2026, 11, 9), end=date(2026, 11, 13), stockout_days={"2026-11-11"})
        self.assertEqual([str(v) for v in s.values], ["0", "3", "0", "5", "0"])
        self.assertEqual(s.censored, [False, False, True, False, False])
        self.assertEqual(s.days, 5)
        filled = series.fill_censored(s)
        self.assertEqual(str(filled.values[2]), "2", "a stock-out day takes the mean of the days the item was available (0+3+5+0)/4")
        self.assertEqual(filled.censored, [False] * 5)
        self.assertEqual(str(s.mean()), "1.6")
        self.assertEqual(s.last(2).values, [Decimal("5"), Decimal("0")])


class MethodsTest(unittest.TestCase):
    def test_flat_series_forecasts_flat_with_a_narrow_band(self) -> None:
        s = _series([5] * 42)
        f = methods.ensemble(s, horizon=14)
        self.assertEqual(f.point, Decimal("70"))
        self.assertEqual((f.low, f.high), (Decimal("70"), Decimal("70")), "no residuals → no band")
        self.assertEqual(f.confidence, "high")
        self.assertEqual(f.method, "ensemble(sma7,sma28,ewma)")

    def test_trending_series_leans_towards_recent_level(self) -> None:
        s = _series(list(range(1, 43)))
        f = methods.ensemble(s, horizon=7)
        daily = f.point / 7
        self.assertGreater(daily, Decimal("30"), "recent level is ~40/day; the 28-day mean alone would say 28")
        self.assertLess(f.low, f.point)
        self.assertGreater(f.high, f.point)
        self.assertIn(f.confidence, ("medium", "low"))

    def test_weekly_pattern_and_last_year(self) -> None:
        week = [2, 2, 2, 2, 2, 8, 8]
        s = _series(week * 60)  # 420 days → last year exists
        f = methods.ensemble(s, horizon=7)
        self.assertEqual(f.method, "ensemble(sma7,sma28,ewma,last_year)")
        self.assertEqual(f.point, Decimal("26"), "any 7-day window of this pattern sums to 26 — every method agrees")
        self.assertEqual(methods.sma(s.values, 7), Decimal(26) / 7)
        self.assertAlmostEqual(float(methods.ewma(s.values, alpha=0.3)), 4.6, delta=1.5)

    def test_censored_days_do_not_drag_the_forecast_down(self) -> None:
        values = [4] * 40 + [0, 0]
        raw = _series(values)
        censored = _series(values, censored=[False] * 40 + [True, True])
        self.assertLess(methods.ensemble(raw, horizon=7).point, methods.ensemble(series.fill_censored(censored), horizon=7).point)
        self.assertEqual(methods.ensemble(series.fill_censored(censored), horizon=7).point, Decimal("28"))


class FestivalTest(unittest.TestCase):
    TOML = """
[[festival]]
name = "Diwali"
start = "2026-11-06"
end = "2026-11-10"
uplift = { "Camphor" = 2.5, "Decoration" = 3.0, "*" = 1.4 }

[[festival]]
name = "Holi"
start = "2027-03-02"
end = "2027-03-04"
uplift = { "Colours" = 4.0 }
"""

    def test_load_and_uplift_lookup(self) -> None:
        fests = festivals.parse_festivals(self.TOML)
        self.assertEqual([f.name for f in fests], ["Diwali", "Holi"])
        self.assertEqual(festivals.uplift_for(date(2026, 11, 8), "Camphor", fests), 2.5)
        self.assertEqual(festivals.uplift_for(date(2026, 11, 8), "Idols", fests), 1.4, "wildcard")
        self.assertEqual(festivals.uplift_for(date(2026, 11, 20), "Camphor", fests), 1.0)
        self.assertEqual(festivals.uplift_for(date(2027, 3, 3), "Camphor", fests), 1.0, "no wildcard for Holi")
        upcoming = festivals.upcoming(date(2026, 11, 1), fests, days=14)
        self.assertEqual([f.name for f in upcoming], ["Diwali"])

    def test_learned_uplift_from_history(self) -> None:
        values = [2] * 27 + [6] * 5 + [2] * 10  # start 10-Oct → 6..10-Nov are the five 6s
        s = series.DailySeries(start=date(2026, 10, 10), values=[Decimal(v) for v in values], censored=[False] * len(values))
        fests = festivals.parse_festivals(self.TOML)
        factor = festivals.learn_uplift(s, fests[0])
        self.assertEqual(factor, 3.0, "6/day inside Diwali vs 2/day outside")
        self.assertEqual(festivals.apply_uplift(Decimal("28"), horizon_start=date(2026, 11, 4), horizon=7, category="Camphor", festivals_=fests), Decimal("28") + Decimal("4") * Decimal("1.5") * 5, "5 festival days in the window add (2.5−1) × daily rate each")


class PlannerTest(unittest.TestCase):
    def test_cover_stockout_reorder_and_classification(self) -> None:
        self.assertEqual(planner.days_of_cover(Decimal("20"), Decimal("4")), Decimal("5"))
        self.assertIsNone(planner.days_of_cover(Decimal("20"), Decimal("0")))
        self.assertEqual(planner.stockout_date(TODAY, Decimal("20"), Decimal("4")), date(2026, 11, 19))
        self.assertEqual(planner.reorder_qty(daily_rate=Decimal("4"), lead_days=3, cover_days=7, stock=Decimal("20"), min_stock=Decimal("5")), Decimal("25"), "4 × (3+7) + 5 − 20")
        self.assertEqual(planner.reorder_qty(daily_rate=Decimal("4"), lead_days=3, cover_days=7, stock=Decimal("60"), min_stock=Decimal("0")), Decimal("0"), "enough stock")
        self.assertEqual(planner.reorder_qty(daily_rate=Decimal("4"), lead_days=3, cover_days=7, stock=Decimal("20"), min_stock=Decimal("5"), pack_size=Decimal("12")), Decimal("36"), "rounded up to packs")
        self.assertEqual(planner.classify(_series([0] * 60)), "DEAD")
        self.assertEqual(planner.classify(_series([0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1] * 3)), "SLOW")
        self.assertEqual(planner.classify(_series([3] * 42)), "STEADY")
        self.assertEqual(planner.classify(_series([20] * 42)), "FAST")
        self.assertEqual(planner.classify(_series([1, 2, 3])), "NEW")


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database, sales=0)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.txn = SqliteTxn(self.paths.database)
        self.service = ForecastService(self.txn, SqliteForecastStore(), SqliteCatalogStore(), settings=lambda: self.settings, festivals_text=FestivalTest.TOML)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _history(self, days: int, qty: int = 3) -> None:
        conn = sqlite3.connect(self.paths.database)
        prev = "0" * 64
        for i in range(days):
            when = T0 - timedelta(days=days - i)
            s = make_sale(i + 1, qty=str(qty), total=qty * 1000, sold_at=iso_utc(when), prev_hash=prev)
            insert_sale(conn, s, with_movement=False)
            prev = s.hash
        conn.close()

    def test_not_enough_history_is_a_plain_error(self) -> None:
        self._history(10)
        with self.assertRaises(ForecastError) as cm:
            self.service.forecast(10)
        self.assertEqual(cm.exception.code, "E-FCST-001")
        self.assertEqual(cm.exception.params["days"], 10)

    def test_forecast_plan_snapshot_and_accuracy(self) -> None:
        self._history(35)
        f = self.service.forecast(10, horizon_days=14)
        self.assertEqual((f.product_id, f.horizon_days, f.point), (10, 14, Decimal("42")), "3 a day × 14")
        self.assertEqual(f.classification, "STEADY")
        self.assertEqual(f.daily_rate, Decimal("3"))
        self.assertEqual(f.confidence, "high")
        plan = self.service.plan(OWNER)
        row = next(p for p in plan if p.product_id == 10)
        self.assertEqual((str(row.stock), str(row.daily_rate), str(row.days_of_cover)), ("100", "3", "33.33"))
        self.assertEqual(row.stockout_date, "2026-12-17")
        self.assertEqual(str(row.reorder_qty), "0", "33 days of cover beats lead 3 + cover 7")
        with self.assertRaises(PermissionDenied):
            self.service.plan(STAFF)
        written = self.service.snapshot_all()
        self.assertEqual(written, 1)
        snaps = self.service.snapshots(10)
        self.assertEqual((len(snaps), str(snaps[0].point), snaps[0].method), (1, "42", f.method))
        self.clock.advance(days=14)
        self._extend(14, qty=3, start_seq=36)
        acc = self.service.accuracy(10)
        assert acc is not None
        self.assertEqual((acc.compared, acc.mape_pct), (1, 0), "forecast 42, actual 42")

    def _extend(self, days: int, *, qty: int, start_seq: int) -> None:
        conn = sqlite3.connect(self.paths.database)
        prev = conn.execute("SELECT hash FROM sale ORDER BY id DESC LIMIT 1").fetchone()[0]
        base = T0 + timedelta(days=0)
        for i in range(days):
            s = make_sale(start_seq + i, qty=str(qty), total=qty * 1000, sold_at=iso_utc(base + timedelta(days=i)), prev_hash=prev)
            insert_sale(conn, s, with_movement=False)
            prev = s.hash
        conn.close()


if __name__ == "__main__":
    unittest.main()
