"""UserService — login, lockout, Owner setup, Owner PIN overrides and staff accounts (stdlib).

Rules (docs/05 §5, docs/08 E-AUTH):
* two roles only; any number of STAFF, at least one active OWNER at all times (``E-AUTH-006``);
* password failures are **persisted** (``failed_logins`` / ``locked_until``) so a restart does not reset
  them: 5 wrong → locked 15 min (``E-AUTH-003``); unknown user and wrong password are the same error
  (``E-AUTH-001``) and take the same time (a dummy hash check), so usernames cannot be probed;
* the seeded Owner carries the ``!SETUP`` sentinel: login says ``E-AUTH-005`` until
  :meth:`complete_owner_setup` has chosen a real password **and** PIN on the shop PC;
* the Owner PIN is verified on a *staff* screen for overrides: 3 wrong → 15 min (``E-AUTH-030``), every
  attempt audited with both user ids (``OVERRIDE`` / ``OVERRIDE_FAILED``);
* hashes are upgraded transparently on login when the parameters changed (``needs_rehash``).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, now_utc, parse_iso_utc
from vaishnavi.core.errors import AuthError
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.security import LockoutPolicy, hash_secret, needs_rehash, password_problem, pin_problem, verify_secret
from vaishnavi.db.records import PASSWORD_SENTINEL, UserRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.user")

MAX_PASSWORD_FAILURES: Final = 5
PASSWORD_LOCK_MINUTES: Final = 15
PIN_SUBJECT: Final = "owner-pin"


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash to verify against when the username is unknown (same cost → no user enumeration by timing)."""
    return hash_secret("timing-equaliser")


# ----------------------------------------------------------------------------- records
@dataclass(frozen=True)
class LoginResult:
    """Outcome of a successful login."""

    user: UserRecord
    actor: Actor
    must_change_password: bool


class UserStore(Protocol):
    """What the service needs from ``UserRepo``."""

    def by_username(self, session: Session, username: str) -> UserRecord | None:  # pragma: no cover - protocol
        """Case-insensitive lookup."""
        ...

    def by_id(self, session: Session, user_id: int) -> UserRecord | None:  # pragma: no cover - protocol
        """Lookup by id."""
        ...

    def list_all(self, session: Session) -> list[UserRecord]:  # pragma: no cover - protocol
        """All users, owners first."""
        ...

    def active_owners(self, session: Session) -> list[UserRecord]:  # pragma: no cover - protocol
        """Active OWNER accounts."""
        ...

    def create(self, session: Session, *, username: str, display_name: str, role: Role, password_hash: str, must_change_password: bool, at: str) -> UserRecord:  # pragma: no cover - protocol
        """Insert and return the row."""
        ...

    def update(self, session: Session, user_id: int, **fields: Any) -> None:  # pragma: no cover - protocol
        """Update the given columns."""
        ...


# ----------------------------------------------------------------------------- service
class UserService:
    """Accounts and authentication."""

    def __init__(self, txn: TxnFactory, store: UserStore, audit: AuditService, *, pin_lockout: LockoutPolicy | None = None) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._pin_lockout = pin_lockout or LockoutPolicy(max_failures=3, lock_minutes=15)

    # -- queries --------------------------------------------------------------------------------
    def get(self, user_id: int) -> UserRecord | None:
        """One user or None."""
        with self._txn(label="user.get") as session:
            return self._store.by_id(session, user_id)

    def list_users(self, actor: Actor) -> list[UserRecord]:
        """All accounts (Owner only)."""
        require(actor, Permission.MANAGE_SYSTEM)
        with self._txn(label="user.list") as session:
            return self._store.list_all(session)

    def login_tiles(self) -> list[tuple[int, str, str, Role]]:
        """``(id, username, display_name, role)`` of active accounts for the login screen's tiles — no secrets, owners first (docs/09 §2)."""
        with self._txn(label="user.tiles") as session:
            return [(u.id, u.username, u.display_name, u.role) for u in self._store.list_all(session) if u.is_active]

    def display_names(self) -> dict[int, str]:
        """``{id: display_name}`` for labels in lists (status page, work history) — no secrets."""
        with self._txn(label="user.names") as session:
            return {u.id: u.display_name for u in self._store.list_all(session)}

    def owner_setup_pending(self) -> bool:
        """True while no Owner has a real password yet (first-run wizard must run)."""
        with self._txn(label="user.setup_pending") as session:
            owners = self._store.active_owners(session)
        return bool(owners) and all(o.setup_pending for o in owners)

    # -- first run ------------------------------------------------------------------------------
    def complete_owner_setup(self, password: str, pin: str, *, display_name: str | None = None) -> UserRecord:
        """Replace the seed sentinel with a real password and PIN. Allowed only while the sentinel is present."""
        self._check_password(password)
        self._check_pin(pin)
        with self._txn(label="user.owner_setup") as session:
            owners = [o for o in self._store.active_owners(session) if o.setup_pending]
            if not owners:
                log.warning("OWNER_SETUP_REJECTED reason=already_done")
                raise AuthError("E-AUTH-010", permission="OWNER_SETUP", detail="owner setup already completed")
            owner = owners[0]
            fields: dict[str, Any] = {"password_hash": hash_secret(password), "owner_pin_hash": hash_secret(pin), "must_change_password": 0, "failed_logins": 0, "locked_until": None}
            if display_name and display_name.strip():
                fields["display_name"] = display_name.strip()
            self._store.update(session, owner.id, **fields)
            self._audit.record(session, "OWNER_SETUP_DONE", user_id=owner.id, entity="user", entity_id=owner.id, after={"username": owner.username})
            log.info("OWNER_SETUP_DONE user=%s", owner.id)
            updated = self._store.by_id(session, owner.id)
        assert updated is not None
        return updated

    # -- login ----------------------------------------------------------------------------------
    def login(self, username: str, password: str, *, client: str | None = None) -> LoginResult:
        """Authenticate; raises ``AuthError`` (E-AUTH-001/003/004/005). Failures are audited and persisted."""
        name = username.strip()
        failure: AuthError | None = None
        result: LoginResult | None = None
        with self._txn(label="user.login") as session:
            user = self._store.by_username(session, name) if name else None
            if user is None:
                verify_secret(password, _dummy_hash())  # same cost as a real check
                failure = AuthError("E-AUTH-001", detail="unknown user")
                self._audit.record(session, "LOGIN_FAILED", entity="user", after={"username": name[:64], "reason": "unknown", "client": client})
            elif not user.is_active:
                failure = AuthError("E-AUTH-004", detail=f"user {user.id}")
                self._audit.record(session, "LOGIN_FAILED", user_id=user.id, entity="user", entity_id=user.id, after={"reason": "inactive", "client": client})
            elif (minutes := self._lock_minutes_left(user)) > 0:
                failure = AuthError("E-AUTH-003", minutes=minutes, detail=f"user {user.id} locked_until={user.locked_until}")
                self._audit.record(session, "LOGIN_FAILED", user_id=user.id, entity="user", entity_id=user.id, after={"reason": "locked", "client": client})
            elif user.setup_pending:
                failure = AuthError("E-AUTH-005", detail=f"user {user.id}")
                self._audit.record(session, "LOGIN_FAILED", user_id=user.id, entity="user", entity_id=user.id, after={"reason": "setup_pending", "client": client})
            elif not verify_secret(password, user.password_hash):
                # An expired lock means the earlier failures have been "served"; start counting afresh.
                failures = (0 if user.locked_until else user.failed_logins) + 1
                fields: dict[str, Any] = {"failed_logins": failures, "locked_until": None}
                if failures >= MAX_PASSWORD_FAILURES:
                    fields["locked_until"] = iso_utc(now_utc() + timedelta(minutes=PASSWORD_LOCK_MINUTES))
                    failure = AuthError("E-AUTH-003", minutes=PASSWORD_LOCK_MINUTES, detail=f"user {user.id} locked after {failures} failures")
                else:
                    failure = AuthError("E-AUTH-001", detail=f"user {user.id} failures={failures}")
                self._store.update(session, user.id, **fields)
                self._audit.record(session, "LOGIN_FAILED", user_id=user.id, entity="user", entity_id=user.id, after={"reason": "password", "failures": failures, "locked": "locked_until" in fields, "client": client})
            else:
                fields = {"failed_logins": 0, "locked_until": None, "last_login_at": iso_utc()}
                if needs_rehash(user.password_hash):
                    fields["password_hash"] = hash_secret(password)
                    log.info("PASSWORD_REHASHED user=%s", user.id)
                self._store.update(session, user.id, **fields)
                self._audit.record(session, "LOGIN", user_id=user.id, entity="user", entity_id=user.id, after={"client": client})
                actor = Actor(user_id=user.id, role=user.role)
                result = LoginResult(user=replace(user, failed_logins=0, locked_until=None, last_login_at=fields["last_login_at"]), actor=actor, must_change_password=user.must_change_password)
        if failure is not None:
            log.warning("LOGIN_FAILED username=%s code=%s detail=%s", name[:64], failure.code, failure.detail)
            raise failure
        assert result is not None
        log.info("LOGIN_OK user=%s role=%s must_change=%s", result.user.id, result.user.role.value, result.must_change_password)
        return result

    # -- passwords & PIN --------------------------------------------------------------------------
    def reauthenticate(self, user_id: int, password: str) -> bool:
        """Idle-lock unlock: re-check the user's own password **without** a new login (no session, no ``last_login_at``).

        Shares the login lockout counter so the lock screen is not a cheaper place to guess (``E-AUTH-003``).
        """
        with self._txn(label="user.reauth") as session:
            user = self._require_user(session, user_id)
            if (minutes := self._lock_minutes_left(user)) > 0:
                raise AuthError("E-AUTH-003", minutes=minutes, detail=f"user {user.id} locked_until={user.locked_until}")
            if verify_secret(password, user.password_hash):
                if user.failed_logins:
                    self._store.update(session, user.id, failed_logins=0, locked_until=None)
                self._audit.record(session, "UNLOCK", user_id=user.id, entity="user", entity_id=user.id)
                return True
            failures = (0 if user.locked_until else user.failed_logins) + 1
            fields: dict[str, Any] = {"failed_logins": failures, "locked_until": None}
            if failures >= MAX_PASSWORD_FAILURES:
                fields["locked_until"] = iso_utc(now_utc() + timedelta(minutes=PASSWORD_LOCK_MINUTES))
            self._store.update(session, user.id, **fields)
            self._audit.record(session, "UNLOCK_FAILED", user_id=user.id, entity="user", entity_id=user.id, after={"failures": failures, "locked": "locked_until" in fields})
        log.warning("UNLOCK_FAILED user=%s failures=%s", user_id, failures)
        return False

    def change_password(self, actor: Actor, current_password: str, new_password: str) -> None:
        """A user changes their own password (requires the current one)."""
        self._check_password(new_password)
        with self._txn(label="user.change_password") as session:
            user = self._require_user(session, actor.user_id)
            if not verify_secret(current_password, user.password_hash):
                self._audit.record(session, "PASSWORD_CHANGE_FAILED", user_id=user.id, entity="user", entity_id=user.id)
                raise AuthError("E-AUTH-001", detail="current password wrong")
            self._store.update(session, user.id, password_hash=hash_secret(new_password), must_change_password=0)
            self._audit.record(session, "PASSWORD_CHANGED", user_id=user.id, entity="user", entity_id=user.id)
            log.info("PASSWORD_CHANGED user=%s", user.id)

    def reset_password(self, owner: Actor, user_id: int, new_password: str) -> None:
        """Owner sets a temporary password for a user; the user must change it at next login."""
        require(owner, Permission.MANAGE_SYSTEM)
        self._check_password(new_password)
        with self._txn(label="user.reset_password") as session:
            user = self._require_user(session, user_id)
            self._store.update(session, user.id, password_hash=hash_secret(new_password), must_change_password=1, failed_logins=0, locked_until=None)
            self._audit.record(session, "PASSWORD_RESET", user_id=user.id, actor_user_id=owner.user_id, entity="user", entity_id=user.id)
            log.info("PASSWORD_RESET user=%s by=%s", user.id, owner.user_id)

    def set_owner_pin(self, owner: Actor, password: str, new_pin: str) -> None:
        """Owner sets/changes the override PIN; the Owner password is re-entered as confirmation."""
        require(owner, Permission.MANAGE_SYSTEM)
        self._check_pin(new_pin)
        with self._txn(label="user.set_pin") as session:
            user = self._require_user(session, owner.user_id)
            if user.role is not Role.OWNER or not verify_secret(password, user.password_hash):
                self._audit.record(session, "PIN_CHANGE_FAILED", user_id=user.id, entity="user", entity_id=user.id)
                raise AuthError("E-AUTH-001", detail="owner password wrong")
            self._store.update(session, user.id, owner_pin_hash=hash_secret(new_pin))
            self._audit.record(session, "PIN_CHANGED", user_id=user.id, entity="user", entity_id=user.id)
            self._pin_lockout.record_success(PIN_SUBJECT)
            log.info("PIN_CHANGED user=%s", user.id)

    def verify_owner_pin(self, pin: str, *, on_behalf_of: Actor, permission: Permission | None = None) -> Actor:
        """Check the PIN typed on a staff screen; returns the Owner actor with ``pin_verified=True``."""
        if self._pin_lockout.is_locked(PIN_SUBJECT):
            log.warning("PIN_LOCKED remaining=%ss", self._pin_lockout.seconds_remaining(PIN_SUBJECT))
            raise AuthError("E-AUTH-030", detail=f"locked {self._pin_lockout.seconds_remaining(PIN_SUBJECT)}s")
        failure: AuthError | None = None
        owner_actor: Actor | None = None
        with self._txn(label="user.verify_pin") as session:
            owners = [o for o in self._store.active_owners(session) if o.owner_pin_hash]
            if not owners:
                raise AuthError("E-AUTH-012")
            matched = next((o for o in owners if verify_secret(pin, o.owner_pin_hash or "")), None)
            perm_name = permission.value if permission else None
            if matched is None:
                locked_now = self._pin_lockout.record_failure(PIN_SUBJECT)
                failure = AuthError("E-AUTH-030" if locked_now else "E-AUTH-002", detail=f"failures={self._pin_lockout.failures(PIN_SUBJECT)}")
                self._audit.record(session, "OVERRIDE_FAILED", user_id=on_behalf_of.user_id, entity="permission", after={"permission": perm_name, "locked": locked_now})
            else:
                self._pin_lockout.record_success(PIN_SUBJECT)
                owner_actor = Actor(user_id=matched.id, role=Role.OWNER, pin_verified=True)
                self._audit.record(session, "OVERRIDE", user_id=on_behalf_of.user_id, actor_user_id=matched.id, entity="permission", after={"permission": perm_name})
        if failure is not None:
            log.warning("OVERRIDE_FAILED staff=%s code=%s", on_behalf_of.user_id, failure.code)
            raise failure
        assert owner_actor is not None
        log.info("OVERRIDE staff=%s owner=%s permission=%s", on_behalf_of.user_id, owner_actor.user_id, permission.value if permission else "-")
        return owner_actor

    # -- accounts -------------------------------------------------------------------------------
    def create_user(self, owner: Actor, username: str, display_name: str, password: str, *, role: Role = Role.STAFF) -> UserRecord:
        """Owner creates a STAFF (or a second OWNER) account; the new user must change the password."""
        require(owner, Permission.MANAGE_SYSTEM)
        name = username.strip()
        if not name or " " in name or len(name) > 32:
            raise AuthError("E-AUTH-009", username=name, detail="invalid username")
        self._check_password(password)
        with self._txn(label="user.create") as session:
            if self._store.by_username(session, name) is not None:
                raise AuthError("E-AUTH-009", username=name)
            user = self._store.create(session, username=name, display_name=display_name.strip() or name, role=role, password_hash=hash_secret(password), must_change_password=True, at=iso_utc())
            self._audit.record(session, "USER_CREATED", user_id=user.id, actor_user_id=owner.user_id, entity="user", entity_id=user.id, after={"username": name, "role": role.value})
            log.info("USER_CREATED user=%s role=%s by=%s", user.id, role.value, owner.user_id)
            return user

    def set_active(self, owner: Actor, user_id: int, active: bool) -> None:
        """Deactivate / reactivate; refuses to deactivate the last active Owner (E-AUTH-006) or oneself."""
        require(owner, Permission.MANAGE_SYSTEM)
        with self._txn(label="user.set_active") as session:
            user = self._require_user(session, user_id)
            if not active and user.id == owner.user_id:
                raise AuthError("E-AUTH-010", permission="DEACTIVATE_SELF", detail="cannot deactivate own account")
            if not active and user.role is Role.OWNER:
                others = [o for o in self._store.active_owners(session) if o.id != user.id]
                if not others:
                    raise AuthError("E-AUTH-006", detail=f"user {user.id}")
            if user.is_active == active:
                return
            self._store.update(session, user.id, is_active=1 if active else 0)
            self._audit.record(session, "USER_ACTIVATED" if active else "USER_DEACTIVATED", user_id=user.id, actor_user_id=owner.user_id, entity="user", entity_id=user.id)
            log.info("USER_%s user=%s by=%s", "ACTIVATED" if active else "DEACTIVATED", user.id, owner.user_id)

    def rename(self, owner: Actor, user_id: int, display_name: str) -> None:
        """Change the display name shown on receipts and attendance."""
        require(owner, Permission.MANAGE_SYSTEM)
        if not display_name.strip():
            raise ValueError("display name must not be empty")
        with self._txn(label="user.rename") as session:
            user = self._require_user(session, user_id)
            self._store.update(session, user.id, display_name=display_name.strip())
            self._audit.record(session, "USER_UPDATED", user_id=user.id, actor_user_id=owner.user_id, entity="user", entity_id=user.id, before={"display_name": user.display_name}, after={"display_name": display_name.strip()})

    # -- internals ------------------------------------------------------------------------------
    def _require_user(self, session: Session, user_id: int) -> UserRecord:
        user = self._store.by_id(session, user_id)
        if user is None:
            raise AuthError("E-AUTH-001", detail=f"no user {user_id}")
        return user

    @staticmethod
    def _lock_minutes_left(user: UserRecord) -> int:
        if not user.locked_until:
            return 0
        try:
            remaining = parse_iso_utc(user.locked_until) - now_utc()
        except ValueError:
            return 0
        seconds = remaining.total_seconds()
        return max(1, int(seconds // 60) + (1 if seconds % 60 else 0)) if seconds > 0 else 0

    @staticmethod
    def _check_password(password: str) -> None:
        problem = password_problem(password)
        if problem:
            raise AuthError("E-AUTH-007", reason=problem)

    @staticmethod
    def _check_pin(pin: str) -> None:
        problem = pin_problem(pin)
        if problem:
            raise AuthError("E-AUTH-008", reason=problem)
