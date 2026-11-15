"""core.security, platform.secrets, platform.paths (stdlib; Fernet tests skip when cryptography is absent)."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaishnavi.core import clock as clk, security as sec
from vaishnavi.core.clock import FrozenClock, SystemClock
from vaishnavi.core.security import (
    LockoutPolicy,
    NullBox,
    SecurityError,
    generate_recovery_key,
    hash_secret,
    masked_preview,
    needs_rehash,
    new_master_key,
    normalise_recovery_key,
    password_problem,
    pin_problem,
    verify_secret,
)
from vaishnavi.platform import paths as pp, secrets as ps
from vaishnavi.platform.paths import AppPaths

HAS_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None


class HashingTest(unittest.TestCase):
    def test_hash_and_verify(self) -> None:
        h = hash_secret("Shop@2026")
        self.assertTrue(h.startswith("$scrypt$ln=15,r=8,p=1$"))
        self.assertTrue(verify_secret("Shop@2026", h))
        self.assertFalse(verify_secret("shop@2026", h))
        self.assertFalse(verify_secret("", h))
        self.assertNotEqual(hash_secret("Shop@2026"), h, "fresh salt every time")

    def test_pin_hashing_same_path(self) -> None:
        h = hash_secret("4826")
        self.assertTrue(verify_secret("4826", h))
        self.assertFalse(verify_secret("4827", h))

    def test_verify_never_raises_on_garbage(self) -> None:
        for bad in ("", "plain", "$scrypt$broken", "$scrypt$ln=x,r=8,p=1$a$b", "$argon2id$v=19$m=1,t=1,p=1$a$b", "$md5$x"):
            with self.subTest(bad=bad):
                self.assertFalse(verify_secret("x", bad))

    def test_needs_rehash_and_old_parameters_still_verify(self) -> None:
        old = hash_secret("pw", ln=12)
        self.assertTrue(verify_secret("pw", old))
        self.assertTrue(needs_rehash(old))
        self.assertFalse(needs_rehash(hash_secret("pw")))
        self.assertTrue(needs_rehash("$argon2id$..."))

    def test_empty_secret_rejected(self) -> None:
        with self.assertRaises(SecurityError):
            hash_secret("")

    def test_password_and_pin_rules(self) -> None:
        self.assertIsNone(password_problem("ramesh26"))
        self.assertIsNone(password_problem("482619"), "6-digit numeric allowed for staff speed")
        self.assertIn("6", password_problem("abc") or "")
        self.assertEqual(password_problem("password"), "too common")
        self.assertIsNotNone(password_problem(" spaced"))
        self.assertIsNone(pin_problem("4826"))
        self.assertIsNone(pin_problem("482619"))
        self.assertIsNotNone(pin_problem("12"))
        self.assertIsNotNone(pin_problem("1234"))
        self.assertIsNotNone(pin_problem("0000"))
        self.assertIsNotNone(pin_problem("12a4"))


class LockoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock()
        clk.set_clock(self.clock)

    def tearDown(self) -> None:
        clk.set_clock(SystemClock())

    def test_three_failures_lock_for_fifteen_minutes(self) -> None:
        pol = LockoutPolicy()
        self.assertFalse(pol.record_failure("owner-pin"))
        self.assertFalse(pol.record_failure("owner-pin"))
        self.assertTrue(pol.record_failure("owner-pin"), "third failure locks")
        self.assertTrue(pol.is_locked("owner-pin"))
        self.assertEqual(pol.seconds_remaining("owner-pin"), 15 * 60)
        self.clock.advance(minutes=14)
        self.assertTrue(pol.is_locked("owner-pin"))
        self.clock.advance(minutes=1)
        self.assertFalse(pol.is_locked("owner-pin"))
        self.assertEqual(pol.failures("owner-pin"), 0, "counter resets when the lock expires")

    def test_success_clears_and_subjects_are_independent(self) -> None:
        pol = LockoutPolicy()
        pol.record_failure("a")
        pol.record_failure("a")
        pol.record_success("a")
        self.assertEqual(pol.failures("a"), 0)
        pol.record_failure("b")
        self.assertEqual(pol.failures("b"), 1)
        self.assertFalse(pol.is_locked("b"))


class RecoveryKeyTest(unittest.TestCase):
    def test_generate_and_normalise(self) -> None:
        words = tuple(f"word{i:04d}" for i in range(2048))
        key = generate_recovery_key(words)
        self.assertEqual(len(key.split()), 12)
        self.assertEqual(normalise_recovery_key("  Word0001   WORD0002 "), "word0001 word0002")
        with self.assertRaises(SecurityError):
            generate_recovery_key(("a", "b"))
        self.assertNotEqual(generate_recovery_key(words), key)


class BoxTest(unittest.TestCase):
    def test_master_key_shape_and_null_box(self) -> None:
        key = new_master_key()
        self.assertEqual(len(key), 44)
        self.assertTrue(key.endswith("="))
        with self.assertRaises(RuntimeError):
            NullBox().encrypt("x")
        self.assertEqual(masked_preview("shop@upi"), "•••• @upi")
        self.assertEqual(masked_preview("ab"), "••••")

    @unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography not installed on this PC")
    def test_fernet_roundtrip_and_tamper(self) -> None:
        box = sec.FernetBox(new_master_key())
        token = box.encrypt("UPI: shop@upi")
        self.assertNotIn("shop@upi", token)
        self.assertEqual(box.decrypt(token), "UPI: shop@upi")
        with self.assertRaises(ValueError):
            box.decrypt(token[:-2] + "xx")
        with self.assertRaises(ValueError):
            sec.FernetBox(new_master_key()).decrypt(token)

    @unittest.skipIf(HAS_CRYPTOGRAPHY, "only meaningful without cryptography")
    def test_fernet_box_reports_missing_library_clearly(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            sec.FernetBox(new_master_key())
        self.assertIn("cryptography", str(cm.exception))


class SecretsStoreTest(unittest.TestCase):
    """Keyring is forced unavailable so the file fallback path is exercised."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.primary = Path(self._tmp.name)
        self._p1 = mock.patch.object(ps, "_keyring_get", return_value=None)
        self._p2 = mock.patch.object(ps, "_keyring_set", return_value=False)
        self._p1.start()
        self._p2.start()

    def tearDown(self) -> None:
        self._p1.stop()
        self._p2.stop()
        self._tmp.cleanup()

    def test_creates_file_key_then_reuses_it(self) -> None:
        first = ps.load_or_create_master_key(self.primary)
        self.assertEqual((first.backend, first.created), ("file", True))
        path = ps.key_file_path(self.primary)
        self.assertTrue(path.exists())
        if not sys.platform.startswith("win"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        second = ps.load_or_create_master_key(self.primary)
        self.assertEqual((second.backend, second.created, second.value), ("file", False, first.value))

    def test_migrates_file_key_into_keyring_when_it_becomes_available(self) -> None:
        first = ps.load_or_create_master_key(self.primary)
        stored: dict[str, str] = {}
        with mock.patch.object(ps, "_keyring_set", side_effect=lambda v: stored.__setitem__("k", v) or True):
            migrated = ps.load_or_create_master_key(self.primary)
        self.assertEqual((migrated.backend, migrated.value), ("keyring", first.value))
        self.assertEqual(stored["k"], first.value)
        self.assertFalse(ps.key_file_path(self.primary).exists(), "file removed after migration")

    def test_keyring_preferred_when_present(self) -> None:
        with mock.patch.object(ps, "_keyring_get", return_value="KEYFROMRING=="):
            mk = ps.load_or_create_master_key(self.primary)
        self.assertEqual((mk.backend, mk.value), ("keyring", "KEYFROMRING=="))
        self.assertFalse(ps.key_file_path(self.primary).exists())

    def test_unavailable_everywhere_raises(self) -> None:
        with mock.patch.object(ps, "_file_set", return_value=False), self.assertRaises(ps.KeyStoreUnavailable):
            ps.load_or_create_master_key(self.primary)
        with self.assertRaises(ps.KeyStoreUnavailable):
            ps.load_or_create_master_key(self.primary, allow_file_fallback=False)

    def test_forget_removes_file(self) -> None:
        ps.load_or_create_master_key(self.primary)
        ps.forget_master_key(self.primary)
        self.assertFalse(ps.key_file_path(self.primary).exists())

    def test_keyring_available_false_without_package(self) -> None:
        if importlib.util.find_spec("keyring") is None:
            self.assertFalse(ps.keyring_available())


class PathsTest(unittest.TestCase):
    def test_layout_and_ensure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths.from_locations(Path(tmp) / "primary", Path(tmp) / "secondary")
            self.assertEqual(paths.database, Path(tmp) / "primary" / "data" / "vaishnavi.db")
            created = paths.ensure()
            self.assertIn(paths.data, created)
            self.assertIn(paths.snapshots, created)
            self.assertTrue(all(p.is_dir() for p in paths.primary_folders()))
            self.assertEqual(paths.ensure(), [], "idempotent")
            self.assertEqual(paths.receipt_folder(2026, 3), paths.receipts / "2026" / "03")
            self.assertTrue(paths.secondary_reachable())

    def test_secondary_unreachable_is_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_secondary = Path(tmp) / "file-not-dir"
            bad_secondary.write_text("x", encoding="utf-8")
            paths = AppPaths(Path(tmp) / "p", bad_secondary)
            paths.ensure()  # must not raise
            self.assertTrue(paths.data.is_dir())
            self.assertFalse(paths.secondary_reachable())

    def test_defaults_and_program_dir(self) -> None:
        paths = AppPaths.from_locations(None, None)
        self.assertEqual(paths.primary.name, "VaishnaviStore")
        self.assertEqual(paths.secondary.name, "VaishnaviStore-Backups")
        prog = pp.program_dir()
        self.assertTrue((prog / "main.py").exists())
        self.assertTrue((pp.config_dir() / "settings.toml").exists())
        with mock.patch.dict(os.environ, {"VAISHNAVI_PROGRAM_DIR": "/elsewhere"}):
            self.assertEqual(pp.program_dir(), Path("/elsewhere"))


if __name__ == "__main__":
    unittest.main()
