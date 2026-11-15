"""Forecast methods (stdlib only): SMA, EWMA, same period last year, and a median ensemble with a band.

Why a **median** ensemble: the shop's items are either flat, trending or weekly-patterned (Saturdays and
festival eves sell more). SMA-7 and SMA-28 agree on flat and weekly series; EWMA follows a trend fast but
over-reacts to a weekend spike; last-year catches the season. Taking the median per-day rate keeps one
odd method from pulling the number, and on a perfectly weekly series every method agrees on a 7-day sum.
The band is ±1.28 × (std of one-step SMA-7 residuals) × √horizon — an 80 % interval, honest about noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from statistics import median, pstdev

from vaishnavi.forecast.series import DailySeries

_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class Forecast:
    """Sum over the horizon with an 80 % band."""

    point: Decimal
    low: Decimal
    high: Decimal
    method: str
    confidence: str  # high | medium | low
    daily_rate: Decimal
    rates: dict[str, Decimal] = field(default_factory=dict)


def sma(values: list[Decimal], window: int) -> Decimal:
    """Mean of the last ``window`` values (all of them when shorter)."""
    if not values:
        return Decimal(0)
    tail = values[-window:] if window > 0 else values
    return sum(tail, Decimal(0)) / Decimal(len(tail))


def ewma(values: list[Decimal], *, alpha: float = 0.3) -> Decimal:
    """Exponentially weighted moving average (most recent weighs ``alpha``)."""
    if not values:
        return Decimal(0)
    a = Decimal(str(alpha))
    level = values[0]
    for v in values[1:]:
        level = a * v + (1 - a) * level
    return level


def same_period_last_year(s: DailySeries, horizon: int) -> Decimal | None:
    """Σ of the ``horizon`` days that started 52 weeks ago (same weekday alignment); None without a year of history."""
    offset = 364
    start = s.days - offset
    if start < 0 or start + horizon > s.days:
        return None
    return sum(s.values[start : start + horizon], Decimal(0))


def residual_std(values: list[Decimal], *, window: int = 7, lookback: int = 28) -> float:
    """Std of one-step-ahead SMA residuals over the last ``lookback`` days."""
    if len(values) < window + 2:
        return 0.0
    tail = values[-(lookback + window) :]
    residuals = []
    for i in range(window, len(tail)):
        predicted = sum(tail[i - window : i], Decimal(0)) / Decimal(window)
        residuals.append(float(tail[i] - predicted))
    return pstdev(residuals) if len(residuals) > 1 else 0.0


def confidence_for(s: DailySeries) -> str:
    """``high`` with 4+ weeks and a calm series, ``medium`` with 4+ weeks, else ``low``."""
    if s.days < 28:
        return "low"
    mean = float(s.mean())
    if mean <= 0:
        return "low"
    cov = pstdev([float(v) for v in s.values]) / mean
    return "high" if cov <= 0.25 else "medium"


def ensemble(s: DailySeries, *, horizon: int, alpha: float = 0.3) -> Forecast:
    """Median of per-day rates from SMA-7, SMA-28, EWMA (+ last year when available), × horizon, with a band."""
    if horizon <= 0:
        raise ValueError("horizon must be > 0")
    rates: dict[str, Decimal] = {"sma7": sma(s.values, 7), "sma28": sma(s.values, 28), "ewma": ewma(s.values, alpha=alpha)}
    last_year = same_period_last_year(s, horizon)
    if last_year is not None:
        rates["last_year"] = last_year / Decimal(horizon)
    rate = Decimal(str(median([float(r) for r in rates.values()])))
    exact = next((r for r in rates.values() if abs(float(r) - float(rate)) < 1e-12), rate)  # keep the exact Decimal when the median is one of the inputs
    point = (exact * horizon).quantize(_Q2, rounding=ROUND_HALF_UP)
    noise = residual_std(s.values) * 1.28 * math.sqrt(horizon)
    disagreement = (max(float(r) for r in rates.values()) - min(float(r) for r in rates.values())) * horizon / 2  # methods that disagree = a trend or a shift the band must cover
    spread = Decimal(str(max(noise, disagreement))).quantize(_Q2, rounding=ROUND_HALF_UP)
    low = max(Decimal(0), point - spread)
    high = point + spread
    return Forecast(point=_clean(point), low=_clean(low), high=_clean(high), method="ensemble(" + ",".join(rates) + ")", confidence=confidence_for(s), daily_rate=_clean(exact.quantize(_Q2, rounding=ROUND_HALF_UP)), rates=rates)


def _clean(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value
