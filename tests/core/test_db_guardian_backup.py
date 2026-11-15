"""db/guardian.py + services/backup.py against docs/14 (start-up triage, snapshots, queue, retention, drill, restore).

Written before the code (docs/13 rule 7). Everything runs on real SQLite files in a temp folder with the
real schema and seed; the "secondary drive" is a sibling folder whose device identity is faked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from vaishnavi.core.clock import UTC, FrozenClock, iso_utc, set_clock
from vaishnavi.core.errors import BackupError, DatabaseError
from vaishnavi.core.events import BackupDone, EventBus, RestoreDone
from vaishnavi.core.settings import BackupSettings
from vaishnavi.db import seed
from vaishnavi.db.guardian import DbGuardian, Fingerprint, TriageKind
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.db.schema import apply_schema, stamp_head
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services import backup as bk
from vaishnavi.services.backup import BackupService
from vaishnavi.services.ledger import LedgerService

T0 = datetime(2026, 11, 14, 16, 0, tzinfo=UTC)  # 21:30 IST


def _close(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.close()


def make_live_db(path: Path, *, sales: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    apply_schema(conn)
    stamp_head(conn)
    seed.apply_seed(conn, now=iso_utc(T0))
    conn.execute("INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (2, 'ramesh', 'Ramesh', 2, 'x')")
    conn.execute("INSERT INTO product(id, sku, name, price_paise) VALUES (10, 'CAM-0001', 'Camphor 50g', 1000)")
    conn.execute("INSERT INTO shift(id, user_id, opened_at) VALUES (1, 2, ?)", (iso_utc(T0),))
    conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id, created_at) VALUES (10, 'OPENING', 100, 100, 1, ?)", (iso_utc(T0),))
    from tests.core.test_services_ledger import insert_sale, make_sale

    prev = "0" * 64
    for seq in range(1, sales + 1):
        s = make_sale(seq, prev_hash=prev, sold_at=iso_utc(T0))
        insert_sale(conn, s)
        prev = s.hash
    _close(conn)


def _damage(path: Path) -> None:
    """Flip 100 bytes in the middle of the file (guaranteed to change content, unlike zeroing free space)."""
    data = bytearray(path.read_bytes())
    off = len(data) // 2
    data[off : off + 100] = bytes(b ^ 0xFF for b in data[off : off + 100])
    path.write_bytes(bytes(data))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "primary", root / "secondary")
        self.paths.ensure()
        self.clock = FrozenClock(T0)
        self._prev_clock = set_clock(self.clock)
        self.bus = EventBus()
        self.events: list[object] = []
        self.bus.subscribe(BackupDone, self.events.append)
        self.bus.subscribe(RestoreDone, self.events.append)
        self.ledger = LedgerService(SqliteLedgerStore())
        # Primary and secondary are on different "devices" unless a test says otherwise.
        self._dev = mock.patch.object(bk, "device_id", side_effect=lambda p: "dev-B" if str(p).startswith(str(self.paths.secondary)) else "dev-A")
        self._dev.start()

    def tearDown(self) -> None:
        self._dev.stop()
        set_clock(self._prev_clock)
        logging.shutdown()
        self._tmp.cleanup()

    def service(self, **overrides) -> BackupService:  # type: ignore[no-untyped-def]
        settings = BackupSettings(**overrides)
        return BackupService(self.paths, settings, self.ledger, event_bus=self.bus, same_device_accepted=False)


# ============================================================================ guardian
class GuardianTest(Base):
    def guardian(self) -> DbGuardian:
        return DbGuardian(self.paths, self.ledger)

    def test_missing_database_is_reported_not_created(self) -> None:
        triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.MISSING)
        self.assertEqual(triage.code, "E-DB-002")
        self.assertFalse(self.paths.database.exists())

    def test_fresh_database_without_identity_is_new(self) -> None:
        conn = sqlite3.connect(self.paths.database)
        apply_schema(conn)
        _close(conn)
        self.assertIs(self.guardian().triage().kind, TriageKind.NEW)

    def test_damaged_file_is_quarantined_with_wal_and_shm_and_reported_e_db_003(self) -> None:
        self.paths.database.write_bytes(b"this is not a database" * 100)
        (self.paths.data / "vaishnavi.db-wal").write_bytes(b"x")
        (self.paths.data / "vaishnavi.db-shm").write_bytes(b"x")
        with self.assertLogs("vaishnavi.db.guardian", level="ERROR"):
            triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.CORRUPT)
        self.assertEqual(triage.code, "E-DB-003")
        self.assertFalse(self.paths.database.exists())
        self.assertEqual(list(self.paths.data.glob("vaishnavi.db*")), [], "nothing stale left next to where the new/restored file will go")
        quarantined = list(self.paths.quarantine.glob("vaishnavi.db.corrupt-*"))
        self.assertGreaterEqual(len(quarantined), 1, "the damaged file is kept for forensics (SQLite itself discards the -wal/-shm of a non-database)")

    def test_normal_start_after_clean_close(self) -> None:
        make_live_db(self.paths.database)
        g = self.guardian()
        g.mark_open()
        g.mark_clean_close()
        self.clock.advance(hours=10)
        triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.NORMAL)
        self.assertTrue(self.paths.fingerprint.exists())
        fp = Fingerprint.load(self.paths.fingerprint)
        assert fp is not None
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(fp.chain_head, self.ledger.chain_head(conn))
        conn.close()

    def test_unclean_close_is_detected_and_counted(self) -> None:
        make_live_db(self.paths.database)
        g = self.guardian()
        g.mark_open()  # ... and the PC was switched off
        self.clock.advance(hours=10)
        with self.assertLogs("vaishnavi.db.guardian", level="WARNING") as logs:
            triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.UNCLEAN)
        self.assertEqual(triage.code, "E-DB-022")
        self.assertTrue(any("DB_UNCLEAN_CLOSE" in line for line in logs.output))
        self.assertEqual(self.guardian().unclean_close_count(), 1)

    def test_restored_copy_is_detected_by_backup_id_marked_and_banner_has_backup_time(self) -> None:
        make_live_db(self.paths.database)
        g = self.guardian()
        g.mark_open()
        g.mark_clean_close()
        snap = self.service().snapshot("manual")
        # Owner copies the snapshot over the live file by hand (docs/14 §5.2).
        self.paths.database.unlink()
        Path(snap.path).replace(self.paths.database)
        with self.assertLogs("vaishnavi.db.guardian", level="WARNING") as logs:
            triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.RESTORED)
        self.assertEqual(triage.code, "E-DB-020")
        self.assertEqual(triage.params["taken_at"], iso_utc(T0))
        self.assertTrue(any("RESTORE_DETECTED" in line for line in logs.output))
        conn = sqlite3.connect(self.paths.database)
        restored_at, restored_from = conn.execute("SELECT restored_at, restored_from FROM db_identity").fetchone()
        conn.close()
        self.assertEqual(restored_at, iso_utc(T0))
        self.assertEqual(restored_from, snap.backup_id)
        # Second start on the same restored file is normal again, not another banner.
        self.guardian().mark_clean_close()
        self.assertIs(self.guardian().triage().kind, TriageKind.NORMAL)

    def test_foreign_database_is_refused_when_instance_differs_from_fingerprint(self) -> None:
        make_live_db(self.paths.database)
        g = self.guardian()
        g.mark_open()
        g.mark_clean_close()
        other = self.paths.tmp / "other.db"
        make_live_db(other)
        self.paths.database.unlink()
        other.replace(self.paths.database)
        with self.assertLogs("vaishnavi.db.guardian", level="ERROR"):
            triage = self.guardian().triage()
        self.assertIs(triage.kind, TriageKind.FOREIGN)
        self.assertEqual(triage.code, "E-DB-021")

    def test_clean_close_checkpoints_wal(self) -> None:
        make_live_db(self.paths.database)
        conn = sqlite3.connect(self.paths.database)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO category(name) VALUES ('x')")
        _close(conn)
        self.guardian().mark_clean_close()
        wal = self.paths.data / "vaishnavi.db-wal"
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0, "WAL checkpointed and truncated at clean close")


# ============================================================================ backup
class SnapshotTest(Base):
    def setUp(self) -> None:
        super().setUp()
        make_live_db(self.paths.database)

    def test_snapshot_uses_backup_api_is_verified_and_has_manifest_and_log_row(self) -> None:
        svc = self.service()
        snap = svc.snapshot("shift_close")
        path = Path(snap.path)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.paths.snapshots)
        self.assertRegex(path.name, r"^vaishnavi_\d{8}_\d{6}_shift_close\.db$")
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        for key in ("backup_id", "taken_at", "trigger", "instance_id", "schema_version", "app_version", "last_invoice", "chain_head", "sha256", "size_bytes", "row_counts"):
            self.assertIn(key, manifest, key)
        self.assertEqual(manifest["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(manifest["last_invoice"], "VPS/26-27/000002")
        live = sqlite3.connect(self.paths.database)
        self.assertEqual(manifest["chain_head"], self.ledger.chain_head(live))
        self.assertTrue(snap.verified)
        rows = live.execute("SELECT path, verified, trigger, backup_id FROM backup_log").fetchall()
        live.close()
        self.assertEqual(rows, [(str(path), 1, "shift_close", snap.backup_id)])
        self.assertIsInstance(self.events[-1], BackupDone)

    def test_snapshot_carries_backup_id_only_in_the_copy_not_in_the_live_db(self) -> None:
        snap = self.service().snapshot("manual")
        copy = sqlite3.connect(Path(snap.path))
        self.assertEqual(copy.execute("SELECT backup_id FROM db_identity").fetchone()[0], snap.backup_id)
        copy.close()
        live = sqlite3.connect(self.paths.database)
        self.assertIsNone(live.execute("SELECT backup_id FROM db_identity").fetchone()[0])
        live.close()

    def test_snapshot_is_consistent_while_a_writer_holds_the_live_db(self) -> None:
        writer = sqlite3.connect(self.paths.database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO category(name) VALUES ('uncommitted')")
        snap = self.service().snapshot("interval")
        writer.rollback()
        writer.close()
        copy = sqlite3.connect(Path(snap.path))
        self.assertIsNone(copy.execute("SELECT 1 FROM category WHERE name = 'uncommitted'").fetchone(), "uncommitted work never reaches a snapshot")
        copy.close()

    def test_local_short_retention_copy_is_kept_in_primary(self) -> None:
        self.service().snapshot("nightly")
        local = list(self.paths.local_backups.glob("*.db"))
        self.assertEqual(len(local), 1)

    def test_secondary_unreachable_queues_locally_with_e_bkp_001_and_flushes_when_back(self) -> None:
        svc = self.service()
        with mock.patch.object(bk, "device_id", side_effect=OSError("unplugged")), self.assertLogs("vaishnavi.services.backup", level="WARNING") as logs:
            snap = svc.snapshot("nightly")
        self.assertEqual(Path(snap.path).parent, self.paths.backup_queue)
        self.assertTrue(snap.queued)
        self.assertTrue(any("E-BKP-001" in line for line in logs.output))
        self.assertEqual(svc.queued_count(), 1)
        moved = svc.flush_queue()
        self.assertEqual(moved, 1)
        self.assertEqual(svc.queued_count(), 0)
        self.assertEqual(len(list(self.paths.snapshots.glob("*.db"))), 1)
        self.assertEqual(len(list(self.paths.snapshots.glob("*.manifest.json"))), 1)

    def test_secondary_on_same_device_as_primary_is_treated_as_unplugged_unless_owner_accepted(self) -> None:
        # The unmounted-mount-point trap: /media/usb exists as a folder but the drive is not there.
        with mock.patch.object(bk, "device_id", return_value="dev-A"):
            snap = self.service().snapshot("nightly")
            self.assertTrue(snap.queued, "same device while the owner chose a separate drive → the drive is not mounted")
            accepted = BackupService(self.paths, BackupSettings(), self.ledger, event_bus=self.bus, same_device_accepted=True)
            self.assertFalse(accepted.snapshot("nightly").queued)

    def test_failed_verification_discards_the_snapshot_with_e_bkp_003(self) -> None:
        svc = self.service()
        with mock.patch.object(bk, "integrity_check", return_value="*** in database main *** page 3: btreeInitPage() returns error code 11"), self.assertLogs("vaishnavi.services.backup", level="ERROR"):
            with self.assertRaises(BackupError) as ctx:
                svc.snapshot("nightly")
        self.assertEqual(ctx.exception.code, "E-BKP-003")
        self.assertEqual(list(self.paths.snapshots.glob("*.db")), [])
        self.assertEqual(list(self.paths.tmp.glob("*.db")), [], "no half-made file left behind")
        live = sqlite3.connect(self.paths.database)
        self.assertEqual(live.execute("SELECT verified FROM backup_log").fetchall(), [(0,)], "the failed attempt is recorded as unverified")
        live.close()

    def test_unwritable_destination_is_e_bkp_002(self) -> None:
        svc = self.service()
        with mock.patch.object(bk.shutil, "move", side_effect=OSError("disk full")):
            with self.assertRaises(BackupError) as ctx:
                svc.snapshot("manual")
        self.assertEqual(ctx.exception.code, "E-BKP-002")

    def test_due_on_start_when_last_backup_older_than_20_hours(self) -> None:
        svc = self.service()
        self.assertTrue(svc.due_on_start(), "no backup yet → due")
        svc.snapshot("start")
        self.assertFalse(svc.due_on_start())
        self.clock.advance(hours=21)
        self.assertTrue(svc.due_on_start())
        self.assertAlmostEqual(svc.last_backup_age_hours() or 0, 21, delta=0.01)

    def test_list_snapshots_reads_manifests_newest_first_including_queue(self) -> None:
        svc = self.service()
        first = svc.snapshot("manual")
        self.clock.advance(minutes=5)
        with mock.patch.object(bk, "device_id", side_effect=OSError("unplugged")):
            second = svc.snapshot("manual")
        listed = svc.list_snapshots()
        self.assertEqual([s.backup_id for s in listed], [second.backup_id, first.backup_id])
        self.assertTrue(listed[0].queued)
        self.assertEqual(listed[1].last_invoice, "VPS/26-27/000002")


class RetentionTest(Base):
    def test_pruning_keeps_daily_weekly_monthly_and_pre_snapshots(self) -> None:
        make_live_db(self.paths.database)
        svc = self.service(keep_daily=3, keep_weekly=2, keep_monthly=1)
        start = T0 - timedelta(days=120)
        self.clock.set(start)
        for day in range(0, 121):
            self.clock.set(start + timedelta(days=day))
            svc.snapshot("nightly")
            if day in (10, 60):
                svc.snapshot("pre-migrate")
        self.clock.set(T0)
        removed = svc.prune()
        self.assertGreater(removed, 100)
        kept = {Path(s.path).name: s for s in svc.list_snapshots()}
        ages = {name: (T0 - datetime.fromisoformat(s.taken_at.replace("Z", "+00:00"))).days for name, s in kept.items()}
        nightly = {n: a for n, a in ages.items() if n.endswith("_nightly.db")}
        self.assertEqual(sorted(a for a in nightly.values() if a <= 3), [0, 1, 2, 3], "every snapshot of the last keep_daily days")
        weekly = [a for a in nightly.values() if 3 < a <= 3 + 7 * 2]
        self.assertTrue(weekly, "one per week in the weekly window")
        self.assertEqual(len(weekly), len({(T0 - timedelta(days=a)).isocalendar()[:2] for a in weekly}), "at most one per ISO week")
        monthly = [a for a in nightly.values() if 3 + 14 < a <= 3 + 14 + 31]
        self.assertTrue(monthly)
        self.assertEqual(len(monthly), len({(T0 - timedelta(days=a)).month for a in monthly}), "at most one per month")
        self.assertEqual([a for a in nightly.values() if a > 3 + 14 + 31], [], "nothing older than the retention windows")
        pre = [a for n, a in ages.items() if "pre-migrate" in n]
        self.assertEqual(pre, [60], "pre-* snapshots are kept for 90 days: day 60 kept, day 110 pruned")
        self.assertEqual(len(list(self.paths.snapshots.glob("*.manifest.json"))), len(kept), "manifests pruned with their files")
        self.assertLessEqual(len(list(self.paths.local_backups.glob("*.db"))), 4, "local copies: 3 days")


class RestoreTest(Base):
    def setUp(self) -> None:
        super().setUp()
        make_live_db(self.paths.database, sales=2)
        self.svc = self.service()
        self.snap = self.svc.snapshot("shift_close")
        # More sales after the backup
        from tests.core.test_services_ledger import insert_sale, make_sale

        conn = sqlite3.connect(self.paths.database)
        head = self.ledger.chain_head(conn)
        insert_sale(conn, make_sale(3, prev_hash=head, sold_at=iso_utc(T0)))
        _close(conn)

    def test_restore_drill_restores_latest_into_temp_verifies_and_cleans_up(self) -> None:
        with self.assertLogs("vaishnavi.services.backup", level="INFO") as logs:
            ok = self.svc.restore_drill()
        self.assertTrue(ok)
        self.assertTrue(any("RESTORE_DRILL_OK" in line for line in logs.output))
        self.assertEqual(list(self.paths.tmp.glob("drill*")), [])
        live = sqlite3.connect(self.paths.database)
        self.assertIn(("DRILL", 1), live.execute("SELECT trigger, verified FROM backup_log").fetchall())
        live.close()

    def test_restore_drill_failure_is_e_bkp_010(self) -> None:
        _damage(Path(self.snap.path))
        with self.assertLogs("vaishnavi.services.backup", level="ERROR") as logs:
            ok = self.svc.restore_drill()
        self.assertFalse(ok)
        self.assertTrue(any("E-BKP-010" in line for line in logs.output))

    def test_restore_takes_pre_restore_snapshot_replaces_live_file_and_verifies(self) -> None:
        migrated: list[Path] = []
        with self.assertLogs("vaishnavi.services.backup", level="INFO"):
            result = self.svc.restore(Path(self.snap.path), migrate=migrated.append)
        self.assertTrue(result.ok)
        self.assertEqual(len(migrated), 1, "migrations are applied to the restored file")
        pre = [p for p in self.paths.snapshots.glob("*_pre-restore.db")]
        self.assertEqual(len(pre), 1)
        live = sqlite3.connect(self.paths.database)
        self.assertEqual(live.execute("SELECT COUNT(*) FROM sale").fetchone()[0], 2, "the 3rd sale (after the backup) is gone — that is the point of a restore")
        self.assertEqual(live.execute("SELECT restored_from FROM db_identity").fetchone()[0], self.snap.backup_id)
        live.close()
        self.assertFalse((self.paths.data / "vaishnavi.db-wal").exists())
        self.assertIsInstance(self.events[-1], RestoreDone)

    def test_restore_refuses_a_foreign_snapshot(self) -> None:
        foreign = self.paths.tmp / "foreign.db"
        make_live_db(foreign)
        with self.assertRaises(DatabaseError) as ctx:
            self.svc.restore(foreign)
        self.assertEqual(ctx.exception.code, "E-DB-021")
        live = sqlite3.connect(self.paths.database)
        self.assertEqual(live.execute("SELECT COUNT(*) FROM sale").fetchone()[0], 3, "live file untouched")
        live.close()

    def test_restore_refuses_a_damaged_snapshot_and_leaves_live_untouched(self) -> None:
        path = Path(self.snap.path)
        _damage(path)
        with self.assertRaises(DatabaseError) as ctx:
            self.svc.restore(path)
        self.assertEqual(ctx.exception.code, "E-DB-003")
        live = sqlite3.connect(self.paths.database)
        self.assertEqual(live.execute("SELECT COUNT(*) FROM sale").fetchone()[0], 3)
        live.close()

    def test_verify_snapshot_checks_sha256_against_manifest(self) -> None:
        self.assertTrue(self.svc.verify_snapshot(Path(self.snap.path)).ok)
        path = Path(self.snap.path)
        manifest = path.with_suffix(".manifest.json")
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        doc["sha256"] = "0" * 64
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        result = self.svc.verify_snapshot(path)
        self.assertFalse(result.ok)
        self.assertIn("sha256", result.detail)


if __name__ == "__main__":
    unittest.main()
