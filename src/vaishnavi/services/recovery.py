"""RecoveryService — the Owner's 12-word recovery key (FR-A9, ADR-037; stdlib only).

There is no role above Owner, so a forgotten Owner password would lock the shop out of its own data.
At setup a 12-word phrase from the shipped BIP-39 wordlist (2048 words → 132 bits) is generated, shown
**once** (printable sheet), and stored only as a scrypt hash in ``recovery_key``. Resetting the Owner
password needs the phrase **and** proof of OS administrator rights (the installer obtains it; this
service only receives ``elevated=True``), is audited, retires the used key and issues a fresh one.
Wrong keys are rate-limited (5 → 15 minutes, ``E-AUTH-042``). The doctor warns while no key has been
acknowledged (``needs_attention()``).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import AuthError
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.security import LockoutPolicy, generate_recovery_key, hash_secret, normalise_recovery_key, password_problem, verify_secret
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.user import UserStore

log = logging.getLogger("vaishnavi.services.recovery")

WORDLIST_FILE: Final = Path(__file__).resolve().parent.parent / "core" / "wordlist.txt"
WORDS: Final = 12
LOCK_SUBJECT: Final = "recovery-key"


# ----------------------------------------------------------------------------- wordlist helpers
@functools.lru_cache(maxsize=1)
def load_wordlist(path: Path = WORDLIST_FILE) -> tuple[str, ...]:
    """The 2048 words (comment lines ignored); cached."""
    words = tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))
    if len(words) < 2048:
        raise RuntimeError(f"wordlist at {path} has only {len(words)} words")
    return words


def looks_like_recovery_key(text: str) -> str | None:
    """Plain-language problem with the typed phrase (count / unknown words), or None when well-formed."""
    words = normalise_recovery_key(text).split()
    if len(words) != WORDS:
        return f"a recovery key has {WORDS} words, you typed {len(words)}"
    known = set(load_wordlist())
    unknown = [w for w in words if w not in known]
    if unknown:
        return "these are not recovery-key words: " + ", ".join(unknown[:3])
    return None


def recovery_sheet(phrase: str, *, shop_name: str, issued_at: str) -> str:
    """Printable text the Owner writes down or prints — the only time the phrase is shown."""
    words = phrase.split()
    rows = ["".join(f"{i + 1:2d}. {words[i]:<12}" for i in range(r, r + 4)) for r in range(0, WORDS, 4)]
    lines = [
        f"{shop_name} — Owner recovery key",
        f"Issued {issued_at}",
        "",
        *rows,
        "",
        "Keep this sheet somewhere safe, away from the shop computer.",
        "If the Owner password is ever forgotten: on the shop computer run",
        "  Start Vaishnavi → Reset owner password   (python main.py --reset-owner-password)",
        "You will be asked for these 12 words and the computer's administrator password.",
        "Whoever has this sheet AND the computer's admin password can reset the Owner password.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- records / store
@dataclass(frozen=True)
class IssuedKey:
    """What the Owner is shown exactly once."""

    phrase: str
    issued_at: str
    key_id: int

    def sheet(self, shop_name: str) -> str:
        """Printable sheet (see :func:`recovery_sheet`)."""
        return recovery_sheet(self.phrase, shop_name=shop_name, issued_at=self.issued_at)


@dataclass(frozen=True)
class ResetResult:
    """Outcome of a successful reset."""

    user_id: int
    username: str
    new_key: IssuedKey | None


class RecoveryStore(Protocol):
    """What the service needs from the ``recovery_key`` table."""

    def current(self, session: Session) -> dict[str, Any] | None:  # pragma: no cover - protocol
        """Newest unused key row (``id, key_hash, issued_at, acknowledged_at, used_at``) or None."""
        ...

    def insert(self, session: Session, key_hash: str, issued_at: str) -> int:  # pragma: no cover - protocol
        """Insert; returns the id."""
        ...

    def update(self, session: Session, key_id: int, **fields: Any) -> None:  # pragma: no cover - protocol
        """Set ``acknowledged_at`` / ``used_at``."""
        ...


# ----------------------------------------------------------------------------- service
class RecoveryService:
    """Issue, acknowledge, verify and use the recovery key."""

    def __init__(self, txn: TxnFactory, store: RecoveryStore, users: UserStore, audit: AuditService, *, lockout: LockoutPolicy | None = None) -> None:
        self._txn = txn
        self._store = store
        self._users = users
        self._audit = audit
        self._lockout = lockout or LockoutPolicy(max_failures=5, lock_minutes=15)

    # -- issue / acknowledge -----------------------------------------------------------------
    def issue(self, actor: Actor | None) -> IssuedKey:
        """Generate a new key (Owner only; ``None`` actor = first-run wizard before any login)."""
        if actor is not None:
            require(actor, Permission.MANAGE_SYSTEM)
        with self._txn(label="recovery.issue") as session:
            return self._issue_in(session, actor.user_id if actor else None)

    def _issue_in(self, session: Session, user_id: int | None) -> IssuedKey:
        phrase = generate_recovery_key(load_wordlist())
        at = iso_utc()
        previous = self._store.current(session)
        if previous is not None:
            self._store.update(session, int(previous["id"]), used_at=at)  # superseded keys stop working
        key_id = self._store.insert(session, hash_secret(phrase), at)
        self._audit.record(session, "RECOVERY_KEY_ISSUED", user_id=user_id, entity="recovery_key", entity_id=key_id, after={"replaced": previous is not None})
        log.info("RECOVERY_KEY_ISSUED key_id=%s user=%s replaced=%s", key_id, user_id, previous is not None)
        return IssuedKey(phrase, at, key_id)

    def acknowledge(self, actor: Actor) -> None:
        """Owner ticked "I have written it down"."""
        require(actor, Permission.MANAGE_SYSTEM)
        with self._txn(label="recovery.ack") as session:
            row = self._store.current(session)
            if row is None:
                raise AuthError("E-AUTH-040", detail="no key issued")
            self._store.update(session, int(row["id"]), acknowledged_at=iso_utc())
            self._audit.record(session, "RECOVERY_KEY_ACKNOWLEDGED", user_id=actor.user_id, entity="recovery_key", entity_id=int(row["id"]))
            log.info("RECOVERY_KEY_ACKNOWLEDGED key_id=%s", row["id"])

    def needs_attention(self) -> bool:
        """True while no key exists or the current one was never acknowledged (doctor WARN)."""
        with self._txn(label="recovery.status") as session:
            row = self._store.current(session)
        return row is None or not row.get("acknowledged_at")

    # -- verify / reset ----------------------------------------------------------------------
    def verify(self, phrase: str) -> bool:
        """True for the current key; ``AuthError`` E-AUTH-040 (wrong) / E-AUTH-042 (locked). Audited."""
        if self._lockout.is_locked(LOCK_SUBJECT):
            log.warning("RECOVERY_KEY_LOCKED remaining=%ss", self._lockout.seconds_remaining(LOCK_SUBJECT))
            raise AuthError("E-AUTH-042", detail=f"locked {self._lockout.seconds_remaining(LOCK_SUBJECT)}s")
        normalised = normalise_recovery_key(phrase)
        with self._txn(label="recovery.verify") as session:
            row = self._store.current(session)
            ok = row is not None and verify_secret(normalised, str(row["key_hash"]))
            if not ok:
                locked = self._lockout.record_failure(LOCK_SUBJECT)
                self._audit.record(session, "RECOVERY_KEY_REJECTED", entity="recovery_key", entity_id=int(row["id"]) if row else None, after={"locked": locked, "failures": self._lockout.failures(LOCK_SUBJECT)})
                log.warning("RECOVERY_KEY_REJECTED failures=%d locked=%s code=%s", self._lockout.failures(LOCK_SUBJECT), locked, "E-AUTH-042" if locked else "E-AUTH-040")
                raise AuthError("E-AUTH-042" if locked else "E-AUTH-040", detail="no key issued" if row is None else "mismatch")
        self._lockout.record_success(LOCK_SUBJECT)
        log.info("RECOVERY_KEY_VERIFIED key_id=%s", row["id"])  # type: ignore[index]
        return True

    def reset_owner_password(self, phrase: str, new_password: str, *, elevated: bool) -> ResetResult:
        """The whole ``--reset-owner-password`` flow (the caller has proven elevation).

        Order matters: elevation first (cheap, no information leaked), then the key (rate-limited),
        then the password rule — so a weak password never consumes the key.
        """
        if not elevated:
            log.warning("OWNER_PASSWORD_RESET_REFUSED reason=not-elevated code=E-AUTH-041")
            raise AuthError("E-AUTH-041")
        self.verify(phrase)
        problem = password_problem(new_password)
        if problem:
            raise AuthError("E-AUTH-007", reason=problem)
        with self._txn(label="recovery.reset") as session:
            owners = self._users.active_owners(session)
            if not owners:
                raise AuthError("E-AUTH-005")
            owner = owners[0]
            self._users.update(session, owner.id, password_hash=hash_secret(new_password), must_change_password=0, failed_logins=0, locked_until=None)
            row = self._store.current(session)
            if row is not None:
                self._store.update(session, int(row["id"]), used_at=iso_utc())
            self._audit.record(session, "OWNER_PASSWORD_RESET", user_id=owner.id, entity="user", entity_id=owner.id, after={"via": "recovery_key", "key_id": int(row["id"]) if row else None})
            log.warning("OWNER_PASSWORD_RESET user=%s via=recovery_key", owner.id)
            new_key = self._issue_in(session, owner.id)
        return ResetResult(owner.id, owner.username, new_key)


def owner_actor_for(user_id: int) -> Actor:
    """Convenience for callers that act on the Owner's behalf after a reset."""
    return Actor(user_id=user_id, role=Role.OWNER)
