"""System pages (docs/08 §8–9, docs/09 §10, docs/14 §5.1, docs/13 unit 29): logs viewer, status page,
Settings → Backups (with the Restore wizard) and Settings → System. View-models only; NiceGUI untouched.

Written first (docs/13 rule 7).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from vaishnavi import __version__
from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, iso_utc, set_clock
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.security import hash_secret
from vaishnavi.core.settings import Settings
from vaishnavi.db.schema import HEAD_REVISION
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteTxn, SqliteUserStore
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.user import UserService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import logs as logs_page
from vaishnavi.ui.pages import settings_backups, settings_system
from vaishnavi.ui.pages import status as status_page

from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

OWNER = Actor(user_id=1, role=Role.OWNER)
EN = UiTexts.load("en")

LOG_LINES = (
    "2026-11-14 10:00:00.000 IST | INFO  | vaishnavi.services.billing | cid=01AAA user=ramesh(STAFF) shift=3 | CHECKOUT_OK invoice=VPS/26-27/000012 total_paise=4000\n"
    "2026-11-14 10:00:01.000 IST | ERROR | vaishnavi.hardware.printer | cid=01AAA user=ramesh(STAFF) shift=3 | PRINT_FAILED code=E-PRN-001 detail=offline\n"
    "2026-11-14 10:00:02.000 IST | DEBUG | vaishnavi.scheduler | cid=01BBB user=system(SYSTEM) shift=None | JOB_RUN name=heartbeat\n"
    "2026-11-14 10:00:03.000 IST | WARN  | vaishnavi.services.backup | cid=01CCC user=system(SYSTEM) shift=None | BACKUP_QUEUED code=E-BKP-001 api_key=***\n"
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        conn = sqlite3.connect(self.paths.database)
        conn.execute("UPDATE user SET password_hash = ?, owner_pin_hash = ? WHERE id = 1", (hash_secret("owner-pass-1"), hash_secret("7391")))
        conn.commit()
        conn.close()
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        settings = Settings()
        settings.locations.same_device = True
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "settings.toml").write_text('[shop]\nname = "Vaishnavi Puja Store"\n[ai]\nprovider = "openai"\n[ai.openai]\napi_key = "sk-should-never-show-1234567890"\n', encoding="utf-8")
        self.services = AppServices.build(settings, self.paths, root / "config", box=ReversibleBox())
        self.state = AppState(settings=settings, paths=self.paths, config_dir=root / "config", started_at=T0)
        self.state.extra["services"] = self.services
        from vaishnavi.bootstrap import startup

        self.state.doctor_runner = startup.make_doctor_runner(settings, self.paths, root / "config")
        self.state.chips.update({"database": ("green", "DB ok"), "backup": ("amber", "Backup: never")})
        self.state.beat("request")
        self.state.beat("scheduler")

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()


# ============================================================================ logs viewer
class LogsPageTest(Base):
    def setUp(self) -> None:
        super().setUp()
        (self.paths.logs / "app").mkdir(parents=True, exist_ok=True)
        (self.paths.logs / "app" / "vaishnavi.log").write_text(LOG_LINES, encoding="utf-8")
        (self.paths.logs / "errors").mkdir(parents=True, exist_ok=True)
        (self.paths.logs / "errors" / "errors.log").write_text(LOG_LINES.splitlines()[1] + "\n", encoding="utf-8")

    def test_lines_are_parsed_filtered_and_explained_in_plain_language(self) -> None:
        model = logs_page.build_logs_model(self.state, EN, family="app", level="INFO", limit=100)
        self.assertEqual(model.families, ["app", "errors"])
        self.assertEqual([line.level for line in model.lines], ["INFO", "ERROR", "WARN"], "DEBUG filtered out")
        error = model.lines[1]
        self.assertEqual(error.logger, "vaishnavi.hardware.printer")
        self.assertEqual(error.cid, "01AAA")
        self.assertEqual(error.user, "ramesh")
        self.assertEqual(error.code, "E-PRN-001")
        self.assertIn("printer", error.plain.lower(), "E- codes get their owner-facing text")
        self.assertEqual(model.lines[0].code, "")
        hi = logs_page.build_logs_model(self.state, UiTexts.load("hi"), family="app", level="ERROR", limit=100)
        self.assertNotEqual(hi.lines[0].plain, error.plain)

    def test_free_text_and_correlation_filters_and_download_text(self) -> None:
        by_cid = logs_page.build_logs_model(self.state, EN, family="app", level="DEBUG", cid="01AAA", limit=100)
        self.assertEqual(len(by_cid.lines), 2, "the whole trail of one correlation id")
        by_text = logs_page.build_logs_model(self.state, EN, family="app", level="DEBUG", q="backup_queued", limit=100)
        self.assertEqual(len(by_text.lines), 1)
        self.assertIn("api_key=***", by_text.lines[0].message, "redaction done at write time is preserved — never undone")
        text = logs_page.download_text(by_cid)
        self.assertTrue(text.startswith("2026-11-14 10:00:00.000 IST"))
        self.assertEqual(len(text.strip().splitlines()), 2)
        self.assertEqual(logs_page.build_logs_model(self.state, EN, family="../etc", level="DEBUG", limit=5).lines, [])


# ============================================================================ status page
class StatusPageTest(Base):
    def test_status_model_aggregates_health_chips_jobs_backup_and_integrity(self) -> None:
        self.state.scheduler_status = lambda: [{"name": "heartbeat", "every_s": 15, "daily_at": None, "next_run": None, "runs": 10, "failures": 0, "last_ms": 2, "last_error": None, "enabled": True}]
        self.services.backup.snapshot("manual")
        model = status_page.build_status_model(self.state, EN)
        self.assertEqual(model.version, __version__)
        self.assertTrue(model.healthy)
        self.assertEqual(model.uptime_text, "0 m")
        self.assertEqual([c.key for c in model.chips][:2], ["database", "ledger"])
        self.assertTrue(model.database.size_text.endswith("KB") or model.database.size_text.endswith("MB"))
        self.assertEqual(model.jobs[0].name, "heartbeat")
        self.assertEqual(model.jobs[0].schedule_text, "every 15 s")
        self.assertIsNotNone(model.last_backup)
        self.assertTrue(model.last_backup.verified)  # type: ignore[union-attr]
        self.assertEqual(model.last_backup.age_text, "just now")  # type: ignore[union-attr]
        self.assertTrue(model.integrity.ok)
        self.assertEqual(model.integrity.head[:4], model.last_backup.chain_head[:4])  # type: ignore[union-attr]
        self.assertEqual(model.logged_in, [])
        self.clock.advance(minutes=90)
        stale = status_page.build_status_model(self.state, EN)
        self.assertFalse(stale.healthy, "heartbeats older than 60 s")
        self.assertEqual(stale.uptime_text, "1 h 30 m")


# ============================================================================ settings → backups + restore wizard
class BackupsPageTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.users = UserService(SqliteTxn(self.paths.database), SqliteUserStore(), AuditService(SqliteAuditStore()))

    def test_backups_model_lists_snapshots_locations_and_drill_status(self) -> None:
        snap = self.services.backup.snapshot("manual")
        self.clock.advance(hours=1)
        model = settings_backups.build_backups_model(self.services, EN)
        self.assertEqual(model.primary, str(self.paths.primary))
        self.assertEqual(model.secondary, str(self.paths.secondary))
        self.assertTrue(model.secondary_ready)
        self.assertEqual(len(model.snapshots), 1)
        row = model.snapshots[0]
        self.assertEqual(row.backup_id, snap.backup_id)
        self.assertTrue(row.verified)
        self.assertEqual(row.last_invoice, "VPS/26-27/000002")
        self.assertEqual(row.taken_text, "14-Nov-2026 21:30")
        self.assertFalse(row.queued)
        self.assertEqual(model.queue_size, 0)
        self.assertEqual(model.drill.status, "never")
        self.assertGreaterEqual(len(model.recovery_steps), 3, "the plain-language manual recovery card")

    def test_restore_wizard_requires_the_owner_password_and_restores_with_a_pre_restore_snapshot(self) -> None:
        snap = self.services.backup.snapshot("manual")
        conn = sqlite3.connect(self.paths.database)
        conn.execute("INSERT INTO product(id, sku, name, price_paise) VALUES (11, 'NEW-0001', 'Added after backup', 500)")
        conn.commit()
        conn.close()
        wizard = settings_backups.RestoreWizard(self.services, self.users, actor=OWNER)
        self.assertEqual(wizard.step, settings_backups.Step.CHOOSE)
        wizard.choose(snap.backup_id)
        self.assertEqual(wizard.step, settings_backups.Step.CONFIRM)
        self.assertIn("VPS/26-27/000002", wizard.summary.last_invoice)  # type: ignore[union-attr]
        refused = wizard.confirm(owner_password="wrong")
        self.assertFalse(refused.ok)
        self.assertEqual(refused.error_code, "E-AUTH-001")
        self.assertEqual(wizard.step, settings_backups.Step.CONFIRM, "stays on the confirmation step")
        result = wizard.confirm(owner_password="owner-pass-1")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(wizard.step, settings_backups.Step.DONE)
        self.assertTrue(result.restart_required)
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM product WHERE id = 11").fetchone()[0], 0, "the live file is the snapshot again")
        self.assertEqual(conn.execute("SELECT backup_id FROM db_identity").fetchone()[0], snap.backup_id, "a restored copy announces itself (docs/14 §6)")
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
        conn.close()
        self.assertIn("RESTORE", actions)
        snaps = self.services.backup.list_snapshots()
        self.assertTrue(any(s.trigger == "pre-restore" for s in snaps), "whatever was live is kept as a pre-restore snapshot")

    def test_staff_cannot_open_the_wizard(self) -> None:
        with self.assertRaises(Exception):
            settings_backups.RestoreWizard(self.services, self.users, actor=Actor(user_id=2, role=Role.STAFF))


# ============================================================================ settings → system
class SystemPageTest(Base):
    def test_system_model_redacts_config_and_reports_integrity_autostart_and_remote_access(self) -> None:
        model = settings_system.build_system_model(self.state, EN)
        self.assertEqual(model.version, __version__)
        self.assertNotIn("sk-should-never-show", model.config_text, "API keys never reach the screen")
        self.assertIn("api_key = \"***\"", model.config_text)
        self.assertIn('name = "Vaishnavi Puja Store"', model.config_text)
        self.assertIn(model.code_integrity.status, ("dev-copy", "ok", "changed"))
        self.assertIn(model.autostart.status, ("registered", "not-registered", "unknown"))
        self.assertFalse(model.remote_access.enabled)
        self.assertEqual(model.remote_access.urls, ["http://127.0.0.1:8080/system/logs"])
        self.state.settings.server.lan_access = True
        lan = settings_system.build_system_model(self.state, EN)
        self.assertTrue(lan.remote_access.enabled)
        self.assertTrue(any(u.endswith(":8080/system/logs") and "127.0.0.1" not in u for u in lan.remote_access.urls), "the LAN URL the owner types on the phone")
        self.assertEqual(model.database.revision, HEAD_REVISION)

    def test_support_bundle_contains_doctor_errors_and_redacted_config_only(self) -> None:
        (self.paths.logs / "errors").mkdir(parents=True, exist_ok=True)
        (self.paths.logs / "errors" / "errors.log").write_text(LOG_LINES, encoding="utf-8")
        (self.paths.logs / "audit").mkdir(parents=True, exist_ok=True)
        (self.paths.logs / "audit" / "audit.jsonl").write_text('{"event": "LOGIN"}\n', encoding="utf-8")
        bundle = settings_system.build_support_bundle(self.state)
        self.assertTrue(bundle.exists())
        self.assertTrue(bundle.parent == self.paths.exports)
        with zipfile.ZipFile(bundle) as zf:
            names = set(zf.namelist())
            self.assertIn("doctor.json", names)
            self.assertIn("logs/errors/errors.log", names)
            self.assertIn("config/settings.toml", names)
            self.assertIn("status.json", names)
            self.assertNotIn("logs/audit/audit.jsonl", names, "the audit trail of staff is not part of a support bundle")
            self.assertNotIn("sk-should-never-show", zf.read("config/settings.toml").decode("utf-8"))
            self.assertEqual(json.loads(zf.read("doctor.json"))["checks"][0]["key"], "python_env")

    def test_shutdown_request_is_owner_only_and_audited(self) -> None:
        with self.assertRaises(Exception):
            settings_system.request_shutdown(self.state, Actor(user_id=2, role=Role.STAFF), reason="test")
        settings_system.request_shutdown(self.state, OWNER, reason="closing for the day")
        self.assertTrue(self.state.extra.get("shutdown_requested"))
        conn = sqlite3.connect(self.paths.database)
        self.assertIn("APP_SHUTDOWN_REQUESTED", [r[0] for r in conn.execute("SELECT action FROM audit_log").fetchall()])
        conn.close()


if __name__ == "__main__":
    unittest.main()
