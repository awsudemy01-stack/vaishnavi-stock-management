"""services.audit / settings / secure_settings / user with in-memory stores (stdlib tier).

The ORM repositories are exercised by ``selftest`` on the shop PC; here the business rules are proven
against fakes that implement the same store protocols.
"""

from __future__ import annotations

import base64
import logging
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from vaishnavi.core.clock import UTC, FrozenClock, iso_utc, parse_iso_utc, set_clock
from vaishnavi.core.errors import AuthError, ConfigError, SecretsError
from vaishnavi.core.events import EventBus, SettingsChanged
from vaishnavi.core.permissions import Actor, Permission, PermissionDenied, Role
from vaishnavi.core.security import hash_secret, verify_secret
from vaishnavi.core.settings import Settings, get_dotted, known_keys
from vaishnavi.db.records import GENESIS_HASH, PASSWORD_SENTINEL, AuditEntry, UserRecord
from vaishnavi.services.audit import AuditService, canonical_json, compute_hash, scrub_secrets, verify_chain
from vaishnavi.services.secure_settings import SECURE_KEYS, SecureSettingsService
from vaishnavi.services.settings import DATA_KEYS, SettingsService, decode, encode
from vaishnavi.services.user import MAX_PASSWORD_FAILURES, UserService

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)


# ----------------------------------------------------------------------------- fakes
class FakeTxn:
    """Counts transactions; the 'session' is the shared dict of tables."""

    def __init__(self) -> None:
        self.db: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def __call__(self, *, label: str = ""):
        try:
            yield self.db
            self.commits += 1
        except BaseException:
            self.rollbacks += 1
            raise


class FakeAuditStore:
    def __init__(self) -> None:
        self.rows: list[AuditEntry] = []

    def last_hash(self, session: Any) -> str:
        return self.rows[-1].hash if self.rows else GENESIS_HASH

    def append(self, session: Any, entry: AuditEntry) -> AuditEntry:
        stored = replace(entry, id=len(self.rows) + 1)
        self.rows.append(stored)
        return stored

    def actions(self) -> list[str]:
        return [r.action for r in self.rows]


class FakeSettingStore:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}
        self.secure: dict[str, tuple[bytes, str]] = {}

    def all(self, session: Any) -> dict[str, tuple[str, str]]:
        return dict(self.rows)

    def upsert(self, session: Any, key: str, value: str, type_: str, *, updated_by: int | None, at: str) -> None:
        self.rows[key] = (value, type_)

    def delete(self, session: Any, key: str) -> bool:
        return self.rows.pop(key, None) is not None

    def secure_get(self, session: Any, key: str) -> tuple[bytes, str] | None:
        return self.secure.get(key)

    def secure_previews(self, session: Any) -> dict[str, str]:
        return {k: v[1] for k, v in self.secure.items()}

    def secure_upsert(self, session: Any, key: str, ciphertext: bytes, preview: str, *, updated_by: int | None, at: str) -> None:
        self.secure[key] = (ciphertext, preview)

    def secure_delete(self, session: Any, key: str) -> bool:
        return self.secure.pop(key, None) is not None


class ReversibleBox:
    """Stand-in for Fernet: reversible, detects a 'wrong key'."""

    def __init__(self, key: str = "k1") -> None:
        self.key = key

    def encrypt(self, plaintext: str) -> str:
        return self.key + ":" + base64.b64encode(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        key, _, body = token.partition(":")
        if key != self.key:
            raise ValueError("wrong key")
        return base64.b64decode(body).decode()


class FakeUserStore:
    def __init__(self) -> None:
        self.rows: dict[int, UserRecord] = {}

    def add(self, **kw: Any) -> UserRecord:
        defaults = dict(owner_pin_hash=None, is_active=True, must_change_password=False, failed_logins=0, locked_until=None, last_login_at=None, created_at="2026-11-14T04:00:00Z")
        defaults.update(kw)
        rec = UserRecord(**defaults)
        self.rows[rec.id] = rec
        return rec

    def by_username(self, session: Any, username: str) -> UserRecord | None:
        return next((u for u in self.rows.values() if u.username.lower() == username.lower()), None)

    def by_id(self, session: Any, user_id: int) -> UserRecord | None:
        return self.rows.get(user_id)

    def list_all(self, session: Any) -> list[UserRecord]:
        return sorted(self.rows.values(), key=lambda u: (u.role is not Role.OWNER, u.username))

    def active_owners(self, session: Any) -> list[UserRecord]:
        return [u for u in self.rows.values() if u.role is Role.OWNER and u.is_active]

    def create(self, session: Any, *, username: str, display_name: str, role: Role, password_hash: str, must_change_password: bool, at: str) -> UserRecord:
        return self.add(id=max(self.rows, default=0) + 1, username=username, display_name=display_name, role=role, password_hash=password_hash, must_change_password=must_change_password, created_at=at)

    def update(self, session: Any, user_id: int, **fields: Any) -> None:
        for key in ("is_active", "must_change_password"):
            if key in fields:
                fields[key] = bool(fields[key])
        self.rows[user_id] = replace(self.rows[user_id], **fields)


# ----------------------------------------------------------------------------- clock helpers
class ClockHelpersTest(unittest.TestCase):
    def test_iso_round_trip_and_variants(self) -> None:
        when = datetime(2026, 11, 14, 9, 30, 5, tzinfo=UTC)
        self.assertEqual(iso_utc(when), "2026-11-14T09:30:05Z")
        self.assertEqual(parse_iso_utc("2026-11-14T09:30:05Z"), when)
        self.assertEqual(parse_iso_utc("2026-11-14T15:00:05+05:30"), when)
        self.assertEqual(parse_iso_utc("2026-11-14T09:30:05"), when)
        with self.assertRaises(ValueError):
            parse_iso_utc("yesterday")

    def test_known_keys_and_get_dotted(self) -> None:
        keys = known_keys()
        self.assertIn("billing.require_printer", keys)
        self.assertIn("ai.ollama.chat_model", keys)
        self.assertNotIn("ai.ollama", keys)
        self.assertEqual(get_dotted(Settings(), "billing.manual_add_policy"), "ALLOW")
        with self.assertRaises(ConfigError):
            get_dotted(Settings(), "billing.nope")


# ----------------------------------------------------------------------------- audit
class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txn = FakeTxn()
        self.store = FakeAuditStore()
        self.audit = AuditService(self.store)

    def test_rows_chain_and_verify(self) -> None:
        with self.txn() as s:
            first = self.audit.record(s, "LOGIN", user_id=1, entity="user", entity_id=1)
            second = self.audit.record(s, "SETTING_CHANGED", user_id=1, before={"b": 1}, after={"a": 2, "z": {"k": "v"}})
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        self.assertEqual(second.prev_hash, first.hash)
        self.assertEqual(len(first.hash), 64)
        self.assertEqual(second.after, '{"a":2,"z":{"k":"v"}}')
        self.assertIsNone(verify_chain(self.store.rows))

    def test_tampering_or_deletion_is_detected(self) -> None:
        with self.txn() as s:
            for i in range(4):
                self.audit.record(s, "LOGIN", user_id=i)
        rows = list(self.store.rows)
        rows[2] = replace(rows[2], user_id=99)
        self.assertEqual(verify_chain(rows), 3)
        del rows[2]
        rows[2] = replace(rows[2])
        self.assertEqual(verify_chain(rows), 4, "the row after the gap no longer matches prev_hash")
        self.assertIsNone(verify_chain(self.store.rows))

    def test_secrets_are_scrubbed_and_actor_collapsed(self) -> None:
        self.assertEqual(scrub_secrets({"password": "x", "nested": {"api_key": "y", "ok": 1}}), {"password": "***", "nested": {"api_key": "***", "ok": 1}})
        self.assertIsNone(canonical_json(None))
        with self.txn() as s:
            row = self.audit.record(s, "OVERRIDE", user_id=2, actor_user_id=1, after={"pin": "1234"})
            same = self.audit.record(s, "LOGIN", user_id=2, actor_user_id=2)
        self.assertEqual(row.actor_user_id, 1)
        self.assertIsNone(same.actor_user_id)
        self.assertIn('"pin":"***"', row.after or "")
        with self.assertRaises(ValueError):
            self.audit.record(s, "login", user_id=1)

    def test_hash_covers_every_field(self) -> None:
        base = AuditEntry(at="t", action="A", user_id=1, entity="e", entity_id=2, before="b", after="a", correlation_id="c", ip="i")
        h = compute_hash(base)
        for change in ({"at": "u"}, {"user_id": 2}, {"entity_id": 3}, {"after": "x"}, {"ip": "j"}, {"prev_hash": "p"}):
            self.assertNotEqual(compute_hash(replace(base, **change)), h, change)


# ----------------------------------------------------------------------------- settings
class SettingsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txn = FakeTxn()
        self.store = FakeSettingStore()
        self.audit = AuditService(FakeAuditStore())
        self.bus = EventBus()
        self.events: list[SettingsChanged] = []
        self.bus.subscribe(SettingsChanged, self.events.append)
        self.svc = SettingsService(self.txn, self.store, self.audit, Settings(), event_bus=self.bus)

    def test_encode_decode_round_trip(self) -> None:
        for value in (True, False, 7, 2.5, "text", ["a", 1], {"k": [1, 2]}):
            text, type_ = encode(value)
            self.assertEqual(decode(text, type_), value, value)
        self.assertEqual(decode("not-int", "int"), "not-int", "never raises")

    def test_set_validates_through_settings_and_publishes(self) -> None:
        self.svc.set(OWNER, "billing.require_printer", "true")
        self.assertEqual(self.store.rows["billing.require_printer"], ("1", "bool"), "coerced before storing")
        self.assertIs(self.svc.get("billing.require_printer"), True)
        self.assertIs(self.svc.effective().billing.require_printer, True)
        self.assertEqual(self.events[-1].keys, ("billing.require_printer",))
        with self.assertRaises(ConfigError) as ctx:
            self.svc.set(OWNER, "billing.manual_add_policy", "MAYBE")
        self.assertEqual(ctx.exception.params["key"], "billing.manual_add_policy")
        self.assertNotIn("billing.manual_add_policy", self.store.rows, "invalid value never stored")
        with self.assertRaises(ConfigError) as ctx2:
            self.svc.set(OWNER, "billing.does_not_exist", 1)
        self.assertEqual(ctx2.exception.code, "E-CFG-002")
        self.assertEqual(self.txn.rollbacks, 2)

    def test_data_keys_secrets_and_permissions(self) -> None:
        self.svc.set(OWNER, "catalog.units", [{"label": "pc"}])
        self.assertEqual(self.svc.get("catalog.units"), [{"label": "pc"}])
        self.assertIsNone(self.svc.get("tax.presets"))
        with self.assertRaises(SecretsError):
            self.svc.set(OWNER, "ai.openai.api_key", "sk-123")
        with self.assertRaises(PermissionDenied):
            self.svc.set(STAFF, "billing.require_printer", True)
        self.assertEqual(self.audit._store.actions().count("SETTING_CHANGED"), 1)  # type: ignore[attr-defined]
        self.assertIn("catalog.units", DATA_KEYS)

    def test_reset_and_invalid_stored_override_fallback(self) -> None:
        self.svc.set(OWNER, "receipt.copies", 2)
        self.assertEqual(self.svc.effective().receipt.copies, 2)
        self.assertTrue(self.svc.reset(OWNER, "receipt.copies"))
        self.assertFalse(self.svc.reset(OWNER, "receipt.copies"))
        self.assertEqual(self.svc.effective().receipt.copies, 1)
        self.store.rows["server.port"] = ("not-a-port", "str")  # e.g. corrupted by hand
        self.svc.invalidate()
        with self.assertLogs("vaishnavi.services.settings", level="ERROR"):
            self.assertEqual(self.svc.effective().server.port, 8080, "falls back to file defaults, keeps running")


# ----------------------------------------------------------------------------- secure settings
class SecureSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txn = FakeTxn()
        self.store = FakeSettingStore()
        self.audit_store = FakeAuditStore()
        self.svc = SecureSettingsService(self.txn, self.store, AuditService(self.audit_store), ReversibleBox())

    def test_store_preview_reveal_and_audit_without_values(self) -> None:
        self.svc.set(OWNER, "payments.upi_id", "shop@upi1234")
        self.assertEqual(self.svc.preview("payments.upi_id"), "•••• 1234")
        self.assertEqual(self.svc.previews(), {"payments.upi_id": "•••• 1234"})
        self.assertEqual(self.svc.reveal(OWNER, "payments.upi_id"), "shop@upi1234")
        self.assertEqual(self.svc.reveal_for_system("payments.upi_id"), "shop@upi1234")
        self.assertNotIn(b"shop@upi1234", self.store.secure["payments.upi_id"][0])
        for row in self.audit_store.rows:
            self.assertNotIn("upi1234", (row.after or "") + (row.before or ""))
        self.svc.set(OWNER, "ai.openai.api_key", "sk-abcdefghijkl")
        self.assertEqual(self.svc.preview("ai.openai.api_key"), "••••", "API keys show nothing")

    def test_registry_permissions_and_wrong_key(self) -> None:
        with self.assertRaises(SecretsError) as ctx:
            self.svc.set(OWNER, "payments.card_number", "1")
        self.assertEqual(ctx.exception.code, "E-SEC-010")
        with self.assertRaises(PermissionDenied):
            self.svc.set(STAFF, "payments.upi_id", "x")
        with self.assertRaises(PermissionDenied):
            self.svc.reveal(STAFF, "payments.upi_id")
        self.svc.set(OWNER, "payments.upi_id", "shop@upi")
        other = SecureSettingsService(self.txn, self.store, AuditService(self.audit_store), ReversibleBox("k2"))
        with self.assertRaises(SecretsError) as ctx2:
            other.reveal(OWNER, "payments.upi_id")
        self.assertEqual(ctx2.exception.code, "E-SEC-011")
        self.assertEqual(other.preview("payments.upi_id"), "•••• @upi", "preview still readable without the key")

    def test_import_pending_and_clear(self) -> None:
        with self.txn() as s:
            stored = self.svc.import_pending(s, {"ai.huggingface.api_key": "hf_xxxxxxxxxxxx", "bogus": "1"}, source="installer")
        self.assertEqual(stored, ["ai.huggingface.api_key"])
        self.assertTrue(self.svc.is_set("ai.huggingface.api_key"))
        self.svc.set(OWNER, "ai.huggingface.api_key", "")
        self.assertFalse(self.svc.is_set("ai.huggingface.api_key"))
        self.assertIn("SECURE_SETTING_CLEARED", self.audit_store.actions())
        self.assertTrue(all("." in k for k in SECURE_KEYS))


# ----------------------------------------------------------------------------- users
class UserServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 11, 14, 9, 0, tzinfo=UTC))
        self._prev = set_clock(self.clock)
        self.txn = FakeTxn()
        self.users = FakeUserStore()
        self.audit_store = FakeAuditStore()
        self.svc = UserService(self.txn, self.users, AuditService(self.audit_store))
        self.owner_hash = hash_secret("owner-pass")
        self.users.add(id=1, username="owner", display_name="Owner", role=Role.OWNER, password_hash=self.owner_hash, owner_pin_hash=hash_secret("2468"))
        self.users.add(id=2, username="ramesh", display_name="Ramesh", role=Role.STAFF, password_hash=hash_secret("ram123"))

    def tearDown(self) -> None:
        set_clock(self._prev)

    def test_login_success_sets_context_and_resets_counters(self) -> None:
        self.users.update(None, 2, failed_logins=3)
        result = self.svc.login("RAMESH", "ram123", client="pos-1")
        self.assertEqual(result.actor, Actor(user_id=2, role=Role.STAFF))
        self.assertEqual(self.users.rows[2].failed_logins, 0)
        self.assertEqual(self.users.rows[2].last_login_at, "2026-11-14T09:00:00Z")
        self.assertEqual(self.audit_store.actions()[-1], "LOGIN")

    def test_wrong_password_locks_after_limit_and_unlocks_later(self) -> None:
        for i in range(MAX_PASSWORD_FAILURES - 1):
            with self.assertRaises(AuthError) as ctx:
                self.svc.login("ramesh", "nope")
            self.assertEqual(ctx.exception.code, "E-AUTH-001", i)
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("ramesh", "nope")
        self.assertEqual(ctx.exception.code, "E-AUTH-003")
        self.assertEqual(self.txn.commits, MAX_PASSWORD_FAILURES, "every failure is committed (audit + counter)")
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("ramesh", "ram123")
        self.assertEqual(ctx.exception.code, "E-AUTH-003", "right password while locked still refused")
        self.assertEqual(ctx.exception.params["minutes"], 15)
        self.clock.advance(minutes=16)
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("ramesh", "nope")
        self.assertEqual(ctx.exception.code, "E-AUTH-001", "after the lock expired the count starts afresh")
        self.assertEqual(self.users.rows[2].failed_logins, 1)
        self.assertEqual(self.svc.login("ramesh", "ram123").user.id, 2)

    def test_unknown_inactive_and_setup_pending(self) -> None:
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("ghost", "x")
        self.assertEqual(ctx.exception.code, "E-AUTH-001")
        self.assertEqual(self.audit_store.rows[-1].after, '{"client":null,"reason":"unknown","username":"ghost"}')
        self.users.update(None, 2, is_active=0)
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("ramesh", "ram123")
        self.assertEqual(ctx.exception.code, "E-AUTH-004")
        self.users.update(None, 1, password_hash=PASSWORD_SENTINEL)
        self.assertTrue(self.svc.owner_setup_pending())
        with self.assertRaises(AuthError) as ctx:
            self.svc.login("owner", PASSWORD_SENTINEL)
        self.assertEqual(ctx.exception.code, "E-AUTH-005")

    def test_owner_setup_once_only_with_valid_password_and_pin(self) -> None:
        self.users.update(None, 1, password_hash=PASSWORD_SENTINEL, owner_pin_hash=None, must_change_password=1)
        with self.assertRaises(AuthError) as ctx:
            self.svc.complete_owner_setup("short", "2468")
        self.assertEqual(ctx.exception.code, "E-AUTH-007")
        with self.assertRaises(AuthError) as ctx:
            self.svc.complete_owner_setup("good-pass-1", "1234")
        self.assertEqual(ctx.exception.code, "E-AUTH-008")
        owner = self.svc.complete_owner_setup("good-pass-1", "7391", display_name="Meera")
        self.assertFalse(owner.setup_pending)
        self.assertFalse(owner.must_change_password)
        self.assertEqual(owner.display_name, "Meera")
        self.assertTrue(verify_secret("7391", owner.owner_pin_hash or ""))
        self.assertFalse(self.svc.owner_setup_pending())
        with self.assertRaises(AuthError):
            self.svc.complete_owner_setup("another-pass", "7391")
        self.assertEqual(self.svc.login("owner", "good-pass-1").actor.role, Role.OWNER)

    def test_owner_pin_override_and_lockout(self) -> None:
        actor = self.svc.verify_owner_pin("2468", on_behalf_of=STAFF, permission=Permission.VOID_REFUND)
        self.assertEqual(actor, Actor(user_id=1, role=Role.OWNER, pin_verified=True))
        row = self.audit_store.rows[-1]
        self.assertEqual((row.action, row.user_id, row.actor_user_id), ("OVERRIDE", 2, 1))
        self.assertIn("VOID_REFUND", row.after or "")
        for _ in range(2):
            with self.assertRaises(AuthError) as ctx:
                self.svc.verify_owner_pin("0000", on_behalf_of=STAFF)
            self.assertEqual(ctx.exception.code, "E-AUTH-002")
        with self.assertRaises(AuthError) as ctx:
            self.svc.verify_owner_pin("0000", on_behalf_of=STAFF)
        self.assertEqual(ctx.exception.code, "E-AUTH-030")
        with self.assertRaises(AuthError) as ctx:
            self.svc.verify_owner_pin("2468", on_behalf_of=STAFF)
        self.assertEqual(ctx.exception.code, "E-AUTH-030", "locked even for the right PIN")
        self.clock.advance(minutes=16)
        self.assertTrue(self.svc.verify_owner_pin("2468", on_behalf_of=STAFF).pin_verified)
        self.users.update(None, 1, owner_pin_hash=None)
        with self.assertRaises(AuthError) as ctx:
            self.svc.verify_owner_pin("2468", on_behalf_of=STAFF)
        self.assertEqual(ctx.exception.code, "E-AUTH-012")

    def test_password_management(self) -> None:
        with self.assertRaises(AuthError):
            self.svc.change_password(STAFF, "wrong", "newpass1")
        self.svc.change_password(STAFF, "ram123", "newpass1")
        self.assertTrue(verify_secret("newpass1", self.users.rows[2].password_hash))
        with self.assertRaises(PermissionDenied):
            self.svc.reset_password(STAFF, 1, "hijack1")
        self.svc.reset_password(OWNER, 2, "temp-pass")
        self.assertTrue(self.users.rows[2].must_change_password)
        self.assertTrue(self.svc.login("ramesh", "temp-pass").must_change_password)
        with self.assertRaises(AuthError):
            self.svc.set_owner_pin(OWNER, "wrong-pass", "9753")
        self.svc.set_owner_pin(OWNER, "owner-pass", "9753")
        self.assertTrue(self.svc.verify_owner_pin("9753", on_behalf_of=STAFF).pin_verified)

    def test_account_management_rules(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.create_user(STAFF, "x", "X", "pass123")
        with self.assertRaises(AuthError) as ctx:
            self.svc.create_user(OWNER, "Ramesh", "Dup", "pass123")
        self.assertEqual(ctx.exception.code, "E-AUTH-009")
        new = self.svc.create_user(OWNER, "sita", "Sita", "pass123")
        self.assertTrue(new.must_change_password)
        self.assertEqual(new.role, Role.STAFF)
        with self.assertRaises(AuthError) as ctx:
            self.svc.set_active(OWNER, 1, False)
        self.assertEqual(ctx.exception.code, "E-AUTH-010", "cannot deactivate oneself")
        second = self.svc.create_user(OWNER, "partner", "Partner", "pass123", role=Role.OWNER)
        self.svc.set_active(OWNER, second.id, False)
        self.assertFalse(self.users.rows[second.id].is_active)
        self.svc.set_active(OWNER, second.id, True)
        owner2 = Actor(user_id=second.id, role=Role.OWNER)
        self.svc.set_active(owner2, 1, False)
        with self.assertRaises(AuthError) as ctx:
            self.svc.set_active(OWNER, second.id, False)
        self.assertEqual(ctx.exception.code, "E-AUTH-006", "last active owner protected")
        self.assertEqual([u.username for u in self.svc.list_users(OWNER)][:2], ["owner", "partner"])
        self.assertNotIn("password_hash", new.public())

    def test_old_hash_is_upgraded_on_login(self) -> None:
        weak = hash_secret("ram123", ln=10)
        self.users.update(None, 2, password_hash=weak)
        self.svc.login("ramesh", "ram123")
        self.assertNotEqual(self.users.rows[2].password_hash, weak)
        self.assertIn("ln=15", self.users.rows[2].password_hash)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
