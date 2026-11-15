"""Owner recovery key — services/recovery.py and the `--reset-owner-password` installer mode (FR-A9, ADR-037).

Promises: 12 words from the shipped wordlist, shown once, stored only as a hash; verify tolerates spacing
and case; reset needs the key AND administrator elevation; audited; a used key is retired and a new one
issued; the doctor warns until the Owner acknowledges having written it down.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from vaishnavi.core.clock import UTC, FrozenClock, set_clock
from vaishnavi.core.errors import AuthError
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.core.security import hash_secret, verify_secret
from vaishnavi.db.records import UserRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.recovery import RecoveryService, load_wordlist, looks_like_recovery_key, recovery_sheet
from vaishnavi.services.user import UserService

from tests.core.test_services_foundation import OWNER, STAFF, FakeAuditStore, FakeTxn, FakeUserStore

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


class FakeRecoveryStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def current(self, session: Any) -> dict[str, Any] | None:
        active = [r for r in self.rows if r["used_at"] is None]
        return active[-1] if active else None

    def insert(self, session: Any, key_hash: str, issued_at: str) -> int:
        self.rows.append({"id": len(self.rows) + 1, "key_hash": key_hash, "issued_at": issued_at, "acknowledged_at": None, "used_at": None})
        return len(self.rows)

    def update(self, session: Any, key_id: int, **fields: Any) -> None:
        self.rows[key_id - 1].update(fields)


class WordlistTest(unittest.TestCase):
    def test_shipped_wordlist_is_the_full_bip39_english_list(self) -> None:
        words = load_wordlist()
        self.assertEqual(len(words), 2048)
        self.assertEqual(len(set(words)), 2048)
        self.assertEqual(len({w[:4] for w in words}), 2048, "4-letter prefixes are unique → typos are easy to spot")
        self.assertEqual(words[0], "abandon")
        self.assertEqual(words[-1], "zoo")

    def test_looks_like_recovery_key_shape_check(self) -> None:
        words = load_wordlist()
        good = " ".join(words[:12])
        self.assertIsNone(looks_like_recovery_key(good))
        self.assertIsNone(looks_like_recovery_key("  " + good.upper().replace(" ", "   ") + "\n"))
        self.assertIn("12", looks_like_recovery_key(" ".join(words[:11])) or "")
        self.assertIn("xyzzy", looks_like_recovery_key(" ".join(words[:11]) + " xyzzy") or "")

    def test_recovery_sheet_is_printable_and_numbered(self) -> None:
        words = load_wordlist()
        sheet = recovery_sheet(" ".join(words[:12]), shop_name="Vaishnavi Puja Store", issued_at="2026-11-14T09:00:00Z")
        for i in range(1, 13):
            self.assertIn(f"{i:2d}.", sheet)
        self.assertIn("Vaishnavi Puja Store", sheet)
        self.assertIn("--reset-owner-password", sheet)
        self.assertNotIn("\t", sheet)


class RecoveryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.txn = FakeTxn()
        self.audit_store = FakeAuditStore()
        self.audit = AuditService(self.audit_store)
        self.users = FakeUserStore()
        self.users.add(id=1, username="owner", display_name="Owner", role=Role.OWNER, password_hash=hash_secret("owner-pass"), owner_pin_hash=hash_secret("2468"))
        self.users.add(id=2, username="ramesh", display_name="Ramesh", role=Role.STAFF, password_hash=hash_secret("ram123"))
        self.user_service = UserService(self.txn, self.users, self.audit)
        self.store = FakeRecoveryStore()
        self.svc = RecoveryService(self.txn, self.store, self.users, self.audit)

    def tearDown(self) -> None:
        set_clock(self._prev)

    def test_issue_generates_12_wordlist_words_and_stores_only_a_hash(self) -> None:
        issued = self.svc.issue(OWNER)
        words = issued.phrase.split()
        self.assertEqual(len(words), 12)
        wordlist = set(load_wordlist())
        self.assertTrue(all(w in wordlist for w in words))
        row = self.store.current(None)
        assert row is not None
        self.assertTrue(row["key_hash"].startswith("$scrypt$"))
        self.assertTrue(verify_secret(issued.phrase, row["key_hash"]))
        self.assertNotIn(issued.phrase, row["key_hash"])
        self.assertEqual(self.audit_store.actions()[-1], "RECOVERY_KEY_ISSUED")
        for entry in self.audit_store.rows:
            self.assertNotIn(words[0], (entry.after or "") + (entry.before or ""), "the phrase never reaches the audit log")
        self.assertNotEqual(self.svc.issue(OWNER).phrase, issued.phrase, "every key is random")

    def test_issue_requires_owner(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.issue(STAFF)

    def test_acknowledge_clears_the_doctor_warning(self) -> None:
        self.assertTrue(self.svc.needs_attention(), "no key yet → the doctor warns")
        self.svc.issue(OWNER)
        self.assertTrue(self.svc.needs_attention(), "issued but not acknowledged → still warns")
        self.svc.acknowledge(OWNER)
        self.assertFalse(self.svc.needs_attention())
        self.assertEqual(self.store.current(None)["acknowledged_at"], "2026-11-14T09:00:00Z")  # type: ignore[index]
        self.assertIn("RECOVERY_KEY_ACKNOWLEDGED", self.audit_store.actions())

    def test_verify_tolerates_spacing_and_case_and_rejects_wrong_with_e_auth_040(self) -> None:
        issued = self.svc.issue(OWNER)
        sloppy = "  " + issued.phrase.upper().replace(" ", "   ") + " \n"
        self.assertTrue(self.svc.verify(sloppy))
        with self.assertRaises(AuthError) as ctx:
            self.svc.verify(" ".join(["abandon"] * 12))
        self.assertEqual(ctx.exception.code, "E-AUTH-040")
        self.assertEqual(self.audit_store.actions()[-1], "RECOVERY_KEY_REJECTED")

    def test_verify_locks_after_repeated_wrong_attempts(self) -> None:
        self.svc.issue(OWNER)
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.svc.verify("wrong " * 12)
        with self.assertRaises(AuthError) as ctx:
            self.svc.verify("wrong " * 12)
        self.assertEqual(ctx.exception.code, "E-AUTH-042", "locked, even for a correct key, until the wait is over")
        self.clock.advance(minutes=16)
        with self.assertRaises(AuthError) as ctx2:
            self.svc.verify("wrong " * 12)
        self.assertEqual(ctx2.exception.code, "E-AUTH-040")

    def test_reset_requires_elevation_and_the_key_then_sets_password_and_rotates_key(self) -> None:
        issued = self.svc.issue(OWNER)
        self.users.update(None, 1, failed_logins=5, locked_until="2026-11-14T09:10:00Z")
        with self.assertRaises(AuthError) as ctx:
            self.svc.reset_owner_password(issued.phrase, "new-owner-pass", elevated=False)
        self.assertEqual(ctx.exception.code, "E-AUTH-041")
        self.assertTrue(verify_secret("owner-pass", self.users.rows[1].password_hash), "nothing changed without elevation")
        with self.assertRaises(AuthError):
            self.svc.reset_owner_password("wrong " * 12, "new-owner-pass", elevated=True)
        with self.assertRaises(AuthError) as weak:
            self.svc.reset_owner_password(issued.phrase, "123", elevated=True)
        self.assertEqual(weak.exception.code, "E-AUTH-007")

        result = self.svc.reset_owner_password(issued.phrase, "new-owner-pass", elevated=True)
        owner = self.users.rows[1]
        self.assertTrue(verify_secret("new-owner-pass", owner.password_hash))
        self.assertEqual((owner.failed_logins, owner.locked_until), (0, None), "the lock that caused the lock-out is cleared")
        self.assertEqual(result.user_id, 1)
        self.assertEqual(self.store.rows[0]["used_at"], "2026-11-14T09:00:00Z", "the key is retired after one use")
        self.assertIsNotNone(result.new_key, "a fresh key is issued and must be written down again")
        self.assertNotEqual(result.new_key.phrase, issued.phrase)  # type: ignore[union-attr]
        actions = self.audit_store.actions()
        self.assertIn("OWNER_PASSWORD_RESET", actions)
        self.assertEqual(actions[-1], "RECOVERY_KEY_ISSUED")
        self.assertEqual(self.user_service.login("owner", "new-owner-pass").actor.role, Role.OWNER)
        with self.assertRaises(AuthError):
            self.svc.verify(issued.phrase)
        self.assertTrue(self.svc.verify(result.new_key.phrase))  # type: ignore[union-attr]

    def test_reset_without_any_key_issued_is_refused(self) -> None:
        with self.assertRaises(AuthError) as ctx:
            self.svc.reset_owner_password("abandon " * 12, "new-owner-pass", elevated=True)
        self.assertEqual(ctx.exception.code, "E-AUTH-040")


class InstallerModeTest(unittest.TestCase):
    """`python main.py --reset-owner-password` with the elevation and the application faked."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.program_dir = Path(self._tmp.name)
        self.out = io.StringIO()
        import logging

        logging.getLogger().addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        import logging

        logging.shutdown()
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        self._tmp.cleanup()

    def _run(self, typed: str, *, secrets: list[str], elevation_ok: bool = True, app_exit: int = 0, app_available: bool = True) -> tuple[int, dict[str, Any]]:
        from bootstrap import cli, handover
        from bootstrap import recovery as rec
        from bootstrap.elevate import Elevation, Outcome

        calls: dict[str, Any] = {}
        words = load_wordlist()
        typed = typed.replace("{KEY}", " ".join(words[:12]))

        def fake_elevated(cmd, **kw):  # type: ignore[no-untyped-def]
            calls["elevated_cmd"] = list(cmd)
            return Elevation(Outcome.OK if elevation_ok else Outcome.DECLINED, 0 if elevation_ok else None)

        def fake_run_app(python, args, *, secrets=None, on_line=None, timeout=0, cwd=None):  # type: ignore[no-untyped-def]
            calls["args"] = list(args)
            calls["secrets"] = dict(secrets or {})
            return handover.AppResult(app_exit, "RESET_OK user=owner" if app_exit == 0 else "E-AUTH-040", 10)

        class FakeStdin(io.StringIO):
            def isatty(self) -> bool:
                return True

        secrets_iter = iter(secrets)
        with mock.patch.object(rec.elevate, "run_elevated", fake_elevated), mock.patch.object(rec.handover, "run_app", fake_run_app), \
             mock.patch.object(rec.handover, "venv_python_from", lambda ctx: Path("C:/fake/python.exe")), \
             mock.patch.object(rec.handover, "app_available", lambda python: (app_available, "0.1.0" if app_available else "no module vaishnavi")), \
             mock.patch.object(rec.getpass, "getpass", lambda prompt="": next(secrets_iter)):
            code = cli.main(["--reset-owner-password"], program_dir=self.program_dir, stdout=self.out, stdin=FakeStdin(typed))
        return code, calls

    def test_happy_path_asks_key_and_password_proves_elevation_and_hands_over_via_stdin(self) -> None:
        code, calls = self._run("{KEY}\n", secrets=["new-owner-pass", "new-owner-pass"])
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(calls["args"], ["reset-owner-password"])
        self.assertEqual(sorted(calls["secrets"]), ["new_password", "recovery_key"])
        self.assertIn("elevated_cmd", calls, "administrator rights were proven before anything was changed")
        text = self.out.getvalue()
        self.assertNotIn("new-owner-pass", text)
        self.assertNotIn("abandon", text, "the key is not echoed")
        for f in (self.program_dir / "logs").rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("new-owner-pass", content)
                self.assertNotIn("abandon ability", content)

    def test_declined_elevation_stops_with_e_sys_004_and_never_calls_the_app(self) -> None:
        code, calls = self._run("{KEY}\n", secrets=["new-owner-pass", "new-owner-pass"], elevation_ok=False)
        self.assertEqual(code, 2)
        self.assertNotIn("args", calls)
        self.assertIn("E-SYS-004", self.out.getvalue())

    def test_wrong_shape_key_is_asked_again_and_mismatched_passwords_too(self) -> None:
        # attempt 1: too short (one prompt consumed) · attempt 2: mismatch · attempt 3: ok
        code, calls = self._run("only three words\n{KEY}\n", secrets=["abc", "new-owner-pass", "typo-pass", "new-owner-pass", "new-owner-pass"])
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(calls["secrets"]["new_password"], "new-owner-pass")
        self.assertIn("12 words", self.out.getvalue())

    def test_app_rejecting_the_key_is_reported_with_its_code(self) -> None:
        code, _ = self._run("{KEY}\n", secrets=["new-owner-pass", "new-owner-pass"], app_exit=2)
        self.assertEqual(code, 2)
        self.assertIn("E-AUTH-040", self.out.getvalue())

    def test_missing_application_is_a_clear_failure(self) -> None:
        code, calls = self._run("{KEY}\n", secrets=["new-owner-pass", "new-owner-pass"], app_available=False)
        self.assertEqual(code, 2)
        self.assertNotIn("args", calls)
        self.assertIn("E-SYS-002", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
