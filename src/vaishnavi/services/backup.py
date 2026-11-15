"""BackupService — verified snapshots, queueing, retention, restore drill and restore (stdlib sqlite3; docs/14 §4–7).

How a snapshot is made (never a file copy of the live DB):

1. ``live.backup(tmp)`` with the SQLite **online backup API** — consistent while billing continues;
2. stamp ``db_identity.backup_id`` / ``backup_taken_at`` **in the copy only** (that is how a restored file
   identifies itself later);
3. ``PRAGMA integrity_check`` on the copy → anything but ``ok`` discards it (``E-BKP-003``);
4. manifest ``.manifest.json`` (schema/app version, instance id, last invoice, chain head, sha256, size,
   row counts) so a snapshot can be verified without the app;
5. move to ``<secondary>/snapshots/`` when the drive is really there (same volume as the primary while the
   Owner chose a separate drive = the mount point exists but the disk is not mounted → treated as
   unplugged), otherwise to ``<primary>/backup-queue/`` (``E-BKP-001``, flushed later);
6. short-retention copy in ``<primary>/backups_local/``; ``backup_log`` row; ``BackupDone`` event.

Retention (``prune``): every snapshot of the last ``keep_daily`` days, one per ISO week for
``keep_weekly`` weeks, one per month for ``keep_monthly`` months, every ``pre-*`` snapshot for 90 days;
local copies 3 days. The weekly **restore drill** restores the latest snapshot into ``tmp/`` and runs the
full integrity pass; ``restore()`` is the in-app restore: pre-restore snapshot → copy in → integrity →
migrate (callable injected) → chain verification → atomic swap → ``RestoreDone``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from vaishnavi import __version__
from vaishnavi.core.clock import iso_utc, now_utc, parse_iso_utc
from vaishnavi.core.errors import BackupError, DatabaseError
from vaishnavi.core.events import BackupDone, EventBus, RestoreDone, bus
from vaishnavi.core.settings import BackupSettings
from vaishnavi.db.schema import SCHEMA_VERSION
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.services.backup")

MANIFEST_SUFFIX: Final = ".manifest.json"
START_DUE_HOURS: Final = 20.0
PRE_KEEP_DAYS: Final = 90
LOCAL_KEEP_DAYS: Final = 3
COUNTED_TABLES: Final = ("sale", "sale_item", "payment", "stock_movement", "product", "audit_log")


# ----------------------------------------------------------------------------- helpers (patchable)
def device_id(path: Path) -> str:
    """Identity of the volume holding ``path``; raises ``OSError`` when the path is not reachable."""
    probe = path if path.exists() else path.parent
    st = os.stat(probe)
    if sys.platform.startswith("win"):
        return str(Path(probe).resolve().anchor).upper()  # drive letter; UNC share root
    return str(st.st_dev)


def integrity_check(conn: sqlite3.Connection) -> str:
    """``PRAGMA integrity_check`` text (``"ok"`` when sound)."""
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return f"error: {exc}"


def sha256_file(path: Path) -> str:
    """Hex digest of a file in 1 MiB chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- records
@dataclass(frozen=True)
class Snapshot:
    """A snapshot on disk and what its manifest says."""

    backup_id: str
    path: str
    taken_at: str
    trigger: str
    size_bytes: int
    verified: bool
    queued: bool
    last_invoice: str | None
    chain_head: str
    instance_id: str
    schema_version: str
    app_version: str

    @property
    def manifest_path(self) -> Path:
        """Sidecar manifest."""
        return Path(self.path).with_suffix(MANIFEST_SUFFIX)


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :meth:`BackupService.verify_snapshot`."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of :meth:`BackupService.restore`."""

    ok: bool
    backup_id: str
    pre_restore_backup_id: str
    detail: str = ""


# ----------------------------------------------------------------------------- service
class BackupService:
    """Snapshots of ``<primary>/data/vaishnavi.db``."""

    def __init__(self, paths: AppPaths, settings: BackupSettings, ledger: LedgerService, *, event_bus: EventBus = bus, same_device_accepted: bool = False) -> None:
        self.paths = paths
        self.settings = settings
        self.ledger = ledger
        self._bus = event_bus
        self.same_device_accepted = same_device_accepted
        """True when the Owner knowingly chose a backup folder on the same disk (``locations.same_device``)."""

    # -- secondary reachability -------------------------------------------------------------
    def secondary_ready(self) -> tuple[bool, str]:
        """(reachable, reason). Same volume as the primary counts as *not* mounted unless accepted."""
        try:
            self.paths.snapshots.mkdir(parents=True, exist_ok=True)
            sec, prim = device_id(self.paths.snapshots), device_id(self.paths.database)
        except OSError as exc:
            return False, f"not reachable: {exc}"
        if sec == prim and not self.same_device_accepted:
            return False, "backup folder is on the same disk as the data — the backup drive is not mounted"
        probe = self.paths.snapshots / f".write-test-{uuid.uuid4().hex[:8]}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return False, f"not writable: {exc}"
        return True, ""

    # -- snapshot ---------------------------------------------------------------------------
    def snapshot(self, trigger: str) -> Snapshot:
        """Make, verify and place one snapshot. Raises ``BackupError`` (E-BKP-002/003)."""
        at = now_utc()
        backup_id = uuid.uuid4().hex[:16]
        stamp = at.strftime("%Y%m%d_%H%M%S")
        name = f"vaishnavi_{stamp}_{trigger}.db"
        self.paths.tmp.mkdir(parents=True, exist_ok=True)
        tmp_db = self.paths.tmp / f"{name}.part"
        log.info("BACKUP_START trigger=%s backup_id=%s", trigger, backup_id)
        try:
            manifest = self._make_copy(tmp_db, backup_id, at, trigger)
        except BackupError:
            self._log_row(str(tmp_db), 0, False, trigger, backup_id)
            tmp_db.unlink(missing_ok=True)
            raise
        ready, reason = self.secondary_ready()
        if ready:
            dest_dir, queued = self.paths.snapshots, False
        else:
            log.warning("BACKUP_SECONDARY_UNAVAILABLE reason=%s code=E-BKP-001 — queuing locally", reason)
            dest_dir, queued = self.paths.backup_queue, True
        final = self._place(tmp_db, dest_dir / name, manifest)
        self._local_copy(final, manifest)
        snap = self._snapshot_from(manifest, final, queued)
        self._log_row(str(final), snap.size_bytes, True, trigger, backup_id)
        self._bus.publish(BackupDone(backup_id=backup_id, path=str(final), trigger=trigger, queued=queued, size_bytes=snap.size_bytes))
        log.info("BACKUP_DONE backup_id=%s path=%s size=%d queued=%s head=%s", backup_id, final, snap.size_bytes, queued, manifest["chain_head"][:12])
        return snap

    def _make_copy(self, tmp_db: Path, backup_id: str, at: datetime, trigger: str) -> dict[str, Any]:
        tmp_db.unlink(missing_ok=True)
        src = sqlite3.connect(self.paths.database)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            dst.execute("UPDATE db_identity SET backup_id = ?, backup_taken_at = ?", (backup_id, iso_utc(at)))
            dst.commit()
            result = integrity_check(dst)
            if result != "ok":
                log.error("BACKUP_VERIFY_FAILED result=%s code=E-BKP-003", result[:200])
                raise BackupError("E-BKP-003", detail=result[:500])
            instance = dst.execute("SELECT instance_id FROM db_identity LIMIT 1").fetchone()
            inv = dst.execute("SELECT invoice_no FROM sale ORDER BY id DESC LIMIT 1").fetchone()
            head = self.ledger.chain_head(dst)
            counts = {t: int(dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in COUNTED_TABLES}
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as exc:
            log.error("BACKUP_COPY_FAILED %s code=E-BKP-003", exc)
            raise BackupError("E-BKP-003", detail=str(exc), cause=exc) from exc
        finally:
            dst.close()
            src.close()
        for side in ("-wal", "-shm"):
            Path(str(tmp_db) + side).unlink(missing_ok=True)
        return {
            "backup_id": backup_id,
            "taken_at": iso_utc(at),
            "trigger": trigger,
            "instance_id": str(instance[0]) if instance else "",
            "schema_version": SCHEMA_VERSION,
            "app_version": __version__,
            "last_invoice": inv[0] if inv else None,
            "chain_head": head,
            "sha256": sha256_file(tmp_db),
            "size_bytes": tmp_db.stat().st_size,
            "row_counts": counts,
        }

    def _place(self, tmp_db: Path, dest: Path, manifest: dict[str, Any]) -> Path:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_db), str(dest))
            dest.with_suffix(MANIFEST_SUFFIX).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.error("BACKUP_WRITE_FAILED dest=%s error=%s code=E-BKP-002", dest, exc)
            tmp_db.unlink(missing_ok=True)
            raise BackupError("E-BKP-002", path=str(dest.parent), detail=str(exc), cause=exc) from exc
        return dest

    def _local_copy(self, final: Path, manifest: dict[str, Any]) -> None:
        try:
            self.paths.local_backups.mkdir(parents=True, exist_ok=True)
            if final.parent != self.paths.local_backups:
                shutil.copy2(final, self.paths.local_backups / final.name)
                (self.paths.local_backups / final.name).with_suffix(MANIFEST_SUFFIX).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            self._prune_local()
        except OSError as exc:
            log.warning("BACKUP_LOCAL_COPY_FAILED %s", exc)

    def _log_row(self, path: str, size: int, verified: bool, trigger: str, backup_id: str) -> None:
        try:
            conn = sqlite3.connect(self.paths.database)
            try:
                conn.execute("INSERT INTO backup_log(at, path, size_bytes, verified, trigger, backup_id) VALUES (?, ?, ?, ?, ?, ?)", (iso_utc(), path, size, 1 if verified else 0, trigger, backup_id))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            log.warning("backup_log row not written: %s", exc)

    @staticmethod
    def _snapshot_from(manifest: dict[str, Any], path: Path, queued: bool) -> Snapshot:
        return Snapshot(
            backup_id=str(manifest.get("backup_id", "")),
            path=str(path),
            taken_at=str(manifest.get("taken_at", "")),
            trigger=str(manifest.get("trigger", "")),
            size_bytes=int(manifest.get("size_bytes", path.stat().st_size if path.exists() else 0)),
            verified=True,
            queued=queued,
            last_invoice=manifest.get("last_invoice"),
            chain_head=str(manifest.get("chain_head", "")),
            instance_id=str(manifest.get("instance_id", "")),
            schema_version=str(manifest.get("schema_version", "")),
            app_version=str(manifest.get("app_version", "")),
        )

    # -- queue ------------------------------------------------------------------------------
    def queued_count(self) -> int:
        """Snapshots waiting for the secondary drive."""
        return len(list(self.paths.backup_queue.glob("*.db"))) if self.paths.backup_queue.exists() else 0

    def flush_queue(self) -> int:
        """Move queued snapshots (and manifests) to the secondary when it is back; returns how many."""
        ready, reason = self.secondary_ready()
        if not ready:
            log.debug("BACKUP_QUEUE_HELD reason=%s", reason)
            return 0
        moved = 0
        for db in sorted(self.paths.backup_queue.glob("*.db")):
            try:
                shutil.move(str(db), str(self.paths.snapshots / db.name))
                manifest = db.with_suffix(MANIFEST_SUFFIX)
                if manifest.exists():
                    shutil.move(str(manifest), str(self.paths.snapshots / manifest.name))
                moved += 1
            except OSError as exc:
                log.warning("BACKUP_QUEUE_MOVE_FAILED %s: %s", db.name, exc)
                break
        if moved:
            log.info("BACKUP_QUEUE_FLUSHED moved=%d", moved)
        return moved

    # -- listing / status -------------------------------------------------------------------
    def list_snapshots(self) -> list[Snapshot]:
        """All snapshots (secondary + queue), newest first, from their manifests."""
        out: list[Snapshot] = []
        for folder, queued in ((self.paths.snapshots, False), (self.paths.backup_queue, True)):
            if not folder.exists():
                continue
            for db in folder.glob("*.db"):
                manifest_path = db.with_suffix(MANIFEST_SUFFIX)
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
                except (OSError, ValueError) as exc:
                    log.warning("manifest unreadable %s: %s", manifest_path, exc)
                    manifest = {}
                if not manifest.get("taken_at"):
                    manifest["taken_at"] = iso_utc(datetime.fromtimestamp(db.stat().st_mtime, tz=now_utc().tzinfo))
                out.append(self._snapshot_from(manifest, db, queued))
        out.sort(key=lambda s: s.taken_at, reverse=True)
        return out

    def last_backup_age_hours(self) -> float | None:
        """Hours since the newest verified snapshot, or None when there is none."""
        snaps = self.list_snapshots()
        if not snaps:
            return None
        try:
            taken = parse_iso_utc(snaps[0].taken_at)
        except ValueError:
            return None
        return (now_utc() - taken).total_seconds() / 3600

    def due_on_start(self) -> bool:
        """docs/14 §4: snapshot at app start when the last one is older than 20 h (or none exists)."""
        age = self.last_backup_age_hours()
        return age is None or age > START_DUE_HOURS

    # -- retention --------------------------------------------------------------------------
    def prune(self) -> int:
        """Apply the retention policy to the secondary; returns the number of snapshots removed."""
        if not self.paths.snapshots.exists():
            return 0
        now = now_utc()
        keep: set[Path] = set()
        snaps = [s for s in self.list_snapshots() if not s.queued]
        weekly_seen: set[tuple[int, int]] = set()
        monthly_seen: set[tuple[int, int]] = set()
        for snap in snaps:  # newest first → the first of each week/month we meet is the newest in it
            try:
                taken = parse_iso_utc(snap.taken_at)
            except ValueError:
                keep.add(Path(snap.path))
                continue
            age_days = (now - taken).total_seconds() / 86400
            path = Path(snap.path)
            if snap.trigger.startswith("pre-"):
                if age_days <= PRE_KEEP_DAYS:
                    keep.add(path)
                continue
            if age_days <= self.settings.keep_daily:
                keep.add(path)
                continue
            iso = taken.isocalendar()
            week_key, month_key = (iso[0], iso[1]), (taken.year, taken.month)
            weekly_limit = self.settings.keep_daily + 7 * self.settings.keep_weekly
            monthly_limit = weekly_limit + 31 * self.settings.keep_monthly
            if age_days <= weekly_limit:
                # Weekly tier: the newest snapshot of each ISO week survives, the others of that week go.
                if week_key not in weekly_seen:
                    weekly_seen.add(week_key)
                    keep.add(path)
                continue
            if age_days <= monthly_limit and month_key not in monthly_seen:
                monthly_seen.add(month_key)
                keep.add(path)
        removed = 0
        for snap in snaps:
            path = Path(snap.path)
            if path in keep:
                continue
            try:
                path.unlink()
                path.with_suffix(MANIFEST_SUFFIX).unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                log.warning("BACKUP_PRUNE_FAILED %s: %s", path.name, exc)
        log.info("BACKUP_PRUNED removed=%d kept=%d", removed, len(keep))
        return removed

    def _prune_local(self) -> None:
        cutoff = now_utc() - timedelta(days=LOCAL_KEEP_DAYS)
        for db in self.paths.local_backups.glob("*.db"):
            manifest = db.with_suffix(MANIFEST_SUFFIX)
            try:
                taken = parse_iso_utc(json.loads(manifest.read_text(encoding="utf-8"))["taken_at"]) if manifest.exists() else None
            except (OSError, ValueError, KeyError):
                taken = None
            if taken is not None and taken < cutoff:
                db.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)

    # -- verification / drill ----------------------------------------------------------------
    def verify_snapshot(self, path: Path) -> VerifyResult:
        """integrity_check + sha256 vs manifest + full ledger pass on the snapshot (read-only)."""
        manifest_path = path.with_suffix(MANIFEST_SUFFIX)
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("sha256") and manifest["sha256"] != sha256_file(path):
                    return VerifyResult(False, "sha256 does not match the manifest — the file was changed after it was written")
            except (OSError, ValueError) as exc:
                return VerifyResult(False, f"manifest unreadable: {exc}")
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = integrity_check(conn)
            if result != "ok":
                return VerifyResult(False, f"integrity_check: {result[:300]}")
            status = self.ledger.run_all(conn, record=False)
            if not status.ok:
                return VerifyResult(False, "ledger: " + ", ".join(status.codes))
        finally:
            conn.close()
        return VerifyResult(True)

    def restore_drill(self) -> bool:
        """Weekly: verify the newest snapshot (manifest sha256, integrity, ledger) **and** prove it can be
        restored by copying it into ``tmp/`` with the backup API and checking the copy; ``E-BKP-010`` on failure."""
        snaps = [s for s in self.list_snapshots() if not s.queued] or self.list_snapshots()
        if not snaps:
            log.warning("RESTORE_DRILL_SKIPPED reason=no-snapshots")
            return False
        source = Path(snaps[0].path)
        drill = self.paths.tmp / f"drill-{uuid.uuid4().hex[:8]}.db"
        ok = False
        detail = ""
        try:
            result = self.verify_snapshot(source)
            if not result.ok:
                detail = result.detail
            else:
                src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
                dst = sqlite3.connect(drill)
                try:
                    src.backup(dst)
                    copy_result = integrity_check(dst)
                    ok = copy_result == "ok" and self.ledger.run_all(dst, record=False).reports["CHAIN"].ok
                    detail = "" if ok else f"restored copy: {copy_result[:200]}"
                finally:
                    dst.close()
                    src.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            detail = str(exc)
        finally:
            for extra in ("", "-wal", "-shm"):
                Path(str(drill) + extra).unlink(missing_ok=True)
        self._log_row(str(source), source.stat().st_size if source.exists() else 0, ok, "DRILL", snaps[0].backup_id)
        if ok:
            log.info("RESTORE_DRILL_OK snapshot=%s", source.name)
        else:
            log.error("RESTORE_DRILL_FAILED snapshot=%s detail=%s code=E-BKP-010", source.name, detail[:300])
        return ok

    # -- restore ----------------------------------------------------------------------------
    def restore(self, snapshot_path: Path, *, migrate: Callable[[Path], None] | None = None) -> RestoreResult:
        """In-app restore (docs/14 §5.1). The caller has stopped the application's engine.

        Raises ``DatabaseError`` E-DB-003 (damaged snapshot or manifest mismatch) / E-DB-021 (foreign
        instance); the live file is untouched until every check passed, then swapped atomically.
        """
        snapshot_path = Path(snapshot_path)
        log.info("RESTORE_START snapshot=%s", snapshot_path)
        verified = self.verify_snapshot(snapshot_path)
        if not verified.ok:
            log.error("RESTORE_REFUSED reason=%s code=E-DB-003", verified.detail[:200])
            raise DatabaseError("E-DB-003", detail=f"snapshot: {verified.detail[:300]}")
        check = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
        try:
            row = check.execute("SELECT instance_id, backup_id, backup_taken_at FROM db_identity LIMIT 1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DatabaseError("E-DB-003", detail=str(exc), cause=exc) from exc
        finally:
            check.close()
        snap_instance, backup_id, taken_at = (str(row[0]), str(row[1] or ""), str(row[2] or "")) if row else ("", "", "")
        live_instance = self._live_instance_id()
        if live_instance and snap_instance and live_instance != snap_instance:
            log.error("RESTORE_REFUSED reason=foreign snapshot_instance=%s live=%s code=E-DB-021", snap_instance, live_instance)
            raise DatabaseError("E-DB-021", detail=f"snapshot instance {snap_instance} ≠ {live_instance}")

        pre = self.snapshot("pre-restore") if self.paths.database.exists() else None
        staged = self.paths.tmp / f"restore-{uuid.uuid4().hex[:8]}.db"
        src = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(staged)
        try:
            src.backup(dst)
            dst.execute("UPDATE db_identity SET restored_at = ?, restored_from = ?", (iso_utc(), backup_id or snapshot_path.name))
            dst.commit()
        finally:
            dst.close()
            src.close()
        try:
            if migrate is not None:
                migrate(staged)
            verify = sqlite3.connect(staged)
            try:
                if integrity_check(verify) != "ok":
                    raise DatabaseError("E-DB-003", detail="restored copy failed integrity_check after migration")
                status = self.ledger.run_all(verify, record=False)
            finally:
                verify.close()
            if not status.reports["CHAIN"].ok:
                raise DatabaseError("E-DB-003", detail="restored copy: sales chain broken")
            for side in ("-wal", "-shm"):
                Path(str(self.paths.database) + side).unlink(missing_ok=True)
                Path(str(staged) + side).unlink(missing_ok=True)
            os.replace(staged, self.paths.database)
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        log.info("RESTORE_DONE backup_id=%s taken_at=%s pre_restore=%s integrity=%s", backup_id, taken_at, pre.backup_id if pre else "-", "ok" if status.ok else ",".join(status.codes))
        self._bus.publish(RestoreDone(backup_id=backup_id, taken_at=taken_at, pre_restore_backup_id=pre.backup_id if pre else ""))
        return RestoreResult(True, backup_id, pre.backup_id if pre else "", detail="" if status.ok else "integrity notes: " + ",".join(status.codes))

    def _live_instance_id(self) -> str:
        if not self.paths.database.exists():
            return ""
        conn = sqlite3.connect(f"file:{self.paths.database.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT instance_id FROM db_identity LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        except sqlite3.DatabaseError:
            return ""
        finally:
            conn.close()


def snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    """JSON-ready (API / Settings → Backups list)."""
    return asdict(snap)
