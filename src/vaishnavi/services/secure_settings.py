"""SecureSettingsService — encrypted Owner-only values (stdlib; crypto behind ``SecretBox``).

UPI id, bank account, cloud API keys and publishing credentials live in ``secure_setting`` as Fernet
ciphertext; the master key sits in the OS keyring (``platform/secrets.py``). Rules enforced here:

* only names in :data:`SECURE_KEYS` may be stored (``E-SEC-010``) — nothing arbitrary can be smuggled in;
* the plain value is returned **only** by :meth:`reveal`, which requires an Owner actor; screens get
  :meth:`preview` (``••••1234``);
* audit rows and log lines carry the key *name* only — never the value, never the preview of a key;
* the AI layer cannot import this module (import-linter contract in ``pyproject.toml``);
* a value that no longer decrypts (new PC, new OS account, restored DB with another key) raises
  ``E-SEC-011`` so the Owner is asked to enter it again instead of the app failing silently.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import SecretsError
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.security import SecretBox, masked_preview
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.secure_settings")

SECURE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "payments.upi_id",
        "payments.payee_name",
        "payments.bank_name",
        "payments.account_no",
        "payments.ifsc",
        "ai.openai.api_key",
        "ai.huggingface.api_key",
        "publish.smtp_password",
        "publish.telegram_token",
        "publish.whatsapp_token",
    }
)
"""The complete list of values that may be stored encrypted (docs/04 secure_setting, docs/06 §2.5)."""

PREVIEW_KEEP: Final = {"payments.upi_id": 4, "payments.account_no": 4, "payments.ifsc": 4, "payments.payee_name": 3, "payments.bank_name": 3}
"""How many trailing characters a preview may show; keys not listed show nothing but length (API keys)."""


class SecureSettingStore(Protocol):
    """What the service needs from ``SettingRepo`` (secure part)."""

    def secure_get(self, session: Session, key: str) -> tuple[bytes, str] | None:  # pragma: no cover - protocol
        """``(ciphertext, masked_preview)`` or None."""
        ...

    def secure_previews(self, session: Session) -> dict[str, str]:  # pragma: no cover - protocol
        """``{key: masked_preview}`` for every stored key."""
        ...

    def secure_upsert(self, session: Session, key: str, ciphertext: bytes, preview: str, *, updated_by: int | None, at: str) -> None:  # pragma: no cover - protocol
        """Insert or replace."""
        ...

    def secure_delete(self, session: Session, key: str) -> bool:  # pragma: no cover - protocol
        """Remove; True when it existed."""
        ...


class SecureSettingsService:
    """Encrypted key/value store with masked previews."""

    def __init__(self, txn: TxnFactory, store: SecureSettingStore, audit: AuditService, box: SecretBox) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._box = box

    # -- reading --------------------------------------------------------------------------------
    def previews(self) -> dict[str, str]:
        """``{key: masked}`` for the Settings screens; safe to show to the Owner."""
        with self._txn(label="secure.previews") as session:
            return self._store.secure_previews(session)

    def preview(self, key: str) -> str | None:
        """Masked preview of one key (None when not set)."""
        self._check_key(key)
        with self._txn(label="secure.preview") as session:
            row = self._store.secure_get(session, key)
        return row[1] if row else None

    def is_set(self, key: str) -> bool:
        """True when a value is stored."""
        return self.preview(key) is not None

    def reveal(self, actor: Actor, key: str) -> str | None:
        """Decrypt and return the value — Owner only; internal callers pass the Owner actor they act for."""
        self._check_key(key)
        require(actor, Permission.MANAGE_SYSTEM)
        if actor.role is not Role.OWNER:  # belt and braces: MANAGE_SYSTEM is Owner-only already
            raise SecretsError("E-SEC-010", key=key, detail="non-owner reveal")
        with self._txn(label="secure.reveal") as session:
            row = self._store.secure_get(session, key)
        if row is None:
            return None
        return self._decrypt(key, row[0])

    def reveal_for_system(self, key: str) -> str | None:
        """Decrypt for a trusted in-process consumer (receipt QR, AI provider factory); logged, never audited to a user."""
        self._check_key(key)
        with self._txn(label="secure.system") as session:
            row = self._store.secure_get(session, key)
        log.debug("SECURE_SYSTEM_READ key=%s present=%s", key, row is not None)
        return self._decrypt(key, row[0]) if row else None

    # -- writing --------------------------------------------------------------------------------
    def set(self, actor: Actor, key: str, value: str) -> None:
        """Encrypt and store (Owner only); empty value → delete."""
        require(actor, Permission.MANAGE_SYSTEM)
        with self._txn(label="secure.set") as session:
            self.set_in(session, actor, key, value)

    def set_in(self, session: Session, actor: Actor | None, key: str, value: str) -> None:
        """Same inside the caller's transaction (onboarding wizard, installer handover)."""
        self._check_key(key)
        user_id = actor.user_id if actor else None
        if value is None or value == "":
            if self._store.secure_delete(session, key):
                self._audit.record(session, "SECURE_SETTING_CLEARED", user_id=user_id, entity="secure_setting", after={"key": key})
                log.info("SECURE_SETTING_CLEARED key=%s user=%s", key, user_id)
            return
        token = self._box.encrypt(value)
        preview = masked_preview(value, keep=PREVIEW_KEEP.get(key, 0))
        existed = self._store.secure_get(session, key) is not None
        self._store.secure_upsert(session, key, token.encode("ascii"), preview, updated_by=user_id, at=iso_utc())
        self._audit.record(session, "SECURE_SETTING_CHANGED", user_id=user_id, entity="secure_setting", after={"key": key, "replaced": existed})
        log.info("SECURE_SETTING_CHANGED key=%s replaced=%s user=%s", key, existed, user_id)

    def import_pending(self, session: Session, pending: Mapping[str, str], *, source: str) -> list[str]:
        """Store secrets handed over by the installer (kept only in memory until now); returns stored keys."""
        stored: list[str] = []
        for key, value in pending.items():
            if key not in SECURE_KEYS:
                log.warning("SECURE_IMPORT_SKIPPED key=%s reason=unknown", key)
                continue
            self.set_in(session, None, key, value)
            stored.append(key)
        log.info("SECRET_IMPORTED source=%s keys=%s", source, ",".join(stored) or "-")
        return stored

    def delete(self, actor: Actor, key: str) -> bool:
        """Remove a value (Owner only)."""
        require(actor, Permission.MANAGE_SYSTEM)
        self._check_key(key)
        with self._txn(label="secure.delete") as session:
            existed = self._store.secure_delete(session, key)
            if existed:
                self._audit.record(session, "SECURE_SETTING_CLEARED", user_id=actor.user_id, entity="secure_setting", after={"key": key})
        return existed

    # -- internals ------------------------------------------------------------------------------
    @staticmethod
    def _check_key(key: str) -> None:
        if key not in SECURE_KEYS:
            raise SecretsError("E-SEC-010", key=key)

    def _decrypt(self, key: str, ciphertext: bytes) -> str:
        try:
            return self._box.decrypt(ciphertext.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.error("SECURE_DECRYPT_FAILED key=%s error=%s", key, type(exc).__name__)
            raise SecretsError("E-SEC-011", key=key, cause=exc) from exc
