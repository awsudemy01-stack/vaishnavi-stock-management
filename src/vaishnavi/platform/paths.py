"""Filesystem layout of the running application, derived from the chosen locations (docs/14 §4).

::

    <primary>/                      <secondary>/
    ├── data/vaishnavi.db           ├── snapshots/  vaishnavi_YYYYMMDD_HHMMSS.db + .manifest.json
    ├── logs/{app,errors,audit,…}   └── queue/      snapshots waiting for the drive to return
    ├── receipts/YYYY/MM/*.pdf
    ├── images/products/
    ├── quarantine/                 damaged DB files set aside by the guardian
    ├── exports/                    CSV/XLSX/PDF the owner asked for
    ├── outbox/                     reports waiting to be published
    ├── tmp/
    └── .keys/                      master key file fallback (platform/secrets)

The repository/program folder (``main.py``, ``config/``) is separate and never holds data.
OS differences (hidden folders, default home) are confined to this package.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger("vaishnavi.platform.paths")

DB_FILE: Final = "vaishnavi.db"
DEFAULT_PRIMARY_NAME: Final = "VaishnaviStore"
DEFAULT_SECONDARY_NAME: Final = "VaishnaviStore-Backups"


def default_primary() -> Path:
    """``~/VaishnaviStore`` on every OS (installer default)."""
    return Path.home() / DEFAULT_PRIMARY_NAME


def default_secondary() -> Path:
    """``~/VaishnaviStore-Backups`` (same disk — the installer suggests another drive when it sees one)."""
    return Path.home() / DEFAULT_SECONDARY_NAME


def program_dir() -> Path:
    """Folder that contains ``main.py`` / ``config/`` (source checkout or installed release)."""
    env = os.environ.get("VAISHNAVI_PROGRAM_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "main.py").exists() and (parent / "config").is_dir():
            return parent
    return Path.cwd()


def config_dir() -> Path:
    """``<program>/config``."""
    return program_dir() / "config"


@dataclass(frozen=True)
class AppPaths:
    """All data folders; create them with ``ensure()``."""

    primary: Path
    secondary: Path

    # -- primary --------------------------------------------------------------------------------
    @property
    def data(self) -> Path:
        """Folder holding the live database."""
        return self.primary / "data"

    @property
    def database(self) -> Path:
        """The live SQLite file."""
        return self.data / DB_FILE

    @property
    def logs(self) -> Path:
        """Application logs root (families beneath)."""
        return self.primary / "logs"

    @property
    def receipts(self) -> Path:
        """Archived receipt PDFs (``YYYY/MM`` beneath)."""
        return self.primary / "receipts"

    @property
    def images(self) -> Path:
        """Product images."""
        return self.primary / "images" / "products"

    @property
    def quarantine(self) -> Path:
        """Damaged database files set aside by the guardian."""
        return self.primary / "quarantine"

    @property
    def exports(self) -> Path:
        """Owner exports (CSV/XLSX/PDF)."""
        return self.primary / "exports"

    @property
    def outbox(self) -> Path:
        """Reports waiting to be published."""
        return self.primary / "outbox"

    @property
    def tmp(self) -> Path:
        """Scratch space on the same volume as the database (atomic renames work)."""
        return self.primary / "tmp"

    @property
    def keys(self) -> Path:
        """Master-key file fallback folder."""
        return self.primary / ".keys"

    @property
    def state(self) -> Path:
        """Small state files the app writes at clean close (fingerprint, last run)."""
        return self.primary / "state"

    @property
    def fingerprint(self) -> Path:
        """``state/last_db_fingerprint.json`` — instance id + chain head + last invoice at the last clean close."""
        return self.state / "last_db_fingerprint.json"

    @property
    def local_backups(self) -> Path:
        """Short-retention snapshot copies kept on the primary disk (docs/14 §5.3)."""
        return self.primary / "backups_local"

    # -- secondary ------------------------------------------------------------------------------
    @property
    def snapshots(self) -> Path:
        """Verified backup snapshots."""
        return self.secondary / "snapshots"

    @property
    def backup_queue(self) -> Path:
        """Snapshots written locally while the secondary drive is unplugged (docs/14 §5)."""
        return self.primary / "backup-queue"

    # -- helpers --------------------------------------------------------------------------------
    def primary_folders(self) -> tuple[Path, ...]:
        """Everything under the primary location that must exist."""
        return (self.data, self.logs, self.receipts, self.images, self.quarantine, self.exports, self.outbox, self.tmp, self.backup_queue, self.state, self.local_backups)

    def ensure(self, *, include_secondary: bool = True) -> list[Path]:
        """Create missing folders; returns the ones created. Secondary failures are not fatal."""
        created: list[Path] = []
        for folder in self.primary_folders():
            if not folder.exists():
                folder.mkdir(parents=True, exist_ok=True)
                created.append(folder)
        if include_secondary:
            try:
                if not self.snapshots.exists():
                    self.snapshots.mkdir(parents=True, exist_ok=True)
                    created.append(self.snapshots)
            except OSError as exc:  # unplugged USB drive → backups queue (E-BKP-001 by the backup service)
                log.warning("secondary location not reachable: %s (%s)", self.secondary, exc)
        if created:
            log.info("PATHS_CREATED %s", [str(p) for p in created])
        return created

    def receipt_folder(self, year: int, month: int) -> Path:
        """``receipts/YYYY/MM`` (created)."""
        folder = self.receipts / f"{year:04d}" / f"{month:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def secondary_reachable(self) -> bool:
        """True when the backup location exists and is writable right now."""
        try:
            self.secondary.mkdir(parents=True, exist_ok=True)
            probe = self.secondary / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    @classmethod
    def from_locations(cls, primary: str | os.PathLike[str] | None, secondary: str | os.PathLike[str] | None) -> AppPaths:
        """Build from the installer's locations (empty → defaults)."""
        return cls(Path(primary) if primary else default_primary(), Path(secondary) if secondary else default_secondary())


def hide_folder(path: Path) -> None:
    """Mark a folder hidden on Windows (dot-prefix already hides it elsewhere)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("hide_folder failed: %r", exc)
