"""Sales (append-only, hash-chained), items, payments, counters, held carts, open-item review, print queue, cashier day book.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class Customer(Base):
    """``customer`` table."""

    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Counter(Base):
    """``counter`` table."""

    __tablename__ = "counter"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[int] = mapped_column()


class Sale(Base):
    """``sale`` table."""

    __tablename__ = "sale"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(Text, unique=True)
    client_txn_id: Mapped[str] = mapped_column(Text, unique=True)
    public_id: Mapped[str] = mapped_column(Text, unique=True)
    fin_year: Mapped[str] = mapped_column(Text)
    seq_no: Mapped[int] = mapped_column()
    sold_at: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text)
    original_sale_id: Mapped[int | None] = mapped_column(ForeignKey("sale.id"), nullable=True)
    subtotal_paise: Mapped[int] = mapped_column()
    item_discount_paise: Mapped[int] = mapped_column()
    bill_discount_paise: Mapped[int] = mapped_column()
    bill_discount_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_savings_paise: Mapped[int] = mapped_column()
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    taxable_paise: Mapped[int] = mapped_column()
    cgst_paise: Mapped[int] = mapped_column()
    sgst_paise: Mapped[int] = mapped_column()
    roundoff_paise: Mapped[int] = mapped_column()
    total_paise: Mapped[int] = mapped_column()
    reprint_count: Mapped[int] = mapped_column()
    receipt_printed: Mapped[int] = mapped_column()
    prev_hash: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(Text, unique=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class SaleItem(Base):
    """``sale_item`` table."""

    __tablename__ = "sale_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sale.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    name_snapshot: Mapped[str] = mapped_column(Text)
    hsn_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric)
    unit: Mapped[str] = mapped_column(Text)
    qty_source: Mapped[str] = mapped_column(Text)
    pricing_unit_qty_snapshot: Mapped[Decimal] = mapped_column(Numeric)
    shelf_price_paise: Mapped[int] = mapped_column()
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offer.id"), nullable=True)
    offer_name_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_bp: Mapped[int] = mapped_column()
    discount_paise: Mapped[int] = mapped_column()
    discount_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price_paise: Mapped[int] = mapped_column()
    tax_rate_bp: Mapped[int] = mapped_column()
    taxable_paise: Mapped[int] = mapped_column()
    cgst_paise: Mapped[int] = mapped_column()
    sgst_paise: Mapped[int] = mapped_column()
    line_total_paise: Mapped[int] = mapped_column()
    cost_paise_snapshot: Mapped[int | None] = mapped_column(nullable=True)
    entry_method: Mapped[str] = mapped_column(Text)
    entry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scanned_barcode: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_open_item: Mapped[int] = mapped_column()
    open_item_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_item_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Payment(Base):
    """``payment`` table."""

    __tablename__ = "payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sale.id"))
    mode: Mapped[str] = mapped_column(Text)
    amount_paise: Mapped[int] = mapped_column()
    tendered_paise: Mapped[int | None] = mapped_column(nullable=True)
    change_paise: Mapped[int] = mapped_column()
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)


class OpenItemReview(Base):
    """``open_item_review`` table."""

    __tablename__ = "open_item_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_item_id: Mapped[int] = mapped_column(ForeignKey("sale_item.id"), unique=True)
    status: Mapped[str] = mapped_column(Text)
    converted_product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    reviewed_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class PrintPending(Base):
    """``print_pending`` table."""

    __tablename__ = "print_pending"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sale.id"))
    queued_at: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column()
    printed_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class HeldCart(Base):
    """``held_cart`` table."""

    __tablename__ = "held_cart"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)


class HeldCartLine(Base):
    """``held_cart_line`` table."""

    __tablename__ = "held_cart_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    held_cart_id: Mapped[int] = mapped_column(ForeignKey("held_cart.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric)
    discount_paise: Mapped[int] = mapped_column()
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class CashierDayRecord(Base):
    """``cashier_day_record`` table — retention-bound copy of each bill with its items (the cashier day book)."""

    __tablename__ = "cashier_day_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sale.id"), unique=True)
    invoice_no: Mapped[str] = mapped_column(Text)
    business_day: Mapped[str] = mapped_column(Text)
    sold_at: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    cashier_name: Mapped[str] = mapped_column(Text)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"))
    status: Mapped[str] = mapped_column(Text)
    line_count: Mapped[int] = mapped_column()
    item_qty: Mapped[Decimal] = mapped_column(Numeric)
    total_paise: Mapped[int] = mapped_column()
    payment_modes: Mapped[str] = mapped_column(Text)
    items_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
