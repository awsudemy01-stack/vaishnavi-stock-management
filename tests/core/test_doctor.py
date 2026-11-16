"""Pre-flight doctor (docs/07 §5): runner, reporters, exit codes and the first ten check groups.

Written before the code (docs/13 rule 7). Checks that need the venv (packages) or the OS (autostart,
ports) are exercised through injected probes; database/ledger/backup checks run on real files.
"""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from vaishnavi.bootstrap import doctor as doc
from vaishnavi.bootstrap.checks import autostart as c_autostart
from vaishnavi.bootstrap.checks import backup as c_backup
from vaishnavi.bootstrap.checks import config as c_config
from vaishnavi.bootstrap.checks import database as c_database
from vaishnavi.bootstrap.checks import filesystem as c_fs
from vaishnavi.bootstrap.checks import ledger as c_ledger
from vaishnavi.bootstrap.checks import network as c_network
from vaishnavi.bootstrap.checks import packages as c_packages
from vaishnavi.bootstrap.checks import python_env as c_python
from vaishnavi.bootstrap.checks import secrets as c_secrets
from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext, Status, run_doctor
from vaishnavi.core.clock import UTC, FrozenClock, iso_utc, set_clock
from vaishnavi.core.i18n import Texts
from vaishnavi.core.settings import Settings
from vaishnavi.db import seed
from vaishnavi.db.schema import HEAD_REVISION, apply_schema
from vaishnavi.platform.paths import AppPaths

from tests.core.test_db_guardian_backup import T0, make_live_db

PROJECT = Path(__file__).resolve().parents[2]


class FakeCheck:
    def __init__(self, key: str, result: CheckResult, *, group: str = "software", fix_result: CheckResult | None = None, raise_exc: bool = False) -> None:
        self.key, self.group, self.result, self.fix_result, self.raise_exc = key, group, result, fix_result, raise_exc
        self.fix_calls = 0

    def run(self, ctx: DoctorContext) -> CheckResult:
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.result

    def fix(self, ctx: DoctorContext) -> CheckResult | None:
        self.fix_calls += 1
        return self.fix_result


def _ctx(tmp: Path, *, lang: str = "en", fix: bool = False, settings: Settings | None = None, **runtime) -> DoctorContext:  # type: ignore[no-untyped-def]
    paths = AppPaths(tmp / "primary", tmp / "secondary")
    paths.ensure()
    config_dir = tmp / "config"
    config_dir.mkdir(exist_ok=True)
    return DoctorContext(settings=settings or Settings(), paths=paths, config_dir=config_dir, lang=lang, fix=fix, runtime=dict(runtime))


class RunnerAndReportersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_runs_in_order_times_each_check_and_maps_worst_status_to_exit_code(self) -> None:
        checks = [FakeCheck("a", CheckResult.ok("fine")), FakeCheck("b", CheckResult.warn("note", error_code="E-SYS-012", hint="move it")), FakeCheck("c", CheckResult.skip("n/a"))]
        report = run_doctor(checks, _ctx(self.tmp))
        self.assertEqual([r.key for r in report.results], ["a", "b", "c"])
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(all(r.elapsed_ms >= 0 for r in report.results))
        self.assertEqual(len(report.ref), 26, "a ULID reference the owner can quote")
        checks.append(FakeCheck("d", CheckResult.fail("broken", error_code="E-DB-003", hint="restore")))
        self.assertEqual(run_doctor(checks, _ctx(self.tmp)).exit_code, 2)
        self.assertEqual(run_doctor([FakeCheck("s", CheckResult.skip("x"))], _ctx(self.tmp)).exit_code, 0)

    def test_crashing_check_becomes_fail_e_sys_099_with_traceback_in_log(self) -> None:
        with self.assertLogs("vaishnavi.bootstrap.doctor", level="ERROR") as logs:
            report = run_doctor([FakeCheck("x", CheckResult.ok(), raise_exc=True)], _ctx(self.tmp))
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.results[0].error_code, "E-SYS-099")
        self.assertTrue(any("Traceback" in line for line in logs.output))

    def test_fix_runs_only_with_fix_flag_and_only_for_non_pass_results(self) -> None:
        fixable = FakeCheck("f", CheckResult.warn("stale", error_code="E-BKP-001"), fix_result=CheckResult.ok("fixed"))
        fine = FakeCheck("g", CheckResult.ok(), fix_result=CheckResult.ok("should not run"))
        run_doctor([fixable, fine], _ctx(self.tmp))
        self.assertEqual((fixable.fix_calls, fine.fix_calls), (0, 0))
        report = run_doctor([fixable, fine], _ctx(self.tmp, fix=True))
        self.assertEqual((fixable.fix_calls, fine.fix_calls), (1, 0))
        self.assertIs(report.results[0].status, Status.PASS)
        self.assertTrue(report.results[0].fix_applied)
        self.assertEqual(report.exit_code, 0)

    def test_text_report_in_english_and_hindi_lists_titles_codes_and_hints(self) -> None:
        checks = [FakeCheck("database", CheckResult.ok("1 238 bills verified"), group="database"), FakeCheck("backup", CheckResult.warn("drive not found", error_code="E-BKP-001", hint="Plug it in"), group="backup")]
        for lang, title in (("en", "Shop database"), ("hi", "दुकान का डेटाबेस")):
            report = run_doctor(checks, _ctx(self.tmp, lang=lang))
            text = doc.render_text(report, Texts.load(lang))
            self.assertIn(title, text)
            self.assertIn("E-BKP-001", text)
            self.assertIn("Plug it in", text)
            self.assertIn(report.ref, text)
            self.assertNotIn("Traceback", text)

    def test_json_report_is_machine_readable_and_complete(self) -> None:
        report = run_doctor([FakeCheck("a", CheckResult.ok("x", items=3))], _ctx(self.tmp))
        data = json.loads(doc.render_json(report))
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["ref"], report.ref)
        self.assertEqual(data["checks"][0]["key"], "a")
        self.assertEqual(data["checks"][0]["status"], "PASS")
        self.assertEqual(data["checks"][0]["data"], {"items": 3})
        self.assertIn("at", data)
        self.assertIn("duration_ms", data)

    def test_default_check_set_covers_the_documented_groups_in_order(self) -> None:
        keys = [c.key for c in doc.default_checks()]
        for expected in ("python_env", "packages", "filesystem", "config", "database", "ledger", "backup", "secrets", "network", "autostart"):
            self.assertIn(expected, keys)
        self.assertLess(keys.index("python_env"), keys.index("database"))
        self.assertLess(keys.index("database"), keys.index("ledger"))
        self.assertLess(keys.index("ledger"), keys.index("backup"))


class EnvironmentChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_python_env_warns_outside_a_venv_and_fails_when_too_old(self) -> None:
        check = c_python.PythonEnvCheck()
        with mock.patch.object(c_python.sys, "prefix", "/usr"), mock.patch.object(c_python.sys, "base_prefix", "/usr"):
            result = check.run(_ctx(self.tmp))
        self.assertIs(result.status, Status.WARN)
        with mock.patch.object(c_python, "version_tuple", return_value=(3, 9)):
            result = check.run(_ctx(self.tmp))
        self.assertIs(result.status, Status.FAIL)
        self.assertEqual(result.error_code, "E-SYS-001")

    def test_packages_check_really_imports_and_fails_on_missing(self) -> None:
        self.assertIs(c_packages.PackagesCheck(modules=("json", "sqlite3")).run(_ctx(self.tmp)).status, Status.PASS)
        result = c_packages.PackagesCheck(modules=("json", "no_such_module_xyz")).run(_ctx(self.tmp))
        self.assertIs(result.status, Status.FAIL)
        self.assertEqual(result.error_code, "E-SYS-002")
        self.assertIn("no_such_module_xyz", result.message)

    def test_filesystem_fails_on_missing_primary_and_warns_on_low_disk(self) -> None:
        ctx = _ctx(self.tmp)
        self.assertIs(c_fs.FilesystemCheck().run(ctx).status, Status.PASS)
        import shutil

        with mock.patch.object(c_fs.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1 * 1024**3)):  # type: ignore[attr-defined]
            result = c_fs.FilesystemCheck().run(ctx)
        self.assertIs(result.status, Status.WARN)
        self.assertIn("GB", result.message)
        gone = AppPaths(self.tmp / "nowhere", self.tmp / "secondary")
        ctx2 = DoctorContext(settings=Settings(), paths=gone, config_dir=self.tmp / "config", lang="en", fix=False, runtime={})
        result = c_fs.FilesystemCheck().run(ctx2)
        self.assertIs(result.status, Status.FAIL)
        self.assertEqual(result.error_code, "E-SYS-013")

    def test_config_check_fails_on_invalid_toml_and_on_a_key_inside_it(self) -> None:
        ctx = _ctx(self.tmp)
        self.assertIs(c_config.ConfigCheck().run(ctx).status, Status.PASS, "no settings.toml → defaults are valid")
        (ctx.config_dir / "settings.toml").write_text('[billing]\nmanual_add_policy = "MAYBE"\n', encoding="utf-8")
        result = c_config.ConfigCheck().run(ctx)
        self.assertIs(result.status, Status.FAIL)
        self.assertEqual(result.error_code, "E-CFG-001")
        self.assertIn("billing.manual_add_policy", result.message)
        (ctx.config_dir / "settings.toml").write_text('[ai.openai]\napi_key = "sk-oops"\n', encoding="utf-8")
        result = c_config.ConfigCheck().run(ctx)
        self.assertIs(result.status, Status.FAIL)
        self.assertEqual(result.error_code, "E-SEC-006")
        self.assertNotIn("sk-oops", result.message + result.detail)

    def test_secrets_check_warns_on_file_fallback_and_on_leftover_secrets_env(self) -> None:
        ctx = _ctx(self.tmp)
        with mock.patch.object(c_secrets, "keyring_available", return_value=True):
            self.assertIs(c_secrets.SecretsCheck().run(ctx).status, Status.PASS)
        with mock.patch.object(c_secrets, "keyring_available", return_value=False):
            result = c_secrets.SecretsCheck().run(ctx)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SEC-001"))
        (ctx.config_dir / "secrets.env").write_text("VAISHNAVI_OPENAI_API_KEY=sk-x\n", encoding="utf-8")
        with mock.patch.object(c_secrets, "keyring_available", return_value=True):
            result = c_secrets.SecretsCheck().run(ctx)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SEC-005"))

    def test_network_check_port_busy_fails_and_clock_behind_last_sale_warns(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        settings = Settings()
        settings.server.port = port
        try:
            result = c_network.NetworkCheck().run(_ctx(self.tmp, settings=settings))
        finally:
            srv.close()
        self.assertIs(result.status, Status.FAIL)
        self.assertIn(str(port), result.message)
        result = c_network.NetworkCheck().run(_ctx(self.tmp, settings=settings, app_running=True))
        self.assertIsNot(result.status, Status.FAIL, "when the running app asks, its own port is not a problem")

    def test_network_check_passes_when_the_port_is_held_by_a_running_vaishnavi(self) -> None:
        """Live run 3: yesterday's instance was still up; the doctor called it 'another program' and refused the day."""
        import json

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)  # three probes connect without anyone accepting; a backlog of 1 would refuse the later ones
        port = srv.getsockname()[1]
        settings = Settings()
        settings.server.port = port
        health = json.dumps({"ok": True, "app": "vaishnavi", "version": "1.2.3", "uptime_s": 7260, "heartbeats": {}})
        try:
            ours = c_network.NetworkCheck().run(_ctx(self.tmp, settings=settings, health_fetch=lambda url: health))
            other = c_network.NetworkCheck().run(_ctx(self.tmp, settings=settings, health_fetch=lambda url: "<html>nginx</html>"))
            dead = c_network.NetworkCheck().run(_ctx(self.tmp, settings=settings, health_fetch=lambda url: (_ for _ in ()).throw(OSError("refused"))))
        finally:
            srv.close()
        self.assertIs(ours.status, Status.PASS)
        self.assertTrue(ours.data.get("already_running"))
        self.assertIn("1.2.3", ours.message)
        self.assertIn("121 min", ours.message)
        self.assertEqual((other.status, other.error_code), (Status.FAIL, "E-SYS-017"))
        self.assertEqual((dead.status, dead.error_code), (Status.FAIL, "E-SYS-017"))
        clock = FrozenClock(T0 - timedelta(hours=2))
        prev = set_clock(clock)
        try:
            result = c_network.NetworkCheck().run(_ctx(self.tmp, last_event_at=iso_utc(T0)))
        finally:
            set_clock(prev)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SYS-030"))

    def test_autostart_check_uses_the_platform_probe(self) -> None:
        self.assertIs(c_autostart.AutostartCheck(probe=lambda: True).run(_ctx(self.tmp)).status, Status.PASS)
        result = c_autostart.AutostartCheck(probe=lambda: False).run(_ctx(self.tmp))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SYS-005"))
        self.assertIs(c_autostart.AutostartCheck(probe=lambda: None).run(_ctx(self.tmp)).status, Status.SKIP)


def _complete_owner(db: Path) -> None:
    """Give the seeded Owner a real password + PIN (what the first-run wizard does) and switch to WAL."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")  # must run outside a transaction → before any UPDATE
    conn.execute("UPDATE user SET password_hash = '$scrypt$ln=15,r=8,p=1$a$b', owner_pin_hash = 'x' WHERE role_id = 1")
    conn.commit()
    conn.close()


class DatabaseChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.ctx = _ctx(self.tmp)
        self._dev = mock.patch("vaishnavi.services.backup.device_id", side_effect=lambda p: "B" if str(p).startswith(str(self.ctx.paths.secondary)) else "A")
        self._dev.start()

    def tearDown(self) -> None:
        self._dev.stop()
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_missing_database_fails_and_fix_creates_and_seeds_it(self) -> None:
        check = c_database.DatabaseCheck()
        result = check.run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.FAIL, "E-DB-002"))
        fixed = check.fix(_ctx(self.tmp, fix=True))
        assert fixed is not None
        self.assertIs(fixed.status, Status.WARN, "created + seeded, but Owner setup is still pending")
        self.assertEqual(fixed.error_code, "E-AUTH-005")
        conn = sqlite3.connect(self.ctx.paths.database)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM role").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT version_num FROM alembic_version").fetchone()[0], HEAD_REVISION, "a stdlib-created database is stamped at head so Alembic agrees later")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

    def test_healthy_database_passes_with_counts(self) -> None:
        make_live_db(self.ctx.paths.database)
        _complete_owner(self.ctx.paths.database)
        result = c_database.DatabaseCheck().run(self.ctx)
        self.assertIs(result.status, Status.PASS, result.detail)
        self.assertEqual(result.data["sales"], 2)

    def test_damaged_database_fails_and_restored_or_unclean_copies_warn(self) -> None:
        self.ctx.paths.database.write_bytes(b"junk" * 1000)
        result = c_database.DatabaseCheck().run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.FAIL, "E-DB-003"))
        make_live_db(self.ctx.paths.database)
        _complete_owner(self.ctx.paths.database)
        conn = sqlite3.connect(self.ctx.paths.database)
        conn.execute("UPDATE db_identity SET last_open_at = ?", (iso_utc(T0),))
        conn.commit()
        conn.close()
        result = c_database.DatabaseCheck().run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-DB-022"))

    def test_schema_behind_head_is_reported(self) -> None:
        make_live_db(self.ctx.paths.database)
        conn = sqlite3.connect(self.ctx.paths.database)
        conn.execute("UPDATE alembic_version SET version_num = '0000_older'")
        conn.commit()
        conn.close()
        result = c_database.DatabaseCheck().run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.FAIL, "E-DB-010"))
        self.assertIn("0000_older", result.detail)

    def test_ledger_check_passes_fails_on_gap_and_warns_on_stock_mismatch(self) -> None:
        make_live_db(self.ctx.paths.database)
        self.assertIs(c_ledger.LedgerCheck().run(self.ctx).status, Status.PASS)
        conn = sqlite3.connect(self.ctx.paths.database)
        conn.execute("DROP TRIGGER trg_product_stock_no_direct_update")
        conn.execute("UPDATE product_stock SET qty = 5 WHERE product_id = 10")
        conn.commit()
        conn.close()
        result = c_ledger.LedgerCheck().run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-LEDGER-003"))
        conn = sqlite3.connect(self.ctx.paths.database)
        conn.execute("DROP TRIGGER trg_sale_no_delete")
        conn.execute("DROP TRIGGER trg_sale_item_no_delete")
        conn.execute("DROP TRIGGER trg_payment_no_delete")
        conn.execute("DELETE FROM payment WHERE sale_id = 1")
        conn.execute("DELETE FROM sale_item WHERE sale_id = 1")
        conn.execute("DELETE FROM sale WHERE id = 1")
        conn.commit()
        conn.close()
        result = c_ledger.LedgerCheck().run(self.ctx)
        self.assertIs(result.status, Status.FAIL)
        self.assertIn(result.error_code, ("E-LEDGER-001", "E-LEDGER-002"))

    def test_backup_check_warns_without_backup_fix_makes_one_and_flags_unplugged_drive(self) -> None:
        make_live_db(self.ctx.paths.database)
        check = c_backup.BackupCheck()
        result = check.run(self.ctx)
        self.assertIs(result.status, Status.WARN)
        fixed = check.fix(self.ctx)
        assert fixed is not None
        self.assertIs(fixed.status, Status.PASS)
        self.assertEqual(len(list(self.ctx.paths.snapshots.glob("*.db"))), 1)
        self.assertIs(check.run(self.ctx).status, Status.PASS)
        self.clock.advance(hours=25)
        self.assertIs(check.run(self.ctx).status, Status.WARN, "last backup older than 24 h")
        with mock.patch("vaishnavi.services.backup.device_id", side_effect=OSError("unplugged")):
            result = check.run(self.ctx)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-BKP-001"))


class CommandLineTest(unittest.TestCase):
    def test_python_m_vaishnavi_doctor_json_runs_with_bare_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "locations.toml").write_text(f'[locations]\nprimary = "{(root / "p").as_posix()}"\nsecondary = "{(root / "s").as_posix()}"\nsame_device = true\n', encoding="utf-8")
            AppPaths(root / "p", root / "s").ensure()  # the installer's locations stage creates these
            env = {**__import__("os").environ, "PYTHONPATH": str(PROJECT / "src"), "VAISHNAVI_PROGRAM_DIR": str(root), "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run([sys.executable, "-m", "vaishnavi", "doctor", "--json", "--checks", "filesystem,config,database"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, check=False)
            self.assertIn(proc.returncode, (0, 1, 2), proc.stderr)
            data = json.loads(proc.stdout)
            keys = [c["key"] for c in data["checks"]]
            self.assertEqual(keys, ["filesystem", "config", "database"])
            self.assertEqual(data["exit_code"], proc.returncode)
            by_key = {c["key"]: c for c in data["checks"]}
            self.assertEqual(by_key["database"]["error_code"], "E-DB-002", "no database yet")
            proc2 = subprocess.run([sys.executable, "-m", "vaishnavi", "doctor", "--lang", "hi", "--checks", "filesystem"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, check=False)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("फ़ोल्डर", proc2.stdout)


if __name__ == "__main__":
    unittest.main()
