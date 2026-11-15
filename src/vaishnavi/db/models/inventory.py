"""Stock movements (append-only ledger), trigger-maintained cache, daily balances, purchases, stock-takes.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class StockMovement(Base):
    """``stock_movement`` table."""

    __tablename__ = "stock_movement"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    type: Mapped[str] = mapped_column(Text)
    qty_delta: Mapped[Decimal] = mapped_column(Numeric)
    qty_after: Mapped[Decimal] = mapped_column(Numeric)
    unit_cost_paise: Mapped[int | None] = mapped_column(nullable=True)
    ref_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_id: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProductStock(Base):
    """``product_stock`` table."""

    __tablename__ = "product_stock"

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric)
    updated_at: Mapped[str] = mapped_column(Text)


class StockDailyBalance(Base):
    """``stock_daily_balance`` table."""

    __tablename__ = "stock_daily_balance"

    day: Mapped[str] = mapped_column(Text, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    qty_close: Mapped[Decimal] = mapped_column(Numeric)
    value_cost_paise: Mapped[int] = mapped_column()


class Supplier(Base):
    """``supplier`` table."""

    __tablename__ = "supplier"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    gstin: Mapped[str | None] = mapped_column(Text, nullable=True)


class Purchase(Base):
    """``purchase`` table."""

    __tablename__ = "purchase"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("supplier.id"), nullable=True)
    supplier_invoice_no: Mapped[str | None] = mapped_column(Text, nullable=True)
    purchase_date: Mapped[str] = mapped_column(Text)
    total_paise: Mapped[int] = mapped_column()
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[str] = mapped_column(Text)


class PurchaseItem(Base):
    """``purchase_item`` table."""

    __tablename__ = "purchase_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchase.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric)
    unit_cost_paise: Mapped[int] = mapped_column()
    expiry_date: Mapped[str | None] = mapped_column(Text, nullable=True)


class StockCount(Base):
    """``stock_count`` table."""

    __tablename__ = "stock_count"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)


class StockCountLine(Base):
    """``stock_count_line`` table."""

    __tablename__ = "stock_count_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_count_id: Mapped[int] = mapped_column(ForeignKey("stock_count.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    system_qty: Mapped[Decimal] = mapped_column(Numeric)
    counted_qty: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    variance: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    variance_value_paise: Mapped[int | None] = mapped_column(nullable=True)
