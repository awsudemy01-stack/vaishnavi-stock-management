"""BillingService — the cart and the **idempotent** checkout (docs/05 §1, §3, §4, §8a–8d, §10).

The cart lives in memory on the POS page (one per browser tab); every mutation goes through this
service so the anti-theft rules are applied in exactly one place:

* **Lines** enter by *scan* (``SCAN`` — the normal path; weight-embedded scale codes fill the
  quantity), *F3 Find* (``LOOKUP`` — torn label, marked ``†`` and counted), *open item* (``OPEN_ITEM``
  — nothing in the catalogue, marked ``*``, policy ``ALLOW | PIN | BLOCK``, per-bill cap, under-price
  check) or *quick pick* (``QUICK_PICK`` — tiles, disabled for staff while ``require_scan`` is on).
* **Discounts**: offers auto-apply and cannot be removed by staff (``E-BILL-061``); ad-hoc discounts
  need a reason and respect ``adhoc_discount_cap_percent`` (``E-BILL-060`` → Owner PIN); the shelf
  price is never overwritten so the receipt shows "10.00 → 1 % → 9.90".
* **Checkout** is one ``BEGIN IMMEDIATE`` transaction: replay check on ``client_txn_id`` → open shift
  (``E-BILL-042``) → per-FY invoice counter (gap-free) → hash with the chain head → ``sale`` →
  ``sale_item`` → ``payment`` → one ``SALE`` movement per catalogue line (negative-stock policy) →
  audit. Crash anywhere = nothing written. The same cart sent twice returns the first invoice
  (``E-BILL-080`` logged, never raised).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol

from vaishnavi.core.barcode import parse_receipt_ean13
from vaishnavi.core.clock import business_day_bounds, financial_year, iso_utc, now_utc, parse_iso_utc, today_ist
from vaishnavi.core.errors import BillingError, InventoryError, MeasureError, ScannerError
from vaishnavi.core.events import EventBus, SaleCommitted, StockChanged, bus
from vaishnavi.core.money import format_inr, multiply, round_off
from vaishnavi.core.permissions import Actor, OpenItemPolicy, Permission, PinRequired, Role, require
from vaishnavi.core.settings import Settings
from vaishnavi.db.records import CustomerRecord, HeldCartRecord, OfferRecord, PaymentRecord, ProductRecord, SaleItemRecord, SaleItemView, SaleRecord, SaleView
from vaishnavi.services.audit import AuditService
from vaishnavi.services.catalog import CatalogService
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.ledger import LedgerService, canonical_qty
from vaishnavi.services.measure import QuantitySpec, ScaleCodeFormat, decode_scale_barcode, qty_for_amount, spec_for, validate_qty
from vaishnavi.services.offers import OfferMatch, OfferService
from vaishnavi.services.tax import BillInput, BillTax, LineInput, TaxEngine

log = logging.getLogger("vaishnavi.services.billing")

OPEN_ITEM_SPEC: Final = QuantitySpec(sale_mode="PIECE", base_unit="pcs", pricing_unit_qty=Decimal("1"), pricing_unit_label="pc", min_qty=Decimal("0.001"), qty_step=Decimal("0.001"), presets=(), allow_decimal=True)
LOW_PRICE_RATIO: Final = Decimal("0.5")
PAYMENT_MODES: Final = ("CASH", "UPI", "CARD")


# ----------------------------------------------------------------------------- cart model
@dataclass
class CartLine:
    """One line of the cart; all money in paise, quantity in base units."""

    line_id: str
    product_id: int | None
    name: str
    name_hi: str | None
    sku: str
    unit: str
    qty: Decimal
    qty_source: str
    spec: QuantitySpec
    shelf_price_paise: int
    pricing_unit_qty: Decimal
    tax_rate_bp: int
    price_includes_tax: bool
    hsn_code: str | None
    cost_paise: int | None
    entry_method: str
    entry_reason: str | None = None
    scanned_barcode: str | None = None
    offer: OfferMatch | None = None
    offer_removed: bool = False
    adhoc_discount_bp: int = 0
    adhoc_discount_paise: int = 0
    discount_reason: str | None = None
    is_open_item: bool = False
    open_item_name: str | None = None
    open_item_detail: str | None = None

    @property
    def active_offer(self) -> OfferMatch | None:
        """The offer that will print (None when removed by the Owner)."""
        return None if self.offer_removed else self.offer

    @property
    def gross_paise(self) -> int:
        """Shelf price × quantity."""
        return multiply(self.shelf_price_paise, self.qty / self.pricing_unit_qty)

    def tax_input(self) -> LineInput:
        """What the TaxEngine needs."""
        offer = self.active_offer
        return LineInput(
            shelf_price_paise=self.shelf_price_paise,
            qty=self.qty,
            pricing_unit_qty=self.pricing_unit_qty,
            tax_rate_bp=self.tax_rate_bp,
            price_includes_tax=self.price_includes_tax,
            offer_discount_bp=offer.discount_bp if offer else 0,
            offer_discount_paise=offer.discount_paise if offer else 0,
            offer_stackable=offer.stackable if offer else False,
            offer_name=offer.offer.name if offer else None,
            adhoc_discount_bp=self.adhoc_discount_bp,
            adhoc_discount_paise=self.adhoc_discount_paise,
            key=self.line_id,
        )


@dataclass
class Cart:
    """A bill in progress. ``client_txn_id`` is minted once and makes the checkout idempotent."""

    client_txn_id: str
    user_id: int
    created_at: str
    lines: list[CartLine] = field(default_factory=list)
    bill_discount_bp: int = 0
    bill_discount_paise: int = 0
    bill_discount_reason: str | None = None
    bill_offer: OfferRecord | None = None
    approved_by_user_id: int | None = None
    customer_id: int | None = None
    customer_phone: str | None = None
    customer_name: str | None = None
    """Phone (E.164 digits) and name as entered with F8 — for the cart header and the WhatsApp bill."""
    note: str | None = None
    committed_sale_id: int | None = None
    """Set by checkout (also on replay) — a committed cart must never be billed again."""

    def line(self, line_id: str) -> CartLine:
        """Find a line or raise ``ValueError``."""
        for ln in self.lines:
            if ln.line_id == line_id:
                return ln
        raise ValueError(f"no line {line_id} in cart")

    @property
    def open_item_count(self) -> int:
        """Open-item lines on this bill (the per-bill cap)."""
        return sum(1 for ln in self.lines if ln.is_open_item)

    @property
    def is_empty(self) -> bool:
        """No lines."""
        return not self.lines


@dataclass(frozen=True)
class ScanResult:
    """What a scanned code resolved to — the POS decides whether to open the quantity pad."""

    product: ProductRecord
    barcode: str
    pack_qty: Decimal
    spec: QuantitySpec
    needs_qty: bool
    suggested_qty: Decimal | None = None
    qty_source: str = "SCAN"


@dataclass(frozen=True)
class PaymentInput:
    """One payment entry from the pay dialog (``amount_paise`` is what is applied to the bill)."""

    mode: str
    amount_paise: int
    tendered_paise: int | None = None
    reference: str | None = None


@dataclass(frozen=True)
class CheckoutResult:
    """Outcome of :meth:`BillingService.checkout`."""

    sale_id: int
    invoice_no: str
    public_id: str
    total_paise: int
    paid_paise: int
    change_paise: int
    replayed: bool
    sale: SaleView
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DaySummary:
    """Bills and rupees for a day (optionally one cashier)."""

    day: str
    user_id: int | None
    bills: int
    total_paise: int
    items: int


@dataclass(frozen=True)
class RefundLine:
    """One returned line (``qty`` in base units, ≤ what is still refundable)."""

    sale_item_id: int
    qty: Decimal


@dataclass(frozen=True)
class VoidInfo:
    """What may still happen to a bill (docs/05 §7)."""

    sale_id: int
    status: str
    can_void: bool
    can_refund: bool
    refundable: dict[int, Decimal]
    """``sale_item_id → quantity not yet returned``."""
    reason_code: str | None
    related: list[SaleView]
    """Credit notes issued against this bill."""


# ----------------------------------------------------------------------------- store protocols
class BillingStore(Protocol):
    """Sales-side rows."""

    def open_shift_id(self, session: Session, user_id: int) -> int | None: ...  # pragma: no cover - protocol
    def open_item_product_id(self, session: Session) -> int: ...  # pragma: no cover - protocol
    def sale_by_client_txn(self, session: Session, client_txn_id: str) -> SaleView | None: ...  # pragma: no cover - protocol
    def sale(self, session: Session, sale_id: int) -> SaleView | None: ...  # pragma: no cover - protocol
    def find_sale(self, session: Session, text: str) -> SaleView | None: ...  # pragma: no cover - protocol
    def recent_sales(self, session: Session, *, user_id: int | None, limit: int) -> list[SaleView]: ...  # pragma: no cover - protocol
    def related_sales(self, session: Session, original_sale_id: int) -> list[SaleView]: ...  # pragma: no cover - protocol
    def search_sales(self, session: Session, *, text: str, user_id: int | None, limit: int) -> list[SaleView]: ...  # pragma: no cover - protocol
    def day_summary(self, session: Session, *, user_id: int | None, start: str, end: str) -> tuple[int, int, int]: ...  # pragma: no cover - protocol
    def next_invoice_seq(self, session: Session, fin_year: str) -> int: ...  # pragma: no cover - protocol
    def peek_invoice_seq(self, session: Session, fin_year: str) -> int: ...  # pragma: no cover - protocol
    def insert_sale(self, session: Session, fields: dict[str, Any]) -> int: ...  # pragma: no cover - protocol
    def insert_sale_item(self, session: Session, fields: dict[str, Any]) -> int: ...  # pragma: no cover - protocol
    def insert_payment(self, session: Session, fields: dict[str, Any]) -> int: ...  # pragma: no cover - protocol
    def insert_held(self, session: Session, *, user_id: int, label: str | None, payload: str, line_count: int, created_at: str) -> int: ...  # pragma: no cover - protocol
    def held_carts(self, session: Session, *, user_id: int | None) -> list[HeldCartRecord]: ...  # pragma: no cover - protocol
    def held_cart(self, session: Session, held_id: int) -> HeldCartRecord | None: ...  # pragma: no cover - protocol
    def delete_held(self, session: Session, held_id: int) -> None: ...  # pragma: no cover - protocol
    def upsert_customer(self, session: Session, *, phone: str, name: str | None) -> CustomerRecord: ...  # pragma: no cover - protocol
    def customer(self, session: Session, customer_id: int) -> CustomerRecord | None: ...  # pragma: no cover - protocol


class StockStore(Protocol):
    """The inventory slice checkout needs (the same store the InventoryService uses)."""

    def stock_of(self, session: Session, product_id: int) -> Decimal: ...  # pragma: no cover - protocol
    def insert_movement(self, session: Session, *, product_id: int, type: str, qty_delta: Decimal, qty_after: Decimal, unit_cost_paise: int | None, ref_type: str | None, ref_id: int | None, reason: str | None, user_id: int, created_at: str) -> int: ...  # noqa: A002  # pragma: no cover - protocol


class ProductLookup(Protocol):
    """Product rows by id."""

    def product_by_id(self, session: Session, product_id: int) -> ProductRecord | None: ...  # pragma: no cover - protocol


CheckoutHook = Callable[[Actor, Cart], None]
"""Called before the transaction (HardwareGuard plugs in here); raise to block the checkout."""


# ----------------------------------------------------------------------------- service
class BillingService:
    """Cart operations and checkout."""

    def __init__(
        self,
        txn: TxnFactory,
        store: BillingStore,
        products: ProductLookup,
        stock: StockStore,
        catalog: CatalogService,
        offers: OfferService,
        ledger: LedgerService,
        audit: AuditService,
        *,
        settings: Callable[[], Settings],
        event_bus: EventBus = bus,
    ) -> None:
        self._txn = txn
        self._store = store
        self._products = products
        self._stock = stock
        self._catalog = catalog
        self._offers = offers
        self._ledger = ledger
        self._audit = audit
        self._settings = settings
        self._bus = event_bus
        self._hooks: list[CheckoutHook] = []
        self._open_item_pid: int | None = None

    # -- wiring -----------------------------------------------------------------------------------
    def add_checkout_hook(self, hook: CheckoutHook) -> None:
        """Register a pre-checkout check (hardware guard, shift rules)."""
        self._hooks.append(hook)

    @property
    def open_item_product_id(self) -> int:
        """The system ``OPEN-ITEM`` product id (cached)."""
        if self._open_item_pid is None:
            with self._txn(label="billing.open_item_pid") as session:
                self._open_item_pid = self._store.open_item_product_id(session)
        return self._open_item_pid

    def engine(self) -> TaxEngine:
        """TaxEngine for the current settings."""
        s = self._settings()
        return TaxEngine(enabled=s.tax.registered, rounding=s.billing.tax_rounding)

    # -- cart lifecycle ---------------------------------------------------------------------------
    def new_cart(self, actor: Actor) -> Cart:
        """A fresh cart with a new ``client_txn_id``."""
        cart = Cart(client_txn_id=uuid.uuid4().hex, user_id=actor.user_id, created_at=iso_utc())
        log.debug("CART_NEW txn=%s user=%s", cart.client_txn_id, actor.user_id)
        return cart

    def totals(self, cart: Cart) -> BillTax:
        """Live totals (empty cart → zeros)."""
        if cart.is_empty:
            return BillTax(lines=[], subtotal_paise=0, item_discount_paise=0, bill_discount_paise=0, total_savings_paise=0, taxable_paise=0, cgst_paise=0, sgst_paise=0, roundoff_paise=0, total_paise=0)
        bp, paise = cart.bill_discount_bp, cart.bill_discount_paise
        if not bp and not paise:
            best = self._best_bill_offer()
            cart.bill_offer = best
            if best is not None:
                if best.type == "PCT":
                    bp = int((best.value * 100).to_integral_value())
                elif best.type == "AMOUNT":
                    paise = int((best.value * 100).to_integral_value())
        return self.engine().bill(BillInput(lines=[ln.tax_input() for ln in cart.lines], bill_discount_bp=bp, bill_discount_paise=paise))

    # -- scanning ---------------------------------------------------------------------------------
    def lookup_scan(self, code: str) -> ScanResult | None:
        """Resolve a scanned code (scale codes first, then the barcode table); None when unknown."""
        fmt = self._scale_format()
        scale = decode_scale_barcode(code, fmt)
        if scale is not None:
            product = self._catalog.get(scale.plu)
            if product is not None and _sellable(product):
                spec = spec_for(product)
                qty = scale.weight_base_units if scale.weight_base_units is not None else qty_for_amount(spec, amount_paise=int(scale.amount_paise or 0), price_paise=product.price_paise)
                log.debug("SCAN_SCALE_CODE code=%s product=%s qty=%s", code, product.sku, qty)
                return ScanResult(product, scale.raw, Decimal("1"), spec, needs_qty=False, suggested_qty=qty, qty_source="EMBEDDED_BARCODE")
            log.debug("SCAN_SCALE_CODE_UNKNOWN_PLU code=%s plu=%s", code, scale.plu)
        hit = self._catalog.resolve_barcode(code)
        if hit is None:
            return None
        spec = spec_for(hit.product)
        return ScanResult(hit.product, hit.barcode.barcode, hit.pack_qty, spec, needs_qty=not spec.is_piece, suggested_qty=None if not spec.is_piece else hit.pack_qty, qty_source="SCAN")

    def add_scan(self, actor: Actor, cart: Cart, code: str, *, qty: Decimal | None = None, qty_source: str | None = None) -> CartLine:
        """The normal path. Weighed items need ``qty`` (from the pad or a scale code); unknown → ``E-SCN-002``."""
        require(actor, Permission.SELL)
        scan = self.lookup_scan(code)
        if scan is None:
            log.info("SCAN_UNKNOWN code=%s user=%s", code, actor.user_id)
            raise ScannerError("E-SCN-002", code=code)
        if qty is None:
            if scan.suggested_qty is None:
                raise MeasureError("E-MEAS-001", detail="weighed item scanned without a quantity", minimum=f"{scan.spec.min_qty} {scan.spec.base_unit}")
            qty = scan.suggested_qty
        source = qty_source or scan.qty_source
        qty = validate_qty(scan.spec, qty)
        for ln in cart.lines:
            if ln.product_id == scan.product.id and ln.entry_method == "SCAN" and not ln.adhoc_discount_bp and not ln.adhoc_discount_paise and not ln.is_open_item:
                ln.qty = validate_qty(scan.spec, ln.qty + qty)
                ln.qty_source = source
                ln.offer = self._offers.best_for(scan.product, ln.qty)
                log.debug("CART_LINE_BUMPED line=%s product=%s qty=%s", ln.line_id, ln.sku, ln.qty)
                return ln
        return self._append(cart, scan.product, qty, spec=scan.spec, entry_method="SCAN", qty_source=source, reason=None, barcode=scan.barcode)

    def add_lookup(self, actor: Actor, cart: Cart, product_id: int, *, qty: Decimal, reason: str) -> CartLine:
        """F3 Find → ``LOOKUP`` line (``†`` on the receipt); price and tax locked to the catalogue."""
        require(actor, Permission.SELL)
        product = self._sellable_product(product_id)
        spec = spec_for(product)
        return self._append(cart, product, validate_qty(spec, qty), spec=spec, entry_method="LOOKUP", qty_source="TYPED", reason=reason.strip() or "label damaged", barcode=None)

    def add_quick_pick(self, actor: Actor, cart: Cart, product_id: int, *, qty: Decimal = Decimal("1")) -> CartLine:
        """Tiles — disabled for staff while ``require_scan`` is on (``E-BILL-070``)."""
        require(actor, Permission.SELL)
        if actor.role is Role.STAFF and self._settings().billing.require_scan:
            raise BillingError("E-BILL-070", detail="quick picks are disabled for staff in strict mode")
        product = self._sellable_product(product_id)
        spec = spec_for(product)
        return self._append(cart, product, validate_qty(spec, qty), spec=spec, entry_method="QUICK_PICK", qty_source="TYPED", reason=None, barcode=None)

    def add_open_item(self, actor: Actor, cart: Cart, *, name: str, detail: str, qty: Decimal, unit_price_paise: int, tax_rate_bp: int | None = None, confirm_low_price: bool = False) -> CartLine:
        """Nothing in the catalogue → ``OPEN_ITEM`` line (``*``), governed by ``manual_add_policy`` + cap + under-price check."""
        settings = self._settings()
        require(actor, Permission.OPEN_ITEM, open_item_policy=OpenItemPolicy(settings.billing.manual_add_policy))
        clean = (name or "").strip()
        if not clean:
            raise ValueError("an open item needs a name")
        if qty <= 0:
            raise MeasureError("E-MEAS-001", minimum="0.001")
        if unit_price_paise < 0:
            raise ValueError("price must be >= 0")
        cap = settings.billing.open_item_cap_per_bill
        if actor.role is Role.STAFF and not actor.pin_verified and cart.open_item_count >= cap:
            log.warning("OPEN_ITEM_CAP user=%s cart=%s cap=%d", actor.user_id, cart.client_txn_id, cap)
            raise BillingError("E-BILL-071", cap=str(cap))
        nearest = self._nearest_price(clean)
        if nearest is not None and not confirm_low_price and Decimal(unit_price_paise) < Decimal(nearest[1]) * LOW_PRICE_RATIO:
            log.warning("OPEN_ITEM_LOW_PRICE name=%r price=%d nearest=%r@%d user=%s", clean, unit_price_paise, nearest[0], nearest[1], actor.user_id)
            raise BillingError("E-BILL-072", price=format_inr(unit_price_paise), detail=f"nearest catalogue match {nearest[0]} at {nearest[1]} paise")
        line = CartLine(
            line_id=uuid.uuid4().hex[:8],
            product_id=self.open_item_product_id,
            name=clean,
            name_hi=None,
            sku="OPEN-ITEM",
            unit="pcs",
            qty=qty,
            qty_source="TYPED",
            spec=OPEN_ITEM_SPEC,
            shelf_price_paise=unit_price_paise,
            pricing_unit_qty=Decimal("1"),
            tax_rate_bp=settings.tax.default_rate_bp if tax_rate_bp is None else tax_rate_bp,
            price_includes_tax=settings.tax.prices_include_tax,
            hsn_code=None,
            cost_paise=None,
            entry_method="OPEN_ITEM",
            entry_reason=(detail or "").strip() or None,
            is_open_item=True,
            open_item_name=clean,
            open_item_detail=(detail or "").strip() or None,
        )
        cart.lines.append(line)
        log.info("CART_OPEN_ITEM cart=%s name=%r qty=%s price=%d user=%s pin=%s", cart.client_txn_id, clean, qty, unit_price_paise, actor.user_id, actor.pin_verified)
        return line

    # -- editing ----------------------------------------------------------------------------------
    def set_qty(self, actor: Actor, cart: Cart, line_id: str, qty: Decimal, *, source: str = "TYPED") -> CartLine:
        """Change a quantity (validated against the product's spec); the offer is re-resolved."""
        require(actor, Permission.SELL)
        ln = cart.line(line_id)
        ln.qty = validate_qty(ln.spec, qty)
        ln.qty_source = source
        if ln.product_id is not None and not ln.is_open_item:
            product = self._catalog.get(ln.product_id)
            if product is not None:
                ln.offer = self._offers.best_for(product, ln.qty)
        log.debug("CART_QTY line=%s qty=%s source=%s", line_id, qty, source)
        return ln

    def remove_line(self, actor: Actor, cart: Cart, line_id: str) -> None:
        """Drop a line."""
        require(actor, Permission.SELL)
        ln = cart.line(line_id)
        cart.lines.remove(ln)
        log.debug("CART_LINE_REMOVED line=%s product=%s", line_id, ln.sku)

    def set_line_discount(self, actor: Actor, cart: Cart, line_id: str, *, percent: Decimal | None = None, amount_paise: int | None = None, reason: str, approver_id: int | None = None) -> CartLine:
        """Ad-hoc line discount: reason mandatory; above the cap → Owner PIN (``E-BILL-060``)."""
        require(actor, Permission.SELL)
        if not reason or not reason.strip():
            raise ValueError("a reason is required for a discount")
        ln = cart.line(line_id)
        pct = percent if percent is not None else (Decimal(amount_paise or 0) * 100 / Decimal(ln.gross_paise) if ln.gross_paise else Decimal(0))
        self._check_cap(actor, cart, pct, approver_id)
        ln.adhoc_discount_bp = int((percent * 100).to_integral_value()) if percent is not None else 0
        ln.adhoc_discount_paise = int(amount_paise or 0) if percent is None else 0
        ln.discount_reason = reason.strip()
        log.info("CART_LINE_DISCOUNT line=%s pct=%s amount=%s reason=%r user=%s", line_id, percent, amount_paise, reason, actor.user_id)
        return ln

    def remove_offer(self, actor: Actor, cart: Cart, line_id: str) -> CartLine:
        """Only the Owner (or PIN) may drop an auto-applied offer (``E-BILL-061``)."""
        try:
            require(actor, Permission.DISCOUNT_OVER_CAP)
        except PinRequired as exc:
            raise BillingError("E-BILL-061", detail=str(exc)) from exc
        ln = cart.line(line_id)
        ln.offer_removed = True
        cart.approved_by_user_id = cart.approved_by_user_id or (actor.user_id if actor.role is Role.OWNER else None)
        log.info("CART_OFFER_REMOVED line=%s offer=%s by=%s", line_id, ln.offer.offer.name if ln.offer else None, actor.user_id)
        return ln

    def set_bill_discount(self, actor: Actor, cart: Cart, *, percent: Decimal | None = None, amount_paise: int | None = None, reason: str, approver_id: int | None = None) -> Cart:
        """Bill-level ad-hoc discount (pro-rated across lines by the engine)."""
        require(actor, Permission.SELL)
        if not reason or not reason.strip():
            raise ValueError("a reason is required for a discount")
        net = sum(ln.gross_paise for ln in cart.lines) - sum(ln.active_offer.benefit_paise(ln.gross_paise) if ln.active_offer else 0 for ln in cart.lines)
        pct = percent if percent is not None else (Decimal(amount_paise or 0) * 100 / Decimal(net) if net else Decimal(0))
        self._check_cap(actor, cart, pct, approver_id)
        cart.bill_discount_bp = int((percent * 100).to_integral_value()) if percent is not None else 0
        cart.bill_discount_paise = int(amount_paise or 0) if percent is None else 0
        cart.bill_discount_reason = reason.strip()
        log.info("CART_BILL_DISCOUNT cart=%s pct=%s amount=%s reason=%r user=%s", cart.client_txn_id, percent, amount_paise, reason, actor.user_id)
        return cart

    # -- customer (F8) ----------------------------------------------------------------------------
    def set_customer(self, actor: Actor, cart: Cart, *, phone: str, name: str = "") -> Cart:
        """Attach the customer's mobile number (and name) to the bill; an empty phone detaches.

        The number is normalised to E.164 digits (``98765 43210`` → ``919876543210``) and kept in the
        ``customer`` table, one row per number, so a regular customer is recognised next time.
        """
        require(actor, Permission.SELL)
        if not phone.strip():
            cart.customer_id = cart.customer_phone = cart.customer_name = None
            log.info("CART_CUSTOMER_CLEARED cart=%s", cart.client_txn_id)
            return cart
        digits = normalise_phone(phone)
        with self._txn(label="billing.customer") as session:
            record = self._store.upsert_customer(session, phone=digits, name=name.strip() or None)
        cart.customer_id, cart.customer_phone, cart.customer_name = record.id, record.phone, record.name
        log.info("CART_CUSTOMER_SET cart=%s customer=%s phone=…%s", cart.client_txn_id, record.id, digits[-4:])
        return cart

    def customer_of(self, sale: SaleView) -> CustomerRecord | None:
        """The customer linked to a committed sale (None when the cashier skipped F8)."""
        if sale.customer_id is None:
            return None
        with self._txn(label="billing.customer_of") as session:
            return self._store.customer(session, sale.customer_id)

    def remember_customer(self, actor: Actor, *, phone: str, name: str = "") -> CustomerRecord:
        """Phone typed *after* the bill (the customer asked for the bill on WhatsApp at the end): store it without touching the sale."""
        require(actor, Permission.SELL)
        with self._txn(label="billing.customer") as session:
            return self._store.upsert_customer(session, phone=normalise_phone(phone), name=name.strip() or None)

    # -- hold / resume ----------------------------------------------------------------------------
    def hold(self, actor: Actor, cart: Cart, *, label: str | None = None) -> int:
        """Park the cart (F6) — serialised to ``held_cart``; the ``client_txn_id`` survives."""
        require(actor, Permission.HOLD_RESUME_REPRINT)
        if cart.is_empty:
            raise ValueError("nothing to hold")
        payload = json.dumps(_encode(cart), ensure_ascii=False)
        with self._txn(label="billing.hold") as session:
            held_id = self._store.insert_held(session, user_id=actor.user_id, label=label, payload=payload, line_count=len(cart.lines), created_at=iso_utc())
        log.info("CART_HELD id=%s cart=%s lines=%d label=%r user=%s", held_id, cart.client_txn_id, len(cart.lines), label, actor.user_id)
        return held_id

    def held_carts(self, actor: Actor) -> list[HeldCartRecord]:
        """Own held carts (Owner: everyone's)."""
        with self._txn(label="billing.held") as session:
            return self._store.held_carts(session, user_id=None if actor.role is Role.OWNER else actor.user_id)

    def resume(self, actor: Actor, held_id: int) -> Cart:
        """Bring a held cart back (and forget the held row)."""
        require(actor, Permission.HOLD_RESUME_REPRINT)
        with self._txn(label="billing.resume") as session:
            record = self._store.held_cart(session, held_id)
            if record is None:
                raise ValueError(f"held cart {held_id} not found")
            if actor.role is not Role.OWNER and record.user_id != actor.user_id:
                raise ValueError("that cart belongs to another cashier")
            self._store.delete_held(session, held_id)
        cart = _decode_cart(json.loads(record.payload))
        log.info("CART_RESUMED id=%s cart=%s lines=%d user=%s", held_id, cart.client_txn_id, len(cart.lines), actor.user_id)
        return cart

    # -- checkout ---------------------------------------------------------------------------------
    def checkout(self, actor: Actor, cart: Cart, payments: list[PaymentInput]) -> CheckoutResult:
        """Commit the bill. See the module docstring for the contract."""
        require(actor, Permission.SELL)
        if cart.is_empty:
            raise BillingError("E-BILL-050", detail="empty cart", short="everything")
        for mode in (p.mode for p in payments):
            if mode not in PAYMENT_MODES:
                raise ValueError(f"unknown payment mode {mode}")
        for hook in self._hooks:
            hook(actor, cart)
        totals = self.totals(cart)
        paid = sum(p.amount_paise for p in payments)
        if paid < totals.total_paise:
            raise BillingError("E-BILL-050", short=format_inr(totals.total_paise - paid))
        settings = self._settings()
        warnings: list[str] = []
        events: list[Any] = []
        sold_at = iso_utc(now_utc())
        fy = financial_year(today_ist())
        try:
            with self._txn(label="billing.checkout") as session:
                existing = self._store.sale_by_client_txn(session, cart.client_txn_id)
                if existing is not None:
                    cart.committed_sale_id = existing.id
                    return self._replay(existing)
                shift_id = self._store.open_shift_id(session, actor.user_id)
                if shift_id is None:
                    raise BillingError("E-BILL-042", detail=f"user {actor.user_id} has no open shift")
                seq = self._store.next_invoice_seq(session, fy)
                invoice_no = f"{settings.receipt.invoice_prefix}/{fy}/{seq:06d}"
                public_id = uuid.uuid4().hex[:16]
                record = SaleRecord(
                    id=0, invoice_no=invoice_no, client_txn_id=cart.client_txn_id, fin_year=fy, seq_no=seq, sold_at=sold_at, user_id=actor.user_id, shift_id=shift_id,
                    status="COMPLETED", original_sale_id=None, subtotal_paise=totals.subtotal_paise, item_discount_paise=totals.item_discount_paise,
                    bill_discount_paise=totals.bill_discount_paise, taxable_paise=totals.taxable_paise, cgst_paise=totals.cgst_paise, sgst_paise=totals.sgst_paise,
                    roundoff_paise=totals.roundoff_paise, total_paise=totals.total_paise,
                    items=tuple(SaleItemRecord(ln.product_id, canonical_qty(ln.qty), ln.unit, ln.shelf_price_paise, (ln.active_offer.offer.id if ln.active_offer and lt.offer_discount_paise else None), lt.discount_paise, lt.unit_price_paise, lt.tax_rate_bp, ln.entry_method, ln.is_open_item, ln.open_item_name) for ln, lt in zip(cart.lines, totals.lines, strict=True)),
                    payments=tuple(PaymentRecord(p.mode, p.amount_paise) for p in payments),
                )
                hashed = self._ledger.hash_for_new_sale(session, record)
                sale_id = self._store.insert_sale(session, {
                    "invoice_no": invoice_no, "client_txn_id": cart.client_txn_id, "public_id": public_id, "fin_year": fy, "seq_no": seq, "sold_at": sold_at,
                    "user_id": actor.user_id, "shift_id": shift_id, "customer_id": cart.customer_id, "status": "COMPLETED", "original_sale_id": None,
                    "subtotal_paise": totals.subtotal_paise, "item_discount_paise": totals.item_discount_paise, "bill_discount_paise": totals.bill_discount_paise,
                    "bill_discount_reason": cart.bill_discount_reason or (cart.bill_offer.name if cart.bill_offer and totals.bill_discount_paise else None),
                    "total_savings_paise": totals.total_savings_paise, "approved_by_user_id": cart.approved_by_user_id, "taxable_paise": totals.taxable_paise,
                    "cgst_paise": totals.cgst_paise, "sgst_paise": totals.sgst_paise, "roundoff_paise": totals.roundoff_paise, "total_paise": totals.total_paise,
                    "reprint_count": 0, "receipt_printed": 0, "prev_hash": hashed.prev_hash, "hash": hashed.hash, "note": cart.note,
                })
                manual = 0
                for ln, lt, item in zip(cart.lines, totals.lines, record.items, strict=True):
                    self._store.insert_sale_item(session, {
                        "sale_id": sale_id, "product_id": ln.product_id, "name_snapshot": ln.name, "hsn_snapshot": ln.hsn_code, "qty": str(ln.qty), "unit": ln.unit,
                        "qty_source": ln.qty_source, "pricing_unit_qty_snapshot": str(ln.pricing_unit_qty), "shelf_price_paise": ln.shelf_price_paise,
                        "offer_id": item.offer_id, "offer_name_snapshot": lt.offer_name, "discount_bp": lt.discount_bp, "discount_paise": lt.discount_paise,
                        "discount_reason": ln.discount_reason, "unit_price_paise": lt.unit_price_paise, "tax_rate_bp": lt.tax_rate_bp, "taxable_paise": lt.taxable_paise,
                        "cgst_paise": lt.cgst_paise, "sgst_paise": lt.sgst_paise, "line_total_paise": lt.line_total_paise, "cost_paise_snapshot": ln.cost_paise,
                        "entry_method": ln.entry_method, "entry_reason": ln.entry_reason, "scanned_barcode": ln.scanned_barcode, "is_open_item": int(ln.is_open_item),
                        "open_item_name": ln.open_item_name, "open_item_detail": ln.open_item_detail,
                    })
                    if ln.is_open_item:
                        manual += 1
                        self._audit.record(session, "OPEN_ITEM_LINE", user_id=actor.user_id, entity="sale", entity_id=sale_id, after={"name": ln.open_item_name, "qty": str(ln.qty), "price_paise": ln.shelf_price_paise, "pin": actor.pin_verified})
                        continue
                    if ln.entry_method == "LOOKUP":
                        manual += 1
                        self._audit.record(session, "MANUAL_LINE", user_id=actor.user_id, entity="sale", entity_id=sale_id, after={"product_id": ln.product_id, "reason": ln.entry_reason})
                    assert ln.product_id is not None
                    event, warning = self._move_stock(session, actor, ln, sale_id, sold_at, settings.billing.negative_stock_policy)
                    events.append(event)
                    if warning:
                        warnings.append(warning)
                for p in payments:
                    change = max(0, (p.tendered_paise or 0) - p.amount_paise) if p.mode == "CASH" and p.tendered_paise is not None else 0
                    self._store.insert_payment(session, {"sale_id": sale_id, "mode": p.mode, "amount_paise": p.amount_paise, "tendered_paise": p.tendered_paise, "change_paise": change, "reference": p.reference})
                if cart.approved_by_user_id is not None:
                    self._audit.record(session, "DISCOUNT_OVERRIDE", user_id=actor.user_id, actor_user_id=cart.approved_by_user_id, entity="sale", entity_id=sale_id, after={"item_discount_paise": totals.item_discount_paise, "bill_discount_paise": totals.bill_discount_paise, "offers_removed": [ln.offer.offer.name for ln in cart.lines if ln.offer_removed and ln.offer]})
                self._audit.record(session, "SALE_CREATED", user_id=actor.user_id, entity="sale", entity_id=sale_id, after={"invoice_no": invoice_no, "total_paise": totals.total_paise, "lines": len(cart.lines), "manual_lines": manual, "payments": [(p.mode, p.amount_paise) for p in payments]})
                view = self._store.sale(session, sale_id)
        except Exception as exc:
            if "client_txn_id" in str(exc) and "UNIQUE" in str(exc).upper():
                # Two clicks raced into two transactions; the second lost — return the winner.
                with self._txn(label="billing.replay") as session:
                    existing = self._store.sale_by_client_txn(session, cart.client_txn_id)
                if existing is not None:
                    cart.committed_sale_id = existing.id
                    return self._replay(existing)
            raise
        assert view is not None
        cart.committed_sale_id = sale_id
        for event in events:
            self._bus.publish(event)
        self._bus.publish(SaleCommitted(sale_id=sale_id, invoice_no=invoice_no, total_paise=totals.total_paise, user_id=actor.user_id, line_count=len(cart.lines)))
        change = sum(p.change_paise for p in view.payments) + (paid - totals.total_paise)
        log.info("SALE_COMMITTED id=%s invoice=%s total=%d paid=%d change=%d lines=%d manual=%d user=%s shift=%s warnings=%d", sale_id, invoice_no, totals.total_paise, paid, change, len(cart.lines), manual, actor.user_id, shift_id, len(warnings))
        return CheckoutResult(sale_id, invoice_no, public_id, totals.total_paise, paid, change, False, view, warnings)

    # -- void / refund ----------------------------------------------------------------------------
    def void_status(self, sale_id: int, *, actor: Actor | None = None) -> VoidInfo:
        """Can this bill still be voided / refunded (and how much per line)?"""
        with self._txn(label="billing.void_status") as session:
            sale = self._store.sale(session, sale_id)
            if sale is None:
                raise ValueError(f"sale {sale_id} not found")
            related = self._store.related_sales(session, sale_id)
        refundable = {it.id: it.qty for it in sale.items}
        for note in related:
            for it in note.items:
                for orig in sale.items:
                    if orig.product_id == it.product_id and orig.name_snapshot == it.name_snapshot and refundable.get(orig.id, Decimal(0)) > 0:
                        refundable[orig.id] = max(Decimal(0), refundable[orig.id] - it.qty)
                        break
        code: str | None = None
        if sale.status != "COMPLETED" or any(n.status == "VOIDED" for n in related):
            code = "E-BILL-092"
        elif actor is not None and actor.role is not Role.OWNER and self._outside_window(sale.sold_at):
            code = "E-BILL-090"
        has_refunds = any(n.status == "REFUND" for n in related)
        anything_left = any(q > 0 for q in refundable.values())
        return VoidInfo(sale.id, sale.status, can_void=code is None and not has_refunds, can_refund=code is None and anything_left, refundable=refundable, reason_code=code, related=related)

    def void(self, actor: Actor, sale_id: int, *, reason: str, approver_id: int | None = None) -> CheckoutResult:
        """Full reversal → a ``VOIDED`` credit note, ``SALE_VOID`` movements, cash out of the actor's drawer."""
        with self._txn(label="billing.void_read") as session:
            sale = self._store.sale(session, sale_id)
        if sale is None:
            raise ValueError(f"sale {sale_id} not found")
        return self._reverse(actor, sale, [(it, it.qty) for it in sale.items], status="VOIDED", reason=reason, approver_id=approver_id)

    def refund(self, actor: Actor, sale_id: int, lines: list[RefundLine], *, reason: str, approver_id: int | None = None) -> CheckoutResult:
        """Partial reversal → a ``REFUND`` credit note, ``RETURN`` movements."""
        if not lines:
            raise ValueError("choose at least one line to refund")
        with self._txn(label="billing.refund_read") as session:
            sale = self._store.sale(session, sale_id)
        if sale is None:
            raise ValueError(f"sale {sale_id} not found")
        by_id = {it.id: it for it in sale.items}
        chosen: list[tuple[SaleItemView, Decimal]] = []
        for ln in lines:
            item = by_id.get(ln.sale_item_id)
            if item is None:
                raise ValueError(f"line {ln.sale_item_id} is not on bill {sale.invoice_no}")
            if ln.qty <= 0:
                raise ValueError("refund quantity must be > 0")
            chosen.append((item, ln.qty))
        return self._reverse(actor, sale, chosen, status="REFUND", reason=reason, approver_id=approver_id)

    def _outside_window(self, sold_at: str) -> bool:
        hours = self._settings().billing.void_window_hours
        return (now_utc() - parse_iso_utc(sold_at)).total_seconds() > hours * 3600

    def _reverse(self, actor: Actor, original: SaleView, lines: list[tuple[SaleItemView, Decimal]], *, status: str, reason: str, approver_id: int | None) -> CheckoutResult:
        require(actor, Permission.VOID_REFUND)
        owner_id = actor.user_id if actor.role is Role.OWNER else approver_id
        if owner_id is None:
            raise ValueError("approver_id (the Owner who entered the PIN) is required")
        if not reason or not reason.strip():
            raise ValueError("a reason is required")
        info = self.void_status(original.id, actor=actor)
        if info.reason_code == "E-BILL-090":
            raise BillingError("E-BILL-090", hours=str(self._settings().billing.void_window_hours), detail=f"sold_at={original.sold_at}")
        if info.reason_code is not None or (status == "VOIDED" and not info.can_void):
            raise BillingError("E-BILL-092", invoice_no=original.invoice_no)
        for item, qty in lines:
            left = info.refundable.get(item.id, Decimal(0))
            if qty > left:
                raise BillingError("E-BILL-091", name=item.name_snapshot, available=str(left))
        settings = self._settings()
        sold_at = iso_utc(now_utc())
        fy = financial_year(today_ist())
        events: list[Any] = []
        with self._txn(label="billing.reverse") as session:
            shift_id = self._store.open_shift_id(session, actor.user_id)
            if shift_id is None:
                raise BillingError("E-BILL-042", detail=f"user {actor.user_id} has no open shift")
            seq = self._store.next_invoice_seq(session, fy)
            invoice_no = f"{settings.receipt.invoice_prefix}/{fy}/{seq:06d}"
            public_id = uuid.uuid4().hex[:16]
            parts = [_scale_item(item, qty) for item, qty in lines]
            subtotal = sum(p["gross"] for p in parts)
            item_discount = sum(p["discount_paise"] for p in parts)
            line_total_sum = sum(p["line_total_paise"] for p in parts)
            orig_lines_total = sum(it.line_total_paise for it in original.items) or 1
            bill_share = int((Decimal(original.bill_discount_paise) * line_total_sum / orig_lines_total).to_integral_value()) if original.bill_discount_paise else 0
            taxable = sum(p["taxable_paise"] for p in parts)
            cgst = sum(p["cgst_paise"] for p in parts)
            sgst = sum(p["sgst_paise"] for p in parts)
            total, roundoff = round_off(line_total_sum - bill_share) if settings.billing.tax_rounding == "NEAREST_RUPEE" else (line_total_sum - bill_share, 0)
            record = SaleRecord(
                id=0, invoice_no=invoice_no, client_txn_id=uuid.uuid4().hex, fin_year=fy, seq_no=seq, sold_at=sold_at, user_id=actor.user_id, shift_id=shift_id,
                status=status, original_sale_id=original.id, subtotal_paise=subtotal, item_discount_paise=item_discount, bill_discount_paise=bill_share,
                taxable_paise=taxable, cgst_paise=cgst, sgst_paise=sgst, roundoff_paise=roundoff, total_paise=total,
                items=tuple(SaleItemRecord(p["product_id"], canonical_qty(p["qty"]), p["unit"], p["shelf_price_paise"], p["offer_id"], p["discount_paise"], p["unit_price_paise"], p["tax_rate_bp"], p["entry_method"], p["is_open_item"], p["open_item_name"]) for p in parts),
                payments=(PaymentRecord("CASH", total),),
            )
            hashed = self._ledger.hash_for_new_sale(session, record)
            sale_id = self._store.insert_sale(session, {
                "invoice_no": invoice_no, "client_txn_id": record.client_txn_id, "public_id": public_id, "fin_year": fy, "seq_no": seq, "sold_at": sold_at,
                "user_id": actor.user_id, "shift_id": shift_id, "customer_id": original.customer_id, "status": status, "original_sale_id": original.id,
                "subtotal_paise": subtotal, "item_discount_paise": item_discount, "bill_discount_paise": bill_share, "bill_discount_reason": original.bill_discount_reason,
                "total_savings_paise": item_discount + bill_share, "approved_by_user_id": owner_id, "taxable_paise": taxable, "cgst_paise": cgst, "sgst_paise": sgst,
                "roundoff_paise": roundoff, "total_paise": total, "reprint_count": 0, "receipt_printed": 0, "prev_hash": hashed.prev_hash, "hash": hashed.hash, "note": reason.strip(),
            })
            for p in parts:
                self._store.insert_sale_item(session, {k: v for k, v in p.items() if k not in ("gross", "qty")} | {"sale_id": sale_id, "qty": str(p["qty"])})
                if p["is_open_item"]:
                    continue
                pid = int(p["product_id"])
                before = self._stock.stock_of(session, pid)
                after = before + p["qty"]
                self._stock.insert_movement(session, product_id=pid, type="SALE_VOID" if status == "VOIDED" else "RETURN", qty_delta=p["qty"], qty_after=after, unit_cost_paise=p["cost_paise_snapshot"], ref_type="SALE", ref_id=sale_id, reason=reason.strip(), user_id=owner_id, created_at=sold_at)
                events.append(StockChanged(product_id=pid, movement_type="SALE_VOID" if status == "VOIDED" else "RETURN", qty_delta=int(p["qty"]), qty_after=int(after), product_name=str(p["name_snapshot"])))
            self._store.insert_payment(session, {"sale_id": sale_id, "mode": "CASH", "amount_paise": total, "tendered_paise": None, "change_paise": 0, "reference": f"refund of {original.invoice_no}"})
            self._audit.record(session, "SALE_VOIDED" if status == "VOIDED" else "REFUND", user_id=actor.user_id, actor_user_id=owner_id, entity="sale", entity_id=original.id, before={"invoice_no": original.invoice_no, "total_paise": original.total_paise}, after={"credit_note": invoice_no, "total_paise": total, "reason": reason.strip(), "lines": len(parts)})
            view = self._store.sale(session, sale_id)
        assert view is not None
        for event in events:
            self._bus.publish(event)
        self._bus.publish(SaleCommitted(sale_id=sale_id, invoice_no=invoice_no, total_paise=total, user_id=actor.user_id, line_count=len(parts)))
        log.warning("%s original=%s credit_note=%s total=%d by=%s owner=%s reason=%r", status, original.invoice_no, invoice_no, total, actor.user_id, owner_id, reason)
        return CheckoutResult(sale_id, invoice_no, public_id, total, total, 0, False, view)

    # -- reads ------------------------------------------------------------------------------------
    def get_sale(self, sale_id: int) -> SaleView | None:
        """Full sale."""
        with self._txn(label="billing.sale") as session:
            return self._store.sale(session, sale_id)

    def find_sale(self, text: str) -> SaleView | None:
        """By invoice number or public id (receipt barcode)."""
        clean = (text or "").strip()
        if not clean:
            return None
        with self._txn(label="billing.find") as session:
            return self._store.find_sale(session, clean)

    def recent_sales(self, *, limit: int = 50, user_id: int | None = None) -> list[SaleView]:
        """Newest first."""
        with self._txn(label="billing.recent") as session:
            return self._store.recent_sales(session, user_id=user_id, limit=limit)

    def search_sales(self, text: str, *, limit: int = 50, user_id: int | None = None) -> list[SaleView]:
        """Invoice number fragment / public id / receipt code, newest first."""
        clean = (text or "").strip()
        with self._txn(label="billing.search") as session:
            if not clean:
                return self._store.recent_sales(session, user_id=user_id, limit=limit)
            if parse_receipt_ean13(clean) is not None:
                one = self._store.find_sale(session, clean)
                return [one] if one is not None and (user_id is None or one.user_id == user_id) else []
            return self._store.search_sales(session, text=clean, user_id=user_id, limit=limit)

    def today_summary(self, *, user_id: int | None = None) -> DaySummary:
        """Bills / rupees / items today (shop day in IST)."""
        day = today_ist()
        start, end = business_day_bounds(day)
        with self._txn(label="billing.today") as session:
            bills, total, items = self._store.day_summary(session, user_id=user_id, start=iso_utc(start), end=iso_utc(end))
        return DaySummary(day.isoformat(), user_id, bills, total, items)

    def next_invoice_preview(self) -> str:
        """The number the *next* bill will most likely get (informational — the counter decides at commit)."""
        fy = financial_year(today_ist())
        with self._txn(label="billing.peek") as session:
            seq = self._store.peek_invoice_seq(session, fy) + 1
        return f"{self._settings().receipt.invoice_prefix}/{fy}/{seq:06d}"

    # -- internals --------------------------------------------------------------------------------
    def _append(self, cart: Cart, product: ProductRecord, qty: Decimal, *, spec: QuantitySpec, entry_method: str, qty_source: str, reason: str | None, barcode: str | None) -> CartLine:
        line = CartLine(
            line_id=uuid.uuid4().hex[:8], product_id=product.id, name=product.name, name_hi=product.name_hi, sku=product.sku, unit=product.base_unit, qty=qty, qty_source=qty_source,
            spec=spec, shelf_price_paise=product.price_paise, pricing_unit_qty=product.pricing_unit_qty, tax_rate_bp=product.tax_rate_bp, price_includes_tax=product.price_includes_tax,
            hsn_code=product.hsn_code, cost_paise=product.cost_paise, entry_method=entry_method, entry_reason=reason, scanned_barcode=barcode, offer=self._offers.best_for(product, qty),
        )
        cart.lines.append(line)
        log.debug("CART_LINE_ADDED line=%s product=%s qty=%s via=%s offer=%s", line.line_id, product.sku, qty, entry_method, line.offer.offer.name if line.offer else None)
        return line

    def _sellable_product(self, product_id: int) -> ProductRecord:
        product = self._catalog.get(product_id)
        if product is None or not _sellable(product):
            raise ScannerError("E-SCN-002", code=f"#{product_id}")
        return product

    def _check_cap(self, actor: Actor, cart: Cart, pct: Decimal, approver_id: int | None) -> None:
        cap = Decimal(self._settings().billing.adhoc_discount_cap_percent)
        if pct <= cap:
            return
        try:
            require(actor, Permission.DISCOUNT_OVER_CAP)
        except PinRequired as exc:
            log.warning("DISCOUNT_OVER_CAP_BLOCKED pct=%s cap=%s user=%s", pct, cap, actor.user_id)
            raise BillingError("E-BILL-060", cap=f"{cap} %", detail=str(exc)) from exc
        cart.approved_by_user_id = approver_id or (actor.user_id if actor.role is Role.OWNER else cart.approved_by_user_id)
        log.info("DISCOUNT_OVER_CAP_APPROVED pct=%s cap=%s user=%s approver=%s", pct, cap, actor.user_id, cart.approved_by_user_id)

    def _nearest_price(self, name: str) -> tuple[str, int] | None:
        for hit in self._catalog.search(name, limit=3):
            if hit.product.price_paise > 0 and not hit.product.is_system:
                return hit.product.name, hit.product.price_paise
        return None

    def _best_bill_offer(self) -> OfferRecord | None:
        offers = [o for o in self._offers.bill_offers() if o.type in ("PCT", "AMOUNT")]
        if not offers:
            return None
        return max(offers, key=lambda o: (o.type == "PCT", o.value))

    def _scale_format(self) -> ScaleCodeFormat | None:
        text = self._settings().hardware.scale_barcode_format
        if not text:
            return None
        try:
            return ScaleCodeFormat.parse(text)
        except ValueError as exc:
            log.warning("bad hardware.scale_barcode_format %r: %s", text, exc)
            return None

    def _move_stock(self, session: Session, actor: Actor, ln: CartLine, sale_id: int, at: str, policy: str) -> tuple[StockChanged, str | None]:
        assert ln.product_id is not None
        before = self._stock.stock_of(session, ln.product_id)
        after = before - ln.qty
        warning: str | None = None
        if after < 0:
            if policy == "BLOCK":
                log.warning("negative_stock policy=BLOCK product=%s have=%s want=%s → E-INV-003", ln.sku, before, ln.qty)
                raise InventoryError("E-INV-003", name=ln.name, available=str(before), requested=str(ln.qty))
            warning = f"E-INV-003 {ln.name}: stock {before} → {after}"
            log.warning("negative_stock policy=%s product=%s have=%s want=%s", policy, ln.sku, before, ln.qty)
        self._stock.insert_movement(session, product_id=ln.product_id, type="SALE", qty_delta=-ln.qty, qty_after=after, unit_cost_paise=ln.cost_paise, ref_type="SALE", ref_id=sale_id, reason=None, user_id=actor.user_id, created_at=at)
        product = self._products.product_by_id(session, ln.product_id)
        low = bool(product and product.min_stock > 0 and after <= product.min_stock < before)
        return StockChanged(product_id=ln.product_id, movement_type="SALE", qty_delta=int(-ln.qty), qty_after=int(after), low_stock=low, product_name=ln.name), warning

    @staticmethod
    def _replay(existing: SaleView) -> CheckoutResult:
        log.info("CHECKOUT_REPLAY invoice=%s code=E-BILL-080", existing.invoice_no)
        return CheckoutResult(existing.id, existing.invoice_no, existing.public_id, existing.total_paise, existing.paid_paise, existing.change_paise, True, existing)


# ----------------------------------------------------------------------------- helpers
def _sellable(product: ProductRecord) -> bool:
    return product.is_active and product.deleted_at is None and not product.is_system


def _scale_item(item: SaleItemView, qty: Decimal) -> dict[str, Any]:
    """The ``sale_item`` columns for ``qty`` of a sold line, amounts scaled proportionally (half-up)."""
    ratio = qty / item.qty if item.qty else Decimal(0)

    def part(paise: int) -> int:
        return int((Decimal(paise) * ratio).to_integral_value(rounding=ROUND_HALF_UP))

    gross = multiply(item.shelf_price_paise, qty / item.pricing_unit_qty_snapshot)
    return {
        "product_id": item.product_id, "name_snapshot": item.name_snapshot, "hsn_snapshot": item.hsn_snapshot, "qty": qty, "unit": item.unit, "qty_source": item.qty_source,
        "pricing_unit_qty_snapshot": str(item.pricing_unit_qty_snapshot), "shelf_price_paise": item.shelf_price_paise, "offer_id": item.offer_id, "offer_name_snapshot": item.offer_name_snapshot,
        "discount_bp": item.discount_bp, "discount_paise": part(item.discount_paise), "discount_reason": item.discount_reason, "unit_price_paise": item.unit_price_paise, "tax_rate_bp": item.tax_rate_bp,
        "taxable_paise": part(item.taxable_paise), "cgst_paise": part(item.cgst_paise), "sgst_paise": part(item.sgst_paise), "line_total_paise": part(item.line_total_paise), "cost_paise_snapshot": item.cost_paise_snapshot,
        "entry_method": item.entry_method, "entry_reason": item.entry_reason, "scanned_barcode": item.scanned_barcode, "is_open_item": int(item.is_open_item), "open_item_name": item.open_item_name, "open_item_detail": item.open_item_detail,
        "gross": gross,
    }


def normalise_phone(text: str) -> str:
    """``98765 43210`` · ``098765-43210`` · ``+91 98765 43210`` · ``919876543210`` → ``919876543210`` (E.164 digits, India default).

    Raises ``ValueError`` with a plain sentence for anything that is not a 10-digit Indian mobile number
    (or an already country-coded 11–15 digit number).
    """
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        if digits[0] not in "6789":
            raise ValueError("mobile numbers start with 6, 7, 8 or 9")
        return "91" + digits
    if 11 <= len(digits) <= 15 and not digits.startswith("0"):
        return digits
    raise ValueError("enter a 10-digit mobile number (or with country code, e.g. +91 98765 43210)")


def _encode(obj: Any) -> Any:
    """Dataclasses → JSON-able dicts; Decimal → ``{"$d": "…"}``."""
    if isinstance(obj, Decimal):
        return {"$d": str(obj)}
    if hasattr(obj, "__dataclass_fields__"):
        return _encode(asdict(obj))
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    return obj


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj) == {"$d"}:
            return Decimal(obj["$d"])
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def _decode_cart(data: dict[str, Any]) -> Cart:
    raw = _decode(data)
    lines = []
    for ld in raw.pop("lines", []):
        spec_d = ld.pop("spec")
        spec = QuantitySpec(**{**spec_d, "presets": tuple(spec_d["presets"])})
        offer_d = ld.pop("offer")
        offer = None
        if offer_d is not None:
            offer = OfferMatch(offer=OfferRecord(**offer_d["offer"]), discount_bp=offer_d["discount_bp"], discount_paise=offer_d["discount_paise"], stackable=offer_d["stackable"])
        lines.append(CartLine(spec=spec, offer=offer, **ld))
    bill_offer = raw.pop("bill_offer")
    cart = Cart(lines=lines, bill_offer=OfferRecord(**bill_offer) if bill_offer else None, **raw)
    return replace(cart)
