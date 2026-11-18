"""Cashier shifts, cash events and time-boxed hardware overrides.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class Shift(Base):
    """``shift`` table."""

    __tablename__ = "shift"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    opened_at: Mapped[str] = mapped_column(Text)
    closed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_float_paise: Mapped[int] = mapped_column()
    opening_denominations: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_cash_paise: Mapped[int | None] = mapped_column(nullable=True)
    counted_cash_paise: Mapped[int | None] = mapped_column(nullable=True)
    denominations: Mapped[str | None] = mapped_column(Text, nullable=True)
    variance_paise: Mapped[int | None] = mapped_column(nullable=True)
    closed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CashEvent(Base):
    """``cash_event`` table."""

    __tablename__ = "cash_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"))
    type: Mapped[str] = mapped_column(Text)
    amount_paise: Mapped[int] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    at: Mapped[str] = mapped_column(Text)


class HardwareOverride(Base):
    """``hardware_override`` table."""

    __tablename__ = "hardware_override"

    kind: Mapped[str] = mapped_column(Text)
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    started_at: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
