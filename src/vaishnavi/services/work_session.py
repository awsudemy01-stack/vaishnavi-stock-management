"""WorkSessionService — attendance: login opens a session, three layers capture the exit (docs/05 §6a; stdlib).

* **End work** (the intended path): summary → open-shift check (``E-AUTH-020`` unless the Owner PIN was
  entered) → ``LOGOUT``.
* **Window closed**: the browser's beacon → ``WINDOW_CLOSED`` at that instant, *flagged*.
* **Heartbeat lost**: the 15-s heartbeat stops for 60 s → ``HEARTBEAT_LOST`` with ``last_seen_at`` as the
  exit time, *flagged*. Flagged sessions must be confirmed by the user at the next login (``E-AUTH-021``
  guards the POS until then).

One active session per user (a second login ends the first as ``FORCED_BY_LOGIN``); idle lock time is
counted; the app closes everything as ``APP_SHUTDOWN`` on a clean stop and recovers forgotten sessions as
``HEARTBEAT_LOST`` after a crash. Every transition is audited and published as ``WorkSessionEvent``.
The service keeps no state of its own — the browser's ``ClientState`` carries ``locked_since``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, now_utc, parse_iso_utc, to_ist
from vaishnavi.core.errors import AuthError
from vaishnavi.core.events import EventBus, WorkSessionEvent, bus
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.db.records import WorkSessionRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.work_session")

HEARTBEAT_TIMEOUT_S: Final = 60
"""Silence after which an open session is closed as ``HEARTBEAT_LOST`` (docs/05 §6a layer 3)."""


class EndReason(str, Enum):
    """``work_session.end_reason`` values (schema CHECK)."""

    LOGOUT = "LOGOUT"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    HEARTBEAT_LOST = "HEARTBEAT_LOST"
    IDLE_LOCK_TIMEOUT = "IDLE_LOCK_TIMEOUT"
    APP_SHUTDOWN = "APP_SHUTDOWN"
    FORCED_BY_OWNER = "FORCED_BY_OWNER"
    FORCED_BY_LOGIN = "FORCED_BY_LOGIN"


UNCLEAN_REASONS: Final = frozenset({EndReason.WINDOW_CLOSED.value, EndReason.HEARTBEAT_LOST.value})


# ----------------------------------------------------------------------------- results
@dataclass(frozen=True)
class StartResult:
    """What a login gets back."""

    session_id: int
    login_at: str
    forced_session_ids: list[int]
    unconfirmed: WorkSessionRecord | None
    """The user's last unclean session that still needs confirming (banner + confirmation step)."""


@dataclass(frozen=True)
class EndWorkPreview:
    """The End-work popup's content."""

    session_id: int
    login_at: str
    now: str
    duration_seconds: int
    bills: int
    sales_paise: int
    open_shift: bool
    open_shift_id: int | None

    @property
    def duration_text(self) -> str:
        """``9 h 15 m``."""
        return duration_text(self.duration_seconds)


@dataclass(frozen=True)
class HistoryRow:
    """One line of *Reports → Staff work history*."""

    id: int
    user_id: int
    login_at: str
    logout_at: str | None
    end_reason: str | None
    duration_seconds: int
    bills: int
    sales_paise: int
    idle_seconds: int
    lock_count: int
    confirmed_at: str | None
    confirmed_note: str | None
    owner_note: str | None
    client: str | None

    @property
    def unclean(self) -> bool:
        """Ended without the End-work popup."""
        return self.end_reason in UNCLEAN_REASONS

    @property
    def open(self) -> bool:
        """Still logged in."""
        return self.logout_at is None


def duration_text(seconds: int) -> str:
    """``9 h 15 m`` / ``45 m`` / ``0 m``."""
    hours, rem = divmod(max(0, int(seconds)), 3600)
    minutes = rem // 60
    return f"{hours} h {minutes} m" if hours else f"{minutes} m"


# ----------------------------------------------------------------------------- store protocol
class WorkSessionStore(Protocol):
    """What the service needs from the database."""

    def insert(self, session: Session, *, user_id: int, app_run_id: int | None, login_at: str, client: str | None) -> int:  # pragma: no cover - protocol
        """Open → id."""
        ...

    def by_id(self, session: Session, session_id: int) -> WorkSessionRecord | None:  # pragma: no cover - protocol
        """One row."""
        ...

    def open_sessions(self, session: Session, *, user_id: int | None = None) -> list[WorkSessionRecord]:  # pragma: no cover - protocol
        """Open rows."""
        ...

    def stale(self, session: Session, *, last_seen_before: str) -> list[WorkSessionRecord]:  # pragma: no cover - protocol
        """Open rows with an old heartbeat."""
        ...

    def last_unconfirmed(self, session: Session, *, user_id: int) -> WorkSessionRecord | None:  # pragma: no cover - protocol
        """Newest flagged row."""
        ...

    def history(self, session: Session, *, user_id: int | None, since: str | None, until: str | None, limit: int) -> list[WorkSessionRecord]:  # pragma: no cover - protocol
        """Newest first."""
        ...

    def recent_end_reasons(self, session: Session, *, user_id: int, limit: int) -> list[str | None]:  # pragma: no cover - protocol
        """For the streak."""
        ...

    def update(self, session: Session, session_id: int, **fields: Any) -> None:  # pragma: no cover - protocol
        """Set columns."""
        ...

    def sales_in_window(self, session: Session, *, user_id: int, since: str, until: str) -> tuple[int, int]:  # pragma: no cover - protocol
        """``(bills, paise)``."""
        ...

    def open_shift_id(self, session: Session, *, user_id: int) -> int | None:  # pragma: no cover - protocol
        """The user's open shift."""
        ...


# ----------------------------------------------------------------------------- service
class WorkSessionService:
    """Attendance."""

    def __init__(self, txn: TxnFactory, store: WorkSessionStore, audit: AuditService, *, event_bus: EventBus = bus, app_run_id: int | None = None, heartbeat_timeout_s: int = HEARTBEAT_TIMEOUT_S) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._bus = event_bus
        self.app_run_id = app_run_id
        self.heartbeat_timeout_s = heartbeat_timeout_s

    # -- login / logout -------------------------------------------------------------------------
    def start(self, *, user_id: int, client: str | None) -> StartResult:
        """Open a session at the server time; any other open session of the user ends as ``FORCED_BY_LOGIN``."""
        now = iso_utc()
        with self._txn(label="ws.start") as session:
            forced: list[int] = []
            for other in self._store.open_sessions(session, user_id=user_id):
                self._close(session, other, EndReason.FORCED_BY_LOGIN, at=other.last_seen_at)
                forced.append(other.id)
            sid = self._store.insert(session, user_id=user_id, app_run_id=self.app_run_id, login_at=now, client=client)
            self._audit.record(session, "LOGIN", user_id=user_id, entity="work_session", entity_id=sid, after={"client": client, "forced": forced})
            unconfirmed = self._store.last_unconfirmed(session, user_id=user_id)
        log.info("WORK_SESSION_START id=%s user=%s client=%s forced=%s unconfirmed=%s", sid, user_id, client, forced or "-", unconfirmed.id if unconfirmed else "-")
        self._bus.publish(WorkSessionEvent(session_id=sid, user_id=user_id, kind="LOGIN"))
        for fid in forced:
            self._bus.publish(WorkSessionEvent(session_id=fid, user_id=user_id, kind=EndReason.FORCED_BY_LOGIN.value))
        return StartResult(session_id=sid, login_at=now, forced_session_ids=forced, unconfirmed=unconfirmed)

    def end_work_preview(self, session_id: int) -> EndWorkPreview:
        """Numbers for the popup (derived from ``sale`` / ``shift`` by time window — nothing stored)."""
        now = now_utc()
        with self._txn(label="ws.preview") as session:
            row = self._require(session, session_id)
            bills, paise = self._store.sales_in_window(session, user_id=row.user_id, since=row.login_at, until=iso_utc(now))
            shift_id = self._store.open_shift_id(session, user_id=row.user_id)
        seconds = int((now - parse_iso_utc(row.login_at)).total_seconds())
        return EndWorkPreview(session_id=session_id, login_at=row.login_at, now=iso_utc(now), duration_seconds=seconds, bills=bills, sales_paise=paise, open_shift=shift_id is not None, open_shift_id=shift_id)

    def end(self, session_id: int, *, actor: Actor, note: str | None = None) -> None:
        """End work (``LOGOUT``). An open shift needs the Owner PIN (``E-AUTH-020``)."""
        with self._txn(label="ws.end") as session:
            row = self._require(session, session_id)
            shift_id = self._store.open_shift_id(session, user_id=row.user_id)
            if shift_id is not None and not actor.pin_verified and actor.role is not Role.OWNER:
                log.warning("END_WORK_REFUSED session=%s user=%s open_shift=%s code=E-AUTH-020", session_id, row.user_id, shift_id)
                raise AuthError("E-AUTH-020", shift_id=shift_id, detail="open shift")
            if not row.is_open:
                log.info("END_WORK_ALREADY_CLOSED session=%s reason=%s", session_id, row.end_reason)
                return
            now = iso_utc()
            self._store.update(session, session_id, logout_at=now, end_reason=EndReason.LOGOUT.value, confirmed_note=(note or None), last_seen_at=now)
            self._audit.record(session, "LOGOUT", user_id=row.user_id, actor_user_id=actor.user_id, entity="work_session", entity_id=session_id, after={"note": note, "open_shift_override": shift_id is not None})
        log.info("WORK_SESSION_END id=%s user=%s reason=LOGOUT", session_id, row.user_id)
        self._bus.publish(WorkSessionEvent(session_id=session_id, user_id=row.user_id, kind="LOGOUT"))

    # -- liveness -------------------------------------------------------------------------------
    def heartbeat(self, session_id: int) -> bool:
        """Browser heartbeat / any action; False when the session is already closed (the page must go to login)."""
        with self._txn(label="ws.heartbeat") as session:
            row = self._store.by_id(session, session_id)
            if row is None or not row.is_open:
                return False
            self._store.update(session, session_id, last_seen_at=iso_utc())
        return True

    def window_closed(self, session_id: int) -> bool:
        """The ``sendBeacon`` on window close → ``WINDOW_CLOSED`` now (flagged). False when already closed."""
        return self._end_unclean(session_id, EndReason.WINDOW_CLOSED, at=iso_utc())

    def monitor(self) -> int:
        """Scheduler job (every 15 s): close sessions silent for longer than the timeout; returns how many."""
        return self.monitor_at(now_utc())

    def monitor_at(self, now: datetime) -> int:
        """``monitor`` at an explicit instant (tests / catch-up)."""
        cutoff = iso_utc(now - timedelta(seconds=self.heartbeat_timeout_s))
        closed: list[WorkSessionRecord] = []
        with self._txn(label="ws.monitor") as session:
            for row in self._store.stale(session, last_seen_before=cutoff):
                self._close(session, row, EndReason.HEARTBEAT_LOST, at=row.last_seen_at)
                closed.append(row)
        for row in closed:
            log.warning("WORK_SESSION_HEARTBEAT_LOST id=%s user=%s last_seen=%s", row.id, row.user_id, row.last_seen_at)
            self._bus.publish(WorkSessionEvent(session_id=row.id, user_id=row.user_id, kind=EndReason.HEARTBEAT_LOST.value))
        return len(closed)

    # -- idle lock ------------------------------------------------------------------------------
    def record_lock(self, session_id: int) -> None:
        """The idle lock engaged (the browser remembers since when)."""
        with self._txn(label="ws.lock") as session:
            row = self._require(session, session_id)
            self._store.update(session, session_id, lock_count=row.lock_count + 1, last_seen_at=iso_utc())
        log.debug("WORK_SESSION_LOCK id=%s count=%s", session_id, row.lock_count + 1)

    def record_unlock(self, session_id: int, *, locked_seconds: int) -> None:
        """PIN accepted — the locked time counts as idle."""
        with self._txn(label="ws.unlock") as session:
            row = self._require(session, session_id)
            self._store.update(session, session_id, idle_seconds=row.idle_seconds + max(0, int(locked_seconds)), last_seen_at=iso_utc())
        log.debug("WORK_SESSION_UNLOCK id=%s idle_added=%ss", session_id, locked_seconds)

    def idle_lock_timeout(self, session_id: int, *, locked_since: datetime) -> bool:
        """Locked and never unlocked → the session ended when the lock engaged."""
        at = iso_utc(locked_since)
        with self._txn(label="ws.idle_timeout") as session:
            row = self._store.by_id(session, session_id)
            if row is None or not row.is_open:
                return False
            self._close(session, row, EndReason.IDLE_LOCK_TIMEOUT, at=at)
        log.info("WORK_SESSION_END id=%s user=%s reason=IDLE_LOCK_TIMEOUT at=%s", session_id, row.user_id, at)
        self._bus.publish(WorkSessionEvent(session_id=session_id, user_id=row.user_id, kind=EndReason.IDLE_LOCK_TIMEOUT.value))
        return True

    # -- app lifecycle --------------------------------------------------------------------------
    def close_all(self, reason: EndReason = EndReason.APP_SHUTDOWN) -> int:
        """Clean stop: every open session ends now."""
        now = iso_utc()
        with self._txn(label="ws.close_all") as session:
            rows = self._store.open_sessions(session)
            for row in rows:
                self._close(session, row, reason, at=now)
        log.info("WORK_SESSIONS_CLOSED count=%d reason=%s", len(rows), reason.value)
        return len(rows)

    def recover_unclean(self) -> int:
        """After an unclean stop: sessions left open end at their last heartbeat (``HEARTBEAT_LOST``, flagged)."""
        with self._txn(label="ws.recover") as session:
            rows = self._store.open_sessions(session)
            for row in rows:
                self._close(session, row, EndReason.HEARTBEAT_LOST, at=row.last_seen_at)
        if rows:
            log.warning("WORK_SESSIONS_RECOVERED count=%d (previous run did not stop cleanly)", len(rows))
        for row in rows:
            self._bus.publish(WorkSessionEvent(session_id=row.id, user_id=row.user_id, kind=EndReason.HEARTBEAT_LOST.value))
        return len(rows)

    # -- confirmation of unclean exits ----------------------------------------------------------
    def pending_confirmation(self, *, user_id: int) -> WorkSessionRecord | None:
        """The flagged session the user still has to confirm (banner on the login tile)."""
        with self._txn(label="ws.pending") as session:
            return self._store.last_unconfirmed(session, user_id=user_id)

    def require_confirmed(self, *, user_id: int) -> None:
        """``E-AUTH-021`` while a flagged session is unconfirmed (guards the POS after login)."""
        pending = self.pending_confirmation(user_id=user_id)
        if pending is not None:
            raise AuthError("E-AUTH-021", ended_at=to_ist(parse_iso_utc(pending.logout_at or pending.last_seen_at)).strftime("%d-%b %H:%M"), session_id=pending.id)

    def confirm(self, session_id: int, *, user_id: int, note: str | None = None) -> None:
        """The user confirms the recorded exit time (optionally with a note)."""
        with self._txn(label="ws.confirm") as session:
            row = self._require(session, session_id)
            if row.user_id != user_id:
                log.warning("SESSION_CONFIRM_REFUSED session=%s user=%s owner=%s code=E-AUTH-010", session_id, user_id, row.user_id)
                raise AuthError("E-AUTH-010", permission="SESSION_CONFIRM", detail="not your session")
            self._store.update(session, session_id, confirmed_at=iso_utc(), confirmed_note=(note or None))
            self._audit.record(session, "SESSION_CONFIRMED", user_id=user_id, entity="work_session", entity_id=session_id, after={"note": note, "exit": row.logout_at})
        log.info("WORK_SESSION_CONFIRMED id=%s user=%s", session_id, user_id)
        self._bus.publish(WorkSessionEvent(session_id=session_id, user_id=user_id, kind="CONFIRMED"))

    # -- owner tools ----------------------------------------------------------------------------
    def force_end(self, owner: Actor, session_id: int, *, note: str | None = None) -> None:
        """Owner ends a stale session (tablet left logged in) — ``FORCED_BY_OWNER``, audited."""
        require(owner, Permission.MANAGE_SYSTEM)
        with self._txn(label="ws.force_end") as session:
            row = self._require(session, session_id)
            if row.is_open:
                self._close(session, row, EndReason.FORCED_BY_OWNER, at=iso_utc(), actor_user_id=owner.user_id)
            if note:
                self._store.update(session, session_id, owner_note=note)
        log.info("WORK_SESSION_FORCED id=%s user=%s by=%s", session_id, row.user_id, owner.user_id)
        self._bus.publish(WorkSessionEvent(session_id=session_id, user_id=row.user_id, kind=EndReason.FORCED_BY_OWNER.value))

    def annotate(self, owner: Actor, session_id: int, *, note: str) -> None:
        """Owner note on a row (staff cannot edit anything)."""
        require(owner, Permission.MANAGE_SYSTEM)
        with self._txn(label="ws.annotate") as session:
            row = self._require(session, session_id)
            self._store.update(session, session_id, owner_note=note)
            self._audit.record(session, "SESSION_ANNOTATED", user_id=row.user_id, actor_user_id=owner.user_id, entity="work_session", entity_id=session_id, after={"note": note})

    # -- queries --------------------------------------------------------------------------------
    def who_is_logged_in(self) -> list[WorkSessionRecord]:
        """Dashboard card."""
        with self._txn(label="ws.open") as session:
            return self._store.open_sessions(session)

    def open_shift_id(self, *, user_id: int) -> int | None:
        """The user's open shift (picked up at login so the badge shows it)."""
        with self._txn(label="ws.shift") as session:
            return self._store.open_shift_id(session, user_id=user_id)

    def history(self, actor: Actor, *, user_id: int | None = None, since: str | None = None, until: str | None = None, limit: int = 500) -> list[HistoryRow]:
        """*Staff work history*: Owner sees everyone; staff only their own rows."""
        if actor.role is not Role.OWNER and user_id != actor.user_id:
            raise AuthError("E-AUTH-010", permission=Permission.REPORT_AUDIT.value, detail="staff may read only their own history")
        now = now_utc()
        with self._txn(label="ws.history") as session:
            rows = self._store.history(session, user_id=user_id, since=since, until=until, limit=limit)
            out: list[HistoryRow] = []
            for r in rows:
                end = r.logout_at or iso_utc(now)
                bills, paise = self._store.sales_in_window(session, user_id=r.user_id, since=r.login_at, until=end)
                seconds = int((parse_iso_utc(end) - parse_iso_utc(r.login_at)).total_seconds())
                out.append(HistoryRow(r.id, r.user_id, r.login_at, r.logout_at, r.end_reason, seconds, bills, paise, r.idle_seconds, r.lock_count, r.confirmed_at, r.confirmed_note, r.owner_note, r.client))
        return out

    def unclean_streak(self, *, user_id: int, window: int = 30) -> int:
        """Consecutive unclean exits, newest first (shown to the Owner)."""
        with self._txn(label="ws.streak") as session:
            reasons = self._store.recent_end_reasons(session, user_id=user_id, limit=window)
        streak = 0
        for reason in reasons:
            if reason in UNCLEAN_REASONS:
                streak += 1
            else:
                break
        return streak

    # -- internals ------------------------------------------------------------------------------
    def _require(self, session: Session, session_id: int) -> WorkSessionRecord:
        row = self._store.by_id(session, session_id)
        if row is None:
            raise AuthError("E-AUTH-010", permission="WORK_SESSION", detail=f"session {session_id} not found")
        return row

    def _close(self, session: Session, row: WorkSessionRecord, reason: EndReason, *, at: str, actor_user_id: int | None = None) -> None:
        self._store.update(session, row.id, logout_at=at, end_reason=reason.value)
        self._audit.record(session, "SESSION_ENDED", user_id=row.user_id, actor_user_id=actor_user_id, entity="work_session", entity_id=row.id, after={"reason": reason.value, "at": at})

    def _end_unclean(self, session_id: int, reason: EndReason, *, at: str) -> bool:
        with self._txn(label=f"ws.{reason.value.lower()}") as session:
            row = self._store.by_id(session, session_id)
            if row is None or not row.is_open:
                return False
            self._close(session, row, reason, at=at)
        log.warning("WORK_SESSION_END id=%s user=%s reason=%s at=%s (flagged for confirmation)", session_id, row.user_id, reason.value, at)
        self._bus.publish(WorkSessionEvent(session_id=session_id, user_id=row.user_id, kind=reason.value))
        return True
