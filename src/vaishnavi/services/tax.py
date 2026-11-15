"""TaxEngine — pure arithmetic for one line and one bill (docs/05 §2, §8a; stdlib only).

Everything is integer paise with half-up rounding (``core/money``). The engine never reads the
database: the caller hands it ``LineInput`` values taken from the product snapshot and the offers,
and gets back exactly the numbers that go into ``sale_item`` / ``sale`` and onto the receipt.

Per line::

    gross       = shelf_price × qty / pricing_unit_qty
    discount    = offer and/or ad-hoc (stacked only when the offer allows; else the higher wins)
    net         = gross − discount − share of the bill discount
    inclusive:  taxable = net × 10000 / (10000 + rate_bp); tax = net − taxable
    exclusive:  taxable = net;                               tax = taxable × rate_bp / 10000
    cgst, sgst  = halves of tax (odd paisa to CGST)
    line_total  = taxable + cgst + sgst

Per bill: subtotal = Σ gross; the bill discount is **pro-rated across lines by their net** (largest-
remainder, so no paisa is created or lost and the per-rate tax summary stays right); round-off to the
nearest rupee (``NEAREST_RUPEE``) or none. When the shop is not GST-registered (``enabled=False``)
every rate is treated as 0 so receipts carry no tax lines.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from vaishnavi.core.errors import BillingError, MeasureError
from vaishnavi.core.money import TaxSplit, add_exclusive_tax, allocate, apply_percent, multiply, round_off, split_inclusive_tax

log = logging.getLogger("vaishnavi.services.tax")

Rounding = Literal["NEAREST_RUPEE", "NONE"]
_BP: Final = Decimal(10_000)
_HUNDRED: Final = Decimal(100)


# ----------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class LineInput:
    """What the engine needs for one cart line (all from the product snapshot + the cart)."""

    shelf_price_paise: int
    qty: Decimal
    """Base units (pieces, grams, millilitres)."""
    pricing_unit_qty: Decimal
    """Base units per price (``100`` for "₹60 per 100 g"; ``1`` for pieces)."""
    tax_rate_bp: int
    price_includes_tax: bool
    offer_discount_bp: int = 0
    offer_discount_paise: int = 0
    """Fixed-amount offers (``AMOUNT`` type) in paise per line."""
    offer_stackable: bool = False
    offer_name: str | None = None
    adhoc_discount_bp: int = 0
    adhoc_discount_paise: int = 0
    key: str = ""
    """Opaque caller reference (cart line id) echoed in the output."""


@dataclass(frozen=True)
class BillInput:
    """All lines plus the bill-level discount (percent **or** amount; amount wins when both are given)."""

    lines: list[LineInput]
    bill_discount_bp: int = 0
    bill_discount_paise: int = 0


# ----------------------------------------------------------------------------- outputs
@dataclass(frozen=True)
class LineTax:
    """Computed figures for one line — these are the ``sale_item`` columns."""

    key: str
    gross_paise: int
    offer_discount_paise: int
    adhoc_discount_paise: int
    discount_paise: int
    """Offer + ad-hoc that actually applied (never above the gross)."""
    applied: str
    """``OFFER`` · ``ADHOC`` · ``BOTH`` · ``NONE`` — which discount(s) the receipt names."""
    bill_share_paise: int
    """This line's slice of the bill-level discount."""
    unit_price_paise: int
    """Effective price per pricing unit after line discounts (informational; ``line_total`` is authoritative)."""
    tax_rate_bp: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    line_total_paise: int
    offer_name: str | None = None

    @property
    def discount_bp(self) -> int:
        """Line discount as basis points of the gross (for ``sale_item.discount_bp`` and "−1 %" on the receipt)."""
        if self.gross_paise == 0:
            return 0
        return int((Decimal(self.discount_paise) * _BP / Decimal(self.gross_paise)).to_integral_value())


@dataclass(frozen=True)
class TaxGroup:
    """One row of the receipt's tax summary (grouped by rate — GSTR-1 friendly)."""

    rate_bp: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int


@dataclass(frozen=True)
class BillTax:
    """Computed figures for the whole bill — these are the ``sale`` columns."""

    lines: list[LineTax]
    subtotal_paise: int
    item_discount_paise: int
    bill_discount_paise: int
    total_savings_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    roundoff_paise: int
    total_paise: int
    tax_groups: list[TaxGroup] = field(default_factory=list)

    @property
    def tax_paise(self) -> int:
        """CGST + SGST."""
        return self.cgst_paise + self.sgst_paise


# ----------------------------------------------------------------------------- engine
class TaxEngine:
    """Stateless; construct once from the effective settings."""

    def __init__(self, *, enabled: bool = True, rounding: Rounding = "NEAREST_RUPEE") -> None:
        self.enabled = enabled
        self.rounding = rounding

    # -- one line ---------------------------------------------------------------------------------
    def line(self, inp: LineInput, *, bill_share_paise: int = 0) -> LineTax:
        """Compute one line; ``bill_share_paise`` is the pro-rated bill discount decided by :meth:`bill`."""
        if inp.qty <= 0:
            raise MeasureError("E-MEAS-001", detail=f"qty {inp.qty} must be > 0", minimum="0")
        if inp.pricing_unit_qty <= 0:
            raise BillingError("E-BILL-072", detail="pricing_unit_qty must be > 0", price="?")
        gross = multiply(inp.shelf_price_paise, inp.qty / inp.pricing_unit_qty)
        offer = min(gross, apply_percent(gross, Decimal(inp.offer_discount_bp) / _HUNDRED) + inp.offer_discount_paise)
        adhoc = min(gross, apply_percent(gross, Decimal(inp.adhoc_discount_bp) / _HUNDRED) + inp.adhoc_discount_paise)
        if offer and adhoc:
            if inp.offer_stackable:
                discount, applied = min(gross, offer + adhoc), "BOTH"
            elif offer >= adhoc:
                discount, applied, adhoc = offer, "OFFER", 0
            else:
                discount, applied, offer = adhoc, "ADHOC", 0
        elif offer:
            discount, applied = offer, "OFFER"
        elif adhoc:
            discount, applied = adhoc, "ADHOC"
        else:
            discount, applied = 0, "NONE"
        net = max(0, gross - discount - bill_share_paise)
        rate = inp.tax_rate_bp if self.enabled else 0
        split: TaxSplit = split_inclusive_tax(net, rate) if inp.price_includes_tax else add_exclusive_tax(net, rate)
        cgst, sgst = split.halves()
        units = inp.qty / inp.pricing_unit_qty
        unit_price = int((Decimal(gross - discount) / units).to_integral_value()) if units else inp.shelf_price_paise
        return LineTax(
            key=inp.key,
            gross_paise=gross,
            offer_discount_paise=offer,
            adhoc_discount_paise=adhoc,
            discount_paise=discount,
            applied=applied,
            bill_share_paise=bill_share_paise,
            unit_price_paise=unit_price,
            tax_rate_bp=rate,
            taxable_paise=split.net,
            cgst_paise=cgst,
            sgst_paise=sgst,
            line_total_paise=split.gross,
            offer_name=inp.offer_name if offer else None,
        )

    # -- whole bill -------------------------------------------------------------------------------
    def bill(self, inp: BillInput) -> BillTax:
        """Compute every line, pro-rate the bill discount, sum, round."""
        if not inp.lines:
            raise BillingError("E-BILL-050", detail="a bill needs at least one line", short="all")
        first = [self.line(ln) for ln in inp.lines]
        subtotal = sum(ln.gross_paise for ln in first)
        item_discount = sum(ln.discount_paise for ln in first)
        nets = [ln.gross_paise - ln.discount_paise for ln in first]
        net_total = sum(nets)
        if inp.bill_discount_paise:
            bill_discount = inp.bill_discount_paise
        elif inp.bill_discount_bp:
            bill_discount = apply_percent(net_total, Decimal(inp.bill_discount_bp) / _HUNDRED)
        else:
            bill_discount = 0
        bill_discount = max(0, min(bill_discount, net_total))
        shares = allocate(bill_discount, nets) if bill_discount else [0] * len(first)
        lines = [self.line(ln, bill_share_paise=share) for ln, share in zip(inp.lines, shares, strict=True)]
        taxable = sum(ln.taxable_paise for ln in lines)
        cgst = sum(ln.cgst_paise for ln in lines)
        sgst = sum(ln.sgst_paise for ln in lines)
        before_round = sum(ln.line_total_paise for ln in lines)
        if self.rounding == "NEAREST_RUPEE":
            total, roundoff = round_off(before_round)
        else:
            total, roundoff = before_round, 0
        groups: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
        for ln in lines:
            g = groups[ln.tax_rate_bp]
            g[0] += ln.taxable_paise
            g[1] += ln.cgst_paise
            g[2] += ln.sgst_paise
        tax_groups = [TaxGroup(rate, t, c, s) for rate, (t, c, s) in sorted(groups.items())]
        log.debug("BILL_COMPUTED lines=%d subtotal=%d item_disc=%d bill_disc=%d taxable=%d tax=%d roundoff=%d total=%d", len(lines), subtotal, item_discount, bill_discount, taxable, cgst + sgst, roundoff, total)
        return BillTax(
            lines=lines,
            subtotal_paise=subtotal,
            item_discount_paise=item_discount,
            bill_discount_paise=bill_discount,
            total_savings_paise=item_discount + bill_discount,
            taxable_paise=taxable,
            cgst_paise=cgst,
            sgst_paise=sgst,
            roundoff_paise=roundoff,
            total_paise=total,
            tax_groups=tax_groups,
        )
