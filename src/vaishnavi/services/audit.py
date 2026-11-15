"""AuditService — hash-chained audit rows for every mutation, mirrored to ``audit.jsonl`` (stdlib).

Every service calls :meth:`AuditService.record` **inside its own transaction**, so the audit row commits
or rolls back together with the change it describes. Rows are chained: ``hash = sha256(canonical JSON of
the row + prev_hash)``; the first row's ``prev_hash`` is :data:`GENESIS`. The table itself is append-only
(``trg_audit_log_no_update/delete``), so a tampered or deleted row breaks the chain — which
:func:`verify_chain` (and the nightly ledger check, unit 21) detects and reports as ``E-LEDGER-001``.

``before``/``after`` are plain dicts of the fields that changed. Callers must never put secrets in them;
:class:`AuditService` additionally scrubs keys that look like secrets as a second line of defence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.logging import current_cid
from vaishnavi.db.records import GENESIS_HASH, AuditEntry
from vaishnavi.services.common import Session

log = logging.getLogger("vaishnavi.services.audit")
audit_log = logging.getLogger("vaishnavi.audit")

GENESIS: Final = GENESIS_HASH
SECRET_KEY_MARKERS: Final = ("password", "pin", "api_key", "token", "secret", "ciphertext", "recovery")


# ----------------------------------------------------------------------------- pure functions
def canonical_json(value: Mapping[str, Any] | None) -> str | None:
    """Deterministic JSON (sorted keys, no spaces, UTF-8 kept) — the only form that is hashed and stored."""
    if value is None:
        return None
    return json.dumps(scrub_secrets(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def scrub_secrets(value: Mapping[str, Any]) -> dict[str, Any]:
    """Replace values of secret-looking keys with ``***`` (recursively)."""
    out: dict[str, Any] = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in SECRET_KEY_MARKERS):
            out[str(key)] = "***"
        elif isinstance(item, Mapping):
            out[str(key)] = scrub_secrets(item)
        else:
            out[str(key)] = item
    return out


def compute_hash(entry: AuditEntry) -> str:
    """SHA-256 over the canonical row content and the previous hash."""
    material = "\x1f".join(
        [
            entry.prev_hash,
            entry.at,
            str(entry.user_id if entry.user_id is not None else ""),
            str(entry.actor_user_id if entry.actor_user_id is not None else ""),
            entry.action,
            entry.entity or "",
            str(entry.entity_id if entry.entity_id is not None else ""),
            entry.before or "",
            entry.after or "",
            entry.correlation_id or "",
            entry.ip or "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_chain(entries: Iterable[AuditEntry], *, expected_prev: str = GENESIS) -> int | None:
    """Return the id (or 1-based position) of the first broken row, or ``None`` when the chain is intact."""
    prev = expected_prev
    for position, entry in enumerate(entries, start=1):
        if entry.prev_hash != prev or compute_hash(entry) != entry.hash:
            broken = entry.id if entry.id is not None else position
            log.error("AUDIT_CHAIN_BROKEN at=%s action=%s row=%s", entry.at, entry.action, broken)
            return broken
        prev = entry.hash
    return None


# ----------------------------------------------------------------------------- store protocol
class AuditStore(Protocol):
    """What the service needs from ``AuditRepo``."""

    def last_hash(self, session: Session) -> str:  # pragma: no cover - protocol
        """Hash of the newest row, or :data:`GENESIS` when the table is empty."""
        ...

    def append(self, session: Session, entry: AuditEntry) -> AuditEntry:  # pragma: no cover - protocol
        """Insert the row and return it with ``id`` filled."""
        ...


# ----------------------------------------------------------------------------- service
class AuditService:
    """Append chained audit rows; one instance shared by all services."""

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def record(
        self,
        session: Session,
        action: str,
        *,
        user_id: int | None = None,
        actor_user_id: int | None = None,
        entity: str | None = None,
        entity_id: int | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> AuditEntry:
        """Append one row inside the caller's transaction and mirror it to ``audit.jsonl``.

        ``user_id`` is the account the action is attributed to; ``actor_user_id`` is who physically did
        it when different (Owner PIN override on a staff screen → ``user_id=staff, actor_user_id=owner``).
        """
        if not action or not action.isupper():
            raise ValueError(f"audit action must be an UPPER_SNAKE identifier, got {action!r}")
        entry = AuditEntry(
            at=iso_utc(),
            action=action,
            user_id=user_id,
            actor_user_id=actor_user_id if actor_user_id != user_id else None,
            entity=entity,
            entity_id=entity_id,
            before=canonical_json(before),
            after=canonical_json(after),
            correlation_id=(current_cid() if current_cid() not in ("", "-") else None),
            ip=ip,
            prev_hash=self._store.last_hash(session),
        )
        entry = replace(entry, hash=compute_hash(entry))
        stored = self._store.append(session, entry)
        audit_log.info(
            "AUDIT %s",
            action,
            extra={
                "audit_id": stored.id,
                "action": action,
                "user_id": user_id,
                "actor_user_id": entry.actor_user_id,
                "entity": entity,
                "entity_id": entity_id,
                "before": entry.before,
                "after": entry.after,
                "hash": stored.hash,
            },
        )
        log.debug("AUDIT_ROW id=%s action=%s entity=%s:%s user=%s actor=%s", stored.id, action, entity, entity_id, user_id, entry.actor_user_id)
        return stored
