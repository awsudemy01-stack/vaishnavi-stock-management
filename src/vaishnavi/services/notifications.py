"""NotificationService — the notification centre behind the bell icon (ADR-038; stdlib).

Anything the Owner should see ends up here: alert firings, queued backups, low stock, unclean exits,
restored databases, hardware changes. Rows are per user or broadcast (``user_id NULL``); an **open**
(unread) alert with the same ``kind`` + ``source_ref`` is not repeated, so a flapping printer does
not create fifty rows. ``subscribe(bus)`` wires the domain events of ``core.events``.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.events import BackupDone, DbRestoredDetected, EventBus, HardwareStatusChanged, NotificationCreated, StockChanged, WorkSessionEvent, bus
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.notifications")

SEVERITIES: Final = ("INFO", "WARN", "HIGH")


class NotificationStore(Protocol):
    """What the service needs from the ``notification`` table."""

    def insert(self, session: Session, *, user_id: int | None, severity: str, kind: str, title: str, body: str | None, deep_link: str | None, created_at: str, source_ref: str | None) -> int:  # pragma: no cover - protocol
        """Insert → id."""
        ...

    def unread(self, session: Session, *, user_id: int) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        """Unread for the user (own + broadcast)."""
        ...

    def recent(self, session: Session, *, user_id: int, limit: int = 50) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        """Newest rows."""
        ...

    def mark_read(self, session: Session, notification_id: int, *, at: str) -> bool:  # pragma: no cover - protocol
        """Set read_at."""
        ...

    def exists_unread(self, session: Session, *, kind: str, source_ref: str | None) -> bool:  # pragma: no cover - protocol
        """Dedup probe."""
        ...


class NotificationService:
    """Create, list and acknowledge notifications."""

    def __init__(self, txn: TxnFactory, store: NotificationStore, *, event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._bus = event_bus

    def notify(self, severity: str, kind: str, title: str, *, body: str | None = None, deep_link: str | None = None, user_id: int | None = None, source_ref: str | None = None, dedupe: bool = True) -> int | None:
        """Create a notification; returns its id, or None when an identical one is still unread."""
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")
        with self._txn(label="notify") as session:
            if dedupe and self._store.exists_unread(session, kind=kind, source_ref=source_ref):
                log.debug("NOTIFY_DEDUP kind=%s ref=%s", kind, source_ref)
                return None
            nid = self._store.insert(session, user_id=user_id, severity=severity, kind=kind, title=title, body=body, deep_link=deep_link, created_at=iso_utc(), source_ref=source_ref)
        log.info("NOTIFY id=%s severity=%s kind=%s ref=%s user=%s title=%s", nid, severity, kind, source_ref or "-", user_id or "all", title)
        self._bus.publish(NotificationCreated(notification_id=nid, severity=severity, kind=kind, user_id=user_id))
        return nid

    def unread(self, *, user_id: int) -> list[dict[str, Any]]:
        """Unread notifications for the bell."""
        with self._txn(label="notify.unread") as session:
            return self._store.unread(session, user_id=user_id)

    def unread_count(self, *, user_id: int) -> int:
        """Badge number."""
        return len(self.unread(user_id=user_id))

    def recent(self, *, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """History list."""
        with self._txn(label="notify.recent") as session:
            return self._store.recent(session, user_id=user_id, limit=limit)

    def mark_read(self, *, user_id: int, notification_id: int) -> bool:
        """Acknowledge one notification."""
        with self._txn(label="notify.read") as session:
            ok = self._store.mark_read(session, notification_id, at=iso_utc())
        log.debug("NOTIFY_READ id=%s user=%s ok=%s", notification_id, user_id, ok)
        return ok

    # -- event wiring -------------------------------------------------------------------------
    def subscribe(self, event_bus: EventBus | None = None) -> None:
        """Turn domain events into notifications."""
        b = event_bus or self._bus

        def on_backup(e: BackupDone) -> None:
            if e.queued:
                self.notify("WARN", "BACKUP_QUEUED", "Backup drive not found — backup saved locally", body="It is copied to the backup drive as soon as it is plugged in.", deep_link="/settings/backups", source_ref="E-BKP-001")

        def on_stock(e: StockChanged) -> None:
            if e.low_stock:
                self.notify("INFO", "LOW_STOCK", f"Low stock: {e.product_name or e.product_id}", body=f"{e.qty_after} left", deep_link=f"/stock?product={e.product_id}", source_ref=f"product:{e.product_id}")

        def on_restored(e: DbRestoredDetected) -> None:
            self.notify("HIGH", "DB_RESTORED", f"Running on a backup from {e.taken_at}", body="Bills after that time must be re-entered from paper receipts.", deep_link="/settings/backups", source_ref=f"backup:{e.backup_id}")

        def on_hardware(e: HardwareStatusChanged) -> None:
            if e.device == "scanner" and not e.present:
                self.notify("WARN", "SCANNER_MISSING", "Barcode scanner not connected", body="Staff cannot sell until it is back.", deep_link="/settings/hardware", source_ref="E-SCN-001")
            if e.device == "printer" and e.present:
                self.notify("INFO", "PRINTER_APPEARED", f"New printer detected: {e.detail}", body="Set it as the receipt printer?", deep_link="/settings/hardware", source_ref=f"printer:{e.detail}")

        def on_session(e: WorkSessionEvent) -> None:
            if e.kind == "HEARTBEAT_LOST":
                self.notify("WARN", "SESSION_UNCLEAN", "A staff session ended without logging out", deep_link="/reports/attendance", source_ref=f"session:{e.session_id}")

        b.subscribe(BackupDone, on_backup)
        b.subscribe(StockChanged, on_stock)
        b.subscribe(DbRestoredDetected, on_restored)
        b.subscribe(HardwareStatusChanged, on_hardware)
        b.subscribe(WorkSessionEvent, on_session)
        log.debug("NOTIFICATIONS_SUBSCRIBED")
