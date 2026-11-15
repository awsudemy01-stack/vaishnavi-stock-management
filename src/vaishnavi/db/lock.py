"""Single-writer advisory lock (docs/05 §9, ``E-DB-030``) — stdlib only so the start-up spine needs no ORM wheel.

Re-exported by ``vaishnavi.db.engine`` for callers that already have SQLAlchemy loaded.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vaishnavi.core.errors import DatabaseError

log = logging.getLogger("vaishnavi.db.lock")


@dataclass
class WriterLock:
    """Advisory lock file next to the database; refuses a second running instance."""

    path: Path
    _handle: Any = None

    def acquire(self) -> None:
        """Take the lock or raise ``DatabaseError('E-DB-030')``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        except OSError as exc:
            raise DatabaseError("E-DB-030", path=str(self.path), detail=str(exc), cause=exc) from exc
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DatabaseError("E-DB-030", path=str(self.path), detail="another instance holds the lock", cause=exc) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._handle = fd
        log.info("DB_WRITER_LOCK_ACQUIRED path=%s pid=%d", self.path, os.getpid())

    def release(self) -> None:
        """Release (safe to call twice)."""
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                os.lseek(self._handle, 0, os.SEEK_SET)
                msvcrt.locking(self._handle, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(self._handle, fcntl.LOCK_UN)
        except OSError as exc:
            log.debug("writer lock release: %s", exc)
        finally:
            os.close(self._handle)
            self._handle = None
            try:
                self.path.unlink()
            except OSError:
                pass
        log.info("DB_WRITER_LOCK_RELEASED path=%s", self.path)
