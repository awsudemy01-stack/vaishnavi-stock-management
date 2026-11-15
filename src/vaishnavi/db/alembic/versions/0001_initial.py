"""Initial schema — every table, constraint, index and protection trigger of docs/04.

Revision ID: 0001_initial
Revises: None
Create Date: 2026-11-14

The DDL is ``vaishnavi/db/schema.sql`` (single source of truth, also tested with stdlib sqlite3);
this migration simply executes it statement by statement. Downgrade drops everything — only ever
used on an empty test database.
"""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

from vaishnavi.db.schema import load_statements

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

log = logging.getLogger("vaishnavi.db.alembic.0001")


def upgrade() -> None:
    """Create the schema."""
    statements = load_statements()
    log.info("MIGRATION_0001_START statements=%d", len(statements))
    bind = op.get_bind()
    for stmt in statements:
        # exec_driver_sql: raw DDL, no ``:name`` bind-parameter parsing (time formats contain colons).
        bind.exec_driver_sql(stmt)
    log.info("MIGRATION_0001_DONE")


def downgrade() -> None:
    """Drop every object created above (reverse order; triggers first)."""
    conn = op.get_bind()
    for (name,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).fetchall():
        op.execute(text(f'DROP TRIGGER IF EXISTS "{name}"'))
    for (name,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")).fetchall():
        op.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    op.execute(text("PRAGMA foreign_keys = OFF"))
    tables = [n for (n,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'")).fetchall()]
    for name in tables:
        op.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    op.execute(text("PRAGMA foreign_keys = ON"))
    log.info("MIGRATION_0001_REVERTED tables=%d", len(tables))
