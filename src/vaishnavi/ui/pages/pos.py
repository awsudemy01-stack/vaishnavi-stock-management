"""``/pos`` — the billing screen (docs/09 §3, docs/05 §8–10, mock-up ``docs/mockups/pos.html``).

Everything the screen shows is computed by framework-free builders in this module (tested); the
NiceGUI code at the bottom only lays the results out and forwards clicks and keys. One bill per cashier
lives in :class:`CartRegistry` so a page reload never loses the customer's items; a committed cart is
replaced automatically.

Scan box rule: the browser measures how fast the code arrived — a HID scanner "types" 13 digits in well
under 100 ms, a human cannot — and :func:`classify_entry` decides *scanned* vs *typed*. In strict mode
a cashier's typed code is rejected (``E-BILL-070``) and pointed to **F3 Find**; the Owner may type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from typing import Any, Final

from vaishnavi.core.barcode import internal_ean13, parse_receipt_ean13
from vaishnavi.core.errors import BillingError, MeasureError, PrinterError, ScannerError, VaishnaviError
from vaishnavi.core.money import format_inr, parse_inr
from vaishnavi.core.permissions import Actor, Grant, OpenItemPolicy, Permission, PermissionDenied, PinRequired, Role, grant_for
from vaishnavi.db.records import ProductRecord, SaleView
from vaishnavi.services.billing import Cart, CartLine, CheckoutResult, PaymentInput, ScanResult
from vaishnavi.services.measure import format_qty, line_amount, qty_for_amount, round_to_step, spec_for
from vaishnavi.services.tax import BillTax
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.pos")

SCAN_MS_PER_CHAR: Final = 40
"""Slower than this per character = a human typed it."""
MINUS: Final = "\u2212"
DOT: Final = " \u00b7 "
LOOKUP_REASONS: Final = ("label_damaged", "label_missing", "not_labelled")
DISCOUNT_REASONS: Final = ("damaged_pack", "regular", "bargaining", "price_match", "other")
QUICK_NOTES: Final = (10000, 50000)
"""₹100 and ₹500 notes offered as one-tap cash amounts."""


# ----------------------------------------------------------------------------- cart registry
class CartRegistry:
    """One open bill per cashier (held bills live in the database)."""

    def __init__(self) -> None:
        self._carts: dict[int, Cart] = {}

    def get(self, billing: Any, actor: Actor) -> Cart:
        """The cashier's current cart; a committed one is replaced by a fresh bill."""
        cart = self._carts.get(actor.user_id)
        if cart is None or cart.committed_sale_id is not None:
            cart = billing.new_cart(actor)
            self._carts[actor.user_id] = cart
        return cart

    def put(self, actor: Actor, cart: Cart) -> None:
        """Make a (resumed) cart current."""
        self._carts[actor.user_id] = cart

    def reset(self, actor: Actor) -> None:
        """Forget the current bill (Esc / after hold)."""
        self._carts.pop(actor.user_id, None)


# ----------------------------------------------------------------------------- view-models
@dataclass(frozen=True)
class CartRow:
    """One line of the bill table."""

    line_id: str
    name: str
    sub: str
    qty_text: str
    shelf_text: str
    discount_text: str
    discount_kind: str  # OFFER | ADHOC | BOTH | NONE
    amount_text: str
    marker: str = ""
    offer_locked: bool = False
    is_measured: bool = False


@dataclass(frozen=True)
class TotalRow:
    """Totals block row."""

    label: str
    amount_text: str
    emphasis: bool = False


@dataclass(frozen=True)
class Chip:
    """Status chip."""

    label: str
    tone: str  # green | amber | red | grey
    detail: str = ""


@dataclass(frozen=True)
class Banner:
    """Full-width notice above the tiles."""

    kind: str  # shift | printer | scanner
    text: str
    tone: str


@dataclass(frozen=True)
class Tile:
    """Quick-pick tile."""

    product_id: int
    name: str
    price_text: str
    stock_text: str
    low: bool
    out: bool
    image_path: str | None


@dataclass(frozen=True)
class PosModel:
    """Everything the POS screen renders."""

    rows: list[CartRow]
    total_rows: list[TotalRow]
    total_text: str
    saved_text: str | None
    pay_label: str
    line_count: int
    next_invoice: str
    scan_hint: str
    scan_mode: str
    scanner_chip: Chip
    printer_chip: Chip
    banners: list[Banner]
    billing_paused: bool
    tiles: list[Tile]
    tiles_enabled: bool
    tiles_hint: str
    held_count: int
    labels: dict[str, str] = field(default_factory=dict)


def _row(ln: CartLine, lt: Any, texts: UiTexts) -> CartRow:
    sub_parts = [texts.t("pos.open_title")] if ln.is_open_item else [f"#{ln.product_id}", ln.sku]
    if ln.entry_method == "LOOKUP":
        sub_parts.append(f"found by name ({ln.entry_reason or 'label damaged'})")
    if ln.is_open_item and ln.open_item_detail:
        sub_parts.append(ln.open_item_detail)
    if not ln.spec.is_piece:
        sub_parts.append(f"@ {format_inr(ln.shelf_price_paise)} / {ln.spec.pricing_unit_label}")
    offer = ln.active_offer
    kind = lt.applied
    if kind == "OFFER":
        disc = f"{offer.offer.name} {MINUS}{_pct(lt.discount_bp)}" if offer else f"{MINUS}{_pct(lt.discount_bp)}"
    elif kind == "ADHOC":
        amount = f"{MINUS}{_pct(ln.adhoc_discount_bp)}" if ln.adhoc_discount_bp else f"{MINUS}{format_inr(lt.adhoc_discount_paise)}"
        disc = f"{amount}{DOT}{ln.discount_reason}" if ln.discount_reason else amount
    elif kind == "BOTH":
        disc = f"{offer.offer.name if offer else 'offer'} + {MINUS}{format_inr(lt.adhoc_discount_paise)}"
    else:
        disc = "\u2014"
    marker = "*" if ln.is_open_item else ("\u2020" if ln.entry_method == "LOOKUP" else "")
    shelf = format_inr(ln.shelf_price_paise, symbol=False, grouping=False)
    return CartRow(ln.line_id, ln.name, DOT.join(sub_parts), format_qty(ln.spec, ln.qty), shelf, disc, kind, format_inr(lt.line_total_paise, symbol=False), marker, offer_locked=kind in ("OFFER", "BOTH"), is_measured=not ln.spec.is_piece)


def _pct(bp: int) -> str:
    value = Decimal(bp) / 100
    return (f"{value.normalize():f}" if value != value.to_integral_value() else str(int(value))) + "%"


def totals_rows(totals: BillTax, texts: UiTexts, *, tax_enabled: bool, cgst_label: str = "CGST", sgst_label: str = "SGST") -> list[TotalRow]:
    """Totals block (mirrors the receipt)."""
    rows = [TotalRow(texts.t("pos.subtotal"), format_inr(totals.subtotal_paise, symbol=False))]
    if totals.item_discount_paise:
        rows.append(TotalRow(texts.t("pos.item_discounts"), f"{MINUS}{format_inr(totals.item_discount_paise, symbol=False)}"))
    if totals.bill_discount_paise:
        rows.append(TotalRow(texts.t("pos.bill_discount"), f"{MINUS}{format_inr(totals.bill_discount_paise, symbol=False)}"))
    if tax_enabled and totals.tax_paise:
        rows.append(TotalRow(f"{texts.t('pos.taxable')}{DOT}{cgst_label}{DOT}{sgst_label}", f"{format_inr(totals.taxable_paise, symbol=False)}{DOT}{format_inr(totals.cgst_paise, symbol=False)}{DOT}{format_inr(totals.sgst_paise, symbol=False)}"))
    if totals.roundoff_paise:
        sign = "+" if totals.roundoff_paise > 0 else MINUS
        rows.append(TotalRow(texts.t("pos.roundoff"), f"{sign}{format_inr(abs(totals.roundoff_paise), symbol=False)}"))
    rows.append(TotalRow(texts.t("pos.total"), format_inr(totals.total_paise), emphasis=True))
    return rows


def build_pos(c: Any, texts: UiTexts, actor: Actor, cart: Cart) -> PosModel:
    """The whole screen for this cashier and cart."""
    settings = c.settings_service.effective()
    totals = c.billing.totals(cart)
    rows = [_row(ln, lt, texts) for ln, lt in zip(cart.lines, totals.lines, strict=True)]
    hw = c.hardware.state()
    chip_p = c.print_queue.chip()
    scan_mode = c.hardware.scan_mode(actor)
    paused = actor.role is not Role.OWNER and settings.billing.require_scan and not hw.scanner_ok and c.hardware.active_override("SCANNER") is None
    if not hw.scanner_paired:
        scanner_chip = Chip("Scanner", "red" if actor.role is not Role.OWNER and settings.billing.require_scan else "grey", hw.scanner_detail)
    elif hw.scanner_present is False:
        scanner_chip = Chip("Scanner", "red", hw.scanner_detail)
    else:
        scanner_chip = Chip("Scanner", "green", hw.scanner_detail)
    printer_label = "Printer" + (f"{DOT}{chip_p.pending} queued" if chip_p.pending else "")
    printer_chip = Chip(printer_label, chip_p.tone, chip_p.detail)
    banners: list[Banner] = []
    if c.shifts.current(actor.user_id) is None:
        banners.append(Banner("shift", texts.t("pos.shift_banner"), "amber"))
    if paused:
        banners.append(Banner("scanner", texts.t("pos.scanner_banner"), "red"))
    if chip_p.pending or (chip_p.configured and not chip_p.ready):
        banners.append(Banner("printer", texts.t("pos.printer_banner", n=chip_p.pending), "amber"))
    tiles_enabled = actor.role is Role.OWNER or not settings.billing.require_scan
    tiles = []
    for p in c.catalog.quick_picks():
        spec = spec_for(p)
        stock = format_qty(spec, p.stock_qty) if not spec.is_piece else f"{_clean(p.stock_qty)} {texts.t('pos.left')}"
        tiles.append(Tile(p.id, p.name, format_inr(p.price_paise), stock, p.low_stock, p.stock_qty <= 0, p.image_path))
    scan_hint = texts.t("pos.scan_paused") if paused else (texts.t("pos.scan_strict") if scan_mode == "SCANNER" else texts.t("pos.scan_free"))
    whole = format_inr(totals.total_paise).split(".")[0] if totals.total_paise % 100 == 0 else format_inr(totals.total_paise)
    labels = {k: texts.t(f"pos.{k}") for k in ("title", "next_bill", "lines", "col_item", "col_qty", "col_shelf", "col_discount", "col_amount", "find", "find_hint", "hold", "hold_hint", "resume", "held", "discount", "discount_hint", "clear", "clear_confirm", "pay_modes", "keys", "saved")}
    return PosModel(
        rows=rows, total_rows=totals_rows(totals, texts, tax_enabled=settings.tax.registered, cgst_label=settings.tax.cgst_label, sgst_label=settings.tax.sgst_label),
        total_text=format_inr(totals.total_paise), saved_text=format_inr(totals.total_savings_paise) if totals.total_savings_paise else None,
        pay_label=f"F9 {texts.t('pos.pay')} {whole}", line_count=len(cart.lines), next_invoice=c.billing.next_invoice_preview(), scan_hint=scan_hint, scan_mode=scan_mode,
        scanner_chip=scanner_chip, printer_chip=printer_chip, banners=banners, billing_paused=paused, tiles=tiles, tiles_enabled=tiles_enabled,
        tiles_hint=texts.t("pos.tiles_owner") if tiles_enabled else texts.t("pos.tiles_staff"), held_count=len(c.billing.held_carts(actor)), labels=labels,
    )


def _clean(value: Decimal) -> str:
    return str(value.quantize(Decimal(1))) if value == value.to_integral_value() else f"{value.normalize():f}"


# ----------------------------------------------------------------------------- scan box
@dataclass(frozen=True)
class ScanOutcome:
    """What happened to a scan-box submission."""

    kind: str  # ADDED | NEEDS_QTY | UNKNOWN | REJECTED | PAUSED | RECEIPT | ERROR
    message: str = ""
    line: CartLine | None = None
    scan: ScanResult | None = None
    sale: SaleView | None = None
    error_code: str | None = None


def classify_entry(code: str, *, elapsed_ms: float | None) -> bool:
    """True when the code arrived at scanner speed (or timing is unavailable)."""
    if elapsed_ms is None:
        return True
    return elapsed_ms <= SCAN_MS_PER_CHAR * max(1, len(code))


def handle_scan_input(c: Any, actor: Actor, cart: Cart, text: str, *, elapsed_ms: float | None, texts: UiTexts | None = None) -> ScanOutcome:
    """Enter in the scan box."""
    t = texts or UiTexts.load("en")
    code = (text or "").strip()
    if not code:
        return ScanOutcome("ERROR", "")
    via_scan = classify_entry(code, elapsed_ms=elapsed_ms)
    if parse_receipt_ean13(code) is not None:
        sale = c.billing.find_sale(code)
        if sale is not None:
            log.info("POS_RECEIPT_SCANNED invoice=%s user=%s", sale.invoice_no, actor.user_id)
            return ScanOutcome("RECEIPT", t.t("pos.receipt_scanned", invoice=sale.invoice_no), sale=sale)
    try:
        c.hardware.can_add_line(actor, via_scan=via_scan)
    except ScannerError as exc:
        return ScanOutcome("PAUSED", t.t("pos.scan_paused"), error_code=exc.code)
    except BillingError as exc:
        log.info("POS_TYPED_REJECTED user=%s len=%d elapsed=%s", actor.user_id, len(code), elapsed_ms)
        return ScanOutcome("REJECTED", t.t("pos.typed_rejected"), error_code=exc.code)
    scan = c.billing.lookup_scan(code)
    if scan is None:
        log.info("POS_UNKNOWN_CODE code=%s user=%s", code, actor.user_id)
        return ScanOutcome("UNKNOWN", t.t("pos.unknown_code", code=code), error_code="E-SCN-002")
    if scan.needs_qty:
        return ScanOutcome("NEEDS_QTY", "", scan=scan)
    try:
        line = c.billing.add_scan(actor, cart, code)
    except VaishnaviError as exc:
        return ScanOutcome("ERROR", f"{exc.user_message()} {exc.what_to_do()}".strip(), error_code=exc.code)
    return ScanOutcome("ADDED", t.t("pos.added", name=line.name), line=line)


# ----------------------------------------------------------------------------- quantity pad
@dataclass(frozen=True)
class QtyPadModel:
    """The pad for weighed / measured items."""

    product_name: str
    unit: str
    presets: list[tuple[str, Decimal]]
    min_text: str
    step_text: str
    price_hint: str
    labels: dict[str, str] = field(default_factory=dict)


def build_qty_pad(scan: ScanResult, texts: UiTexts) -> QtyPadModel:
    """Presets + hints from the product's spec."""
    spec = scan.spec
    labels = {k: texts.t(f"pos.{k}") for k in ("qty_title", "qty_presets", "qty_typed", "by_amount", "use_weight", "amount_preview")}
    return QtyPadModel(scan.product.name, spec.base_unit, [(format_qty(spec, q), q) for q in spec.presets], format_qty(spec, spec.min_qty), format_qty(spec, spec.qty_step), f"{format_inr(scan.product.price_paise)} / {spec.pricing_unit_label}", labels)


def qty_from_pad(scan: ScanResult, *, qty_text: str = "", amount_text: str = "") -> Decimal:
    """Typed quantity (snapped to the step) or a rupee amount turned into quantity."""
    if amount_text.strip():
        return qty_for_amount(scan.spec, amount_paise=parse_inr(amount_text), price_paise=scan.product.price_paise)
    try:
        return round_to_step(scan.spec, Decimal(qty_text.strip()))
    except (InvalidOperation, ValueError) as exc:
        raise MeasureError("E-MEAS-001", minimum=format_qty(scan.spec, scan.spec.min_qty), detail=f"not a number: {qty_text!r}") from exc


def pad_amount_text(scan: ScanResult, qty: Decimal) -> str:
    """Live amount for the pad."""
    return format_inr(line_amount(scan.spec, price_paise=scan.product.price_paise, qty=qty))


def add_measured(c: Any, actor: Actor, cart: Cart, scan: ScanResult, qty: Decimal, *, source: str) -> CartLine:
    """Pad confirmed → the line."""
    return c.billing.add_scan(actor, cart, scan.barcode, qty=qty, qty_source=source)


# ----------------------------------------------------------------------------- F3 Find / Open item
@dataclass(frozen=True)
class FindHit:
    """One result card."""

    product_id: int
    name: str
    name_hi: str | None
    sku: str
    price_text: str
    stock_text: str
    barcode: str
    matched: str
    image_path: str | None
    needs_qty: bool


@dataclass(frozen=True)
class FindModel:
    """The F3 dialog."""

    query: str
    hits: list[FindHit]
    reasons: list[tuple[str, str]]
    open_item_policy: str
    can_open_item: bool
    open_item_needs_pin: bool
    open_items_left: int
    tax_presets: list[tuple[str, int]]
    labels: dict[str, str] = field(default_factory=dict)


def build_find(c: Any, texts: UiTexts, actor: Actor, query: str, *, cart: Cart | None = None) -> FindModel:
    """Search + the open-item tab's policy."""
    settings = c.settings_service.effective()
    hits = []
    for h in c.catalog.search(query, limit=12) if query.strip() else []:
        p: ProductRecord = h.product
        spec = spec_for(p)
        hits.append(FindHit(p.id, p.name, p.name_hi, p.sku, format_inr(p.price_paise), format_qty(spec, p.stock_qty), p.internal_barcode or internal_ean13(p.id), h.matched, p.image_path, not spec.is_piece))
    policy = settings.billing.manual_add_policy
    grant = grant_for(Permission.OPEN_ITEM, actor.role, OpenItemPolicy(policy))
    left = max(0, settings.billing.open_item_cap_per_bill - (cart.open_item_count if cart else 0)) if actor.role is not Role.OWNER else 999
    reasons = [(texts.t(f"pos.reason_{k}"), k) for k in LOOKUP_REASONS]
    presets = [(f"{Decimal(r) / 100:g}%", int(r)) for r in (0, 500, 1200, 1800)]
    labels = {k: texts.t(f"pos.{k}") for k in ("find_title", "find_search", "find_reason", "find_add", "find_label", "open_title", "open_name", "open_detail", "open_qty", "open_price", "open_discount", "open_add", "open_blocked", "open_pin", "low_price_confirm")}
    return FindModel(query, hits, reasons, policy, grant is not Grant.DENY, grant is Grant.PIN and not actor.pin_verified, left, presets, labels)


def find_add(c: Any, actor: Actor, cart: Cart, product_id: int, *, qty_text: str, reason: str) -> CartLine:
    """Add to bill from the Find tab."""
    return c.billing.add_lookup(actor, cart, product_id, qty=Decimal(qty_text.strip() or "1"), reason=reason)


def find_print_label(c: Any, actor: Actor, product_id: int) -> Any:
    """Replacement label straight to the POS printer (audited ``LABEL_REPRINTED``)."""
    return c.labels.print_job(actor, product_ids=[product_id], copies=1, target="escpos", reason="REPLACEMENT")


@dataclass(frozen=True)
class ActionOutcome:
    """Result of a dialog submit."""

    ok: bool
    message: str = ""
    error_code: str | None = None
    needs_pin: bool = False
    needs_confirmation: bool = False
    line: CartLine | None = None


def open_item_add(c: Any, actor: Actor, cart: Cart, *, name: str, detail: str, qty_text: str, price_text: str, discount_text: str, reason: str, tax_rate_bp: int | None = None, confirm_low_price: bool = False) -> ActionOutcome:
    """Open-item tab submit (policy, cap and under-price check happen in the service)."""
    try:
        qty = Decimal(qty_text.strip() or "1")
        price = parse_inr(price_text)
        line = c.billing.add_open_item(actor, cart, name=name, detail=detail, qty=qty, unit_price_paise=price, tax_rate_bp=tax_rate_bp, confirm_low_price=confirm_low_price)
        if discount_text.strip():
            c.billing.set_line_discount(actor, cart, line.line_id, percent=Decimal(discount_text.strip()), reason=reason or "open item")
    except PinRequired as exc:
        return ActionOutcome(False, str(exc), exc.code, needs_pin=True)
    except PermissionDenied as exc:
        return ActionOutcome(False, str(exc), exc.code)
    except BillingError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code, needs_pin=exc.code in ("E-BILL-060", "E-BILL-071"), needs_confirmation=exc.code == "E-BILL-072")
    except (ValueError, InvalidOperation) as exc:
        return ActionOutcome(False, str(exc))
    return ActionOutcome(True, line=line)


# ----------------------------------------------------------------------------- discount dialog
@dataclass(frozen=True)
class DiscountModel:
    """F4."""

    target: str  # line | bill
    title: str
    cap_text: str
    reasons: list[tuple[str, str]]
    current_text: str
    labels: dict[str, str] = field(default_factory=dict)


def build_discount(c: Any, texts: UiTexts, actor: Actor, cart: Cart, line_id: str | None) -> DiscountModel:
    """Dialog model for a line (or the bill when ``line_id`` is None)."""
    settings = c.settings_service.effective()
    cap = f"{settings.billing.adhoc_discount_cap_percent} %"
    reasons = [(texts.t(f"pos.reason_{k}"), k) for k in DISCOUNT_REASONS]
    if line_id is not None:
        ln = cart.line(line_id)
        current = _pct(ln.adhoc_discount_bp) if ln.adhoc_discount_bp else (format_inr(ln.adhoc_discount_paise) if ln.adhoc_discount_paise else "")
        title = f"{texts.t('pos.discount_title')}{DOT}{ln.name}"
        target = "line"
    else:
        current = _pct(cart.bill_discount_bp) if cart.bill_discount_bp else (format_inr(cart.bill_discount_paise) if cart.bill_discount_paise else "")
        title = f"{texts.t('pos.discount_title')}{DOT}{texts.t('pos.bill_discount')}"
        target = "bill"
    labels = {k: texts.t(f"pos.{k}") for k in ("discount_percent", "discount_amount", "discount_reason", "pin_title", "pin_hint")}
    labels["cap"] = texts.t("pos.discount_cap", cap=cap)
    return DiscountModel(target, title, cap, reasons, current, labels)


def apply_discount(c: Any, actor: Actor, cart: Cart, line_id: str | None, *, percent_text: str, amount_text: str, reason: str, approver_id: int | None = None) -> ActionOutcome:
    """Submit; above the cap without PIN → ``needs_pin``."""
    try:
        percent = Decimal(percent_text.strip()) if percent_text.strip() else None
        amount = parse_inr(amount_text) if amount_text.strip() else None
        if percent is None and amount is None:
            raise ValueError("enter a percent or an amount")
        if line_id is not None:
            c.billing.set_line_discount(actor, cart, line_id, percent=percent, amount_paise=amount, reason=reason, approver_id=approver_id)
        else:
            c.billing.set_bill_discount(actor, cart, percent=percent, amount_paise=amount, reason=reason, approver_id=approver_id)
    except BillingError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code, needs_pin=exc.code in ("E-BILL-060", "E-BILL-061"))
    except (ValueError, InvalidOperation) as exc:
        return ActionOutcome(False, str(exc))
    return ActionOutcome(True)


def remove_offer(c: Any, actor: Actor, cart: Cart, line_id: str) -> ActionOutcome:
    """Owner / PIN only."""
    try:
        c.billing.remove_offer(actor, cart, line_id)
    except BillingError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code, needs_pin=True)
    return ActionOutcome(True)


# ----------------------------------------------------------------------------- payment
@dataclass(frozen=True)
class PaymentModel:
    """F9."""

    total_paise: int
    total_text: str
    quick_cash: list[int]
    labels: dict[str, str] = field(default_factory=dict)


def build_payment(c: Any, texts: UiTexts, cart: Cart) -> PaymentModel:
    """Total + one-tap cash amounts."""
    total = c.billing.totals(cart).total_paise
    options = [total]
    for unit in (1000, 10000):  # next ₹10, next ₹100
        up = -(-total // unit) * unit
        if up > total:
            options.append(up)
    options.extend(n for n in QUICK_NOTES if n > total)
    quick = sorted(set(options))
    labels = {k: texts.t(f"pos.{k}") for k in ("pay_title", "cash", "upi", "card", "split", "tendered", "change", "reference", "confirm_pay")}
    return PaymentModel(total, format_inr(total), quick, labels)


def change_text(p: PaymentModel, *, tendered_paise: int) -> str:
    """Change to give."""
    return format_inr(max(0, tendered_paise - p.total_paise))


def cash_payment(amount_paise: int, *, tendered_paise: int | None = None) -> PaymentInput:
    """Helper for a single cash payment."""
    return PaymentInput("CASH", amount_paise, tendered_paise)


def make_payments(p: PaymentModel, *, mode: str, tendered_paise: int | None = None, cash_paise: int | None = None, upi_paise: int | None = None, card_paise: int | None = None, reference: str | None = None) -> list[PaymentInput]:
    """Dialog → payment rows (``amount`` applied to the bill, cash ``tendered`` for change)."""
    if mode == "CASH":
        tendered = tendered_paise if tendered_paise is not None else p.total_paise
        return [PaymentInput("CASH", min(p.total_paise, tendered), tendered)]
    if mode in ("UPI", "CARD"):
        return [PaymentInput(mode, p.total_paise, None, reference)]
    out: list[PaymentInput] = []
    if cash_paise:
        out.append(PaymentInput("CASH", cash_paise, tendered_paise if tendered_paise is not None else cash_paise))
    if upi_paise:
        out.append(PaymentInput("UPI", upi_paise, None, reference))
    if card_paise:
        out.append(PaymentInput("CARD", card_paise, None, reference))
    return out


@dataclass(frozen=True)
class CheckoutOutcome:
    """F9 confirm."""

    ok: bool
    result: CheckoutResult | None = None
    error_code: str | None = None
    message: str = ""
    receipt_preview: str = ""
    print_status: str = "none"  # printed | queued | none
    needs_pin: bool = False


def do_checkout(c: Any, actor: Actor, cart: Cart, payments: list[PaymentInput]) -> CheckoutOutcome:
    """Commit; the print queue has already reacted to ``SaleCommitted`` when this returns."""
    try:
        result = c.billing.checkout(actor, cart, payments)
    except PinRequired as exc:
        return CheckoutOutcome(False, error_code=exc.code, message=str(exc), needs_pin=True)
    except (BillingError, ScannerError, PrinterError) as exc:
        return CheckoutOutcome(False, error_code=exc.code, message=f"{exc.user_message()} {exc.what_to_do()}".strip(), needs_pin=exc.code == "E-PRN-002")
    except VaishnaviError as exc:
        return CheckoutOutcome(False, error_code=exc.code, message=f"{exc.user_message()} {exc.what_to_do()}".strip())
    sale = c.receipts.load(result.sale_id) or result.sale
    model = c.receipts.build(sale)
    from vaishnavi.hardware.receipts import text_lines  # noqa: PLC0415 - ui may import hardware

    preview = "\n".join(text_lines(c.receipts.escpos(model, paper_mm=80)))
    queued = any(sale_id == result.sale_id for _qid, sale_id, _at, _n in c.print_queue.pending())
    status = "queued" if queued else ("printed" if sale.receipt_printed else "none")
    log.info("POS_CHECKOUT_DONE invoice=%s print=%s replayed=%s", result.invoice_no, status, result.replayed)
    return CheckoutOutcome(True, result, receipt_preview=preview, print_status=status)


# ----------------------------------------------------------------------------- hold / resume
@dataclass(frozen=True)
class HeldRow:
    """Held bills list."""

    id: int
    label: str
    lines: int
    since: str


def hold_current(c: Any, actor: Actor, carts: CartRegistry, *, label: str | None) -> int:
    """F6."""
    cart = carts.get(c.billing, actor)
    held_id = c.billing.hold(actor, cart, label=label)
    carts.reset(actor)
    return held_id


def held_list(c: Any, texts: UiTexts, actor: Actor) -> list[HeldRow]:
    """F7 list."""
    return [HeldRow(h.id, h.label or f"#{h.id}", h.line_count, h.created_at[11:16]) for h in c.billing.held_carts(actor)]


def resume_held(c: Any, actor: Actor, carts: CartRegistry, held_id: int) -> Cart:
    """F7 pick (the current empty bill is dropped; a non-empty one is held first)."""
    current = carts.get(c.billing, actor)
    if not current.is_empty:
        c.billing.hold(actor, current, label=None)
    cart = c.billing.resume(actor, held_id)
    carts.put(actor, cart)
    return cart


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
SCAN_JS: Final = """
(() => {
  const el = document.getElementById('%(id)s');
  if (!el) return;
  const input = el.querySelector('input') || el;
  let first = 0;
  input.addEventListener('keydown', (e) => {
    if (e.key.length === 1 && first === 0) first = performance.now();
    if (e.key === 'Enter') {
      const elapsed = first ? performance.now() - first : null;
      first = 0;
      input.dispatchEvent(new CustomEvent('scan-submit', { detail: { value: input.value, elapsed: elapsed }, bubbles: true }));
    }
  });
})();
"""


def register(state: Any) -> None:
    """``/pos``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]
    carts: CartRegistry = state.extra.setdefault("pos_carts", CartRegistry())

    @ui.page("/pos")
    def _pos() -> None:
        client = load_client(app)
        decision = guards.decide("/pos", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/pos"))
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        selected: dict[str, str | None] = {"line_id": None}

        with frame(state, current_path="/pos"):
            body = ui.row().classes("w-full no-wrap gap-3 items-start")

        def model() -> PosModel:
            return build_pos(services, texts, actor, carts.get(services.billing, actor))

        def render() -> None:
            m = model()
            body.clear()
            with body:
                with ui.column().classes("flex-1 gap-2"):
                    with ui.row().classes("items-center gap-2 w-full"):
                        scan = ui.input(placeholder=m.scan_hint).props("outlined dense autofocus").classes("flex-1 vsm-mono")
                        scan_id = f"scan-{id(scan)}"
                        scan.props(f"id={scan_id}")
                        ui.add_body_html(f"<script>{SCAN_JS % {'id': scan_id}}</script>")
                        scan.on("scan-submit", lambda e: on_scan(str(e.args.get("value", "")), e.args.get("elapsed")))
                        for chip in (m.scanner_chip, m.printer_chip):
                            colour = {"green": "positive", "amber": "warning", "red": "negative", "grey": "grey"}[chip.tone]
                            ui.badge(chip.label, color=colour).props("outline").tooltip(chip.detail)
                    with ui.row().classes("gap-2"):
                        ui.button(f"F3 {m.labels['find']}", on_click=open_find).props("outline").tooltip(m.labels["find_hint"])
                        ui.button(f"F6 {m.labels['hold']}", on_click=hold).props("outline").tooltip(m.labels["hold_hint"])
                        ui.button(f"F7 {m.labels['resume']} ({m.held_count})", on_click=open_held).props("outline").set_enabled(m.held_count > 0)
                        ui.button(f"F4 {m.labels['discount']}", on_click=lambda: open_discount(selected["line_id"])).props("outline").tooltip(m.labels["discount_hint"])
                        ui.button(f"Esc {m.labels['clear']}", on_click=clear).props("flat color=negative")
                    for b in m.banners:
                        with ui.row().classes("w-full p-2 rounded text-sm items-center gap-3").style(f"background:{'#FFEBEE' if b.tone == 'red' else '#FFF3E0'}"):
                            ui.label(b.text).classes("flex-1")
                            if b.kind == "shift":
                                ui.button(texts.t("pos.open_shift"), icon="point_of_sale", on_click=lambda: ui.navigate.to("/shifts?open=1")).props("unelevated color=primary dense no-caps")
                    ui.label(m.tiles_hint).classes("text-xs text-gray-500")
                    with ui.grid(columns=5).classes("w-full gap-2"):
                        for t in m.tiles:
                            card = ui.column().classes("vsm-card p-2 gap-0 cursor-pointer" if m.tiles_enabled else "vsm-card p-2 gap-0 opacity-70")
                            with card:
                                ui.label(t.name).classes("font-semibold text-sm")
                                ui.label(t.price_text)
                                ui.label(t.stock_text + (" \u26a0" if t.low else "") + (" \u2716" if t.out else "")).classes("text-xs text-gray-500")
                            if m.tiles_enabled:
                                card.on("click", lambda _e, pid=t.product_id: tile(pid))
                with ui.column().classes("gap-2").style("width: 520px"):
                    ui.label(f"{m.labels['title']}{DOT}{m.labels['next_bill']} {m.next_invoice}{DOT}{m.line_count} {m.labels['lines']}").classes("text-sm text-gray-600")
                    with ui.column().classes("vsm-card p-2 gap-1 w-full"):
                        for r in m.rows:
                            row = ui.row().classes("items-center w-full gap-2 cursor-pointer" + (" bg-orange-50" if selected["line_id"] == r.line_id else ""))
                            with row:
                                with ui.column().classes("flex-1 gap-0"):
                                    ui.label(f"{r.name} {r.marker}".strip()).classes("font-semibold")
                                    ui.label(r.sub).classes("text-xs text-gray-500")
                                ui.button("\u2212", on_click=lambda _e, lid=r.line_id: bump(lid, -1)).props("flat dense").set_enabled(not r.is_measured)
                                ui.label(r.qty_text).classes("w-16 text-center")
                                ui.button("+", on_click=lambda _e, lid=r.line_id: bump(lid, 1)).props("flat dense").set_enabled(not r.is_measured)
                                ui.label(r.shelf_text).classes("w-16 text-right vsm-mono")
                                ui.label(r.discount_text + (" \U0001f512" if r.offer_locked else "")).classes("w-32 text-xs text-right")
                                ui.label(r.amount_text).classes("w-20 text-right vsm-mono font-semibold")
                                ui.button(icon="close", on_click=lambda _e, lid=r.line_id: remove(lid)).props("flat dense color=negative")
                            row.on("click", lambda _e, lid=r.line_id: select(lid))
                    with ui.column().classes("vsm-card p-2 gap-0 w-full"):
                        for tr in m.total_rows:
                            with ui.row().classes("w-full justify-between" + (" text-xl font-bold" if tr.emphasis else " text-sm")):
                                ui.label(tr.label)
                                ui.label(tr.amount_text).classes("vsm-mono")
                        if m.saved_text:
                            ui.label(f"{m.labels['saved']} {m.saved_text}").classes("text-sm text-green-700")
                    ui.button(m.pay_label, on_click=open_pay).props("unelevated color=positive size=lg").classes("w-full vsm-touch").set_enabled(m.line_count > 0 and not m.billing_paused)
                    ui.label(m.labels["pay_modes"]).classes("text-xs text-gray-500 text-center w-full")
                    ui.label(m.labels["keys"]).classes("text-xs text-gray-400")

        # -- handlers -------------------------------------------------------------------------------
        def on_scan(value: str, elapsed: float | None) -> None:
            cart = carts.get(services.billing, actor)
            outcome = handle_scan_input(services, actor, cart, value, elapsed_ms=elapsed, texts=texts)
            if outcome.kind == "NEEDS_QTY" and outcome.scan is not None:
                open_qty_pad(outcome.scan)
            elif outcome.kind == "RECEIPT" and outcome.sale is not None:
                ui.navigate.to(f"/sales/{outcome.sale.id}")
                return
            elif outcome.kind == "ADDED":
                ui.notify(outcome.message, type="positive")
            elif outcome.message:
                ui.notify(outcome.message, type="warning" if outcome.kind in ("UNKNOWN", "REJECTED") else "negative")
            render()

        def tile(product_id: int) -> None:
            try:
                services.billing.add_quick_pick(actor, carts.get(services.billing, actor), product_id)
            except VaishnaviError as exc:
                ui.notify(f"{exc.user_message()} {exc.what_to_do()}", type="negative")
            render()

        def select(line_id: str) -> None:
            selected["line_id"] = line_id
            render()

        def bump(line_id: str, delta: int) -> None:
            cart = carts.get(services.billing, actor)
            ln = cart.line(line_id)
            new = ln.qty + delta
            try:
                if new <= 0:
                    services.billing.remove_line(actor, cart, line_id)
                else:
                    services.billing.set_qty(actor, cart, line_id, new)
            except VaishnaviError as exc:
                ui.notify(exc.user_message(), type="negative")
            render()

        def remove(line_id: str) -> None:
            services.billing.remove_line(actor, carts.get(services.billing, actor), line_id)
            render()

        def clear() -> None:
            with ui.dialog() as dlg, ui.card():
                ui.label(texts.t("pos.clear_confirm"))
                with ui.row():
                    ui.button(texts.t("common.cancel"), on_click=dlg.close).props("flat")
                    ui.button(texts.t("common.confirm"), on_click=lambda: (carts.reset(actor), dlg.close(), render())).props("unelevated color=negative")
            dlg.open()

        def hold() -> None:
            with ui.dialog() as dlg, ui.card():
                label = ui.input(texts.t("pos.hold_label"))
                ui.button(texts.t("pos.hold"), on_click=lambda: (hold_current(services, actor, carts, label=label.value or None), dlg.close(), render())).props("unelevated color=primary")
            dlg.open()

        def open_held() -> None:
            with ui.dialog() as dlg, ui.card():
                ui.label(texts.t("pos.held_title")).classes("font-semibold")
                for h in held_list(services, texts, actor):
                    ui.button(f"{h.label}{DOT}{h.lines} {texts.t('pos.lines')}{DOT}{h.since}", on_click=lambda _e, hid=h.id: (resume_held(services, actor, carts, hid), dlg.close(), render())).props("outline").classes("w-full")
            dlg.open()

        def open_qty_pad(scan: ScanResult) -> None:
            pad = build_qty_pad(scan, texts)
            form = {"qty": "", "amount": ""}
            with ui.dialog() as dlg, ui.card().classes("gap-2"):
                ui.label(f"{pad.labels['qty_title']}{DOT}{pad.product_name}").classes("font-semibold")
                ui.label(pad.price_hint).classes("text-sm text-gray-600")
                with ui.row().classes("gap-1"):
                    for text, q in pad.presets:
                        ui.button(text, on_click=lambda _e, qq=q: (add_measured(services, actor, carts.get(services.billing, actor), scan, qq, source="PRESET"), dlg.close(), render())).props("outline")
                ui.input(f"{pad.labels['qty_typed']} ({pad.unit})").bind_value(form, "qty").props("inputmode=decimal")
                ui.input(pad.labels["by_amount"]).bind_value(form, "amount").props("inputmode=decimal")

                def confirm() -> None:
                    try:
                        qty = qty_from_pad(scan, qty_text=form["qty"], amount_text=form["amount"])
                        add_measured(services, actor, carts.get(services.billing, actor), scan, qty, source="BY_AMOUNT" if form["amount"].strip() else "TYPED")
                    except VaishnaviError as exc:
                        ui.notify(f"{exc.user_message()} {exc.what_to_do()}", type="negative")
                        return
                    dlg.close()
                    render()

                ui.button(texts.t("common.confirm"), on_click=confirm).props("unelevated color=primary")
            dlg.open()

        def open_find() -> None:
            cart = carts.get(services.billing, actor)
            query = {"q": ""}
            with ui.dialog() as dlg, ui.card().classes("gap-2").style("min-width: 640px"):
                fm = build_find(services, texts, actor, "", cart=cart)
                with ui.tabs() as tabs:
                    t_find = ui.tab(fm.labels["find_title"])
                    t_open = ui.tab(fm.labels["open_title"])
                with ui.tab_panels(tabs, value=t_find).classes("w-full"):
                    with ui.tab_panel(t_find):
                        box = ui.input(fm.labels["find_search"]).bind_value(query, "q").props("autofocus clearable").classes("w-full")
                        results = ui.column().classes("w-full gap-1")

                        def refresh() -> None:
                            m2 = build_find(services, texts, actor, query["q"], cart=cart)
                            results.clear()
                            with results:
                                for h in m2.hits:
                                    with ui.row().classes("items-center w-full gap-2 vsm-card p-2"):
                                        with ui.column().classes("flex-1 gap-0"):
                                            ui.label(f"{h.name}{DOT}#{h.product_id}{DOT}{h.sku}").classes("font-semibold")
                                            ui.label(f"{h.price_text}{DOT}{h.stock_text}{DOT}{h.barcode}").classes("text-xs text-gray-500 vsm-mono")
                                        reason = ui.select({k: t for t, k in m2.reasons}, value=m2.reasons[0][1]).props("dense")
                                        ui.button(m2.labels["find_add"], on_click=lambda _e, pid=h.product_id, r=reason: (find_add(services, actor, cart, pid, qty_text="1", reason=str(r.value)), dlg.close(), render())).props("unelevated color=primary")
                                        ui.button(icon="label", on_click=lambda _e, pid=h.product_id: (find_print_label(services, actor, pid), ui.notify(texts.t("common.saved")))).props("flat").tooltip(m2.labels["find_label"])

                        box.on("keydown.enter", lambda _e: refresh())
                        box.on("update:model-value", lambda _e: refresh() if len(query["q"]) >= 2 else None)
                    with ui.tab_panel(t_open):
                        if not fm.can_open_item:
                            ui.label(fm.labels["open_blocked"]).classes("text-negative")
                        else:
                            form = {"name": "", "detail": "", "qty": "1", "price": "", "discount": "", "tax": fm.tax_presets[0][1], "pin": ""}
                            ui.input(fm.labels["open_name"]).bind_value(form, "name").classes("w-full")
                            ui.input(fm.labels["open_detail"]).bind_value(form, "detail").classes("w-full")
                            with ui.row():
                                ui.input(fm.labels["open_qty"]).bind_value(form, "qty").props("inputmode=decimal").classes("w-24")
                                ui.input(fm.labels["open_price"]).bind_value(form, "price").props("inputmode=decimal").classes("w-32")
                                ui.input(fm.labels["open_discount"]).bind_value(form, "discount").props("inputmode=decimal").classes("w-32")
                                ui.select({v: k for k, v in fm.tax_presets}, value=form["tax"]).bind_value(form, "tax").props("dense")
                            if fm.open_item_needs_pin:
                                ui.label(fm.labels["open_pin"]).classes("text-warning text-sm")
                                ui.input(texts.t("pos.pin_title"), password=True).bind_value(form, "pin").props("inputmode=numeric")

                            def submit(confirm_low: bool = False) -> None:
                                a = actor
                                if fm.open_item_needs_pin:
                                    pin_actor, _owner, err = _pin_actor(services, client, str(form["pin"]), Permission.OPEN_ITEM)
                                    if pin_actor is None:
                                        ui.notify(err or fm.labels["open_pin"], type="negative")
                                        return
                                    a = pin_actor
                                o = open_item_add(services, a, cart, name=str(form["name"]), detail=str(form["detail"]), qty_text=str(form["qty"]), price_text=str(form["price"]), discount_text=str(form["discount"]), reason="open item", tax_rate_bp=int(form["tax"]), confirm_low_price=confirm_low)
                                if o.needs_confirmation:
                                    with ui.dialog() as c2, ui.card():
                                        ui.label(fm.labels["low_price_confirm"])
                                        ui.button(texts.t("common.confirm"), on_click=lambda: (c2.close(), submit(True))).props("unelevated color=warning")
                                    c2.open()
                                    return
                                if not o.ok:
                                    ui.notify(o.message, type="negative")
                                    return
                                dlg.close()
                                render()

                            ui.button(fm.labels["open_add"], on_click=lambda: submit()).props("unelevated color=primary")
            dlg.open()

        def open_discount(line_id: str | None) -> None:
            cart = carts.get(services.billing, actor)
            if cart.is_empty:
                return
            dm = build_discount(services, texts, actor, cart, line_id)
            form = {"pct": "", "amt": "", "reason": dm.reasons[0][1], "pin": ""}
            with ui.dialog() as dlg, ui.card().classes("gap-2"):
                ui.label(dm.title).classes("font-semibold")
                ui.label(dm.labels["cap"]).classes("text-xs text-gray-500")
                with ui.row():
                    ui.input(dm.labels["discount_percent"]).bind_value(form, "pct").props("inputmode=decimal").classes("w-28")
                    ui.input(dm.labels["discount_amount"]).bind_value(form, "amt").props("inputmode=decimal").classes("w-28")
                ui.select({k: t for t, k in dm.reasons}, label=dm.labels["discount_reason"]).bind_value(form, "reason")
                pin_box = ui.input(dm.labels["pin_title"], password=True).bind_value(form, "pin").props("inputmode=numeric")
                pin_box.set_visibility(False)

                def submit() -> None:
                    a = actor
                    approver = None
                    if form["pin"]:
                        pin_actor, approver, err = _pin_actor(services, client, str(form["pin"]), Permission.DISCOUNT_OVER_CAP)
                        if pin_actor is None:
                            ui.notify(err or texts.t("pos.pin_hint"), type="negative")
                            return
                        a = pin_actor
                    o = apply_discount(services, a, cart, line_id, percent_text=str(form["pct"]), amount_text=str(form["amt"]), reason=str(form["reason"]), approver_id=approver)
                    if o.needs_pin:
                        pin_box.set_visibility(True)
                        ui.notify(o.message, type="warning")
                        return
                    if not o.ok:
                        ui.notify(o.message, type="negative")
                        return
                    dlg.close()
                    render()

                ui.button(texts.t("common.confirm"), on_click=submit).props("unelevated color=primary")
            dlg.open()

        def open_pay() -> None:
            cart = carts.get(services.billing, actor)
            pm = build_payment(services, texts, cart)
            form: dict[str, Any] = {"mode": "CASH", "tendered": pm.total_paise, "cash": 0, "upi": 0, "card": 0, "ref": ""}
            with ui.dialog() as dlg, ui.card().classes("gap-2").style("min-width: 420px"):
                ui.label(f"{pm.labels['pay_title']}{DOT}{pm.total_text}").classes("text-xl font-bold")
                ui.toggle({"CASH": pm.labels["cash"], "UPI": pm.labels["upi"], "CARD": pm.labels["card"], "SPLIT": pm.labels["split"]}, value="CASH").bind_value(form, "mode")
                with ui.row().classes("gap-1"):
                    for amount in pm.quick_cash:
                        ui.button(format_inr(amount), on_click=lambda _e, a=amount: form.__setitem__("tendered", a)).props("outline")
                ui.number(pm.labels["tendered"], value=pm.total_paise / 100, format="%.2f", on_change=lambda e: form.__setitem__("tendered", int(round((e.value or 0) * 100))))
                ui.label().bind_text_from(form, "tendered", backward=lambda t: f"{pm.labels['change']}: {change_text(pm, tendered_paise=int(t))}")
                with ui.row():
                    ui.number(pm.labels["cash"], value=0, on_change=lambda e: form.__setitem__("cash", int(round((e.value or 0) * 100)))).classes("w-28")
                    ui.number(pm.labels["upi"], value=0, on_change=lambda e: form.__setitem__("upi", int(round((e.value or 0) * 100)))).classes("w-28")
                    ui.number(pm.labels["card"], value=0, on_change=lambda e: form.__setitem__("card", int(round((e.value or 0) * 100)))).classes("w-28")
                ui.input(pm.labels["reference"]).bind_value(form, "ref")

                def confirm() -> None:
                    payments = make_payments(pm, mode=str(form["mode"]), tendered_paise=int(form["tendered"]), cash_paise=int(form["cash"]) or None, upi_paise=int(form["upi"]) or None, card_paise=int(form["card"]) or None, reference=form["ref"] or None)
                    o = do_checkout(services, actor, cart, payments)
                    if not o.ok:
                        if o.error_code == "E-BILL-042":
                            # No shift: the cart stays; take the cashier straight to the drawer-open form (live run 3).
                            ui.notify(o.message, type="warning")
                            dlg.close()
                            ui.navigate.to("/shifts?open=1")
                            return
                        ui.notify(o.message, type="negative")
                        return
                    dlg.close()
                    show_done(o)

                ui.button(pm.labels["confirm_pay"], on_click=confirm).props("unelevated color=positive size=lg").classes("w-full vsm-touch")
            dlg.open()

        def show_done(o: CheckoutOutcome) -> None:
            assert o.result is not None
            result = o.result
            with ui.dialog() as dlg, ui.card().classes("gap-2"):
                ui.label(f"{texts.t('pos.done_title')}{DOT}{result.invoice_no}").classes("text-xl font-bold")
                ui.label(texts.t(f"pos.done_{o.print_status}")).classes("text-sm")
                ui.label(f"{texts.t('pos.change')}: {format_inr(result.change_paise)}").classes("text-lg")
                ui.html(f"<pre class='vsm-mono text-xs'>{html_escape(o.receipt_preview)}</pre>", sanitize=False)
                with ui.row():
                    ui.button(texts.t("pos.reprint"), on_click=lambda: services.print_queue.reprint(actor, result.sale_id)).props("flat")
                    ui.button(texts.t("pos.new_bill"), on_click=lambda: (dlg.close(), render())).props("unelevated color=primary")
            dlg.open()

        ui.keyboard(on_key=lambda e: _keys(e, open_find, lambda: open_discount(selected["line_id"]), hold, open_held, open_pay, clear))
        render()


def _keys(e: Any, find: Any, discount: Any, hold: Any, held: Any, pay: Any, clear: Any) -> None:
    if not getattr(e.action, "keydown", False):
        return
    {"F3": find, "F4": discount, "F6": hold, "F7": held, "F9": pay, "Escape": clear}.get(str(e.key), lambda: None)()


def _pin_actor(services: Any, client: Any, pin: str, permission: Permission) -> tuple[Actor | None, int | None, str]:
    """Verify the Owner PIN typed on a staff screen → (staff actor with ``pin_verified``, owner id, error text)."""
    from vaishnavi.ui.components.pin_dialog import verify  # noqa: PLC0415

    outcome = verify(services, client, pin, permission)
    if not outcome.ok:
        return None, None, outcome.message
    return outcome.actor, outcome.approver_id, ""


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
