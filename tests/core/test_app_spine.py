"""App spine (docs/07 §6, docs/13 unit 26): notifications, scheduler, system API handlers, start-up orchestrator,
app-side commands, and the installer's database / doctor / start+watchdog stages.

Written first (docs/13 rule 7). Servers, Ollama and the browser are faked; databases are real files.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from vaishnavi.api import system as api_system
from vaishnavi.bootstrap import startup
from vaishnavi.core.clock import UTC, FrozenClock, iso_utc, set_clock
from vaishnavi.core.events import BackupDone, EventBus, StockChanged
from vaishnavi.core.settings import Settings
from vaishnavi.db.sqlite_stores import SqliteNotificationStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.scheduler import jobs as jobs_module
from vaishnavi.scheduler.simple import SimpleScheduler
from vaishnavi.services.notifications import NotificationService

from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_doctor import _complete_owner

PROJECT = Path(__file__).resolve().parents[2]


def _env_for(root: Path) -> dict[str, str]:
    import os

    return {**os.environ, "PYTHONPATH": str(PROJECT / "src"), "VAISHNAVI_PROGRAM_DIR": str(root), "PYTHONIOENCODING": "utf-8"}


def _cryptography_available() -> bool:
    try:
        import cryptography  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _program(root: Path, *, same_device: bool = True) -> AppPaths:
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "locations.toml").write_text(f'[locations]\nprimary = "{(root / "p").as_posix()}"\nsecondary = "{(root / "s").as_posix()}"\nsame_device = {"true" if same_device else "false"}\n', encoding="utf-8")
    paths = AppPaths(root / "p", root / "s")
    paths.ensure()
    return paths


# ============================================================================ notifications
class NotificationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = _program(root)
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.svc = NotificationService(SqliteTxn(self.paths.database), SqliteNotificationStore(), event_bus=self.bus)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_notify_lists_marks_read_and_deduplicates_open_alerts(self) -> None:
        nid = self.svc.notify("WARN", "BACKUP", "Backup drive not found", body="2 waiting", deep_link="/settings/backups", source_ref="E-BKP-001")
        self.assertIsNotNone(nid)
        self.assertIsNone(self.svc.notify("WARN", "BACKUP", "Backup drive not found", source_ref="E-BKP-001"), "same open alert is not repeated")
        unread = self.svc.unread(user_id=1)
        self.assertEqual([n["title"] for n in unread], ["Backup drive not found"])
        self.assertEqual(self.svc.unread_count(user_id=1), 1)
        self.assertTrue(self.svc.mark_read(user_id=1, notification_id=nid))  # type: ignore[arg-type]
        self.assertEqual(self.svc.unread_count(user_id=1), 0)
        self.assertIsNotNone(self.svc.notify("WARN", "BACKUP", "Backup drive not found", source_ref="E-BKP-001"), "after it was read it may fire again")
        with self.assertRaises(ValueError):
            self.svc.notify("LOUD", "X", "bad severity")

    def test_subscribed_events_create_notifications(self) -> None:
        self.svc.subscribe(self.bus)
        self.bus.publish(BackupDone(backup_id="b1", path="/x", trigger="nightly", queued=True, size_bytes=10))
        self.bus.publish(StockChanged(product_id=10, movement_type="SALE", qty_delta=-1, qty_after=2, low_stock=True, product_name="Camphor 50g"))
        titles = [n["kind"] for n in self.svc.unread(user_id=1)]
        self.assertIn("BACKUP_QUEUED", titles)
        self.assertIn("LOW_STOCK", titles)


# ============================================================================ scheduler
class SchedulerTest(unittest.TestCase):
    def test_interval_and_daily_jobs_run_and_errors_are_isolated(self) -> None:
        now = [datetime(2026, 11, 14, 15, 59, 50, tzinfo=UTC)]  # 21:29:50 IST
        ran: list[str] = []
        sched = SimpleScheduler(clock=lambda: now[0], sleep=lambda s: None)
        sched.every(seconds=15, name="heartbeat", func=lambda: ran.append("hb"))
        sched.daily(at="21:30", name="nightly", func=lambda: ran.append("nightly"))
        sched.every(seconds=15, name="broken", func=lambda: 1 / 0)
        with self.assertLogs("vaishnavi.scheduler", level="ERROR") as logs:
            sched.run_pending()
        self.assertEqual(ran, ["hb"], "interval jobs run immediately on first tick; the daily one waits for 21:30 IST")
        self.assertTrue(any("JOB_FAILED" in line and "broken" in line for line in logs.output))
        now[0] += timedelta(seconds=5)
        sched.run_pending()
        self.assertEqual(ran, ["hb"], "not yet due again")
        now[0] += timedelta(seconds=10)
        sched.run_pending()
        self.assertEqual(ran, ["hb", "hb", "nightly"])
        now[0] += timedelta(hours=1)
        sched.run_pending()
        self.assertEqual(ran.count("nightly"), 1, "daily job runs once per day")
        status = sched.status()
        self.assertEqual({j["name"] for j in status}, {"heartbeat", "nightly", "broken"})
        self.assertEqual(next(j for j in status if j["name"] == "broken")["failures"], 3)

    def test_documented_job_table_is_built_from_settings(self) -> None:
        settings = Settings()
        names = {j.name for j in jobs_module.build_jobs(settings)}
        for expected in ("backup_nightly", "verify_chain", "session_monitor", "hardware_poll", "alerts", "heartbeat", "restore_drill", "db_maintenance", "log_prune", "backup_queue_flush"):
            self.assertIn(expected, names)
        settings.backup.interval_minutes = 15
        self.assertIn("backup_interval", {j.name for j in jobs_module.build_jobs(settings)})


# ============================================================================ system API handlers
class SystemApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = _program(root)
        make_live_db(self.paths.database)
        _complete_owner(self.paths.database)
        self.state = api_system.AppState(settings=Settings(), paths=self.paths, config_dir=root / "config", started_at=T0)
        self.clock = FrozenClock(T0 + timedelta(minutes=10))
        self._prev = set_clock(self.clock)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_healthz_reports_heartbeats_and_goes_unhealthy_when_stale(self) -> None:
        self.state.beat("request")
        self.state.beat("scheduler")
        health = api_system.healthz(self.state)
        self.assertTrue(health["ok"])
        self.assertEqual(health["status"], "ok")
        self.assertIn("request", health["heartbeats"])
        self.clock.advance(seconds=90)
        health = api_system.healthz(self.state)
        self.assertFalse(health["ok"], "a heartbeat older than 60 s means the loop is stuck")
        self.assertEqual(health["stale"], ["request", "scheduler"])
        self.assertEqual(api_system.http_status_for(health), 503)

    def test_status_aggregates_the_chips(self) -> None:
        self.state.chips["printer"] = ("amber", "not set up")
        status = api_system.status(self.state)
        self.assertEqual(status["chips"]["printer"], {"level": "amber", "text": "not set up"})
        self.assertEqual(status["version"], "0.1.0")
        self.assertIn("uptime_s", status)
        self.assertTrue(status["database"]["exists"])

    def test_logs_tail_filters_by_level_and_family_and_never_leaves_the_logs_folder(self) -> None:
        app_dir = self.paths.logs / "app"
        app_dir.mkdir(parents=True)
        (app_dir / "vaishnavi.log").write_text("2026-11-14 10:00:00.000 IST | INFO  | a | cid=1 | hello\n2026-11-14 10:00:01.000 IST | ERROR | b | cid=2 | bad thing\n2026-11-14 10:00:02.000 IST | DEBUG | c | cid=3 | noisy\n", encoding="utf-8")
        tail = api_system.logs_tail(self.state, family="app", level="INFO", limit=10)
        self.assertEqual([line.split("|")[1].strip() for line in tail["lines"]], ["INFO", "ERROR"])
        self.assertEqual(api_system.logs_tail(self.state, family="app", level="ERROR", limit=10)["lines"][0].split("|")[1].strip(), "ERROR")
        self.assertEqual(api_system.logs_tail(self.state, family="../../etc", level="DEBUG", limit=10)["lines"], [])
        self.assertIn("app", api_system.log_families(self.state))

    def test_metrics_in_json_and_prometheus_text(self) -> None:
        self.state.metrics.inc("http_requests_total", 3)
        self.state.metrics.observe("checkout_ms", 120)
        self.state.metrics.observe("checkout_ms", 80)
        data = api_system.metrics(self.state)
        self.assertEqual(data["counters"]["http_requests_total"], 3)
        self.assertEqual(data["histograms"]["checkout_ms"]["count"], 2)
        self.assertEqual(data["histograms"]["checkout_ms"]["max"], 120)
        text = api_system.metrics_prometheus(self.state)
        self.assertIn("vaishnavi_http_requests_total 3", text)
        self.assertIn("vaishnavi_checkout_ms_count 2", text)

    def test_doctor_endpoint_runs_the_selected_checks_through_the_injected_runner(self) -> None:
        self.state.doctor_runner = startup.make_doctor_runner(self.state.settings, self.paths, self.state.config_dir)
        report = api_system.doctor(self.state, checks=["filesystem", "database"])
        self.assertEqual([c["key"] for c in report["checks"]], ["filesystem", "database"])
        self.assertIn(report["exit_code"], (0, 1))
        self.state.doctor_runner = None
        bare = api_system.doctor(self.state, checks=["database"])
        self.assertEqual(bare["exit_code"], 2, "without a runner the endpoint reports, never crashes")
        self.assertEqual(bare["checks"], [])


# ============================================================================ start-up orchestrator
class StartupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = _program(self.root)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _run(self, **kw) -> tuple[int, list[str]]:  # type: ignore[no-untyped-def]
        events: list[str] = []

        def fake_server(state: api_system.AppState, on_ready) -> None:  # type: ignore[no-untyped-def]
            events.append("server")
            state.beat("request")
            on_ready()
            events.append("server_exit")

        settings = Settings()
        settings.ai.provider = "none"
        code = startup.run_app(settings=settings, paths=self.paths, config_dir=self.root / "config", serve=fake_server, open_browser=False, lang="en", **kw)
        return code, events

    def test_normal_start_runs_triage_mark_open_light_doctor_scheduler_server_then_clean_close(self) -> None:
        make_live_db(self.paths.database)
        _complete_owner(self.paths.database)
        seen: dict[str, object] = {}

        def fake_server(state: api_system.AppState, on_ready) -> None:  # type: ignore[no-untyped-def]
            seen["chips"] = dict(state.chips)
            seen["secret"] = state.extra.get("storage_secret")
            state.beat("request")
            on_ready()

        settings = Settings()
        settings.ai.provider = "none"
        with self.assertLogs("vaishnavi.bootstrap.startup", level="INFO") as logs:
            code = startup.run_app(settings=settings, paths=self.paths, config_dir=self.root / "config", serve=fake_server, open_browser=False, lang="en")
        self.assertEqual(code, 0)
        text = "\n".join(logs.output)
        for marker in ("APP_START", "DB_TRIAGE", "DOCTOR_LIGHT", "SCHEDULER_STARTED", "SERVER_READY", "APP_CLEAN_CLOSE"):
            self.assertIn(marker, text)
        chips = seen["chips"]
        assert isinstance(chips, dict)
        self.assertIn("scanner", chips, "the hardware result is split into Scanner and Printer chips for the top bar")
        self.assertIn("printer", chips)
        self.assertNotIn("hardware", chips)
        self.assertEqual(chips["ai_provider"][0], "grey", "provider 'none' shows the assistant as off, not as a problem")
        self.assertTrue((self.paths.state / "ui.secret").exists(), "browser sessions survive a restart")
        self.assertEqual(seen["secret"], (self.paths.state / "ui.secret").read_text(encoding="ascii"))
        self.assertTrue(self.paths.fingerprint.exists(), "clean close wrote the fingerprint")
        self.assertTrue((self.paths.state / "heartbeat.json").exists())
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM app_run WHERE clean_stop = 1").fetchone()[0], 1)
        conn.close()

    def test_missing_database_is_created_and_seeded_on_start(self) -> None:
        code, events = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(self.paths.database.exists())
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM role").fetchone()[0], 2)
        conn.close()

    def test_damaged_database_refuses_to_start_with_e_db_003(self) -> None:
        self.paths.database.write_bytes(b"junk" * 500)
        with self.assertLogs("vaishnavi.bootstrap.startup", level="ERROR") as logs:
            code, events = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(events, [], "the server never started")
        self.assertTrue(any("E-DB-003" in line for line in logs.output))

    def test_missing_data_root_is_never_recreated_e_sys_013(self) -> None:
        import shutil

        make_live_db(self.paths.database)
        shutil.rmtree(self.paths.primary)  # the USB disk was unplugged
        with self.assertLogs("vaishnavi.bootstrap.startup", level="ERROR") as logs:
            code, events = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(events, [])
        self.assertTrue(any("E-SYS-013" in line for line in logs.output))
        self.assertFalse(self.paths.primary.exists(), "an empty folder on the system disk must not appear in place of the data")

    def test_second_instance_is_refused_e_db_030(self) -> None:
        make_live_db(self.paths.database)
        _complete_owner(self.paths.database)
        from vaishnavi.db.lock import WriterLock

        lock = WriterLock(self.paths.data / "vaishnavi.db.lock")
        lock.acquire()
        try:
            with self.assertLogs("vaishnavi.bootstrap.startup", level="ERROR") as logs:
                code, _ = self._run()
        finally:
            lock.release()
        self.assertEqual(code, 2)
        self.assertTrue(any("E-DB-030" in line for line in logs.output))


# ============================================================================ app-side commands (subprocess, bare python)
class CommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = _program(self.root)
        self.env = _env_for(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cmd(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "vaishnavi", *args], input=stdin, capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=180, check=False)

    def test_init_db_creates_seeds_imports_secrets_and_prints_a_summary(self) -> None:
        proc = self._cmd("init-db", "--json", stdin=json.dumps({"secrets": {"ai.openai.api_key": "sk-testkey123456789"}}) + "\n")
        summary = json.loads(proc.stdout)
        self.assertTrue(summary["created"])
        self.assertTrue(summary["owner_setup_pending"])
        conn = sqlite3.connect(self.paths.database)
        try:
            if _cryptography_available():
                # The shop PC: the key is stored encrypted and the installer may delete its copy.
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(summary["secrets_stored"], ["ai.openai.api_key"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM secure_setting").fetchone()[0], 1)
                self.assertNotIn(b"sk-testkey", conn.execute("SELECT ciphertext FROM secure_setting").fetchone()[0])
            else:
                # No secret box → the key is NOT stored (never in clear) and the installer is told to keep its copy.
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(summary["secrets_stored"], [])
                self.assertEqual(summary["secrets_failed"], ["ai.openai.api_key"])
                self.assertIn("E-SEC-001", proc.stderr)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM secure_setting").fetchone()[0], 0)
        finally:
            conn.close()
        again = self._cmd("init-db", "--json", stdin="{}\n")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertFalse(json.loads(again.stdout)["created"], "idempotent")
        self.assertNotIn("sk-testkey", proc.stdout + proc.stderr)

    def test_reset_owner_password_via_stdin_json(self) -> None:
        self._cmd("init-db", stdin="{}\n")
        from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteRecoveryStore, SqliteTxn, SqliteUserStore
        from vaishnavi.services.audit import AuditService
        from vaishnavi.services.recovery import RecoveryService
        from vaishnavi.services.user import UserService

        txn = SqliteTxn(self.paths.database)
        users = UserService(txn, SqliteUserStore(), AuditService(SqliteAuditStore()))
        users.complete_owner_setup("owner-pass-1", "7391")
        issued = RecoveryService(txn, SqliteRecoveryStore(), SqliteUserStore(), AuditService(SqliteAuditStore())).issue(None)
        bad = self._cmd("reset-owner-password", stdin=json.dumps({"secrets": {"recovery_key": "abandon " * 12, "new_password": "new-owner-pass"}}) + "\n")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("E-AUTH-040", bad.stdout + bad.stderr)
        good = self._cmd("reset-owner-password", stdin=json.dumps({"secrets": {"recovery_key": issued.phrase, "new_password": "new-owner-pass"}}) + "\n")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn("RESET_OK", good.stdout)
        sheet_lines = [line for line in good.stdout.splitlines() if line.startswith("SECRET|")]
        self.assertGreaterEqual(len(sheet_lines), 5, "the new recovery sheet is returned for the owner's eyes only")
        self.assertEqual(users.login("owner", "new-owner-pass").actor.role.value, "OWNER")

    def test_verify_chain_and_backup_commands(self) -> None:
        make_live_db(self.paths.database)
        ok = self._cmd("verify-chain", "--json")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(json.loads(ok.stdout)["ok"])
        snap = self._cmd("backup", "--json", "--trigger", "manual")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertTrue(Path(json.loads(snap.stdout)["path"]).exists())
        conn = sqlite3.connect(self.paths.database)
        conn.execute("DROP TRIGGER trg_sale_limited_update")
        conn.execute("UPDATE sale SET total_paise = 1 WHERE id = 1")
        conn.commit()
        conn.close()
        bad = self._cmd("verify-chain")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("E-LEDGER-001", bad.stdout + bad.stderr)


# ============================================================================ installer stages
class InstallerAppStagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.program_dir = Path(self._tmp.name)
        self.out = io.StringIO()

    def tearDown(self) -> None:
        import logging

        logging.shutdown()
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.NullHandler())
        self._tmp.cleanup()

    def test_pipeline_ends_with_database_doctor_and_start_stages_in_order(self) -> None:
        from bootstrap.cli import Options, Mode
        from bootstrap.stages import build_default_pipeline

        keys = [s.key for s in build_default_pipeline(Options(mode=Mode.INSTALL_RUN))]
        self.assertEqual(keys[-3:], ["database", "doctor", "start"])
        keys_only = [s.key for s in build_default_pipeline(Options(mode=Mode.INSTALL_ONLY, install_only=True))]
        self.assertEqual(keys_only[-2:], ["database", "doctor"], "--install-only does not start the app")

    def test_database_stage_hands_secrets_over_via_stdin_and_removes_the_secrets_file_after_commit(self) -> None:
        from bootstrap import app_stages
        from bootstrap.ai_provider import RUNTIME_DELETE_SECRETS_FILE, RUNTIME_SECRETS
        from bootstrap.handover import AppResult

        from tests.core.test_bootstrap_ai_provider import _ctx as installer_ctx

        ctx, out = installer_ctx(self.program_dir, "")
        ctx.results["venv"] = type("R", (), {"data": {"python": sys.executable}, "status": None})()  # type: ignore[assignment]
        secrets_file = self.program_dir / "config" / "secrets.env"
        secrets_file.parent.mkdir(exist_ok=True)
        secrets_file.write_text("x", encoding="utf-8")
        ctx.runtime[RUNTIME_SECRETS] = {"ai.openai.api_key": "sk-abc"}
        ctx.runtime[RUNTIME_DELETE_SECRETS_FILE] = secrets_file
        calls: dict[str, object] = {}

        def fake_run_app(python, args, *, secrets=None, on_line=None, timeout=0, cwd=None):  # type: ignore[no-untyped-def]
            calls["args"], calls["secrets"] = list(args), dict(secrets or {})
            return AppResult(0, json.dumps({"created": True, "seeded_rows": 40, "secrets_stored": list((secrets or {}).keys()), "owner_setup_pending": True, "migrated": False}), 10)

        with mock.patch.object(app_stages.handover, "run_app", fake_run_app), mock.patch.object(app_stages.handover, "app_available", lambda p: (True, "0.1.0")):
            result = app_stages.DatabaseStage().run(ctx)
        self.assertEqual(result.status.value, "OK", result.detail)
        self.assertEqual(calls["args"], ["init-db", "--json"])
        self.assertEqual(calls["secrets"], {"ai.openai.api_key": "sk-abc"})
        self.assertFalse(secrets_file.exists(), "removed only after the app confirmed the key is stored encrypted")
        self.assertNotIn(RUNTIME_SECRETS, ctx.runtime, "the in-memory copy is dropped")
        self.assertNotIn("sk-abc", out.getvalue())

    def test_doctor_stage_maps_exit_codes_and_shows_the_report(self) -> None:
        from bootstrap import app_stages
        from bootstrap.handover import AppResult

        from tests.core.test_bootstrap_ai_provider import _ctx as installer_ctx

        ctx, out = installer_ctx(self.program_dir, "")
        ctx.results["venv"] = type("R", (), {"data": {"python": sys.executable}, "status": None})()  # type: ignore[assignment]
        for exit_code, expected in ((0, "OK"), (1, "WARN"), (2, "FAIL")):
            with mock.patch.object(app_stages.handover, "run_app", lambda *a, **k: AppResult(exit_code, " ✔ Software ok\n ! Backup stale (E-BKP-001)\n RESULT: x", 5)), mock.patch.object(app_stages.handover, "app_available", lambda p: (True, "0.1.0")):
                result = app_stages.DoctorStage().run(ctx)
            self.assertEqual(result.status.value, expected)
        self.assertIn("Backup stale", out.getvalue())

    def test_start_stage_launches_detached_waits_for_health_and_prints_ready(self) -> None:
        from bootstrap import app_stages
        from bootstrap.stages import StageStatus

        from tests.core.test_bootstrap_ai_provider import _ctx as installer_ctx

        ctx, out = installer_ctx(self.program_dir, "")
        ctx.results["venv"] = type("R", (), {"data": {"python": sys.executable}, "status": None})()  # type: ignore[assignment]
        launched: list[list[str]] = []
        health = {"calls": 0}

        def fake_popen(cmd, **kw):  # type: ignore[no-untyped-def]
            launched.append(list(cmd))
            proc = mock.Mock()
            proc.pid = 777
            proc.poll.return_value = None
            return proc

        def fake_health(url: str, timeout: float = 2.0) -> dict | None:
            health["calls"] += 1
            return {"ok": True, "status": "ok"} if health["calls"] >= 2 else None

        with mock.patch.object(app_stages.subprocess, "Popen", fake_popen), mock.patch.object(app_stages, "fetch_health", fake_health), mock.patch.object(app_stages.time, "sleep", lambda s: None), mock.patch.object(app_stages.handover, "app_available", lambda p: (True, "0.1.0")):
            stage = app_stages.StartStage(watchdog=False)
            result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(launched[0][1:3], ["-m", "vaishnavi"])
        self.assertIn("run", launched[0])
        self.assertIn("Ready to open the shop", result.message)
        self.assertIn("http://127.0.0.1:8080", result.message)

    def test_watchdog_restarts_a_hung_or_dead_app_and_gives_up_after_too_many_restarts(self) -> None:
        from bootstrap import app_stages

        now = [0.0]
        procs: list[mock.Mock] = []

        def fake_popen(cmd, **kw):  # type: ignore[no-untyped-def]
            proc = mock.Mock()
            proc.pid = 100 + len(procs)
            proc.poll.return_value = None
            procs.append(proc)
            return proc

        responses = iter([{"ok": True}, {"ok": True}, None, None, None, None, None, {"ok": True}] + [None] * 50)

        def fake_health(url: str, timeout: float = 2.0) -> dict | None:
            return next(responses, None)

        log_lines: list[str] = []
        wd = app_stages.Watchdog(launch=lambda: fake_popen([]), health=fake_health, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s), stale_after=60, max_restarts_per_hour=2, log_hook=log_lines.append)
        wd.run(max_iterations=40)
        self.assertGreaterEqual(len(procs), 3, "restarted at least twice")
        self.assertTrue(any("E-SYS-020" in line for line in log_lines))
        self.assertTrue(any("WATCHDOG_GAVE_UP" in line for line in log_lines), "after max restarts per hour it stops relaunching and reports")
        for proc in procs[:-1]:
            proc.kill.assert_called()


if __name__ == "__main__":
    unittest.main()
