"""SQLAlchemy engine for the shop database with the durability pragmas of ADR-031.

* ``journal_mode=WAL`` — readers never block the single writer; crash-safe.
* ``synchronous=FULL`` — every commit is on disk before we say "bill saved" (a power cut mid-sale
  rolls back the whole sale, never half of it — docs/14 §2).
* ``foreign_keys=ON``, ``busy_timeout=5000``, ``temp_store=MEMORY``.
* **Writer sessions** begin with ``BEGIN IMMEDIATE`` so two counters cannot both read the invoice
  counter and both increment it (gap-free numbers); readers use plain deferred transactions.
* A **single-writer lock file** (``data/vaishnavi.db.lock``) refuses a second application instance
  on the same database (``E-DB-030``) — two servers on one SQLite file is how corruption happens.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.pool import QueuePool, StaticPool

from vaishnavi.core.errors import DatabaseError

log = logging.getLogger("vaishnavi.db.engine")

PRAGMAS: dict[str, str] = {
    "journal_mode": "WAL",
    "synchronous": "FULL",
    "foreign_keys": "ON",
    "busy_timeout": "5000",
    "temp_store": "MEMORY",
}


def _apply_pragmas(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    for name, value in PRAGMAS.items():
        cur.execute(f"PRAGMA {name} = {value}")
    cur.close()
    # SQLAlchemy's pysqlite dialect leaves transaction control to us (BEGIN IMMEDIATE below).
    dbapi_conn.isolation_level = None


def _begin(conn: Connection) -> None:
    # Writer connections start IMMEDIATE so the invoice counter is read and written under one lock.
    mode = "IMMEDIATE" if conn.info.get("writer", False) else "DEFERRED"
    conn.exec_driver_sql(f"BEGIN {mode}")


def make_engine(database: Path | str, *, writer: bool = False, echo: bool = False, memory: bool = False) -> Engine:
    """Engine for the live file (or ``memory=True`` for tests).

    Two engines are normally built by `AppServices`: one ``writer=True`` (used by every unit of
    work that changes data, serialised by ``BEGIN IMMEDIATE``) and one read-only for reports and
    the AI tools.
    """
    if memory:
        engine = create_engine("sqlite+pysqlite:///:memory:", echo=echo, poolclass=StaticPool, connect_args={"check_same_thread": False})
    else:
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        # QueuePool (SQLAlchemy's default for file databases): connections survive thread turnover in the
        # web server's worker pool; SingletonThreadPool would leak one connection per dead thread.
        engine = create_engine(
            f"sqlite+pysqlite:///{path.as_posix()}",
            echo=echo,
            poolclass=QueuePool,
            pool_size=4,
            max_overflow=6,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, record: Any) -> None:  # noqa: ANN001
        _apply_pragmas(dbapi_conn, record)

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.info.setdefault("writer", writer)
        _begin(conn)

    log.info("DB_ENGINE_CREATED target=%s writer=%s", "memory" if memory else database, writer)
    return engine


def verify_pragmas(engine: Engine) -> dict[str, str]:
    """Read back the effective pragmas (doctor check D4)."""
    out: dict[str, str] = {}
    with engine.connect() as conn:
        for name in ("journal_mode", "synchronous", "foreign_keys"):
            out[name] = str(conn.exec_driver_sql(f"PRAGMA {name}").scalar())
    log.debug("DB_PRAGMAS %s", out)
    return out


# ----------------------------------------------------------------------------- single writer lock
from vaishnavi.db.lock import WriterLock  # noqa: E402  (stdlib-only module; re-exported for existing callers)

__all__ = ["WriterLock", "make_engine", "verify_pragmas"]
