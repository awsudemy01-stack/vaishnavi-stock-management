"""Measured quantities — ghee by grams, oil by millilitres, idols by the piece (docs/05 §8d; stdlib only).

A :class:`QuantitySpec` is the product's selling contract (mode, base unit, pricing unit, minimum,
step, presets). Everything here is pure: validate a typed quantity (``E-MEAS-001`` below minimum ·
``E-MEAS-002`` off-step · ``E-MEAS-003`` fraction of a piece), round to the step, turn "₹50 of ghee"
into grams, compute the line amount, format for the receipt, and decode **weight-embedded barcodes**
printed by label scales (EAN-13 prefix 20–29; the layout is configurable because Indian scales vary —
``"21PPPPPWWWWWC"`` = prefix 21, 5-digit PLU, 5-digit weight in base units, check digit; ``A`` digits
carry an amount in paise instead).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from vaishnavi.core.barcode import ean13_check_digit
from vaishnavi.core.errors import BillingError, MeasureError
from vaishnavi.core.money import multiply

log = logging.getLogger("vaishnavi.services.measure")

BIG_UNITS: Final = {"g": ("kg", Decimal(1000)), "ml": ("L", Decimal(1000)), "cm": ("m", Decimal(100))}
"""Base unit → (larger unit, factor) for receipt-friendly formatting."""


@dataclass(frozen=True)
class QuantitySpec:
    """The product's quantity rules (built from ``ProductRecord`` by :func:`spec_for`)."""

    sale_mode: str  # PIECE | WEIGHT | VOLUME | LENGTH
    base_unit: str  # pcs | g | ml | cm
    pricing_unit_qty: Decimal
    pricing_unit_label: str
    min_qty: Decimal
    qty_step: Decimal
    presets: tuple[Decimal, ...]
    allow_decimal: bool

    @property
    def is_piece(self) -> bool:
        """Counted, not measured."""
        return self.sale_mode == "PIECE"


def spec_for(product: object) -> QuantitySpec:
    """Build the spec from any object with the product columns (``ProductRecord`` or a snapshot)."""
    import json  # noqa: PLC0415 - tiny, local

    raw = getattr(product, "qty_presets", "[]") or "[]"
    try:
        presets = tuple(Decimal(str(x)) for x in json.loads(raw))
    except (ValueError, ArithmeticError):
        log.warning("bad qty_presets on product %s: %r", getattr(product, "id", "?"), raw)
        presets = ()
    return QuantitySpec(
        sale_mode=str(getattr(product, "sale_mode", "PIECE")),
        base_unit=str(getattr(product, "base_unit", "pcs")),
        pricing_unit_qty=Decimal(str(getattr(product, "pricing_unit_qty", 1))),
        pricing_unit_label=str(getattr(product, "pricing_unit_label", "pc")),
        min_qty=Decimal(str(getattr(product, "min_qty", 1))),
        qty_step=Decimal(str(getattr(product, "qty_step", 1))),
        presets=presets,
        allow_decimal=bool(getattr(product, "allow_decimal_qty", False)),
    )


# ----------------------------------------------------------------------------- validation
def validate_qty(spec: QuantitySpec, qty: Decimal) -> Decimal:
    """Return ``qty`` when it respects the spec; raise :class:`BillingError` otherwise."""
    if qty <= 0 or qty < spec.min_qty:
        raise MeasureError("E-MEAS-001", detail=f"qty {qty} < min {spec.min_qty}", minimum=format_qty(spec, spec.min_qty))
    if not spec.allow_decimal and qty != qty.to_integral_value():
        raise MeasureError("E-MEAS-003", detail=f"qty {qty} is not whole")
    if spec.qty_step > 0 and (qty / spec.qty_step) % 1 != 0:
        raise MeasureError("E-MEAS-002", detail=f"qty {qty} not a multiple of {spec.qty_step}", step=format_qty(spec, spec.qty_step))
    return qty


def round_to_step(spec: QuantitySpec, qty: Decimal) -> Decimal:
    """Nearest multiple of the step (half-up), never below the minimum."""
    if spec.qty_step <= 0:
        return max(qty, spec.min_qty)
    steps = (qty / spec.qty_step).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return max(_clean(steps * spec.qty_step), spec.min_qty)


def qty_for_amount(spec: QuantitySpec, *, amount_paise: int, price_paise: int) -> Decimal:
    """"₹50 of ghee" → base units, rounded to the step (docs/05 §8d by-amount toggle)."""
    if price_paise <= 0:
        raise BillingError("E-BILL-072", detail="price must be > 0 for by-amount", price="0")
    raw = Decimal(amount_paise) / Decimal(price_paise) * spec.pricing_unit_qty
    qty = round_to_step(spec, raw)
    log.debug("QTY_BY_AMOUNT amount=%d price=%d raw=%s qty=%s", amount_paise, price_paise, raw, qty)
    return qty


def line_amount(spec: QuantitySpec, *, price_paise: int, qty: Decimal) -> int:
    """``price × qty / pricing_unit_qty`` half-up (the same formula the TaxEngine uses)."""
    return multiply(price_paise, qty / spec.pricing_unit_qty)


def format_qty(spec: QuantitySpec, qty: Decimal) -> str:
    """``"250 g"``, ``"1 kg"``, ``"1.5 L"``, ``"2 pcs"`` — what the receipt and the cart print."""
    qty = _clean(qty)
    if spec.is_piece or spec.base_unit == "pcs":
        return f"{qty} pc" if qty == 1 else f"{qty} pcs"
    big = BIG_UNITS.get(spec.base_unit)
    if big is not None and qty >= big[1]:
        return f"{_clean(qty / big[1])} {big[0]}"
    return f"{qty} {spec.base_unit}"


def _clean(value: Decimal) -> Decimal:
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


# ----------------------------------------------------------------------------- scale barcodes
@dataclass(frozen=True)
class ScaleCodeFormat:
    """Layout of a 13-digit weight/price-embedded barcode: digits ``0-9`` are a fixed prefix, ``P`` PLU, ``W`` weight (base units), ``A`` amount (paise), ``C`` check digit (last)."""

    layout: str

    @classmethod
    def parse(cls, layout: str) -> ScaleCodeFormat:
        """Validate the layout string."""
        text = (layout or "").strip().upper()
        if len(text) != 13 or not text.endswith("C") or text.count("C") != 1:
            raise ValueError("scale barcode layout must be 13 characters ending in a single C")
        if "P" not in text or ("W" not in text and "A" not in text) or ("W" in text and "A" in text):
            raise ValueError("layout needs P digits and either W or A digits")
        if any(ch not in "0123456789PWAC" for ch in text):
            raise ValueError("layout may contain only digits, P, W, A and C")
        return cls(text)

    @property
    def prefix(self) -> str:
        """Leading fixed digits."""
        out = []
        for ch in self.layout:
            if ch.isdigit():
                out.append(ch)
            else:
                break
        return "".join(out)


@dataclass(frozen=True)
class ScaleCode:
    """A decoded scale barcode."""

    plu: int
    weight_base_units: Decimal | None
    amount_paise: int | None
    raw: str


def decode_scale_barcode(code: str, fmt: ScaleCodeFormat | None) -> ScaleCode | None:
    """``None`` when the code does not match the layout (wrong prefix / length / check digit)."""
    if fmt is None:
        return None
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(digits) != 13 or not digits.startswith(fmt.prefix):
        return None
    if ean13_check_digit(digits[:12]) != int(digits[12]):
        log.debug("SCALE_CODE_BAD_CHECK code=%s", digits)
        return None
    plu = int("".join(d for d, ch in zip(digits, fmt.layout, strict=True) if ch == "P") or "0")
    weight_text = "".join(d for d, ch in zip(digits, fmt.layout, strict=True) if ch == "W")
    amount_text = "".join(d for d, ch in zip(digits, fmt.layout, strict=True) if ch == "A")
    hit = ScaleCode(plu=plu, weight_base_units=Decimal(int(weight_text)) if weight_text else None, amount_paise=int(amount_text) if amount_text else None, raw=digits)
    log.debug("SCALE_CODE_DECODED plu=%s weight=%s amount=%s", hit.plu, hit.weight_base_units, hit.amount_paise)
    return hit
