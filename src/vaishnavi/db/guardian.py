"""DbGuardian — start-up triage and clean-close bookkeeping for the live database (stdlib sqlite3).

Answers one question before anyone may log in: *which database are we running on, and is it sound?*
(docs/14 §5–6). Outcomes (:class:`TriageKind`):

* ``MISSING``  — no file (``E-DB-002``): a new installation, or the Owner must restore.
* ``CORRUPT``  — ``PRAGMA quick_check`` failed (``E-DB-003``): the file **and its -wal/-shm** are moved
  to ``quarantine/`` so the next start can offer a restore instead of crashing every morning.
* ``NEW``      — schema present but no ``db_identity`` row yet (the seed will create it).
* ``FOREIGN``  — ``instance_id`` differs from the fingerprint written at the last clean close
  (``E-DB-021``): another shop's file was dropped in → refuse.
* ``RESTORED`` — the file carries a ``backup_id`` (only snapshots do) that has not been acknowledged
  (``E-DB-020``): a manual copy-back; ``restored_at``/``restored_from`` are stamped and the caller shows the
  banner with the backup time.
* ``UNCLEAN``  — ``last_open_at`` newer than ``last_clean_close_at`` (``E-DB-022``): power cut; WAL was
  replayed by SQLite, nothing to do but count it.
* ``NORMAL``.

``mark_open()`` / ``mark_clean_close()`` maintain the identity timestamps; the clean close also
checkpoints the WAL (``TRUNCATE``) so a snapshot copied by hand is complete, and writes the fingerprint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from vaishnavi.core.clock import iso_utc, now_utc
from vaishnavi.core.errors import DatabaseError
from vaishnavi.db.ledger_store import SqliteLedgerStore
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.db.guardian")

QUARANTINE_PREFIX = "vaishnavi.db.corrupt-"
SIDE_FILES = ("-wal", "-shm")


class TriageKind(str, Enum):
    """What start-up found."""

    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    NEW = "NEW"
    FOREIGN = "FOREIGN"
    RESTORED = "RESTORED"
    UNCLEAN = "UNCLEAN"
    NORMAL = "NORMAL"


@dataclass(frozen=True)
class Triage:
    """Outcome of :meth:`DbGuardian.triage`."""

    kind: TriageKind
    code: str = ""
    detail: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def may_start(self) -> bool:
        """False → the app must not accept logins (missing/corrupt/foreign)."""
        return self.kind in (TriageKind.NEW, TriageKind.NORMAL, TriageKind.UNCLEAN, TriageKind.RESTORED)


@dataclass(frozen=True)
class Fingerprint:
    """What the last clean close recorded about the database (``state/last_db_fingerprint.json``)."""

    instance_id: str
    chain_head: str
    last_invoice: str | None
    closed_at: str
    app_version: str

    def save(self, path: Path) -> None:
        """Atomic write."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Fingerprint | None:
        """None when missing or unreadable (logged)."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(str(data["instance_id"]), str(data["chain_head"]), data.get("last_invoice"), str(data["closed_at"]), str(data.get("app_version", "")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("fingerprint unreadable (%s) — ignoring it", exc)
            return None


def quick_check(conn: sqlite3.Connection) -> str:
    """``PRAGMA quick_check`` result text (``"ok"`` when sound); exceptions become the text."""
    try:
        return str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return f"error: {exc}"


class DbGuardian:
    """Start-up triage + open/close bookkeeping for ``<primary>/data/vaishnavi.db``."""

    def __init__(self, paths: AppPaths, ledger: LedgerService, *, store: SqliteLedgerStore | None = None) -> None:
        self.paths = paths
        self.ledger = ledger
        self.store = store or SqliteLedgerStore()

    # -- triage -----------------------------------------------------------------------------
    def triage(self) -> Triage:
        """Classify the database file (see module docstring). Never raises for a bad file."""
        db = self.paths.database
        if not db.exists():
            log.warning("DB_MISSING path=%s code=E-DB-002", db)
            return Triage(TriageKind.MISSING, "E-DB-002", params={"path": str(db)})
        conn = sqlite3.connect(db)
        try:
            result = quick_check(conn)
            if result != "ok":
                conn.close()
                moved = self.quarantine()
                log.error("DB_CORRUPT quick_check=%s quarantined=%s code=E-DB-003", result[:200], [str(m) for m in moved])
                return Triage(TriageKind.CORRUPT, "E-DB-003", detail=result[:500], params={"path": str(db)})
            if not _table_exists(conn, "db_identity"):
                log.info("DB_TRIAGE kind=NEW (schema not yet applied)")
                return Triage(TriageKind.NEW)
            identity = conn.execute("SELECT instance_id, last_open_at, last_clean_close_at, backup_id, backup_taken_at, restored_from FROM db_identity LIMIT 1").fetchone()
            if identity is None:
                log.info("DB_TRIAGE kind=NEW (no identity row yet)")
                return Triage(TriageKind.NEW)
            instance_id, last_open, last_close, backup_id, backup_taken_at, restored_from = identity
            fingerprint = Fingerprint.load(self.paths.fingerprint)
            if fingerprint is not None and fingerprint.instance_id != instance_id:
                log.error("DB_FOREIGN instance=%s expected=%s code=E-DB-021", instance_id, fingerprint.instance_id)
                return Triage(TriageKind.FOREIGN, "E-DB-021", detail=f"instance {instance_id} ≠ {fingerprint.instance_id}", params={"path": str(db)})
            if backup_id and backup_id != restored_from:
                conn.execute("UPDATE db_identity SET restored_at = ?, restored_from = ?", (iso_utc(), backup_id))
                conn.commit()
                log.warning("RESTORE_DETECTED backup_id=%s taken_at=%s code=E-DB-020", backup_id, backup_taken_at)
                return self._record(conn, Triage(TriageKind.RESTORED, "E-DB-020", detail=f"backup {backup_id}", params={"taken_at": backup_taken_at or "?", "backup_id": backup_id}))
            if last_open and (last_close is None or last_open > last_close):
                log.warning("DB_UNCLEAN_CLOSE last_open=%s last_close=%s code=E-DB-022", last_open, last_close)
                return self._record(conn, Triage(TriageKind.UNCLEAN, "E-DB-022", params={"last_open": last_open}))
            log.info("DB_TRIAGE kind=NORMAL instance=%s", instance_id)
            return Triage(TriageKind.NORMAL)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _record(conn: sqlite3.Connection, triage: Triage) -> Triage:
        """Keep a trace of notable start-ups (unclean / restored) as ``integrity_check`` rows."""
        if _table_exists(conn, "integrity_check"):
            details = json.dumps({"kind": triage.kind.value, "code": triage.code, "detail": triage.detail, **triage.params}, ensure_ascii=False)
            conn.execute("INSERT INTO integrity_check(at, kind, ok, details) VALUES (?, 'QUICK_CHECK', 1, ?)", (iso_utc(), details))
            conn.commit()
        return triage

    def quarantine(self) -> list[Path]:
        """Move the live file and its side files to ``quarantine/vaishnavi.db.corrupt-<ts>*``."""
        stamp = now_utc().strftime("%Y%m%d_%H%M%S")
        self.paths.quarantine.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        for suffix in ("", *SIDE_FILES):
            src = Path(str(self.paths.database) + suffix)
            if src.exists():
                dest = self.paths.quarantine / f"{QUARANTINE_PREFIX}{stamp}{suffix}"
                src.replace(dest)
                moved.append(dest)
        return moved

    # -- bookkeeping -------------------------------------------------------------------------
    def mark_open(self) -> None:
        """Stamp ``last_open_at`` (called once the app accepts the file)."""
        conn = sqlite3.connect(self.paths.database)
        try:
            conn.execute("UPDATE db_identity SET last_open_at = ?", (iso_utc(),))
            conn.commit()
            log.debug("DB_MARK_OPEN")
        finally:
            conn.close()

    def mark_clean_close(self) -> Fingerprint:
        """Checkpoint the WAL, stamp ``last_clean_close_at`` and write the fingerprint."""
        conn = sqlite3.connect(self.paths.database)
        try:
            at = iso_utc()
            conn.execute("UPDATE db_identity SET last_clean_close_at = ?", (at,))
            conn.commit()
            row = conn.execute("SELECT instance_id, app_version FROM db_identity LIMIT 1").fetchone()
            head = self.ledger.chain_head(conn)
            inv = conn.execute("SELECT invoice_no FROM sale ORDER BY id DESC LIMIT 1").fetchone()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError as exc:
                log.warning("wal_checkpoint failed: %s", exc)
        finally:
            conn.close()
        fp = Fingerprint(str(row[0]) if row else "", head, inv[0] if inv else None, at, str(row[1]) if row else "")
        fp.save(self.paths.fingerprint)
        log.info("DB_CLEAN_CLOSE head=%s last_invoice=%s", head[:12], fp.last_invoice)
        return fp

    def unclean_close_count(self) -> int:
        """How often the app was not closed properly (doctor summary)."""
        conn = sqlite3.connect(self.paths.database)
        try:
            row = conn.execute("SELECT COUNT(*) FROM integrity_check WHERE kind = 'QUICK_CHECK' AND details LIKE '%E-DB-022%'").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def vacuum(self) -> None:
        """Monthly ``VACUUM`` + full ``integrity_check`` (docs/14 §7); raises ``E-DB-003`` on failure."""
        conn = sqlite3.connect(self.paths.database)
        try:
            result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            conn.execute("INSERT INTO integrity_check(at, kind, ok, details) VALUES (?, 'FULL_CHECK', ?, ?)", (iso_utc(), 1 if result == "ok" else 0, result[:2000]))
            conn.commit()
            if result != "ok":
                log.error("DB_FULL_CHECK_FAILED %s code=E-DB-003", result[:200])
                raise DatabaseError("E-DB-003", detail=result[:500])
            conn.execute("VACUUM")
            log.info("DB_VACUUM_DONE")
        finally:
            conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
