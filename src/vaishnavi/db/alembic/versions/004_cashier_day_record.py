"""Cashier day book: a retention-bound copy of every bill with its items, per cashier (owner's request, 21-Nov).

Revision ID: 0004_cashier_day_record
Revises: 0003_shift_reconcile
Create Date: 2026-11-21

* ``cashier_day_record`` — one row per bill / credit note (invoice, shop day, time, cashier, status, line count,
  quantity, total, payment modes, items as JSON). Written by ``CashierRecordService`` after ``SaleCommitted``;
  listed per day and per cashier on Reports → Cashier day book; deleted by the nightly job after
  ``billing.day_record_retention_days`` (90) or earlier by the Owner (audited ``CASHIER_RECORDS_PURGED``).
  The permanent, append-only ``sale`` ledger is untouched — this table is the deletable working copy.

The statements come from ``schema.sql`` (``schema.statement_for``), so fresh and upgraded databases match.
"""

from __future__ import annotations

import logging

from alembic import op

from vaishnavi.db.schema import statement_for

revision = "0004_cashier_day_record"
down_revision = "0003_shift_reconcile"
branch_labels = None
depends_on = None

log = logging.getLogger("vaishnavi.db.alembic.0004")

TABLE = "cashier_day_record"
INDEX = "ix_cashier_day_record_day"


def upgrade() -> None:
    """Create the table and its index when missing."""
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if TABLE not in tables:
        bind.exec_driver_sql(statement_for(TABLE))
    indexes = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    if INDEX not in indexes:
        bind.exec_driver_sql(statement_for(INDEX))
    log.info("MIGRATION_0004_DONE table_created=%s", TABLE not in tables)


def downgrade() -> None:
    """Drop the day book (it is a derived copy; the sale ledger keeps every bill)."""
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP INDEX IF EXISTS {INDEX}")
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
    log.info("MIGRATION_0004_REVERTED")
