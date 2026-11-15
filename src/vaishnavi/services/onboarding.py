"""OnboardingService — the first-day checklist (docs/09 §2a; stdlib).

A step is **done when the data says so** (shop address present, a scanner paired, a key acknowledged…),
not merely when somebody ticked it — so the dashboard card "Setup 6/11" is always truthful. The Owner may
**skip** an optional step (printer, payments, labels…); a skipped step is passed over by *Next* but still
listed, and can be un-skipped. Every probe is a cheap read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.onboarding")

STEPS: Final = ("shop_profile", "tax", "payments", "staff", "products", "labels", "opening_stock", "scanner", "printer", "backup_drive", "recovery_key")
OPTIONAL: Final = frozenset({"payments", "labels", "printer", "tax"})
SKIPPED_MARK: Final = "skipped"


@dataclass(frozen=True)
class StepStatus:
    """One checklist line."""

    key: str
    done: bool
    skipped: bool
    optional: bool
    done_at: str | None
    detail: str = ""


@dataclass(frozen=True)
class ChecklistItem:
    """Dashboard card line."""

    key: str
    title: str
    done: bool
    skipped: bool
    path: str


@dataclass(frozen=True)
class ChecklistCard:
    """Dashboard *Setup checklist* card."""

    title: str
    done: int
    total: int
    items: list[ChecklistItem]
    next_path: str | None
    complete: bool


class SetupStore:
    """``setup_step`` access — satisfied by ``SqliteSystemStore``."""

    def setup_steps(self, session: Session) -> dict[str, str | None]: ...  # pragma: no cover - protocol
    def mark_setup_step(self, session: Session, step: str, *, done_at: str | None, done_by: int | None) -> bool: ...  # pragma: no cover - protocol


Probe = Callable[[], tuple[bool, str]]


class OnboardingService:
    """Checklist with data-driven probes."""

    def __init__(self, txn: TxnFactory, store: Any, audit: AuditService, *, probes: dict[str, Probe] | None = None) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._probes: dict[str, Probe] = dict(probes or {})

    def register_probe(self, step: str, probe: Probe) -> None:
        """AppServices wiring: ``probe() -> (done, detail)``."""
        if step not in STEPS:
            raise ValueError(f"unknown step {step}")
        self._probes[step] = probe

    # -- reads ------------------------------------------------------------------------------------
    def status(self) -> list[StepStatus]:
        """Every step, data-probed; manual ticks (``done_by`` set) count too."""
        with self._txn(label="setup.status") as session:
            rows = self._store.setup_steps(session)
        out: list[StepStatus] = []
        for key in STEPS:
            done_at = rows.get(key)
            skipped = done_at == SKIPPED_MARK
            probe = self._probes.get(key)
            detected, detail = (False, "")
            if probe is not None:
                try:
                    detected, detail = probe()
                except Exception as exc:  # noqa: BLE001 - a probe must never break the checklist
                    log.warning("SETUP_PROBE_FAILED step=%s %r", key, exc)
            ticked = bool(done_at) and not skipped
            out.append(StepStatus(key, detected or ticked, skipped, key in OPTIONAL, None if skipped else done_at, detail))
        return out

    def progress(self) -> tuple[int, int]:
        """(done or skipped, total)."""
        status = self.status()
        return sum(1 for s in status if s.done or s.skipped), len(status)

    def next_step(self) -> str | None:
        """First step neither done nor skipped."""
        return next((s.key for s in self.status() if not s.done and not s.skipped), None)

    def is_complete(self) -> bool:
        """Everything done or skipped."""
        done, total = self.progress()
        return done == total

    def checklist(self, texts: Any) -> ChecklistCard:
        """Dashboard card in the owner's language (``texts`` = ``UiTexts``)."""
        status = self.status()
        done = sum(1 for s in status if s.done or s.skipped)
        items = [ChecklistItem(s.key, texts.t(f"setup.step.{s.key}"), s.done, s.skipped, f"/onboarding?step={s.key}") for s in status]
        nxt = next((s.key for s in status if not s.done and not s.skipped), None)
        return ChecklistCard(texts.t("setup.card_title", done=done, total=len(status)), done, len(status), items, f"/onboarding?step={nxt}" if nxt else None, nxt is None)

    # -- writes -----------------------------------------------------------------------------------
    def mark_done(self, actor: Actor, step: str) -> None:
        """Explicit tick (used for steps without a data probe, e.g. the owner confirms the printer is fine)."""
        self._set(actor, step, iso_utc(), actor.user_id, "SETUP_STEP_DONE")

    def skip(self, actor: Actor, step: str) -> None:
        """Pass over an optional step for now."""
        self._set(actor, step, SKIPPED_MARK, actor.user_id, "SETUP_STEP_SKIPPED")

    def unskip(self, actor: Actor, step: str) -> None:
        """Bring a skipped step back."""
        self._set(actor, step, None, None, "SETUP_STEP_REOPENED")

    def _set(self, actor: Actor, step: str, done_at: str | None, done_by: int | None, action: str) -> None:
        require(actor, Permission.MANAGE_SYSTEM)
        if step not in STEPS:
            raise ValueError(f"unknown step {step}")
        with self._txn(label="setup.mark") as session:
            self._store.mark_setup_step(session, step, done_at=done_at, done_by=done_by)
            self._audit.record(session, action, user_id=actor.user_id, entity="setup_step", after={"step": step})
        log.info("%s step=%s by=%s", action, step, actor.user_id)
