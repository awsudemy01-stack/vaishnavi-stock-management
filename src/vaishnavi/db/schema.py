"""Load and apply ``schema.sql`` (stdlib only) — shared by the Alembic migration, tests and the doctor.

Statements are split with ``sqlite3.complete_statement`` so the ``;`` inside trigger bodies does not
break them. ``apply_schema`` works on a raw ``sqlite3`` connection; the Alembic migration feeds the
same statements through SQLAlchemy.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Final

SCHEMA_FILE: Final = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION: Final = "0001"
HEAD_REVISION: Final = "0001_initial"
"""Newest Alembic revision shipped (kept equal to ``alembic/versions`` by ``tests/core/test_db_schema.py``);
lets the stdlib doctor compare ``alembic_version`` without importing Alembic."""


def stamp_head(conn: sqlite3.Connection) -> None:
    """Record :data:`HEAD_REVISION` in ``alembic_version`` on a database created with :func:`apply_schema`,
    so a later Alembic run (``upgrade head``) sees it as current instead of re-creating tables."""
    conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
    conn.execute("DELETE FROM alembic_version")
    conn.execute("INSERT INTO alembic_version(version_num) VALUES (?)", (HEAD_REVISION,))
    conn.commit()


def current_revision(conn: sqlite3.Connection) -> str | None:
    """``alembic_version`` content via stdlib (None when the table is absent/empty)."""
    try:
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    return str(row[0]) if row else None


def load_statements(path: Path = SCHEMA_FILE) -> list[str]:
    """Executable statements of the schema file, in order (comments and PRAGMAs stripped)."""
    statements: list[str] = []
    buffer: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            stmt = candidate.strip()
            if not stmt.upper().startswith("PRAGMA"):
                statements.append(stmt)
            buffer = []
    if buffer and "".join(buffer).strip():
        raise ValueError("schema.sql ends with an incomplete statement")
    return statements


def schema_hash(path: Path = SCHEMA_FILE) -> str:
    """Short SHA-256 of the schema file (recorded in ``db_identity.schema_version`` detail / manifests)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def apply_schema(conn: sqlite3.Connection, path: Path = SCHEMA_FILE) -> int:
    """Create every object on a fresh sqlite3 connection; returns the number of statements run."""
    conn.execute("PRAGMA foreign_keys = ON")
    count = 0
    for stmt in load_statements(path):
        conn.execute(stmt)
        count += 1
    conn.commit()
    return count


def table_names(conn: sqlite3.Connection) -> set[str]:
    """User tables present (excludes SQLite internals)."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return {r[0] for r in rows}


def trigger_names(conn: sqlite3.Connection) -> set[str]:
    """Triggers present."""
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
