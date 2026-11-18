"""SettingsService — Owner-editable runtime settings stored in the ``setting`` table (stdlib).

The file settings (``config/settings.toml`` + environment) are the base; rows in ``setting`` are
**overrides** the Owner saved from the Settings screens. ``effective()`` returns the merged, validated
:class:`~vaishnavi.core.settings.Settings` and is cached until the next ``set``/``reset``.

Two kinds of keys live in the table:
* **override keys** — any dotted leaf of ``Settings`` (``billing.require_printer``). A value is validated
  through ``Settings.with_overrides`` *before* it is written, so an invalid value is never stored;
* **data keys** — small reference lists that have no file counterpart (``catalog.units``, ``tax.presets``);
  only names in :data:`DATA_KEYS` are accepted (``E-CFG-002`` otherwise).

Secrets never belong here (``E-SEC-006`` guards the key names) — see ``SecureSettingsService``.
Every change is audited ``SETTING_CHANGED`` (before → after) and published as ``SettingsChanged``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from typing import Any, Final, Literal, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import ConfigError, SecretsError
from vaishnavi.core.events import EventBus, SettingsChanged, bus
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.core.settings import FORBIDDEN_KEYS, Settings, get_dotted, known_keys
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.settings")

SettingType = Literal["str", "int", "bool", "json", "float"]

DATA_KEYS: Final[frozenset[str]] = frozenset({"catalog.units", "tax.presets", "ui.quick_pick_order", "setup.first_run_done", "billing.discount_tiers"})
"""Keys that are stored in the table but are not fields of ``Settings``."""


# ----------------------------------------------------------------------------- encoding
def encode(value: Any) -> tuple[str, SettingType]:
    """Python value → (text, type) as stored in the table."""
    if isinstance(value, bool):
        return ("1" if value else "0", "bool")
    if isinstance(value, int):
        return (str(value), "int")
    if isinstance(value, float):
        return (repr(value), "float")
    if isinstance(value, str):
        return (value, "str")
    return (json.dumps(value, ensure_ascii=False, sort_keys=True), "json")


def decode(text: str, type_: str) -> Any:
    """Inverse of :func:`encode`; unknown types fall back to the raw text (never raises)."""
    try:
        if type_ == "bool":
            return text.strip().lower() in ("1", "true", "yes", "on")
        if type_ == "int":
            return int(text)
        if type_ == "float":
            return float(text)
        if type_ == "json":
            return json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("SETTING_DECODE_FAILED type=%s error=%s", type_, exc)
    return text


# ----------------------------------------------------------------------------- store protocol
class SettingStore(Protocol):
    """What the service needs from ``SettingRepo``."""

    def all(self, session: Session) -> dict[str, tuple[str, str]]:  # pragma: no cover - protocol
        """``{key: (value_text, type)}`` for every row."""
        ...

    def upsert(self, session: Session, key: str, value: str, type_: str, *, updated_by: int | None, at: str) -> None:  # pragma: no cover - protocol
        """Insert or replace one row."""
        ...

    def delete(self, session: Session, key: str) -> bool:  # pragma: no cover - protocol
        """Remove one row; True when it existed."""
        ...


# ----------------------------------------------------------------------------- service
class SettingsService:
    """Typed get/set over the ``setting`` table with a process-wide cache."""

    def __init__(self, txn: TxnFactory, store: SettingStore, audit: AuditService, base: Settings, *, event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._base = base
        self._bus = event_bus
        self._lock = threading.RLock()
        self._rows: dict[str, Any] | None = None
        self._effective: Settings | None = None
        self._known = known_keys()

    # -- reading --------------------------------------------------------------------------------
    def invalidate(self) -> None:
        """Drop the cache (called after every write and by the restore wizard)."""
        with self._lock:
            self._rows = None
            self._effective = None
        log.debug("SETTINGS_CACHE_INVALIDATED")

    def rows(self) -> dict[str, Any]:
        """All stored rows decoded: ``{key: value}`` (overrides and data keys)."""
        with self._lock:
            if self._rows is None:
                with self._txn(label="settings.load") as session:
                    raw = self._store.all(session)
                self._rows = {key: decode(text, type_) for key, (text, type_) in raw.items()}
                log.debug("SETTINGS_LOADED rows=%d", len(self._rows))
            return dict(self._rows)

    def overrides(self) -> dict[str, Any]:
        """Only the rows that override a ``Settings`` field."""
        return {k: v for k, v in self.rows().items() if k in self._known}

    def effective(self) -> Settings:
        """Base settings with database overrides applied (cached, validated)."""
        with self._lock:
            if self._effective is None:
                overrides = self.overrides()
                try:
                    self._effective = self._base.with_overrides(overrides)
                except ConfigError as exc:
                    # A stored override became invalid (e.g. after an upgrade) — fall back and shout.
                    log.error("SETTINGS_OVERRIDE_INVALID %s — ignoring database overrides until fixed", exc)
                    self._effective = self._base.with_overrides({})
            return self._effective

    def get(self, key: str) -> Any:
        """Effective value of a settings key, or a data key's value (``None`` when unset)."""
        if key in self._known:
            return get_dotted(self.effective(), key)
        if key in DATA_KEYS:
            return self.rows().get(key)
        raise ConfigError("E-CFG-002", key=key)

    # -- writing --------------------------------------------------------------------------------
    def set(self, actor: Actor, key: str, value: Any) -> None:
        """Validate, store, audit and publish one change (Owner only)."""
        self.set_many(actor, {key: value})

    def set_many(self, actor: Actor, changes: Mapping[str, Any]) -> None:
        """Atomically store several changes (one transaction, one event)."""
        require(actor, Permission.MANAGE_SYSTEM)
        with self._txn(label="settings.set") as session:
            self.set_many_in(session, actor, changes)

    def set_many_in(self, session: Session, actor: Actor | None, changes: Mapping[str, Any]) -> None:
        """Same as :meth:`set_many` inside the caller's transaction (onboarding wizard, installer handover)."""
        if not changes:
            return
        current = {key: decode(text, type_) for key, (text, type_) in self._store.all(session).items()}
        validated = self._validate(changes, current)
        at = iso_utc()
        user_id = actor.user_id if actor else None
        for key, value in validated.items():
            text, type_ = encode(value)
            self._store.upsert(session, key, text, type_, updated_by=user_id, at=at)
            self._audit.record(session, "SETTING_CHANGED", user_id=user_id, entity="setting", before={"key": key, "value": current.get(key)}, after={"key": key, "value": value})
            log.info("SETTING_CHANGED key=%s user=%s", key, user_id)
        self.invalidate()
        self._bus.publish(SettingsChanged(keys=tuple(validated), user_id=user_id or 0))

    def reset(self, actor: Actor, key: str) -> bool:
        """Remove an override so the file/default value applies again; True when a row existed."""
        require(actor, Permission.MANAGE_SYSTEM)
        self._check_key(key)
        with self._txn(label="settings.reset") as session:
            stored = self._store.all(session).get(key)
            before = decode(*stored) if stored else None
            existed = self._store.delete(session, key)
            if existed:
                self._audit.record(session, "SETTING_RESET", user_id=actor.user_id, entity="setting", before={"key": key, "value": before}, after={"key": key})
                log.info("SETTING_RESET key=%s user=%s", key, actor.user_id)
        self.invalidate()
        if existed:
            self._bus.publish(SettingsChanged(keys=(key,), user_id=actor.user_id))
        return existed

    # -- internals ------------------------------------------------------------------------------
    def _check_key(self, key: str) -> None:
        leaf = key.rsplit(".", 1)[-1].lower()
        if leaf in FORBIDDEN_KEYS:
            raise SecretsError("E-SEC-006", path="setting table", detail=f"key {key}")
        if key not in self._known and key not in DATA_KEYS:
            raise ConfigError("E-CFG-002", key=key)

    def _validate(self, changes: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
        """Coerce override values through ``Settings`` so what is stored is what will load."""
        for key in changes:
            self._check_key(key)
        override_candidates = {k: v for k, v in current.items() if k in self._known}
        override_candidates.update({k: v for k, v in changes.items() if k in self._known})
        merged = self._base.with_overrides(override_candidates)  # raises E-CFG-001 with the key name
        validated: dict[str, Any] = {}
        for key, value in changes.items():
            validated[key] = get_dotted(merged, key) if key in self._known else value
        return validated
