"""Products, categories, barcodes, tags, price history, label prints, offers.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class Category(Base):
    """``category`` table."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    name_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    sort_order: Mapped[int] = mapped_column()
    icon: Mapped[str | None] = mapped_column(Text, nullable=True)


class Product(Base):
    """``product`` table."""

    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(Text, unique=True)
    internal_barcode: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(Text)
    name_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    parent_product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    unit: Mapped[str] = mapped_column(Text)
    sale_mode: Mapped[str] = mapped_column(Text)
    base_unit: Mapped[str] = mapped_column(Text)
    pricing_unit_qty: Mapped[Decimal] = mapped_column(Numeric)
    pricing_unit_label: Mapped[str] = mapped_column(Text)
    min_qty: Mapped[Decimal] = mapped_column(Numeric)
    qty_step: Mapped[Decimal] = mapped_column(Numeric)
    qty_presets: Mapped[str] = mapped_column(Text)
    allow_decimal_qty: Mapped[int] = mapped_column()
    price_paise: Mapped[int] = mapped_column()
    mrp_paise: Mapped[int | None] = mapped_column(nullable=True)
    cost_paise: Mapped[int | None] = mapped_column(nullable=True)
    tax_rate_bp: Mapped[int] = mapped_column()
    price_includes_tax: Mapped[int] = mapped_column()
    hsn_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_stock: Mapped[Decimal] = mapped_column(Numeric)
    reorder_qty: Mapped[Decimal] = mapped_column(Numeric)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[int] = mapped_column()
    quick_pick: Mapped[int] = mapped_column()
    is_system: Mapped[int] = mapped_column()
    row_version: Mapped[int] = mapped_column()
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProductBarcode(Base):
    """``product_barcode`` table."""

    __tablename__ = "product_barcode"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    barcode: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    symbology: Mapped[str] = mapped_column(Text)
    pack_qty: Mapped[Decimal] = mapped_column(Numeric)
    is_primary: Mapped[int] = mapped_column()
    created_at: Mapped[str] = mapped_column(Text)


class Tag(Base):
    """``tag`` table."""

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)


class ProductTag(Base):
    """``product_tag`` table."""

    __tablename__ = "product_tag"

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"), primary_key=True)


class PriceHistory(Base):
    """``price_history`` table."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    field: Mapped[str] = mapped_column(Text)
    old_value_paise: Mapped[int | None] = mapped_column(nullable=True)
    new_value_paise: Mapped[int | None] = mapped_column(nullable=True)
    old_bp: Mapped[int | None] = mapped_column(nullable=True)
    new_bp: Mapped[int | None] = mapped_column(nullable=True)
    effective_from: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class LabelPrintLog(Base):
    """``label_print_log`` table."""

    __tablename__ = "label_print_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    copies: Mapped[int] = mapped_column()
    target: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    at: Mapped[str] = mapped_column(Text)


class Offer(Base):
    """``offer`` table."""

    __tablename__ = "offer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    name_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric)
    scope: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[int | None] = mapped_column(nullable=True)
    min_qty: Mapped[Decimal] = mapped_column(Numeric)
    valid_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    stackable: Mapped[int] = mapped_column()
    is_active: Mapped[int] = mapped_column()
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text)
