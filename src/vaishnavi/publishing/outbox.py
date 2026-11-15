"""Outbox — the ``publish_queue`` table with exponential retry (docs/05 §11).

A document is queued once per channel; delivery attempts that fail set ``next_retry_at`` from the backoff
ladder and, after ``max_attempts``, mark the row ``FAILED`` (the chip turns red and the Owner is told).
Rows never carry credentials — only channel, document kind and file path.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, now_utc
from vaishnavi.db.records import PublishQueueRecord as QueueItem
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.publishing.outbox")

BACKOFF_MINUTES: Final = (1, 5, 15, 60, 240)
"""Retry delays per attempt; the last value repeats until ``max_attempts``."""


class PublishStore(Protocol):
    """Persistence."""

    def insert(self, session: Session, *, channel: str, document: str, file_path: str, created_at: str) -> int: ...  # pragma: no cover - protocol
    def due(self, session: Session, *, now: str) -> list[QueueItem]: ...  # pragma: no cover - protocol
    def update(self, session: Session, item_id: int, *, status: str, attempts: int, next_retry_at: str | None, last_error: str | None) -> None: ...  # pragma: no cover - protocol
    def get(self, session: Session, item_id: int) -> QueueItem | None: ...  # pragma: no cover - protocol
    def counts(self, session: Session) -> dict[str, int]: ...  # pragma: no cover - protocol
    def recent(self, session: Session, *, limit: int) -> list[QueueItem]: ...  # pragma: no cover - protocol
    def last_sent_at(self, session: Session) -> str | None: ...  # pragma: no cover - protocol


class Outbox:
    """Queue operations."""

    def __init__(self, txn: TxnFactory, store: PublishStore, *, max_attempts: int = 10) -> None:
        self._txn = txn
        self._store = store
        self._max = max_attempts

    def enqueue(self, channel: str, document: str, path: Path) -> int:
        """Queue one delivery."""
        with self._txn(label="publish.enqueue") as session:
            qid = self._store.insert(session, channel=channel, document=document, file_path=str(path), created_at=iso_utc())
        log.info("PUBLISH_QUEUED id=%d channel=%s document=%s file=%s", qid, channel, document, path.name)
        return qid

    def due(self) -> list[QueueItem]:
        """Pending rows whose retry time has passed."""
        with self._txn(label="publish.due") as session:
            return self._store.due(session, now=iso_utc())

    def mark_sent(self, item_id: int) -> None:
        """Delivered."""
        with self._txn(label="publish.sent") as session:
            item = self._store.get(session, item_id)
            self._store.update(session, item_id, status="SENT", attempts=(item.attempts if item else 0) + 1, next_retry_at=None, last_error=None)
        log.info("PUBLISH_SENT id=%d", item_id)

    def mark_failed(self, item_id: int, error: str) -> None:
        """Schedule a retry or give up after ``max_attempts`` (``E-PUB-003`` logged either way)."""
        with self._txn(label="publish.failed") as session:
            item = self._store.get(session, item_id)
            attempts = (item.attempts if item else 0) + 1
            if attempts >= self._max:
                self._store.update(session, item_id, status="FAILED", attempts=attempts, next_retry_at=None, last_error=error[:300])
                log.error("PUBLISH_FAILED id=%d attempts=%d code=E-PUB-003 error=%s", item_id, attempts, error[:120])
                return
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            retry_at = (now_utc() + timedelta(minutes=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._store.update(session, item_id, status="PENDING", attempts=attempts, next_retry_at=retry_at, last_error=error[:300])
        log.warning("PUBLISH_RETRY id=%d attempt=%d next=%s code=E-PUB-003 error=%s", item_id, attempts, retry_at, error[:120])

    def counts(self) -> dict[str, int]:
        """``{PENDING, SENT, FAILED}``."""
        with self._txn(label="publish.counts") as session:
            raw = self._store.counts(session)
        return {k: int(raw.get(k, 0)) for k in ("PENDING", "SENT", "FAILED")}

    def recent(self, *, limit: int = 50) -> list[QueueItem]:
        """Newest rows for the Settings tab."""
        with self._txn(label="publish.recent") as session:
            return self._store.recent(session, limit=limit)

    def last_sent_at(self) -> str | None:
        """When something last went out."""
        with self._txn(label="publish.last") as session:
            return self._store.last_sent_at(session)

    def as_dict(self, item: QueueItem) -> dict[str, Any]:
        """For the UI."""
        return {"id": item.id, "channel": item.channel, "document": item.document, "file": Path(item.file_path).name, "status": item.status, "attempts": item.attempts, "next_retry_at": item.next_retry_at or "", "error": item.last_error or ""}
