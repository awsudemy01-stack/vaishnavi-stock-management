"""Daily demand series (stdlib only).

A :class:`DailySeries` is one value per calendar day from ``start``; days with no sale are ``0``. Days
on which the item was **out of stock** are *censored* — the shop could not sell what it did not have,
so a zero there is not demand. :func:`fill_censored` replaces those days with the mean of the days the
item was available, which stops a stock-out from dragging the forecast (and the reorder) down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class DailySeries:
    """Values per day starting at ``start`` (inclusive)."""

    start: date
    values: list[Decimal]
    censored: list[bool]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.censored):
            raise ValueError("values and censored must have the same length")

    @property
    def days(self) -> int:
        """Length."""
        return len(self.values)

    @property
    def end(self) -> date:
        """Last day (inclusive)."""
        return self.start + timedelta(days=max(0, self.days - 1))

    def mean(self) -> Decimal:
        """Average per day over the whole series (0 when empty)."""
        return sum(self.values, Decimal(0)) / Decimal(self.days) if self.days else Decimal(0)

    def last(self, n: int) -> DailySeries:
        """The final ``n`` days."""
        n = max(0, min(n, self.days))
        return DailySeries(self.start + timedelta(days=self.days - n), self.values[-n:] if n else [], self.censored[-n:] if n else [])

    def total(self) -> Decimal:
        """Σ values."""
        return sum(self.values, Decimal(0))


def build_series(rows: list[tuple[str, Decimal]], *, start: date, end: date, stockout_days: set[str] | None = None) -> DailySeries:
    """``(YYYY-MM-DD, qty)`` rows → one value per day from ``start`` to ``end`` (gaps = 0, stock-outs censored)."""
    if end < start:
        raise ValueError("end before start")
    by_day = {day: Decimal(str(q)) for day, q in rows}
    out_days = stockout_days or set()
    values: list[Decimal] = []
    censored: list[bool] = []
    day = start
    while day <= end:
        key = day.isoformat()
        values.append(by_day.get(key, Decimal(0)))
        censored.append(key in out_days)
        day += timedelta(days=1)
    return DailySeries(start, values, censored)


def fill_censored(s: DailySeries) -> DailySeries:
    """Replace censored days with the mean of the uncensored ones (all censored → leave as is)."""
    open_values = [v for v, c in zip(s.values, s.censored, strict=True) if not c]
    if not open_values or all(not c for c in s.censored):
        return DailySeries(s.start, list(s.values), [False] * s.days)
    fill = sum(open_values, Decimal(0)) / Decimal(len(open_values))
    return DailySeries(s.start, [fill if c else v for v, c in zip(s.values, s.censored, strict=True)], [False] * s.days)
