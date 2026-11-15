"""db/sqlite_stores.py — stdlib sqlite3 implementations of the service store protocols, on the real schema.

Written first (docs/13 rule 7). These stores back the installer-side commands (init-db, reset-owner-
password, verify-chain, backup) so the lock-out escape hatch and first-day setup never depend on the ORM.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from vaishnavi.core.clock import UTC, FrozenClock, set_clock
from vaishnavi.core.errors import AuthError
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.security import NullBox, hash_secret, verify_secret
from vaishnavi.core.settings import Settings
from vaishnavi.db import seed
from vaishnavi.db.schema import apply_schema, stamp_head
from vaishnavi.db.sqlite_stores import (
    SqliteAuditStore,
    SqliteNotificationStore,
    SqliteRecoveryStore,
    SqliteSettingStore,
    SqliteSystemStore,
    SqliteTxn,
    SqliteUserStore,
)
from vaishnavi.services.audit import AuditService, verify_chain
from vaishnavi.services.recovery import RecoveryService
from vaishnavi.services.secure_settings import SecureSettingsService
from vaishnavi.services.settings import SettingsService
from vaishnavi.services.user import UserService

from tests.core.test_services_foundation import ReversibleBox

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
OWNER = Actor(user_id=1, role=Role.OWNER)


class SqliteStoresTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "vaishnavi.db"
        conn = sqlite3.connect(self.db)
        apply_schema(conn)
        stamp_head(conn)
        seed.apply_seed(conn, now="2026-11-14T04:00:00Z")
        conn.commit()
        conn.close()
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.txn = SqliteTxn(self.db)
        self.audit = AuditService(SqliteAuditStore())
        self.users = UserService(self.txn, SqliteUserStore(), self.audit)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_txn_commits_on_success_and_rolls_back_on_error(self) -> None:
        with self.txn(label="t") as conn:
            conn.execute("INSERT INTO category(name) VALUES ('x')")
        with self.assertRaises(RuntimeError):
            with self.txn(label="t") as conn:
                conn.execute("INSERT INTO category(name) VALUES ('y')")
                raise RuntimeError("boom")
        check = sqlite3.connect(self.db)
        names = {r[0] for r in check.execute("SELECT name FROM category").fetchall()}
        check.close()
        self.assertIn("x", names)
        self.assertNotIn("y", names)

    def test_owner_setup_login_and_staff_lifecycle_on_a_real_database(self) -> None:
        self.assertTrue(self.users.owner_setup_pending())
        with self.assertRaises(AuthError) as ctx:
            self.users.login("owner", "anything")
        self.assertEqual(ctx.exception.code, "E-AUTH-005")
        owner = self.users.complete_owner_setup("owner-pass-1", "7391", display_name="Meera")
        self.assertFalse(owner.setup_pending)
        result = self.users.login("owner", "owner-pass-1")
        self.assertEqual(result.actor.role, Role.OWNER)
        staff = self.users.create_user(OWNER, "ramesh", "Ramesh", "ram123")
        self.assertEqual(staff.role, Role.STAFF)
        self.assertTrue(self.users.login("ramesh", "ram123").must_change_password)
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.users.login("ramesh", "wrong")
        with self.assertRaises(AuthError) as locked:
            self.users.login("ramesh", "ram123")
        self.assertEqual(locked.exception.code, "E-AUTH-003", "lock persisted in the database")
        pin_actor = self.users.verify_owner_pin("7391", on_behalf_of=Actor(user_id=staff.id, role=Role.STAFF))
        self.assertTrue(pin_actor.pin_verified)
        check = sqlite3.connect(self.db)
        actions = [r[0] for r in check.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
        entries = list(SqliteAuditStore().iter_all(check))
        check.close()
        self.assertIn("OWNER_SETUP_DONE", actions)
        self.assertIn("OVERRIDE", actions)
        self.assertIsNone(verify_chain(entries), "audit rows written through sqlite3 chain correctly")

    def test_recovery_key_round_trip_on_a_real_database(self) -> None:
        self.users.complete_owner_setup("owner-pass-1", "7391")
        recovery = RecoveryService(self.txn, SqliteRecoveryStore(), SqliteUserStore(), self.audit)
        self.assertTrue(recovery.needs_attention())
        issued = recovery.issue(OWNER)
        recovery.acknowledge(OWNER)
        self.assertFalse(recovery.needs_attention())
        self.assertTrue(recovery.verify(issued.phrase))
        result = recovery.reset_owner_password(issued.phrase, "brand-new-pass", elevated=True)
        self.assertEqual(self.users.login("owner", "brand-new-pass").user.id, result.user_id)
        with self.assertRaises(AuthError):
            recovery.verify(issued.phrase)
        self.assertTrue(recovery.verify(result.new_key.phrase))  # type: ignore[union-attr]

    def test_settings_and_secure_settings_stores(self) -> None:
        store = SqliteSettingStore()
        settings = SettingsService(self.txn, store, self.audit, Settings())
        self.assertEqual(len(settings.get("catalog.units")), 11, "seeded data key readable")
        settings.set(OWNER, "billing.require_printer", True)
        self.assertIs(SettingsService(self.txn, store, self.audit, Settings()).effective().billing.require_printer, True, "persisted")
        secure = SecureSettingsService(self.txn, store, self.audit, ReversibleBox())
        secure.set(OWNER, "payments.upi_id", "shop@upi1234")
        self.assertEqual(secure.preview("payments.upi_id"), "•••• 1234")
        self.assertEqual(secure.reveal(OWNER, "payments.upi_id"), "shop@upi1234")
        with self.txn(label="import") as conn:
            stored = secure.import_pending(conn, {"ai.openai.api_key": "sk-abcdefghijklmnop"}, source="installer")
        self.assertEqual(stored, ["ai.openai.api_key"])
        check = sqlite3.connect(self.db)
        row = check.execute("SELECT ciphertext, masked_preview FROM secure_setting WHERE key = 'ai.openai.api_key'").fetchone()
        check.close()
        self.assertNotIn(b"abcdefghijklmnop", bytes(row[0]))
        self.assertEqual(row[1], "••••")

    def test_system_store_identity_steps_counters_and_notifications(self) -> None:
        system = SqliteSystemStore()
        with self.txn(label="s") as conn:
            identity = system.identity(conn)
            assert identity is not None
            self.assertEqual(identity["app_version"], "0.1.0")
            self.assertEqual(len(system.setup_steps(conn)), 11)
            self.assertTrue(system.mark_setup_step(conn, "shop_profile", done_at="2026-11-14T09:00:00Z", done_by=1))
            self.assertFalse(system.mark_setup_step(conn, "no_such_step", done_at="x", done_by=1))
            self.assertEqual(system.next_counter(conn, "invoice"), 1)
            self.assertEqual(system.next_counter(conn, "invoice"), 2)
            self.assertEqual(system.peek_counter(conn, "invoice"), 2)
        notes = SqliteNotificationStore()
        with self.txn(label="n") as conn:
            nid = notes.insert(conn, user_id=None, severity="WARN", kind="BACKUP", title="Backup drive not found", body="2 waiting", deep_link="/settings/backups", created_at="2026-11-14T09:00:00Z", source_ref="E-BKP-001")
            self.assertEqual([n["id"] for n in notes.unread(conn, user_id=1)], [nid], "broadcast rows are visible to every user")
            self.assertTrue(notes.mark_read(conn, nid, at="2026-11-14T09:05:00Z"))
            self.assertEqual(notes.unread(conn, user_id=1), [])
            self.assertEqual(notes.recent(conn, user_id=1, limit=5)[0]["severity"], "WARN")

    def test_secure_store_with_nullbox_refuses_rather_than_storing_plaintext(self) -> None:
        secure = SecureSettingsService(self.txn, SqliteSettingStore(), self.audit, NullBox())
        with self.assertRaises(RuntimeError):
            secure.set(OWNER, "payments.upi_id", "x@y")
        check = sqlite3.connect(self.db)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM secure_setting").fetchone()[0], 0)
        check.close()


if __name__ == "__main__":
    unittest.main()
