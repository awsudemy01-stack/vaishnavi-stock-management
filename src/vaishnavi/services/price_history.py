"""PriceHistoryService — read side of the append-only ``price_history`` table (ADR-033; stdlib).

Writers are the catalogue (editor / import), the purchase entry (cost) and conversions; this service
answers the *Price history* pop-up: rows for **1 month / 6 months / 1 year / custom dates** and the step
chart points (each change plus a closing point at "now" so the line reaches the right edge).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from vaishnavi.core.clock import iso_utc, now_utc
from vaishnavi.db.records import PriceHistoryRecord
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.price_history")

Window = Literal["1m", "6m", "1y", "all"]
WINDOW_DAYS: Final[dict[str, int | None]] = {"1m": 30, "6m": 182, "1y": 365, "all": None}
FIELDS: Final = ("PRICE", "MRP", "COST", "TAX_RATE")


class PriceHistoryStore(Protocol):
    """What the service reads."""

    def price_history(self, session: Session, *, product_id: int, field: str | None, since: str | None, until: str | None) -> list[PriceHistoryRecord]: ...  # pragma: no cover - protocol
    def last_price_before(self, session: Session, *, product_id: int, field: str, before: str) -> int | None: ...  # pragma: no cover - protocol


class PriceHistoryService:
    """Queries for the pop-up and the reports."""

    def __init__(self, txn: TxnFactory, store: PriceHistoryStore) -> None:
        self._txn = txn
        self._store = store

    @staticmethod
    def window_bounds(window: Window | None, since: datetime | None, until: datetime | None) -> tuple[str | None, str | None]:
        """Explicit dates win; otherwise the named window ends now."""
        if since is not None or until is not None:
            return (iso_utc(since) if since else None, iso_utc(until) if until else None)
        days = WINDOW_DAYS.get(window or "all")
        if days is None:
            return (None, None)
        return (iso_utc(now_utc() - timedelta(days=days)), None)

    def history(self, product_id: int, *, field: str | None = None, window: Window | None = None, since: datetime | None = None, until: datetime | None = None) -> list[PriceHistoryRecord]:
        """Rows newest first within the window (all fields when ``field`` is None)."""
        lo, hi = self.window_bounds(window, since, until)
        with self._txn(label="price.history") as session:
            rows = self._store.price_history(session, product_id=product_id, field=field, since=lo, until=hi)
        log.debug("PRICE_HISTORY product=%s field=%s window=%s rows=%d", product_id, field or "*", window or (lo, hi), len(rows))
        return rows

    def current(self, product_id: int, field: str) -> int | None:
        """Latest value on record."""
        with self._txn(label="price.current") as session:
            rows = self._store.price_history(session, product_id=product_id, field=field, since=None, until=None)
        return rows[0].new_value if rows else None

    def chart_points(self, product_id: int, *, field: str = "PRICE", window: Window | None = "1y", since: datetime | None = None, until: datetime | None = None) -> list[tuple[str, int]]:
        """Step-chart series ``[(iso_time, value), …]`` oldest first: the value in force at the window start,
        one point per change, and a closing point at the window end carrying the last value."""
        lo, hi = self.window_bounds(window, since, until)
        end = hi or iso_utc(now_utc())
        with self._txn(label="price.chart") as session:
            rows = sorted(self._store.price_history(session, product_id=product_id, field=field, since=lo, until=hi), key=lambda r: (r.effective_from, r.id))
            opening = self._store.last_price_before(session, product_id=product_id, field=field, before=lo) if lo else None
        points: list[tuple[str, int]] = []
        if lo and opening is not None:
            points.append((lo, opening))
        for r in rows:
            if r.new_value is not None:
                points.append((r.effective_from, r.new_value))
        if points and points[-1][0] != end:
            points.append((end, points[-1][1]))
        return points
