"""Single source of time for the application (stdlib only).

Everything that stamps a sale, a login or a log line asks :class:`Clock` instead of calling
``datetime.now()`` directly, so tests can freeze or advance time deterministically and so the whole
app agrees on one timezone — the shop's (Asia/Kolkata, IST, no DST). Wall-clock values are stored
as timezone-aware UTC in the database and shown in IST; monotonic time is used for durations.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
"""India Standard Time — fixed offset, no DST (``zoneinfo`` would need tzdata on Windows)."""


class Clock(Protocol):
    """What the rest of the code depends on."""

    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary point; never goes backwards (for durations/timeouts)."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """UTC now."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """``time.monotonic()``."""
        return time.monotonic()


@dataclass
class FrozenClock:
    """Test clock: fixed until ``advance``/``set`` is called; monotonic advances with it."""

    current: datetime = field(default_factory=lambda: datetime(2026, 1, 1, 9, 0, tzinfo=IST).astimezone(UTC))
    _mono: float = 1000.0

    def now(self) -> datetime:
        """The frozen instant (UTC)."""
        return self.current.astimezone(UTC)

    def monotonic(self) -> float:
        """Frozen monotonic seconds."""
        return self._mono

    def advance(self, seconds: float = 0, **delta: float) -> None:
        """Move time forward (``advance(90)`` or ``advance(minutes=5)``)."""
        step = timedelta(seconds=seconds, **delta)
        self.current += step
        self._mono += step.total_seconds()

    def set(self, when: datetime) -> None:
        """Jump to an instant (naive values are taken as IST)."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        self.current = when.astimezone(UTC)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """The process-wide clock (``SystemClock`` unless a test installed another)."""
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install a clock (tests); returns the previous one so it can be restored."""
    global _clock
    previous, _clock = _clock, clock
    return previous


# ----------------------------------------------------------------------------- conveniences
def now_utc() -> datetime:
    """Timezone-aware UTC now from the installed clock."""
    return get_clock().now()


def now_ist() -> datetime:
    """Now in shop time."""
    return now_utc().astimezone(IST)


def today_ist() -> date:
    """Today's date in shop time (a sale at 00:30 IST belongs to that day, not to UTC's yesterday)."""
    return now_ist().date()


def to_ist(when: datetime) -> datetime:
    """Convert a stored (UTC or aware) datetime to IST; naive values are assumed UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(IST)


def to_utc(when: datetime) -> datetime:
    """Convert an aware datetime to UTC; naive values are assumed IST (owner-typed times)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    return when.astimezone(UTC)


def format_ist(when: datetime, fmt: str = "%d-%b-%Y %H:%M") -> str:
    """'14-Nov-2026 09:30' in shop time — the receipt/report convention."""
    return to_ist(when).strftime(fmt)


ISO_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
"""Database convention for every datetime column (docs/04): second precision, UTC, trailing ``Z``."""


def iso_utc(when: datetime | None = None) -> str:
    """``when`` (default: now) as the database text form ``2026-11-14T04:00:00Z``."""
    return to_utc(when if when is not None else now_utc()).strftime(ISO_UTC_FMT)


def parse_iso_utc(text: str) -> datetime:
    """Inverse of :func:`iso_utc`; also accepts an offset or fractional seconds. Raises ``ValueError``."""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    return to_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a shop day in UTC — for 'today's sales' queries."""
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def financial_year(day: date | None = None) -> str:
    """Indian financial year label for invoice numbers: April–March → ``"26-27"`` (docs/05 §3)."""
    day = day if day is not None else today_ist()
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"
