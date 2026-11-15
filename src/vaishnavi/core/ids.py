"""Identifiers: ULIDs, the shop's internal EAN-13 barcodes, generic barcode checks, invoice numbers.

Stdlib only (ADR-023, ADR-040). The product identity triangle is **ID ↔ SKU ↔ barcode**:

* ``product.id`` — a permanent integer; never reused.
* internal EAN-13 — ``29`` + ten-digit zero-padded id + check digit. Prefix 20–29 is the GS1
  *restricted circulation* range, i.e. legal for in-store use and never clashes with retail codes.
  A damaged label can therefore always be regenerated from the ID alone.
* manufacturer barcodes (EAN-13 / EAN-8 / UPC-A) are validated with the same modulo-10 rule and
  stored in the ``barcode`` table alongside the internal one.

Invoice numbers are gap-free, per financial year (April–March): ``VPS/25-26/000123``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Final

# ----------------------------------------------------------------------------- ULID
_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE: Final = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ulid_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """26-char ULID (time-ordered, URL-safe); monotonic within the same millisecond.

    Used for correlation ids, client transaction ids and file names. The 80-bit random part is
    incremented (not re-drawn) when two ids share a millisecond, so sort order equals creation order.
    """
    global _last_ms, _last_rand
    ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    with _ulid_lock:
        if ms == _last_ms:
            _last_rand = (_last_rand + 1) & ((1 << 80) - 1)
        else:
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10), "big")
        rand = _last_rand
    return _encode(ms, 10) + _encode(rand, 16)


def is_ulid(value: str) -> bool:
    """Shape check for a ULID string."""
    return bool(_ULID_RE.match(value or ""))


def ulid_timestamp_ms(value: str) -> int:
    """Milliseconds since the epoch encoded in a ULID (raises ValueError when malformed)."""
    if not is_ulid(value):
        raise ValueError(f"not a ULID: {value!r}")
    ms = 0
    for ch in value[:10]:
        ms = ms * 32 + _CROCKFORD.index(ch)
    return ms


# ----------------------------------------------------------------------------- barcodes
INTERNAL_PREFIX: Final = "29"
INTERNAL_ID_DIGITS: Final = 10
MAX_INTERNAL_ID: Final = 10**INTERNAL_ID_DIGITS - 1
_DIGITS_RE: Final = re.compile(r"^\d+$")


class BarcodeError(ValueError):
    """Malformed barcode or out-of-range id (error code E-CAT-020)."""

    code = "E-CAT-020"


def gs1_check_digit(payload: str) -> int:
    """Modulo-10 check digit for GS1 codes (EAN-8/13, UPC-A, GTIN-14) given the payload without it.

    Weights alternate 3,1,3,… from the **rightmost** payload digit.

    >>> gs1_check_digit("400638133393"), gs1_check_digit("290000000012")
    (1, 4)
    """
    if not _DIGITS_RE.match(payload):
        raise BarcodeError(f"payload must be digits: {payload!r}")
    total = 0
    for i, ch in enumerate(reversed(payload)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10


def is_valid_gs1(code: str) -> bool:
    """True for a well-formed EAN-8, UPC-A (12), EAN-13 or GTIN-14 with a correct check digit.

    >>> is_valid_gs1("4006381333931"), is_valid_gs1("4006381333932"), is_valid_gs1("12345")
    (True, False, False)
    """
    code = (code or "").strip()
    if len(code) not in (8, 12, 13, 14) or not _DIGITS_RE.match(code):
        return False
    return gs1_check_digit(code[:-1]) == int(code[-1])


def internal_barcode(product_id: int) -> str:
    """Internal EAN-13 for a product id: '29' + 10-digit id + check digit.

    >>> internal_barcode(123)
    '2900000001237'
    """
    if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0 or product_id > MAX_INTERNAL_ID:
        raise BarcodeError(f"product id out of range for an internal barcode: {product_id!r}")
    payload = f"{INTERNAL_PREFIX}{product_id:0{INTERNAL_ID_DIGITS}d}"
    return payload + str(gs1_check_digit(payload))


def is_internal_barcode(code: str) -> bool:
    """True when ``code`` is one of *our* internal EAN-13s (prefix 29, valid check digit)."""
    code = (code or "").strip()
    return len(code) == 13 and code.startswith(INTERNAL_PREFIX) and is_valid_gs1(code)


def parse_internal_barcode(code: str) -> int:
    """Product id encoded in an internal barcode (raises BarcodeError otherwise).

    >>> parse_internal_barcode("2900000001237")
    123
    """
    if not is_internal_barcode(code):
        raise BarcodeError(f"not an internal barcode: {code!r}")
    return int(code[len(INTERNAL_PREFIX) : -1])


def normalise_scan(raw: str) -> str:
    """Clean a scanner-wedge string: strip whitespace/CR/LF and a common 'ñ'/'~' prefix some wedges add."""
    s = (raw or "").strip().strip("\r\n\t ")
    if s[:1] in ("~", "\u00f1"):
        s = s[1:]
    if len(s) == 12 and _DIGITS_RE.match(s) and is_valid_gs1(s):
        return "0" + s  # UPC-A presented as EAN-13 so one lookup key serves both
    return s


# ----------------------------------------------------------------------------- invoice numbers
INVOICE_PREFIX_DEFAULT: Final = "VPS"
INVOICE_SEQ_DIGITS: Final = 6
_INVOICE_RE: Final = re.compile(r"^(?P<prefix>[A-Z0-9]{1,8})/(?P<fy>\d{2}-\d{2})/(?P<seq>\d{6,})$")


def financial_year(on: date) -> str:
    """Indian financial year label for a date: 1 Apr 2025 – 31 Mar 2026 → '25-26'.

    >>> financial_year(date(2025, 11, 14)), financial_year(date(2026, 3, 31)), financial_year(date(2026, 4, 1))
    ('25-26', '25-26', '26-27')
    """
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


@dataclass(frozen=True)
class InvoiceNumber:
    """Parsed invoice number."""

    prefix: str
    fy: str
    sequence: int

    def __str__(self) -> str:
        return format_invoice(self.sequence, self.fy, self.prefix)


def format_invoice(sequence: int, fy: str, prefix: str = INVOICE_PREFIX_DEFAULT) -> str:
    """'VPS/25-26/000123' — the sequence is per financial year and gap-free (enforced by the DB).

    >>> format_invoice(123, "25-26")
    'VPS/25-26/000123'
    """
    if sequence <= 0:
        raise ValueError(f"invoice sequence must be positive: {sequence}")
    return f"{prefix}/{fy}/{sequence:0{INVOICE_SEQ_DIGITS}d}"


def parse_invoice(text: str) -> InvoiceNumber:
    """Inverse of ``format_invoice`` (raises ValueError when malformed).

    >>> parse_invoice("VPS/25-26/000123")
    InvoiceNumber(prefix='VPS', fy='25-26', sequence=123)
    """
    m = _INVOICE_RE.match((text or "").strip().upper())
    if not m:
        raise ValueError(f"not an invoice number: {text!r}")
    return InvoiceNumber(m["prefix"], m["fy"], int(m["seq"]))
