"""Plain record types exchanged between repositories and services (stdlib only — no SQLAlchemy).

Repositories map ORM rows to these frozen dataclasses; services never see ORM objects, which keeps
the business layer testable without a database and keeps the dependency direction services → db.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from vaishnavi.core.permissions import Role

PASSWORD_SENTINEL: Final = "!SETUP"
"""Seeded Owner password marker (not a hash); see ``db.seed`` and ``services.user``."""

GENESIS_HASH: Final = "0" * 64
"""``prev_hash`` of the first row of every hash chain (audit log, sales ledger)."""


@dataclass(frozen=True)
class WorkSessionRecord:
    """A ``work_session`` row (docs/05 §6a) — attendance; derived figures live in ``services.work_session``."""

    id: int
    user_id: int
    app_run_id: int | None
    login_at: str
    logout_at: str | None
    end_reason: str | None
    last_seen_at: str
    confirmed_at: str | None
    confirmed_note: str | None
    owner_note: str | None
    client: str | None
    idle_seconds: int
    lock_count: int
    correlation_id: str | None

    @property
    def is_open(self) -> bool:
        """Still logged in."""
        return self.logout_at is None

    @property
    def unclean(self) -> bool:
        """Ended without the End-work popup (flagged for confirmation)."""
        return self.end_reason in ("WINDOW_CLOSED", "HEARTBEAT_LOST")


@dataclass(frozen=True)
class UserRecord:
    """A ``user`` row as the service layer sees it (hashes included; never leaves the service layer)."""

    id: int
    username: str
    display_name: str
    role: Role
    password_hash: str
    owner_pin_hash: str | None
    is_active: bool
    must_change_password: bool
    failed_logins: int
    locked_until: str | None
    last_login_at: str | None
    created_at: str

    @property
    def setup_pending(self) -> bool:
        """True while the account still carries the seed sentinel."""
        return self.password_hash == PASSWORD_SENTINEL

    def public(self) -> dict[str, Any]:
        """Fields that may be shown in the Users screen / API (no hashes)."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role.value,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "has_pin": bool(self.owner_pin_hash),
            "failed_logins": self.failed_logins,
            "locked_until": self.locked_until,
            "last_login_at": self.last_login_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AuditEntry:
    """One ``audit_log`` row as the chain sees it (``id`` is filled once stored)."""

    at: str
    action: str
    user_id: int | None = None
    actor_user_id: int | None = None
    entity: str | None = None
    entity_id: int | None = None
    before: str | None = None
    after: str | None = None
    correlation_id: str | None = None
    ip: str | None = None
    prev_hash: str = GENESIS_HASH
    hash: str = ""
    id: int | None = None


# ----------------------------------------------------------------------------- catalogue
@dataclass(frozen=True)
class CategoryRecord:
    """A ``category`` row."""

    id: int
    name: str
    name_hi: str | None
    parent_id: int | None
    sort_order: int
    icon: str | None


@dataclass(frozen=True)
class ProductRecord:
    """A ``product`` row (docs/04). Money in paise, quantities as ``Decimal`` text-safe values."""

    id: int
    sku: str
    internal_barcode: str | None
    name: str
    name_hi: str | None
    category_id: int | None
    parent_product_id: int | None
    unit: str
    sale_mode: str
    base_unit: str
    pricing_unit_qty: Decimal
    pricing_unit_label: str
    min_qty: Decimal
    qty_step: Decimal
    qty_presets: str
    allow_decimal_qty: bool
    price_paise: int
    mrp_paise: int | None
    cost_paise: int | None
    tax_rate_bp: int
    price_includes_tax: bool
    hsn_code: str | None
    min_stock: Decimal
    reorder_qty: Decimal
    image_path: str | None
    is_active: bool
    quick_pick: bool
    is_system: bool
    row_version: int
    created_at: str
    updated_at: str
    deleted_at: str | None
    stock_qty: Decimal = Decimal("0")
    """From ``product_stock`` (0 when no movement yet) — joined for grids and the POS."""

    @property
    def display_id(self) -> str:
        """``#1042`` as printed on labels and receipts."""
        return f"#{self.id}"

    @property
    def low_stock(self) -> bool:
        """At or below the minimum."""
        return self.min_stock > 0 and self.stock_qty <= self.min_stock


@dataclass(frozen=True)
class BarcodeRecord:
    """A ``product_barcode`` row."""

    id: int
    product_id: int
    barcode: str
    kind: str
    symbology: str
    pack_qty: Decimal
    is_primary: bool
    created_at: str


@dataclass(frozen=True)
class PriceHistoryRecord:
    """A ``price_history`` row; ``old_value``/``new_value`` are paise for money fields and basis points for ``TAX_RATE``."""

    id: int
    product_id: int
    field: str
    old_value: int | None
    new_value: int | None
    effective_from: str
    changed_by: int | None
    reason: str | None
    source: str
    correlation_id: str | None


# ----------------------------------------------------------------------------- inventory
@dataclass(frozen=True)
class MovementRecord:
    """A ``stock_movement`` row (append-only ledger of quantities in base units)."""

    id: int
    product_id: int
    type: str
    qty_delta: Decimal
    qty_after: Decimal
    unit_cost_paise: int | None
    ref_type: str | None
    ref_id: int | None
    reason: str | None
    user_id: int
    created_at: str
    correlation_id: str | None


@dataclass(frozen=True)
class SupplierRecord:
    """A ``supplier`` row."""

    id: int
    name: str
    phone: str | None
    gstin: str | None


@dataclass(frozen=True)
class PurchaseRecord:
    """A ``purchase`` header with the number of labels the entry offers to print."""

    id: int
    supplier_id: int | None
    supplier_invoice_no: str | None
    purchase_date: str
    total_paise: int
    user_id: int
    created_at: str
    label_count: int = 0


@dataclass(frozen=True)
class StockCountRecord:
    """A ``stock_count`` header."""

    id: int
    started_at: str
    approved_at: str | None
    started_by_user_id: int
    approved_by_user_id: int | None
    status: str
    scope: str


@dataclass(frozen=True)
class StockCountLineRecord:
    """A ``stock_count_line`` row."""

    id: int
    stock_count_id: int
    product_id: int
    system_qty: Decimal
    counted_qty: Decimal | None
    variance: Decimal | None
    variance_value_paise: int | None


# ----------------------------------------------------------------------------- sales ledger
@dataclass(frozen=True)
class SaleItemRecord:
    """The hashed fields of one ``sale_item`` row (docs/05 §4)."""

    product_id: int | None
    qty: str
    """Decimal text in base units, normalised by :func:`vaishnavi.services.ledger.canonical_qty`."""
    unit: str
    shelf_price_paise: int
    offer_id: int | None
    discount_paise: int
    unit_price_paise: int
    tax_rate_bp: int
    entry_method: str
    is_open_item: bool
    open_item_name: str | None


@dataclass(frozen=True)
class PaymentRecord:
    """The hashed fields of one ``payment`` row."""

    mode: str
    amount_paise: int


@dataclass(frozen=True)
class SaleRecord:
    """A ``sale`` row with its lines and payments — exactly what the hash covers, plus ``id``/``hash``."""

    id: int
    invoice_no: str
    client_txn_id: str
    fin_year: str
    seq_no: int
    sold_at: str
    user_id: int
    shift_id: int
    status: str
    original_sale_id: int | None
    subtotal_paise: int
    item_discount_paise: int
    bill_discount_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    roundoff_paise: int
    total_paise: int
    items: tuple[SaleItemRecord, ...]
    payments: tuple[PaymentRecord, ...]
    prev_hash: str = GENESIS_HASH
    hash: str = ""


# ----------------------------------------------------------------------------- offers & full sale views
@dataclass(frozen=True)
class OfferRecord:
    """An ``offer`` row (docs/05 §8a)."""

    id: int
    name: str
    name_hi: str | None
    type: str  # PCT | AMOUNT | BUY_X_GET_Y
    value: Decimal
    """Percent for PCT, rupees for AMOUNT, free pieces (Y) for BUY_X_GET_Y (X = ``min_qty``)."""
    scope: str  # PRODUCT | CATEGORY | TAG | BILL
    scope_id: int | None
    min_qty: Decimal
    valid_from: str | None
    valid_to: str | None
    stackable: bool
    is_active: bool
    created_by: int | None
    created_at: str


@dataclass(frozen=True)
class SaleItemView:
    """Every ``sale_item`` column — what the receipt and the reports read."""

    id: int
    sale_id: int
    product_id: int | None
    name_snapshot: str
    hsn_snapshot: str | None
    qty: Decimal
    unit: str
    qty_source: str
    pricing_unit_qty_snapshot: Decimal
    shelf_price_paise: int
    offer_id: int | None
    offer_name_snapshot: str | None
    discount_bp: int
    discount_paise: int
    discount_reason: str | None
    unit_price_paise: int
    tax_rate_bp: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    line_total_paise: int
    cost_paise_snapshot: int | None
    entry_method: str
    entry_reason: str | None
    scanned_barcode: str | None
    is_open_item: bool
    open_item_name: str | None
    open_item_detail: str | None


@dataclass(frozen=True)
class PaymentView:
    """A ``payment`` row."""

    id: int
    sale_id: int
    mode: str
    amount_paise: int
    tendered_paise: int | None
    change_paise: int
    reference: str | None


@dataclass(frozen=True)
class SaleView:
    """A ``sale`` row with every column plus its lines and payments."""

    id: int
    invoice_no: str
    client_txn_id: str
    public_id: str
    fin_year: str
    seq_no: int
    sold_at: str
    user_id: int
    shift_id: int
    customer_id: int | None
    status: str
    original_sale_id: int | None
    subtotal_paise: int
    item_discount_paise: int
    bill_discount_paise: int
    bill_discount_reason: str | None
    total_savings_paise: int
    approved_by_user_id: int | None
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    roundoff_paise: int
    total_paise: int
    reprint_count: int
    receipt_printed: bool
    prev_hash: str
    hash: str
    note: str | None
    items: tuple[SaleItemView, ...]
    payments: tuple[PaymentView, ...]

    @property
    def paid_paise(self) -> int:
        """Σ payments."""
        return sum(p.amount_paise for p in self.payments)

    @property
    def change_paise(self) -> int:
        """Σ change given."""
        return sum(p.change_paise for p in self.payments)

    def as_ledger_record(self) -> SaleRecord:
        """The hashed subset (for re-verification and the chain head)."""
        items = tuple(SaleItemRecord(it.product_id, str(it.qty), it.unit, it.shelf_price_paise, it.offer_id, it.discount_paise, it.unit_price_paise, it.tax_rate_bp, it.entry_method, it.is_open_item, it.open_item_name) for it in self.items)
        payments = tuple(PaymentRecord(p.mode, p.amount_paise) for p in self.payments)
        return SaleRecord(self.id, self.invoice_no, self.client_txn_id, self.fin_year, self.seq_no, self.sold_at, self.user_id, self.shift_id, self.status, self.original_sale_id, self.subtotal_paise, self.item_discount_paise, self.bill_discount_paise, self.taxable_paise, self.cgst_paise, self.sgst_paise, self.roundoff_paise, self.total_paise, items, payments, self.prev_hash, self.hash)


@dataclass(frozen=True)
class HeldCartRecord:
    """A ``held_cart`` row (the cart itself is the JSON ``payload`` of its single line row)."""

    id: int
    user_id: int
    label: str | None
    created_at: str
    line_count: int
    payload: str


@dataclass(frozen=True)
class OverrideRecord:
    """A ``hardware_override`` row — the Owner allowed selling without a device until ``expires_at``."""

    id: int
    kind: str  # PRINTER | SCANNER
    owner_user_id: int
    started_at: str
    expires_at: str
    reason: str | None


# ----------------------------------------------------------------------------- shifts & cash
@dataclass(frozen=True)
class ShiftRecord:
    """A ``shift`` row."""

    id: int
    user_id: int
    opened_at: str
    closed_at: str | None
    opening_float_paise: int
    expected_cash_paise: int | None
    counted_cash_paise: int | None
    denominations: str | None
    """JSON ``{"500": 4, "200": 3, …}``."""
    variance_paise: int | None
    closed_by_user_id: int | None
    status: str  # OPEN | CLOSED
    note: str | None


@dataclass(frozen=True)
class CashEventRecord:
    """A ``cash_event`` row."""

    id: int
    shift_id: int
    type: str  # CASH_IN | CASH_OUT
    amount_paise: int
    reason: str | None
    user_id: int
    approved_by_user_id: int | None
    at: str


# ----------------------------------------------------------------------------- forecasting
@dataclass(frozen=True)
class ForecastSnapshotRecord:
    """A ``forecast_snapshot`` row (nightly point + band per product)."""

    id: int
    computed_at: str
    product_id: int
    horizon_days: int
    point: Decimal
    low: Decimal
    high: Decimal
    method: str
    confidence: str
    caveats: str | None


# ----------------------------------------------------------------------------- assistant
@dataclass(frozen=True)
class PromptVersionRecord:
    """A ``prompt_version`` row (Owner-editable assistant instructions)."""

    id: int
    version: int
    editable_instructions: str
    core_hash: str
    default_language: str
    extra_blocked_phrases: str
    is_active: bool
    redteam_passed: bool
    created_by: int | None
    created_at: str
    note: str | None


@dataclass(frozen=True)
class ChatMessageRecord:
    """A ``chat_message`` row."""

    id: int
    session_id: int
    role: str
    content: str
    tool_calls: str | None
    provider: str | None
    model: str | None
    language: str | None
    guardrail_decision: str | None
    created_at: str


@dataclass(frozen=True)
class ChatSecurityEventRecord:
    """A ``chat_security_event`` row (docs/06 section 7 layer 3/6)."""

    id: int
    at: str
    user_id: int
    category: str
    severity: str
    message_excerpt: str | None
    layer: str
    owner_alerted: bool


@dataclass(frozen=True)
class PublishQueueRecord:
    """A ``publish_queue`` row (docs/05 section 11)."""

    id: int
    channel: str
    document: str
    file_path: str
    status: str
    attempts: int
    next_retry_at: str | None
    last_error: str | None
    created_at: str
