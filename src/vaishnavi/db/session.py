"""Unit of work and session factories.

``UnitOfWork`` wraps one writer transaction: ``with uow() as session:`` commits on success and rolls
back on any exception — a sale is therefore all-or-nothing together with its stock movements and
payments (docs/14 §2). ``ReadOnlySession`` is what reports and the AI tools get: it opens a deferred
transaction and raises if anyone tries to flush a change, so the assistant *cannot* modify data even
through a bug.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from vaishnavi.core.logging import current_cid

log = logging.getLogger("vaishnavi.db.session")

SLOW_TXN_MS = 500


class ReadOnlyViolation(RuntimeError):
    """A read-only session tried to write (programming error, caught in tests and logged as ERROR)."""


def _forbid_flush(session: Session, _ctx: object, _instances: object) -> None:
    if session.new or session.dirty or session.deleted:
        log.error("READONLY_WRITE_ATTEMPT cid=%s new=%d dirty=%d deleted=%d", current_cid(), len(session.new), len(session.dirty), len(session.deleted))
        raise ReadOnlyViolation("read-only session: writes are not allowed here")


class UnitOfWork:
    """Factory of writer sessions bound to the writer engine."""

    def __init__(self, engine: Engine) -> None:
        self._factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)

    @contextmanager
    def __call__(self, *, label: str = "") -> Iterator[Session]:
        """Open a transaction; commit on normal exit, roll back on exception; log duration."""
        started = time.perf_counter()
        session = self._factory()
        cid = current_cid()
        log.debug("UOW_BEGIN cid=%s label=%s", cid, label)
        try:
            yield session
            session.commit()
            elapsed = int((time.perf_counter() - started) * 1000)
            level = logging.WARNING if elapsed > SLOW_TXN_MS else logging.DEBUG
            log.log(level, "UOW_COMMIT cid=%s label=%s ms=%d", cid, label, elapsed)
        except BaseException as exc:
            session.rollback()
            log.warning("UOW_ROLLBACK cid=%s label=%s ms=%d error=%r", cid, label, int((time.perf_counter() - started) * 1000), exc)
            raise
        finally:
            session.close()


class ReadOnlySession:
    """Factory of sessions that cannot write (reports, AI tools, log console)."""

    def __init__(self, engine: Engine) -> None:
        self._factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def __call__(self, *, label: str = "") -> Iterator[Session]:
        """Open, yield, always roll back."""
        session = self._factory()
        event.listen(session, "before_flush", _forbid_flush)
        log.debug("RO_BEGIN cid=%s label=%s", current_cid(), label)
        try:
            yield session
        finally:
            session.rollback()
            session.close()
