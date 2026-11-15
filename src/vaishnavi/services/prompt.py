"""PromptService — versioned Owner instructions with the red-team quick-check (docs/06 §7 layer 4).

A new version is *proposed* (stored, ``redteam_passed`` set by the quick-check) and only a passing version
can be *activated*; rollback is activation of an older version. Every step is audited
(``PROMPT_VERSION_CREATED`` / ``PROMPT_ACTIVATED``). The locked core's hash is stored with each row.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from vaishnavi.ai import guardrails
from vaishnavi.ai import prompts as prompt_assets
from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import PromptVersionRecord
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.audit import AuditService

log = logging.getLogger("vaishnavi.services.prompt")


class PromptError(VaishnaviError):
    """E-GRD — instructions rejected."""

    area = "GRD"
    default_code = "E-GRD-006"


class PromptStore(Protocol):
    """Persistence."""

    def versions(self, session: Session) -> list[PromptVersionRecord]: ...  # pragma: no cover - protocol
    def active(self, session: Session) -> PromptVersionRecord | None: ...  # pragma: no cover - protocol
    def insert(self, session: Session, *, version: int, editable_instructions: str, core_hash: str, default_language: str, extra_blocked_phrases: str, redteam_passed: bool, created_by: int | None, created_at: str, note: str | None) -> int: ...  # pragma: no cover - protocol
    def activate(self, session: Session, version: int) -> None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class Proposal:
    """Outcome of ``propose``."""

    version: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Passed the quick-check."""
        return not self.problems


class PromptService:
    """Owner-editable instructions."""

    def __init__(self, txn: TxnFactory, store: PromptStore, audit: AuditService) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit

    def active(self) -> PromptVersionRecord:
        """The active version (seeded version 1 always exists)."""
        with self._txn(label="prompt.active") as session:
            row = self._store.active(session)
        if row is None:  # pragma: no cover - seed guarantees one
            raise PromptError(detail="no active prompt version")
        return row

    def versions(self) -> list[PromptVersionRecord]:
        """Newest first."""
        with self._txn(label="prompt.versions") as session:
            return self._store.versions(session)

    def extra_blocked(self) -> list[str]:
        """Owner-added blocked phrases of the active version."""
        try:
            return [str(p) for p in json.loads(self.active().extra_blocked_phrases or "[]")]
        except ValueError:
            return []

    def propose(self, owner: Actor, text: str, *, note: str = "", default_language: str | None = None, extra_blocked: list[str] | None = None) -> Proposal:
        """Store a new version; the quick-check result decides whether it may be activated."""
        require(owner, Permission.MANAGE_SYSTEM)
        text = text.strip()
        if not text:
            raise PromptError(detail="instructions are empty")
        problems = guardrails.quick_check_instructions(text)
        with self._txn(label="prompt.propose") as session:
            current = self._store.active(session)
            number = max((v.version for v in self._store.versions(session)), default=0) + 1
            self._store.insert(session, version=number, editable_instructions=text, core_hash=prompt_assets.core_hash(), default_language=default_language or (current.default_language if current else "hinglish"), extra_blocked_phrases=json.dumps(extra_blocked if extra_blocked is not None else (json.loads(current.extra_blocked_phrases) if current else [])), redteam_passed=not problems, created_by=owner.user_id, created_at=iso_utc(), note=note or None)
            self._audit.record(session, "PROMPT_VERSION_CREATED", user_id=owner.user_id, entity="prompt_version", entity_id=number, after={"version": number, "passed": not problems, "problems": problems[:5], "chars": len(text)})
        log.info("PROMPT_VERSION_CREATED version=%d passed=%s problems=%d", number, not problems, len(problems))
        return Proposal(number, problems)

    def activate(self, owner: Actor, version: int) -> None:
        """Make a passing version active."""
        require(owner, Permission.MANAGE_SYSTEM)
        with self._txn(label="prompt.activate") as session:
            rows = {v.version: v for v in self._store.versions(session)}
            target = rows.get(version)
            if target is None:
                raise PromptError(detail=f"version {version} does not exist")
            if not target.redteam_passed:
                raise PromptError(detail=f"version {version} failed the quick-check and cannot be activated")
            current = self._store.active(session)
            self._store.activate(session, version)
            self._audit.record(session, "PROMPT_ACTIVATED", user_id=owner.user_id, entity="prompt_version", entity_id=version, before={"version": current.version if current else None}, after={"version": version})
        log.info("PROMPT_ACTIVATED version=%d", version)

    def rollback(self, owner: Actor, version: int) -> None:
        """Alias of activate for an older version (reads better in the UI)."""
        self.activate(owner, version)

    def as_dict(self, row: PromptVersionRecord) -> dict[str, Any]:
        """For the editor."""
        return {"version": row.version, "text": row.editable_instructions, "active": bool(row.is_active), "passed": bool(row.redteam_passed), "created_at": row.created_at, "note": row.note or ""}
