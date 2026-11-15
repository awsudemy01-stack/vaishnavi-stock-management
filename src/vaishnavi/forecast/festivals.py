"""Festival calendar and uplift (stdlib only; ``config/festivals.toml``).

Puja demand is seasonal in a way no moving average sees coming: camphor and diyas triple in Diwali
week, colours explode before Holi. The calendar lists each festival window with an **uplift factor per
category** (``"*"`` = every category); the planner multiplies the daily rate on those days. Factors can
also be **learned** from last year's series (mean inside the window ÷ mean outside) so the owner's
guesses get corrected by the shop's own history.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from vaishnavi.forecast.series import DailySeries

log = logging.getLogger("vaishnavi.forecast.festivals")


@dataclass(frozen=True)
class Festival:
    """One window."""

    name: str
    start: date
    end: date
    uplift: dict[str, float] = field(default_factory=dict)

    def covers(self, day: date) -> bool:
        """Inclusive."""
        return self.start <= day <= self.end

    def factor(self, category: str | None) -> float:
        """Category factor, wildcard, or 1.0."""
        if category and category in self.uplift:
            return float(self.uplift[category])
        return float(self.uplift.get("*", 1.0))


def parse_festivals(text: str) -> list[Festival]:
    """TOML ``[[festival]]`` tables → sorted list."""
    data = tomllib.loads(text or "")
    out: list[Festival] = []
    for entry in data.get("festival", []):
        try:
            out.append(Festival(str(entry["name"]), date.fromisoformat(str(entry["start"])), date.fromisoformat(str(entry["end"])), {str(k): float(v) for k, v in dict(entry.get("uplift", {})).items()}))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("festival entry skipped: %s (%s)", entry, exc)
    out.sort(key=lambda f: f.start)
    return out


def load_festivals(path: Path) -> list[Festival]:
    """Read the file (missing → empty calendar)."""
    try:
        return parse_festivals(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("festivals file unreadable %s: %s", path, exc)
        return []


def uplift_for(day: date, category: str | None, festivals: list[Festival]) -> float:
    """Highest factor of any festival covering ``day`` (1.0 when none)."""
    factors = [f.factor(category) for f in festivals if f.covers(day)]
    return max(factors) if factors else 1.0


def upcoming(day: date, festivals: list[Festival], *, days: int = 30) -> list[Festival]:
    """Festivals ongoing or starting within ``days``."""
    limit = day + timedelta(days=days)
    return [f for f in festivals if f.end >= day and f.start <= limit]


def learn_uplift(s: DailySeries, festival: Festival) -> float:
    """Mean inside the window ÷ mean outside (1.0 when either side is empty or zero)."""
    inside: list[Decimal] = []
    outside: list[Decimal] = []
    for i, v in enumerate(s.values):
        day = s.start + timedelta(days=i)
        (inside if festival.covers(day) else outside).append(v)
    if not inside or not outside:
        return 1.0
    base = sum(outside, Decimal(0)) / Decimal(len(outside))
    if base <= 0:
        return 1.0
    return round(float((sum(inside, Decimal(0)) / Decimal(len(inside))) / base), 2)


def apply_uplift(point: Decimal, *, horizon_start: date, horizon: int, category: str | None, festivals_: list[Festival]) -> Decimal:
    """Add ``rate × (factor − 1)`` for every festival day inside the horizon."""
    if horizon <= 0 or not festivals_:
        return point
    rate = point / Decimal(horizon)
    extra = Decimal(0)
    for i in range(horizon):
        factor = uplift_for(horizon_start + timedelta(days=i), category, festivals_)
        if factor > 1.0:
            extra += rate * (Decimal(str(factor)) - 1)
    return (point + extra).quantize(Decimal("0.01"))
