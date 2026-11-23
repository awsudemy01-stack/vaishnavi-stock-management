"""ReceiptService — one view-model → thermal ESC/POS, HTML archive (docs/05 §8; stdlib only).

The model is built from the committed ``SaleView`` (never from the cart, so a reprint is byte-identical
to the original apart from the ``DUPLICATE`` label). Every line shows **shelf price → discount → final
amount**, the offer name is printed beneath and "You saved" totals the discounts. Lookup (``†``) and
open-item (``*``) markers are **not** printed on the customer's receipt (owner, live run 5b: "label damaged –
looked up" is back-office information) — they stay on the billing screen, in the review queue and in the
per-cashier report. The invoice is also encoded as an EAN-13 (``core/barcode.receipt_ean13``)
so a return starts by scanning the customer's receipt. Rendering is delegated to an injected renderer
module (``hardware/receipts.py``) because services never import the hardware layer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlencode

from vaishnavi.core.barcode import receipt_ean13
from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.money import format_inr, from_paise
from vaishnavi.core.settings import Settings
from vaishnavi.db.records import SaleItemView, SaleView
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.measure import QuantitySpec, format_qty

log = logging.getLogger("vaishnavi.services.receipt")

MARK_LOOKUP: Final = "\u2020"  # † — used on the billing screen, not printed
MARK_OPEN: Final = "*"
_BIG: Final = {"g": "kg", "ml": "L"}


# ----------------------------------------------------------------------------- model
@dataclass(frozen=True)
class ReceiptLine:
    """One printed line (+ optional discount row and note beneath).

    For a discounted item ``amount_text`` is the **shelf** amount (price × qty) and ``discount_row`` carries
    ``("discount 1% (Diwali Offer)", "-10.00", "990.00")`` — the customer can follow the arithmetic line by line
    (owner's request, live run 4). Undiscounted lines have ``discount_row=None`` and the net amount.
    """

    name: str
    qty_text: str
    price_text: str
    discount_text: str
    amount_text: str
    marker: str = ""
    note: str = ""
    discount_row: tuple[str, str, str] | None = None
    """``(label, discount_amount, net_amount)``."""


@dataclass(frozen=True)
class TotalRow:
    """Label / amount pair in the totals block."""

    label: str
    amount_text: str


@dataclass(frozen=True)
class ReceiptModel:
    """Everything a renderer needs; no money arithmetic happens after this point."""

    sale_id: int
    public_id: str
    invoice_no: str
    status: str
    shop_name: str
    shop_name_hi: str
    header_lines: list[str]
    datetime_text: str
    cashier: str
    shift_label: str
    lines: list[ReceiptLine]
    total_rows: list[TotalRow]
    tax_rows: list[TotalRow]
    total_text: str
    saved_text: str | None
    """Total savings (item + bill) — the single "You saved" line when there is no bill discount."""
    payment_lines: list[str]
    legend: str
    footer_lines: list[str]
    barcode: str
    upi_id: str | None = None
    upi_payload: str | None = None
    copy_label: str | None = None
    paper_mm: int = 80
    show_hindi: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    saved_lines: list[str] = field(default_factory=list)
    """Printed in bold under TOTAL. With a bill discount the story is told in two lines — "You saved ₹0.30 on
    items" and "You saved another ₹10.00 as our Regular customer" — then a thank-you; otherwise just "You saved …"."""


class ReceiptStore(Protocol):
    """Rows the service touches."""

    def sale(self, session: Session, sale_id: int) -> SaleView | None: ...  # pragma: no cover - protocol
    def mark_printed(self, session: Session, sale_id: int, *, reprint: bool) -> None: ...  # pragma: no cover - protocol
    def name_hi(self, session: Session, product_id: int) -> str | None: ...  # pragma: no cover - protocol


class ReceiptRenderer(Protocol):
    """``hardware/receipts.py`` shape."""

    def render_escpos(self, model: ReceiptModel, *, paper_mm: int, hindi_raster: Callable[[str], bytes] | None, drawer_kick: bool) -> bytes: ...  # pragma: no cover - protocol
    def render_html(self, model: ReceiptModel) -> str: ...  # pragma: no cover - protocol
    def render_pdf(self, html: str, path: Path) -> bool: ...  # pragma: no cover - protocol


# ----------------------------------------------------------------------------- service
class ReceiptService:
    """Build, render, archive, mark printed."""

    def __init__(self, txn: TxnFactory, store: ReceiptStore, paths: AppPaths, *, settings: Callable[[], Settings], cashier_name: Callable[[int], str], upi_id: Callable[[], str | None], renderer: ReceiptRenderer, hindi_raster: Callable[[str], bytes] | None = None) -> None:
        self._txn = txn
        self._store = store
        self._paths = paths
        self._settings = settings
        self._cashier_name = cashier_name
        self._upi_id = upi_id
        self._renderer = renderer
        self._hindi_raster = hindi_raster

    # -- reads ------------------------------------------------------------------------------------
    def load(self, sale_id: int) -> SaleView | None:
        """The committed sale."""
        with self._txn(label="receipt.load") as session:
            return self._store.sale(session, sale_id)

    # -- model ------------------------------------------------------------------------------------
    def build(self, sale: SaleView, *, copy_label: str | None = None) -> ReceiptModel:
        """View-model from a committed sale (docs/05 §8 layout)."""
        s = self._settings()
        shop, tax, rc = s.shop, s.tax, s.receipt
        header: list[str] = []
        addr = " \u00b7 ".join(x for x in (shop.address_line1, shop.phone) if x)
        if addr:
            header.append(addr)
        if shop.address_line2:
            header.append(shop.address_line2)
        if tax.registered and shop.gstin:
            header.append(f"GSTIN: {shop.gstin}")
        header.extend(rc.header_lines)
        if sale.original_sale_id is not None:
            with self._txn(label="receipt.original") as session:
                original = self._store.sale(session, sale.original_sale_id)
            if original is not None:
                header.append(f"Against bill {original.invoice_no}")
            if sale.note:
                header.append(f"Reason: {sale.note}")

        lines = []
        hindi: dict[int, str | None] = {}
        if rc.show_hindi_name:
            with self._txn(label="receipt.hindi") as session:
                for it in sale.items:
                    if it.product_id is not None and not it.is_open_item and it.product_id not in hindi:
                        hindi[it.product_id] = self._store.name_hi(session, it.product_id)
        for it in sale.items:
            lines.append(self._line(it, name_hi=hindi.get(it.product_id or -1)))
        flagged = sum(1 for it in sale.items if it.is_open_item or it.entry_method == "LOOKUP")

        rows = [TotalRow("Subtotal (shelf price)", _amt(sale.subtotal_paise))]
        if sale.item_discount_paise:
            rows.append(TotalRow("Item discounts", f"-{_amt(sale.item_discount_paise)}"))
        if sale.bill_discount_paise:
            # "Extra" on purpose: the customer should feel the seller went beyond the shelf offers.
            label = "Extra discount" + (f" \u2014 {sale.bill_discount_reason}" if sale.bill_discount_reason else "")
            rows.append(TotalRow(label, f"-{_amt(sale.bill_discount_paise)}"))
        tax_rows: list[TotalRow] = []
        if tax.registered and (sale.cgst_paise or sale.sgst_paise):
            rows.append(TotalRow("Taxable", _amt(sale.taxable_paise)))
            for rate_bp, taxable, cgst, sgst in _groups(sale.items):
                if rate_bp == 0:
                    continue
                half = _pct(rate_bp // 2) if rate_bp % 2 == 0 else _pct(rate_bp / 2)
                tax_rows.append(TotalRow(f"{tax.cgst_label} @ {half}", _amt(cgst)))
                tax_rows.append(TotalRow(f"{tax.sgst_label} @ {half}", _amt(sgst)))
                log.debug("RECEIPT_TAX_GROUP rate_bp=%s taxable=%d cgst=%d sgst=%d", rate_bp, taxable, cgst, sgst)
        if sale.roundoff_paise:
            rows.append(TotalRow("Round-off", f"{'+' if sale.roundoff_paise > 0 else '-'}{_amt(abs(sale.roundoff_paise))}"))

        payments: list[str] = []
        for p in sale.payments:
            shown = p.tendered_paise if p.tendered_paise is not None else p.amount_paise
            text = f"Paid: {p.mode} {format_inr(shown)}"
            if p.change_paise:
                text += f"  Change {format_inr(p.change_paise)}"
            if p.reference:
                text += f" ({p.reference})"
            payments.append(text)

        legend = ""  # lookup / open-item flags are back-office information (review queue, cashier report), never printed
        upi = self._upi_id() if rc.show_qr_upi else None
        payload = None
        if upi:
            payload = "upi://pay?" + urlencode({"pa": upi, "pn": shop.name, "am": f"{from_paise(sale.total_paise):.2f}", "cu": "INR"})
        footer = list(rc.footer_lines)
        if upi:
            footer.insert(0, f"Pay by UPI: {upi}")
        label = copy_label or ("CREDIT NOTE" if sale.status in ("VOIDED", "REFUND") else None)
        saved_lines: list[str] = []
        if sale.bill_discount_paise:
            if sale.item_discount_paise:
                saved_lines.append(f"You saved {format_inr(sale.item_discount_paise)} on items")
            who = f" as our {sale.bill_discount_reason}" if sale.bill_discount_reason else ""
            saved_lines.append(f"You saved {'another ' if sale.item_discount_paise else ''}{format_inr(sale.bill_discount_paise)}{who}")
            if rc.savings_message.strip():
                saved_lines.append(rc.savings_message.strip())
        elif sale.total_savings_paise:
            saved_lines.append(f"You saved {format_inr(sale.total_savings_paise)} today")
        model = ReceiptModel(
            sale_id=sale.id, public_id=sale.public_id, invoice_no=sale.invoice_no, status=sale.status, shop_name=shop.name, shop_name_hi=shop.name_hi, header_lines=header,
            datetime_text=format_ist(parse_iso_utc(sale.sold_at)), cashier=self._cashier_name(sale.user_id), shift_label=f"#{sale.shift_id}", lines=lines, total_rows=rows,
            tax_rows=tax_rows, total_text=format_inr(sale.total_paise), saved_text=format_inr(sale.total_savings_paise) if sale.total_savings_paise else None,
            payment_lines=payments, legend=legend, footer_lines=footer, barcode=receipt_ean13(sale.fin_year, sale.seq_no), upi_id=upi, upi_payload=payload,
            copy_label=label, paper_mm=rc.paper_width_mm, show_hindi=rc.show_hindi_name, extras={"sold_at": sale.sold_at}, saved_lines=saved_lines,
        )
        log.debug("RECEIPT_MODEL sale=%s invoice=%s lines=%d flagged_lines=%d copy=%s", sale.id, sale.invoice_no, len(lines), flagged, label)
        return model

    # -- renderers --------------------------------------------------------------------------------
    def escpos(self, model: ReceiptModel, *, paper_mm: int | None = None, drawer_kick: bool = False) -> bytes:
        """Thermal bytes (80 or 58 mm)."""
        return self._renderer.render_escpos(model, paper_mm=paper_mm or model.paper_mm, hindi_raster=self._hindi_raster, drawer_kick=drawer_kick)

    def html(self, model: ReceiptModel) -> str:
        """Archive / on-screen / A4 fallback."""
        return self._renderer.render_html(model)

    def archive(self, model: ReceiptModel) -> Path:
        """Write ``<primary>/receipts/YYYY/MM/<invoice>.html`` (+ ``.pdf`` when a converter exists); returns the HTML path."""
        when = parse_iso_utc(str(model.extras["sold_at"]))
        folder = self._paths.receipts / f"{when.year:04d}" / f"{when.month:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        stem = model.invoice_no.replace("/", "-").replace("\\", "-")
        path = folder / f"{stem}.html"
        if not path.exists():
            path.write_text(self.html(model), encoding="utf-8")
            pdf = folder / f"{stem}.pdf"
            if self._renderer.render_pdf(self.html(model), pdf):
                log.debug("RECEIPT_PDF path=%s", pdf)
            log.info("RECEIPT_ARCHIVED sale=%s path=%s", model.sale_id, path)
        return path

    # -- state ------------------------------------------------------------------------------------
    def mark_printed(self, sale_id: int, *, reprint: bool = False) -> None:
        """``receipt_printed = 1`` (+ reprint counter)."""
        with self._txn(label="receipt.mark") as session:
            self._store.mark_printed(session, sale_id, reprint=reprint)
        log.debug("RECEIPT_MARKED_PRINTED sale=%s reprint=%s", sale_id, reprint)

    # -- internals --------------------------------------------------------------------------------
    @staticmethod
    def _line(it: SaleItemView, *, name_hi: str | None) -> ReceiptLine:
        measured = it.unit != "pcs"
        spec = QuantitySpec(sale_mode="WEIGHT" if measured else "PIECE", base_unit=it.unit, pricing_unit_qty=it.pricing_unit_qty_snapshot, pricing_unit_label="", min_qty=Decimal(0), qty_step=Decimal(0), presets=(), allow_decimal=True)
        qty_text = format_qty(spec, it.qty)
        price = _amt(it.shelf_price_paise)
        if measured:
            # "/kg", "/100g", "/L" — the pricing unit in its natural size (never "/1000g", which the narrow column truncates).
            per = format_qty(spec, it.pricing_unit_qty_snapshot).replace(" ", "")
            if re.match(r"^1(?=[A-Za-z])", per):
                per = per[1:]
            price = f"{price}/{per}"
        name = it.open_item_name or it.name_snapshot
        if name_hi and not it.is_open_item:
            name = f"{name} ({name_hi})"
        discount = "\u2014"
        note = ""
        discount_row: tuple[str, str, str] | None = None
        amount = _amt(it.line_total_paise)
        if it.discount_paise:
            pct = _pct(it.discount_bp) if it.discount_bp else None
            discount = f"-{pct}" if pct else f"-{_amt(it.discount_paise)}"
            who = it.offer_name_snapshot or (it.discount_reason or "").strip()
            label = f"discount {pct}" if pct else f"discount {_amt(it.discount_paise)}"
            if who:
                label += f" ({who})"
            # Item row shows what the shelf said; the row beneath shows the discount and what was actually charged.
            amount = _amt(it.line_total_paise + it.discount_paise)
            discount_row = (label, f"-{_amt(it.discount_paise)}", _amt(it.line_total_paise))
        return ReceiptLine(name=name, qty_text=qty_text, price_text=price, discount_text=discount, amount_text=amount, note=note, discount_row=discount_row)


# ----------------------------------------------------------------------------- helpers
def _amt(paise: int) -> str:
    return format_inr(paise, symbol=False, grouping=False)


def _pct(bp: int | float) -> str:
    value = Decimal(str(bp)) / 100
    text = f"{value.normalize():f}" if value != value.to_integral_value() else str(int(value))
    return f"{text}%"


def _clean(value: Decimal) -> str:
    return str(value.quantize(Decimal(1))) if value == value.to_integral_value() else f"{value.normalize():f}"


def _groups(items: tuple[SaleItemView, ...]) -> list[tuple[int, int, int, int]]:
    acc: dict[int, list[int]] = {}
    for it in items:
        g = acc.setdefault(it.tax_rate_bp, [0, 0, 0])
        g[0] += it.taxable_paise
        g[1] += it.cgst_paise
        g[2] += it.sgst_paise
    return [(rate, t, c, s) for rate, (t, c, s) in sorted(acc.items())]
