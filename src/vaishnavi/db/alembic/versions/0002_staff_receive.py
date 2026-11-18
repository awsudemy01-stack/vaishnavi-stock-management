"""Staff may record goods received; shifts remember the opening note count (live run 4, observations 2 and 9).

Revision ID: 0002_staff_receive
Revises: 0001_initial
Create Date: 2026-11-18

* Until now ``trg_stock_movement_validate`` let non-Owner users create only SALE movements (E-INV-010).
  A parcel arriving while the Owner is away could therefore not be booked in, and stock ran "negative"
  on paper. The trigger now also admits ``PURCHASE`` movements with a *positive* ``qty_delta`` for any
  user — staff can add stock, never remove or correct it. The statement is taken from ``schema.sql``
  (single source of truth), so fresh databases and upgraded ones end with the identical trigger.
* ``shift.opening_denominations`` (TEXT, JSON) stores the note-by-note count made when a shift opens,
  mirroring ``denominations`` at close.
"""

from __future__ import annotations

import logging

from alembic import op

from vaishnavi.db.schema import statement_for

revision = "0002_staff_receive"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

log = logging.getLogger("vaishnavi.db.alembic.0002")

TRIGGER = "trg_stock_movement_validate"
COLUMN = "opening_denominations"

_PREVIOUS = """
CREATE TRIGGER trg_stock_movement_validate
BEFORE INSERT ON stock_movement
BEGIN
    SELECT CASE
        WHEN NEW.qty_after <> COALESCE((SELECT qty FROM product_stock WHERE product_id = NEW.product_id), 0) + NEW.qty_delta
            THEN RAISE(ABORT, 'E-INV-013')
        WHEN NEW.type = 'SALE' AND (NEW.ref_type IS NOT 'SALE' OR NOT EXISTS (SELECT 1 FROM sale WHERE id = NEW.ref_id))
            THEN RAISE(ABORT, 'E-INV-011')
        WHEN NEW.type IN ('SALE_VOID', 'RETURN') AND (NEW.ref_type IS NOT 'SALE' OR NOT EXISTS (SELECT 1 FROM sale WHERE id = NEW.ref_id))
            THEN RAISE(ABORT, 'E-INV-011')
        WHEN NEW.type <> 'SALE'
             AND (SELECT r.name FROM user u JOIN role r ON r.id = u.role_id WHERE u.id = NEW.user_id) IS NOT 'OWNER'
            THEN RAISE(ABORT, 'E-INV-010')
        WHEN (SELECT is_system FROM product WHERE id = NEW.product_id) = 1
            THEN RAISE(ABORT, 'E-INV-011')
    END;
END
"""


def upgrade() -> None:
    """Re-create the movement trigger with the staff goods-received clause; add the opening count column."""
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {TRIGGER}")
    bind.exec_driver_sql(statement_for(TRIGGER))
    columns = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(shift)").fetchall()}
    if COLUMN not in columns:
        bind.exec_driver_sql(f"ALTER TABLE shift ADD COLUMN {COLUMN} TEXT")
    log.info("MIGRATION_0002_DONE trigger=%s column_added=%s", TRIGGER, COLUMN not in columns)


def downgrade() -> None:
    """Back to Owner-only non-SALE movements (the column stays — SQLite drops are destructive and it is harmless)."""
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {TRIGGER}")
    bind.exec_driver_sql(_PREVIOUS.strip())
    log.info("MIGRATION_0002_REVERTED trigger=%s", TRIGGER)
