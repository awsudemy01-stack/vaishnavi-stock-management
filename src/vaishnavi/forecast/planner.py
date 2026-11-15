"""Planner (stdlib only): days of cover, stock-out date, reorder quantity, demand classification."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from vaishnavi.forecast.series import DailySeries

_Q2 = Decimal("0.01")


def days_of_cover(stock: Decimal, daily_rate: Decimal) -> Decimal | None:
    """How many days the stock lasts at the current rate (None when nothing sells)."""
    if daily_rate <= 0:
        return None
    return (Decimal(stock) / Decimal(daily_rate)).quantize(_Q2, rounding=ROUND_HALF_UP)


def stockout_date(today: date, stock: Decimal, daily_rate: Decimal) -> date | None:
    """The day the shelf goes empty."""
    cover = days_of_cover(stock, daily_rate)
    if cover is None:
        return None
    return today + timedelta(days=int(cover))


def reorder_qty(*, daily_rate: Decimal, lead_days: int, cover_days: int, stock: Decimal, min_stock: Decimal, pack_size: Decimal | None = None) -> Decimal:
    """``rate × (lead + cover) + minimum − stock``, never negative, rounded up to whole packs."""
    need = Decimal(daily_rate) * (lead_days + cover_days) + Decimal(min_stock) - Decimal(stock)
    if need <= 0:
        return Decimal(0)
    if pack_size and pack_size > 0:
        packs = math.ceil(need / pack_size)
        return Decimal(packs) * pack_size
    return need.quantize(Decimal(1), rounding=ROUND_HALF_UP) if need == need.to_integral_value() else need.quantize(_Q2, rounding=ROUND_HALF_UP)


def classify(s: DailySeries, *, min_days: int = 14, fast_rate: Decimal = Decimal("10"), steady_rate: Decimal = Decimal("1")) -> str:
    """``NEW`` (too little history) · ``DEAD`` (nothing in 60 days) · ``SLOW`` · ``STEADY`` · ``FAST`` by the 28-day rate."""
    if s.days < min_days:
        return "NEW"
    if s.last(60).total() == 0:
        return "DEAD"
    rate = s.last(28).mean()
    if rate >= fast_rate:
        return "FAST"
    if rate >= steady_rate:
        return "STEADY"
    return "SLOW"
