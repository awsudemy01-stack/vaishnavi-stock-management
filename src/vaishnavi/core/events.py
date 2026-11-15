"""Tiny in-process publish/subscribe bus (stdlib only).

Services publish facts (``SaleCommitted``, ``ProductChanged``, ``ShiftClosed`` …); the AI indexer,
the dashboard and the notification centre subscribe. Delivery is synchronous and **isolated**: a
failing subscriber is logged with its error and never breaks the publisher — a dashboard glitch
must not roll back a sale. Events are frozen dataclasses so subscribers cannot mutate them.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from vaishnavi.core.clock import now_utc

log = logging.getLogger("vaishnavi.core.events")


@dataclass(frozen=True)
class Event:
    """Base class; ``at`` is stamped from the clock when the event is created."""

    at: datetime = field(default_factory=now_utc, kw_only=True)
    correlation_id: str = field(default="", kw_only=True)


# ----------------------------------------------------------------------------- domain events
@dataclass(frozen=True)
class ProductChanged(Event):
    """A product was created or edited (name, price, barcode, …) — AI index refresh, tile refresh."""

    product_id: int
    change: str  # created | updated | price | barcode | archived


@dataclass(frozen=True)
class StockChanged(Event):
    """A stock movement was committed."""

    product_id: int
    movement_type: str  # SALE | PURCHASE | ADJUSTMENT | …
    qty_delta: int  # base units, signed
    qty_after: int
    low_stock: bool = False
    """True when ``qty_after`` fell to/below the product's minimum (notification centre, dashboard badge)."""
    product_name: str = ""


@dataclass(frozen=True)
class SaleCommitted(Event):
    """A sale was committed (invoice issued)."""

    sale_id: int
    invoice_no: str
    total_paise: int
    user_id: int
    line_count: int


@dataclass(frozen=True)
class ShiftClosed(Event):
    """A cashier shift was closed."""

    shift_id: int
    user_id: int
    variance_paise: int


@dataclass(frozen=True)
class WorkSessionEvent(Event):
    """Login / End-work / heartbeat-lost for attendance."""

    session_id: int
    user_id: int
    kind: str  # LOGIN | LOGOUT | HEARTBEAT_LOST | CONFIRMED


@dataclass(frozen=True)
class HardwareStatusChanged(Event):
    """Printer / scanner / scale appeared or disappeared."""

    device: str  # printer | scanner | scale
    present: bool
    detail: str = ""


@dataclass(frozen=True)
class SettingsChanged(Event):
    """One or more Owner-editable settings were saved (listeners re-read the effective settings)."""

    keys: tuple[str, ...]
    user_id: int = 0


@dataclass(frozen=True)
class BackupDone(Event):
    """A verified snapshot was written (to the secondary, or queued locally when it is unplugged)."""

    backup_id: str
    path: str
    trigger: str
    queued: bool
    size_bytes: int


@dataclass(frozen=True)
class RestoreDone(Event):
    """The live database was replaced from a snapshot (in-app restore) — the app must restart."""

    backup_id: str
    taken_at: str
    pre_restore_backup_id: str


@dataclass(frozen=True)
class DbRestoredDetected(Event):
    """Start-up found a restored copy (backup id present in ``db_identity``) — Owner banner."""

    backup_id: str
    taken_at: str


@dataclass(frozen=True)
class NotificationCreated(Event):
    """A notification row was written (the bell refreshes)."""

    notification_id: int | None
    severity: str
    kind: str
    user_id: int | None = None


@dataclass(frozen=True)
class Alert(Event):
    """Something the owner should see in the notification centre."""

    kind: str
    severity: str  # INFO | WARN | ERROR
    message: str
    link: str = ""


E = TypeVar("E", bound=Event)
Handler = Callable[[Any], None]


class EventBus:
    """Synchronous bus; thread-safe registration; subscriber errors are isolated."""

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()
        self.published = 0
        self.failed = 0

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type`` (and subclasses); returns an unsubscribe function."""
        with self._lock:
            self._handlers[event_type].append(handler)  # type: ignore[arg-type]
        log.debug("events: subscribed %s -> %s", event_type.__name__, getattr(handler, "__qualname__", handler))

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._handlers[event_type].remove(handler)  # type: ignore[arg-type]
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, event: Event) -> int:
        """Deliver to every matching handler; returns how many handlers ran without error."""
        with self._lock:
            targets = [h for et, hs in self._handlers.items() if isinstance(event, et) for h in hs]
        self.published += 1
        ok = 0
        log.debug("events: publish %s cid=%s handlers=%d", type(event).__name__, event.correlation_id, len(targets))
        for handler in targets:
            try:
                handler(event)
                ok += 1
            except Exception as exc:  # isolate: one bad subscriber never breaks the publisher
                self.failed += 1
                log.exception("events: handler %s failed on %s: %r", getattr(handler, "__qualname__", handler), type(event).__name__, exc)
        return ok

    def clear(self) -> None:
        """Remove all handlers (tests / shutdown)."""
        with self._lock:
            self._handlers.clear()


bus = EventBus()
"""Process-wide bus used by services; tests may construct their own ``EventBus``."""
