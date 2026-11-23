"""Shift close reconciliation: declared non-cash totals and the approving Owner (live run 5b, owner's observation 3).

Revision ID: 0003_shift_reconcile
Revises: 0002_staff_receive
Create Date: 2026-11-20

* ``shift.declared_json`` (TEXT, JSON ``{"UPI": {"declared": paise, "system": paise}}``) — what the cashier read
  off the UPI / card app at close, next to what the system recorded, so a UPI shortfall is as visible as a
  cash shortfall.
* ``shift.approved_by_user_id`` (INTEGER → user.id) — the Owner who entered the PIN when the drawer or the UPI
  total did not match. NULL when everything matched or the Owner closed the shift personally.

Both are plain nullable columns: ``ALTER TABLE … ADD COLUMN`` is safe on SQLite and old rows read back as NULL.
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0003_shift_reconcile"
down_revision = "0002_staff_receive"
branch_labels = None
depends_on = None

log = logging.getLogger("vaishnavi.db.alembic.0003")

COLUMNS = (("declared_json", "TEXT"), ("approved_by_user_id", "INTEGER REFERENCES user(id)"))


def upgrade() -> None:
    """Add the two reconciliation columns when missing."""
    bind = op.get_bind()
    existing = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(shift)").fetchall()}
    added = []
    for name, ddl in COLUMNS:
        if name not in existing:
            bind.exec_driver_sql(f"ALTER TABLE shift ADD COLUMN {name} {ddl}")
            added.append(name)
    log.info("MIGRATION_0003_DONE columns_added=%s", added)


def downgrade() -> None:
    """The columns stay (SQLite column drops rewrite the table; NULL columns are harmless to 0002 code)."""
    log.info("MIGRATION_0003_REVERTED (columns kept)")
