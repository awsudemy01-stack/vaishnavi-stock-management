"""Per-client UI state (docs/09 §1): who is signed in, language, work session, idle lock.

Lives in NiceGUI's ``app.storage.user`` (a JSON dict per browser) — hence ``to_dict`` / ``from_dict``
with only JSON scalars and a tolerant parser: a stale or hand-edited storage entry must never crash the
shell, it simply becomes an anonymous client. Time comes from ``core.clock`` so idle rules are testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vaishnavi.core.clock import iso_utc, now_utc, parse_iso_utc
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.ui.i18n import normalise_lang

log = logging.getLogger("vaishnavi.ui.state")


def initials(display_name: str) -> str:
    """``Rajesh Kumar`` → ``RK``; one word → first two letters; empty → ``?``."""
    words = [w for w in display_name.strip().split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        word = words[0]
        # Devanagari: one syllable sign already fills the circle; Latin: two letters.
        return word[:1] if any("\u0900" <= ch <= "\u097f" for ch in word) else word[:2].upper()
    return (words[0][0] + words[-1][0]).upper()


@dataclass(frozen=True)
class ClientUser:
    """The signed-in user as the shell needs it (no secrets)."""

    user_id: int
    username: str
    display_name: str
    role: Role

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {"user_id": self.user_id, "username": self.username, "display_name": self.display_name, "role": self.role.value}

    @classmethod
    def from_dict(cls, data: Any) -> ClientUser | None:
        """Tolerant parse (None on anything unexpected)."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(user_id=int(data["user_id"]), username=str(data["username"]), display_name=str(data.get("display_name") or data["username"]), role=Role(str(data["role"])))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("CLIENT_USER_INVALID %s — treating as signed out", exc)
            return None


@dataclass
class ClientState:
    """Everything the shell remembers about one browser."""

    lang: str = "en"
    user: ClientUser | None = None
    work_session_id: int | None = None
    shift_id: int | None = None
    session_since: datetime | None = None
    last_activity: datetime | None = None
    locked: bool = False
    locked_since: datetime | None = None
    pending_exit_confirmation: dict[str, Any] | None = None
    """Set at login when the previous session ended uncleanly (docs/09 §1 "Session banner")."""
    extra: dict[str, Any] = field(default_factory=dict)

    # -- construction ---------------------------------------------------------------------------
    @classmethod
    def anonymous(cls, lang: str | None = "en") -> ClientState:
        """A fresh browser."""
        return cls(lang=normalise_lang(lang))

    @property
    def is_authenticated(self) -> bool:
        """Signed in (locked or not)."""
        return self.user is not None

    @property
    def role(self) -> Role | None:
        """Role of the signed-in user."""
        return self.user.role if self.user else None

    # -- transitions ----------------------------------------------------------------------------
    def sign_in(self, user: ClientUser, *, work_session_id: int | None, shift_id: int | None = None) -> None:
        """Record a successful login (the service already created the work session)."""
        now = now_utc()
        self.user = user
        self.work_session_id = work_session_id
        self.shift_id = shift_id
        self.session_since = now
        self.last_activity = now
        self.locked = False
        self.locked_since = None
        log.info("CLIENT_SIGNED_IN user=%s role=%s session=%s", user.username, user.role.value, work_session_id)

    def sign_out(self) -> None:
        """Back to anonymous; the language choice stays."""
        if self.user:
            log.info("CLIENT_SIGNED_OUT user=%s session=%s", self.user.username, self.work_session_id)
        self.user = None
        self.work_session_id = None
        self.shift_id = None
        self.session_since = None
        self.last_activity = None
        self.locked = False
        self.locked_since = None
        self.pending_exit_confirmation = None
        self.extra.clear()

    def touch(self) -> None:
        """Any user interaction."""
        self.last_activity = now_utc()

    def idle_seconds(self) -> float:
        """Seconds since the last interaction (0 when unknown)."""
        if self.last_activity is None:
            return 0.0
        return max(0.0, (now_utc() - self.last_activity).total_seconds())

    def should_lock(self, *, idle_minutes: int) -> bool:
        """True when a signed-in, unlocked client has been idle longer than the setting."""
        return self.is_authenticated and not self.locked and idle_minutes > 0 and self.idle_seconds() >= idle_minutes * 60

    def lock(self) -> None:
        """Show the PIN overlay; the locked time counts as idle in the work session."""
        if not self.locked:
            self.locked = True
            self.locked_since = now_utc()
            log.info("CLIENT_LOCKED user=%s", self.user.username if self.user else "-")

    def unlock(self) -> None:
        """PIN accepted."""
        self.locked = False
        self.locked_since = None
        self.touch()

    def lock_timed_out(self, *, max_locked_minutes: int) -> bool:
        """Locked for longer than allowed → the work session ends ``IDLE_LOCK_TIMEOUT``."""
        if not self.locked or self.locked_since is None or max_locked_minutes <= 0:
            return False
        return (now_utc() - self.locked_since).total_seconds() >= max_locked_minutes * 60

    def locked_seconds(self) -> int:
        """How long the screen has been locked (0 when not locked) — counted as idle in the work session."""
        if not self.locked or self.locked_since is None:
            return 0
        return max(0, int((now_utc() - self.locked_since).total_seconds()))

    def actor(self, *, pin_verified: bool = False) -> Actor:
        """The permissions ``Actor`` for service calls; raises when nobody is signed in."""
        if self.user is None:
            raise PermissionError("no user signed in")
        return Actor(user_id=self.user.user_id, role=self.user.role, pin_verified=pin_verified)

    def initials(self) -> str:
        """Avatar letters."""
        return initials(self.user.display_name) if self.user else "?"

    # -- storage --------------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for ``app.storage.user``."""
        return {
            "lang": self.lang,
            "user": self.user.to_dict() if self.user else None,
            "work_session_id": self.work_session_id,
            "shift_id": self.shift_id,
            "session_since": iso_utc(self.session_since) if self.session_since else None,
            "last_activity": iso_utc(self.last_activity) if self.last_activity else None,
            "locked": self.locked,
            "locked_since": iso_utc(self.locked_since) if self.locked_since else None,
            "pending_exit_confirmation": self.pending_exit_confirmation,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ClientState:
        """Tolerant inverse: anything malformed degrades to the anonymous defaults."""
        if not isinstance(data, dict):
            return cls.anonymous()
        state = cls.anonymous(data.get("lang"))
        state.user = ClientUser.from_dict(data.get("user"))
        if state.user is None:
            return state
        state.work_session_id = _opt_int(data.get("work_session_id"))
        state.shift_id = _opt_int(data.get("shift_id"))
        state.session_since = _opt_dt(data.get("session_since"))
        state.last_activity = _opt_dt(data.get("last_activity"))
        state.locked = bool(data.get("locked", False))
        state.locked_since = _opt_dt(data.get("locked_since"))
        pending = data.get("pending_exit_confirmation")
        state.pending_exit_confirmation = dict(pending) if isinstance(pending, dict) else None
        extra = data.get("extra")
        state.extra = dict(extra) if isinstance(extra, dict) else {}
        return state


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opt_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso_utc(str(value))
    except ValueError:
        return None
