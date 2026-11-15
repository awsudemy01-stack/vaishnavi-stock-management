"""Money for the shop: integer paise everywhere, Decimal only at the edges (stdlib only, ADR-003).

Why integers: ₹9.99 × 3 in binary floats is 29.970000000000002; summed over a day's bills that drift
shows up as a one-paisa mismatch between the receipt and the ledger — exactly the kind of discrepancy
an anti-theft system must never produce itself. So every amount is stored and computed as an ``int``
number of paise. ``Decimal`` is used only to parse owner input ("9.99", "₹ 1,250") and to split
tax-inclusive prices, always with **half-up** rounding (Indian GST practice), never banker's rounding.

Public API (all pure functions):
- ``to_paise`` / ``from_paise`` / ``format_inr`` / ``parse_inr``
- ``multiply`` (qty × unit price with half-up rounding), ``apply_percent``
- ``split_inclusive_tax`` / ``add_exclusive_tax`` → ``TaxSplit``
- ``round_off`` to the nearest rupee for the bill total (adds a ± round-off line)
- ``allocate`` a total across parts without losing a paisa (largest-remainder method)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

Paise = int
"""Type alias used throughout the codebase for amounts in paise."""

PAISE_PER_RUPEE: Final = 100
RUPEE_SIGN: Final = "\u20b9"  # ₹
_QUANT_PAISE: Final = Decimal("1")
_QUANT_RUPEE: Final = Decimal("1")
_HUNDRED: Final = Decimal(100)
_BASIS_POINTS: Final = Decimal(10_000)
_PARSE_CLEAN = re.compile(r"[\s,\u20b9]|rs\.?|inr", re.IGNORECASE)


class MoneyError(ValueError):
    """Raised for unparseable or out-of-range monetary input (error code E-MONEY-001)."""

    code = "E-MONEY-001"


# ----------------------------------------------------------------------------- conversion
def to_paise(amount: Decimal | int | str) -> Paise:
    """Rupees (Decimal/str/int) → paise, half-up to the nearest paisa.

    >>> to_paise("9.99"), to_paise(Decimal("1.005")), to_paise(10)
    (999, 101, 1000)
    """
    if isinstance(amount, bool):  # bool is an int subclass; never meaningful as money
        raise MoneyError(f"not an amount: {amount!r}")
    if isinstance(amount, int):
        return amount * PAISE_PER_RUPEE
    try:
        dec = amount if isinstance(amount, Decimal) else Decimal(str(amount).strip())
    except InvalidOperation as exc:
        raise MoneyError(f"not an amount: {amount!r}") from exc
    if not dec.is_finite():
        raise MoneyError(f"not an amount: {amount!r}")
    return int((dec * _HUNDRED).quantize(_QUANT_PAISE, rounding=ROUND_HALF_UP))


def from_paise(paise: Paise) -> Decimal:
    """Paise → exact Decimal rupees with two places.

    >>> from_paise(999)
    Decimal('9.99')
    """
    return (Decimal(paise) / _HUNDRED).quantize(Decimal("0.01"))


def parse_inr(text: str) -> Paise:
    """Owner-typed amount → paise. Accepts '9.99', '₹ 1,250', 'Rs. 10', '-5.50'.

    >>> parse_inr("₹ 1,250.5")
    125050
    """
    cleaned = _PARSE_CLEAN.sub("", text or "")
    if cleaned in ("", "-", "+", "."):
        raise MoneyError(f"not an amount: {text!r}")
    return to_paise(cleaned)


def format_inr(paise: Paise, *, symbol: bool = True, grouping: bool = True) -> str:
    """Paise → '₹1,23,456.78' with Indian digit grouping (lakh/crore).

    >>> format_inr(12345678), format_inr(-999, symbol=False), format_inr(50, grouping=False)
    ('₹1,23,456.78', '-9.99', '₹0.50')
    """
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), PAISE_PER_RUPEE)
    digits = str(rupees)
    if grouping and len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join([*parts, tail])
    return f"{sign}{RUPEE_SIGN if symbol else ''}{digits}.{p:02d}"


# ----------------------------------------------------------------------------- arithmetic
def _half_up(value: Decimal) -> Paise:
    return int(value.quantize(_QUANT_PAISE, rounding=ROUND_HALF_UP))


def multiply(unit_paise: Paise, quantity: Decimal | int | str) -> Paise:
    """Line amount = unit price × quantity, half-up to a paisa (quantity may be fractional: 0.250 kg).

    >>> multiply(999, 3), multiply(45000, "0.250"), multiply(333, Decimal("1.5"))
    (2997, 11250, 500)
    """
    try:
        qty = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    except InvalidOperation as exc:
        raise MoneyError(f"not a quantity: {quantity!r}") from exc
    if not qty.is_finite():
        raise MoneyError(f"not a quantity: {quantity!r}")
    return _half_up(Decimal(unit_paise) * qty)


def apply_percent(paise: Paise, percent: Decimal | int | str) -> Paise:
    """``percent`` % of ``paise`` (e.g. a discount or a tax amount), half-up.

    >>> apply_percent(1000, 1), apply_percent(999, "12.5"), apply_percent(1, 50)
    (10, 125, 1)
    """
    pct = percent if isinstance(percent, Decimal) else Decimal(str(percent))
    return _half_up(Decimal(paise) * pct / _HUNDRED)


# ----------------------------------------------------------------------------- tax
@dataclass(frozen=True)
class TaxSplit:
    """A gross amount decomposed into taxable value and tax (all paise); ``gross == net + tax``."""

    net: Paise
    tax: Paise
    gross: Paise
    rate_bp: int
    """Tax rate in basis points (12 % → 1200) so rates are exact integers."""

    @property
    def rate_percent(self) -> Decimal:
        """Rate as a Decimal percentage (1200 → Decimal('12'))."""
        return Decimal(self.rate_bp) / _HUNDRED

    def halves(self) -> tuple[Paise, Paise]:
        """CGST, SGST — split the tax in two with any odd paisa going to CGST (fixed, documented rule).

        >>> TaxSplit(net=893, tax=107, gross=1000, rate_bp=1200).halves()
        (54, 53)
        """
        half, odd = divmod(self.tax, 2)
        return half + odd, half


def rate_to_bp(rate_percent: Decimal | int | str) -> int:
    """'12' / '0.25' / 5 → basis points (1200 / 25 / 500)."""
    rate = rate_percent if isinstance(rate_percent, Decimal) else Decimal(str(rate_percent))
    if rate < 0 or rate > 100:
        raise MoneyError(f"tax rate out of range: {rate_percent!r}")
    return int((rate * _HUNDRED).quantize(_QUANT_PAISE, rounding=ROUND_HALF_UP))


def split_inclusive_tax(gross: Paise, rate_bp: int) -> TaxSplit:
    """Shelf price already includes GST (the normal case in a puja shop): back out the tax.

    net = gross × 10000 / (10000 + rate_bp), half-up; tax = gross − net (so the sum is exact).

    >>> split_inclusive_tax(1000, 1200)
    TaxSplit(net=893, tax=107, gross=1000, rate_bp=1200)
    >>> split_inclusive_tax(500, 0)
    TaxSplit(net=500, tax=0, gross=500, rate_bp=0)
    """
    if rate_bp == 0:
        return TaxSplit(gross, 0, gross, 0)
    net = _half_up(Decimal(gross) * _BASIS_POINTS / (_BASIS_POINTS + rate_bp))
    return TaxSplit(net, gross - net, gross, rate_bp)


def add_exclusive_tax(net: Paise, rate_bp: int) -> TaxSplit:
    """Price quoted before tax: add GST on top.

    >>> add_exclusive_tax(893, 1200)
    TaxSplit(net=893, tax=107, gross=1000, rate_bp=1200)
    """
    tax = _half_up(Decimal(net) * rate_bp / _BASIS_POINTS)
    return TaxSplit(net, tax, net + tax, rate_bp)


# ----------------------------------------------------------------------------- bill-level helpers
def round_off(total: Paise) -> tuple[Paise, Paise]:
    """Round a bill total to the nearest rupee (half-up); returns (rounded_total, round_off_line).

    Indian receipts show a 'Round off' line so cash can be tendered in whole rupees.

    >>> round_off(12345), round_off(12350), round_off(12399)
    ((12300, -45), (12400, 50), (12400, 1))
    """
    rupees = (Decimal(total) / _HUNDRED).quantize(_QUANT_RUPEE, rounding=ROUND_HALF_UP)
    rounded = int(rupees) * PAISE_PER_RUPEE
    return rounded, rounded - total


def allocate(total: Paise, weights: list[int]) -> list[Paise]:
    """Split ``total`` proportionally to ``weights`` so the parts sum exactly to ``total``.

    Largest-remainder method — used to spread a bill-level discount or round-off across lines for
    per-line tax reporting without ever creating or losing a paisa.

    >>> allocate(100, [1, 1, 1]), allocate(1001, [3, 7]), allocate(-10, [1, 2])
    ([34, 33, 33], [300, 701], [-3, -7])
    """
    if not weights:
        raise MoneyError("allocate needs at least one weight")
    if any(w < 0 for w in weights):
        raise MoneyError("weights must be non-negative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        parts = [0] * len(weights)
        parts[0] = total
        return parts
    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    raw = [Decimal(magnitude) * w / weight_sum for w in weights]
    floors = [int(r) for r in raw]
    remainder = magnitude - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - floors[i], weights[i]), reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return [sign * p for p in floors]
