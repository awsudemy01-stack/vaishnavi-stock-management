"""ForecastService — demand statistics for the owner, the planner and the assistant (docs/10 §11; stdlib).

Deterministic, explainable, no LLM: build the item's daily series (stock-out days censored), run the
median ensemble, add the festival uplift for the horizon, classify demand, and turn the rate into days
of cover / stock-out date / reorder quantity. Below ``forecast.min_history_days`` the answer is an honest
``E-FCST-001`` ("not enough history yet"). The nightly job snapshots every active item so accuracy can be
measured later against what actually sold.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from vaishnavi.core.clock import iso_utc, today_ist
from vaishnavi.core.errors import ForecastError
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.core.settings import Settings
from vaishnavi.db.records import ForecastSnapshotRecord, ProductRecord
from vaishnavi.forecast import festivals as fest
from vaishnavi.forecast import methods, planner, series
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.forecast")

IST_OFFSET_MINUTES = 330


@dataclass(frozen=True)
class ForecastResult:
    """One item over a horizon."""

    product_id: int
    horizon_days: int
    point: Decimal
    low: Decimal
    high: Decimal
    method: str
    confidence: str
    daily_rate: Decimal
    classification: str
    history_days: int
    caveats: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanRow:
    """Reorder planner row."""

    product_id: int
    name: str
    stock: Decimal
    daily_rate: Decimal | None
    days_of_cover: Decimal | None
    stockout_date: str | None
    reorder_qty: Decimal
    classification: str
    confidence: str


@dataclass(frozen=True)
class Accuracy:
    """How past snapshots compared with what sold."""

    product_id: int
    compared: int
    mape_pct: int
    bias_pct: int


SnapshotRecord = ForecastSnapshotRecord
"""Service-facing alias of the ``forecast_snapshot`` record."""


class ForecastStore(Protocol):
    """Rows the service needs."""

    def daily_sales(self, session: Session, *, product_id: int, since: str, offset_minutes: int) -> list[tuple[str, Decimal]]: ...  # pragma: no cover - protocol
    def stockout_days(self, session: Session, *, product_id: int, since_day: str) -> set[str]: ...  # pragma: no cover - protocol
    def category_name(self, session: Session, product_id: int) -> str | None: ...  # pragma: no cover - protocol
    def insert_snapshot(self, session: Session, *, computed_at: str, product_id: int, horizon_days: int, point: Decimal, low: Decimal, high: Decimal, method: str, confidence: str, caveats: str | None) -> int: ...  # pragma: no cover - protocol
    def snapshots(self, session: Session, *, product_id: int, limit: int) -> list[SnapshotRecord]: ...  # pragma: no cover - protocol


class ProductLookup(Protocol):
    """Catalogue slice."""

    def product_by_id(self, session: Session, product_id: int) -> ProductRecord | None: ...  # pragma: no cover - protocol
    def active_products(self, session: Session, *, limit: int) -> list[ProductRecord]: ...  # pragma: no cover - protocol


class ForecastService:
    """Forecast · plan · snapshot · accuracy."""

    def __init__(self, txn: TxnFactory, store: ForecastStore, products: ProductLookup, *, settings: Callable[[], Settings], festivals_text: str | None = None, festivals_loader: Callable[[], list[fest.Festival]] | None = None) -> None:
        self._txn = txn
        self._store = store
        self._products = products
        self._settings = settings
        self._festivals_text = festivals_text
        self._festivals_loader = festivals_loader

    # -- calendar ---------------------------------------------------------------------------------
    def festivals(self) -> list[fest.Festival]:
        """The configured calendar."""
        if self._festivals_text is not None:
            return fest.parse_festivals(self._festivals_text)
        if self._festivals_loader is not None:
            return self._festivals_loader()
        return []

    # -- one item ---------------------------------------------------------------------------------
    def series_for(self, product_id: int, *, days: int = 400) -> series.DailySeries | None:
        """Daily demand up to yesterday (None when the item never sold)."""
        today = today_ist()
        start_floor = today - timedelta(days=days)
        with self._txn(label="forecast.series") as session:
            rows = self._store.daily_sales(session, product_id=product_id, since=iso_utc(_utc_midnight(start_floor)), offset_minutes=IST_OFFSET_MINUTES)
            if not rows:
                return None
            first = date.fromisoformat(rows[0][0])
            outs = self._store.stockout_days(session, product_id=product_id, since_day=first.isoformat())
        end = today - timedelta(days=1)
        if end < first:
            return None
        return series.build_series(rows, start=first, end=end, stockout_days=outs)

    def forecast(self, product_id: int, *, horizon_days: int | None = None) -> ForecastResult:
        """Point + band over the horizon (``E-FCST-001`` when history is shorter than ``min_history_days``)."""
        cfg = self._settings().forecast
        horizon = horizon_days or cfg.horizon_days
        raw = self.series_for(product_id)
        history = raw.days if raw is not None else 0
        if raw is None or history < cfg.min_history_days:
            log.info("FORECAST_INSUFFICIENT product=%s days=%d min=%d", product_id, history, cfg.min_history_days)
            raise ForecastError("E-FCST-001", days=history, detail=f"min {cfg.min_history_days}")
        caveats: list[str] = []
        censored = sum(1 for c in raw.censored if c)
        if censored:
            caveats.append(f"{censored} stock-out day(s) censored")
        s = series.fill_censored(raw)
        base = methods.ensemble(s, horizon=horizon, alpha=cfg.ewma_alpha)
        with self._txn(label="forecast.category") as session:
            category = self._store.category_name(session, product_id)
        calendar = self.festivals()
        point = fest.apply_uplift(base.point, horizon_start=today_ist(), horizon=horizon, category=category, festivals_=calendar)
        if point != base.point:
            caveats.append("festival uplift applied")
        scale = (point / base.point) if base.point else Decimal(1)
        result = ForecastResult(product_id, horizon, _clean(point), _clean((base.low * scale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), _clean((base.high * scale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), base.method, base.confidence, base.daily_rate, planner.classify(s), history, caveats)
        log.debug("FORECAST product=%s horizon=%d point=%s band=%s..%s rate=%s class=%s conf=%s", product_id, horizon, result.point, result.low, result.high, result.daily_rate, result.classification, result.confidence)
        return result

    # -- planner ----------------------------------------------------------------------------------
    def plan(self, actor: Actor, *, limit: int = 5000) -> list[PlanRow]:
        """Every active item: stock, rate, days of cover, stock-out date, reorder quantity (Owner)."""
        require(actor, Permission.REPORT_FORECAST)
        cfg = self._settings().forecast
        today = today_ist()
        with self._txn(label="forecast.plan_products") as session:
            products = [p for p in self._products.active_products(session, limit=limit) if not p.is_system]
        rows: list[PlanRow] = []
        for p in products:
            try:
                f = self.forecast(p.id, horizon_days=cfg.horizon_days)
            except ForecastError:
                # No usable history yet: the only defensible suggestion is the gap to the minimum.
                pack = p.pricing_unit_qty if p.sale_mode != "PIECE" else None
                gap = planner.reorder_qty(daily_rate=Decimal(0), lead_days=0, cover_days=0, stock=p.stock_qty, min_stock=p.min_stock, pack_size=pack)
                rows.append(PlanRow(p.id, p.name, p.stock_qty, None, None, None, gap, "NEW", "low"))
                continue
            rate = f.daily_rate
            cover = planner.days_of_cover(p.stock_qty, rate)
            out = planner.stockout_date(today, p.stock_qty, rate)
            pack = p.pricing_unit_qty if p.sale_mode != "PIECE" else None
            qty = planner.reorder_qty(daily_rate=rate, lead_days=cfg.lead_days, cover_days=cfg.cover_days, stock=p.stock_qty, min_stock=p.min_stock, pack_size=pack)
            rows.append(PlanRow(p.id, p.name, p.stock_qty, rate, cover, out.isoformat() if out else None, qty, f.classification, f.confidence))
        rows.sort(key=lambda r: (r.days_of_cover if r.days_of_cover is not None else Decimal(10**9), r.name))
        log.info("FORECAST_PLAN products=%d reorder=%d", len(rows), sum(1 for r in rows if r.reorder_qty > 0))
        return rows

    # -- snapshots & accuracy ---------------------------------------------------------------------
    def snapshot_all(self, *, limit: int = 5000) -> int:
        """Nightly: one snapshot per forecastable item; returns how many were written."""
        cfg = self._settings().forecast
        with self._txn(label="forecast.snapshot_products") as session:
            products = [p for p in self._products.active_products(session, limit=limit) if not p.is_system]
        written = 0
        now = iso_utc()
        for p in products:
            try:
                f = self.forecast(p.id, horizon_days=cfg.horizon_days)
            except ForecastError:
                continue
            with self._txn(label="forecast.snapshot") as session:
                self._store.insert_snapshot(session, computed_at=now, product_id=p.id, horizon_days=f.horizon_days, point=f.point, low=f.low, high=f.high, method=f.method, confidence=f.confidence, caveats="; ".join(f.caveats) or None)
            written += 1
        log.info("FORECAST_SNAPSHOTS written=%d of %d", written, len(products))
        return written

    def snapshots(self, product_id: int, *, limit: int = 50) -> list[SnapshotRecord]:
        """Newest first."""
        with self._txn(label="forecast.snapshots") as session:
            return self._store.snapshots(session, product_id=product_id, limit=limit)

    def accuracy(self, product_id: int) -> Accuracy | None:
        """MAPE / bias of snapshots whose horizon has fully elapsed (None when none has)."""
        today = today_ist()
        snaps = self.snapshots(product_id, limit=200)
        full = self.series_for(product_id)
        if full is None:
            return None
        errors: list[float] = []
        bias: list[float] = []
        for snap in snaps:
            computed_day = _ist_day(snap.computed_at)
            window_end = computed_day + timedelta(days=snap.horizon_days - 1)
            if window_end >= today:
                continue
            actual = _window_sum(full, computed_day, snap.horizon_days)
            if actual is None:
                continue
            point = float(snap.point)
            denominator = float(actual) if actual else 1.0
            errors.append(abs(point - float(actual)) / denominator * 100)
            bias.append((point - float(actual)) / denominator * 100)
        if not errors:
            return None
        return Accuracy(product_id, len(errors), int(round(sum(errors) / len(errors))), int(round(sum(bias) / len(bias))))


# ----------------------------------------------------------------------------- helpers
def _utc_midnight(day: date) -> Any:
    from vaishnavi.core.clock import business_day_bounds  # noqa: PLC0415

    return business_day_bounds(day)[0]


def _ist_day(iso: str) -> date:
    from vaishnavi.core.clock import parse_iso_utc, to_ist  # noqa: PLC0415

    return to_ist(parse_iso_utc(iso)).date()


def _window_sum(s: series.DailySeries, start: date, horizon: int) -> Decimal | None:
    first = (start - s.start).days
    if first < 0 or first + horizon > s.days:
        return None
    return sum(s.values[first : first + horizon], Decimal(0))


def _clean(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value
