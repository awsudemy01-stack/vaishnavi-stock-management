"""Unit 54 — hardening: ``restore`` command, ``--update`` planner/applier with rollback, TLS material, watchdog decision, placeholder sweep."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.settings import Settings
from vaishnavi.platform import tls, watchdog
from vaishnavi.platform.paths import AppPaths

from bootstrap import update as upd
from tests.core.test_db_guardian_backup import T0, make_live_db

ROOT = Path(__file__).resolve().parents[2]


class RestoreCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = AppPaths(self.root / "p", self.root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self._prev = set_clock(FrozenClock(T0))
        (self.root / "config").mkdir()

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _run(self, *args: str) -> tuple[int, str]:
        import io
        import sys

        from vaishnavi.__main__ import main

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            code = main(["--config-dir", str(self.root / "config"), *args])
        finally:
            sys.stdout = old
        return code, buf.getvalue()

    def test_list_restore_and_refusal_while_app_runs(self) -> None:
        import vaishnavi.__main__ as entry

        entry._import_commands()
        self.assertIn("restore", entry.command_names())
        # Settings default to the real locations; point them at the temp data root through locations.toml
        (self.root / "config" / "locations.toml").write_text(f'[locations]\nprimary = "{self.paths.primary.as_posix()}"\nsecondary = "{self.paths.secondary.as_posix()}"\nsame_device = true\n', encoding="utf-8")
        code, out = self._run("backup", "--trigger", "manual", "--json")
        self.assertEqual(code, 0, out)
        backup_id = json.loads(out)["backup_id"]
        code, out = self._run("restore", "--list", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)[0]["backup_id"], backup_id)
        code, out = self._run("restore", "--id", "nope", "--yes")
        self.assertEqual(code, 2)
        code, out = self._run("restore", "--id", backup_id, "--yes", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertTrue(data["pre_restore_backup_id"])
        # a fresh heartbeat means the app is running → refuse (E-SYS-016)
        self.paths.state.mkdir(parents=True, exist_ok=True)
        (self.paths.state / "heartbeat.json").write_text(json.dumps({"at": time.time()}), encoding="utf-8")
        code, out = self._run("restore", "--id", backup_id, "--yes")
        self.assertEqual(code, 2)


class UpdateTest(unittest.TestCase):
    def _release(self, root: Path, version: str, *, broken: bool = False) -> Path:
        rel = root / f"release-{version}"
        (rel / "src" / "vaishnavi").mkdir(parents=True)
        (rel / "bootstrap").mkdir()
        (rel / "src" / "vaishnavi" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (rel / "bootstrap" / "__init__.py").write_text("", encoding="utf-8")
        (rel / "main.py").write_text("print('new')\n", encoding="utf-8")
        (rel / "pyproject.toml").write_text(f'[project]\nname = "vaishnavi"\nversion = "{version}"\n', encoding="utf-8")
        if broken:
            (rel / "src" / "vaishnavi" / "__init__.py").write_text("this is not python (\n", encoding="utf-8")
        return rel

    def test_plan_validates_release_and_apply_rolls_back_on_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            program = root / "program"
            self._release(root, "0.9.0")  # current install
            (root / "release-0.9.0").rename(program)
            new = self._release(root, "1.0.0")
            plan = upd.plan_update(program, new)
            self.assertEqual(plan.new_version, "1.0.0")
            self.assertEqual(plan.current_version, "0.9.0")
            self.assertEqual(sorted(e.name for e in plan.entries), ["bootstrap", "main.py", "pyproject.toml", "src"])
            with self.assertRaises(upd.UpdateError):
                upd.plan_update(program, root / "nowhere")
            (root / "junk").mkdir()
            with self.assertRaises(upd.UpdateError):
                upd.plan_update(program, root / "junk")
            calls: list[str] = []

            def ok_checks(stage: str) -> tuple[bool, str]:
                calls.append(stage)
                return True, ""

            result = upd.apply_update(plan, run_check=ok_checks)
            self.assertTrue(result.ok, result.detail)
            self.assertEqual((program / "src" / "vaishnavi" / "__init__.py").read_text(encoding="utf-8"), '__version__ = "1.0.0"\n')
            self.assertTrue((program / ".previous" / "src").exists(), "old copy kept for rollback")
            self.assertEqual(calls, ["install", "selftest"])
            # second update whose self-test fails → rolled back to 1.0.0
            newer = self._release(root, "1.1.0")
            plan2 = upd.plan_update(program, newer)

            def failing(stage: str) -> tuple[bool, str]:
                return (True, "") if stage == "install" else (False, "3 tests failed")

            result = upd.apply_update(plan2, run_check=failing)
            self.assertFalse(result.ok)
            self.assertIn("3 tests failed", result.detail)
            self.assertTrue(result.rolled_back)
            self.assertEqual((program / "src" / "vaishnavi" / "__init__.py").read_text(encoding="utf-8"), '__version__ = "1.0.0"\n')

    def test_zip_release_is_unpacked_first(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            program = root / "program"
            self._release(root, "0.9.0").rename(program)
            new = self._release(root, "2.0.0")
            archive = Path(shutil.make_archive(str(root / "release-2.0.0"), "zip", root_dir=new))
            plan = upd.plan_update(program, archive)
            self.assertEqual(plan.new_version, "2.0.0")
            self.assertTrue(plan.source.is_dir())


class TlsAndWatchdogTest(unittest.TestCase):
    def test_self_signed_material_is_created_once_or_reported_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            result = tls.ensure_self_signed(Path(d), hosts=["127.0.0.1", "localhost"])
            if result is None:
                self.assertFalse(list(Path(d).iterdir()), "nothing half-written when cryptography is missing")
                return
            cert, key = result
            self.assertTrue(cert.exists() and key.exists())
            self.assertIn(b"BEGIN CERTIFICATE", cert.read_bytes())
            again = tls.ensure_self_signed(Path(d), hosts=["127.0.0.1"])
            assert again is not None
            self.assertEqual(again[0].read_bytes(), cert.read_bytes(), "existing material is reused")

    def test_watchdog_decision_from_heartbeat_age(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            self.assertEqual(watchdog.decide(state, now=1000.0).action, "start", "no heartbeat at all → start the app")
            (state / "heartbeat.json").write_text(json.dumps({"at": 990.0, "pid": 4242}), encoding="utf-8")
            self.assertEqual(watchdog.decide(state, now=1000.0).action, "ok")
            d2 = watchdog.decide(state, now=1000.0 + watchdog.STALE_AFTER_S + 1)
            self.assertEqual((d2.action, d2.pid), ("restart", 4242))
            (state / "heartbeat.json").write_text("garbage", encoding="utf-8")
            self.assertEqual(watchdog.decide(state, now=2000.0).action, "start")


class SweepTest(unittest.TestCase):
    def test_no_docstring_only_placeholder_modules_remain(self) -> None:
        src = ROOT / "src" / "vaishnavi"
        leftovers = []
        for path in src.rglob("*.py"):
            if path.name == "__init__.py":
                continue
            text = path.read_text(encoding="utf-8").strip()
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) <= 2 and text.startswith('"""') and text.endswith('"""'):
                leftovers.append(str(path.relative_to(ROOT)))
        self.assertEqual(leftovers, [], "placeholder modules must be implemented or removed")
        self.assertFalse((src / "db" / "repositories").exists(), "ORM repositories were superseded by sqlite_stores")


if __name__ == "__main__":
    unittest.main()
