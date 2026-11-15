"""Barcode arithmetic (ADR-023, docs/05 "Barcode generation"; stdlib only).

Internal barcodes are EAN-13 in the in-store range: ``29`` + 10-digit zero-padded Product ID + check digit
(ID 1042 → ``2900000010426``). Being derivable from the ID means a torn label can always be recreated.
Manufacturer codes (EAN-13 / EAN-8 / UPC-A / Code-128) are only *validated* here.
"""

from __future__ import annotations

import re
from typing import Final

INTERNAL_PREFIX: Final = "29"
INTERNAL_LEN: Final = 13
_DIGITS: Final = re.compile(r"^\d+$")


def normalise(text: str) -> str:
    """Trim whitespace and scanner suffixes (CR/LF/TAB)."""
    return text.strip().strip("\r\n\t ")


def ean13_check_digit(first12: str) -> int:
    """GS1 modulo-10: weights 1,3 from the left over the first 12 digits."""
    if len(first12) != 12 or not _DIGITS.match(first12):
        raise ValueError(f"need 12 digits, got {first12!r}")
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def ean8_check_digit(first7: str) -> int:
    """Modulo-10 with weights 3,1 from the left over 7 digits."""
    if len(first7) != 7 or not _DIGITS.match(first7):
        raise ValueError(f"need 7 digits, got {first7!r}")
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(first7))
    return (10 - total % 10) % 10


def is_valid_ean13(code: str) -> bool:
    """13 digits with a correct check digit."""
    code = normalise(code)
    return len(code) == 13 and bool(_DIGITS.match(code)) and ean13_check_digit(code[:12]) == int(code[12])


def is_valid_ean8(code: str) -> bool:
    """8 digits with a correct check digit."""
    code = normalise(code)
    return len(code) == 8 and bool(_DIGITS.match(code)) and ean8_check_digit(code[:7]) == int(code[7])


def is_valid_upca(code: str) -> bool:
    """12 digits; UPC-A is EAN-13 with a leading zero."""
    code = normalise(code)
    return len(code) == 12 and bool(_DIGITS.match(code)) and is_valid_ean13("0" + code)


def internal_ean13(product_id: int) -> str:
    """The product's permanent internal barcode."""
    if product_id <= 0 or product_id >= 10**10:
        raise ValueError(f"product id out of range: {product_id}")
    body = f"{INTERNAL_PREFIX}{product_id:010d}"
    return body + str(ean13_check_digit(body))


def product_id_from_internal(code: str) -> int | None:
    """Inverse of :func:`internal_ean13`; None for anything that is not one of ours."""
    code = normalise(code)
    if not (len(code) == INTERNAL_LEN and code.startswith(INTERNAL_PREFIX) and is_valid_ean13(code)):
        return None
    return int(code[2:12])


RECEIPT_PREFIX: Final = "28"


def receipt_ean13(fin_year: str, seq_no: int) -> str:
    """The barcode printed on a receipt: ``28`` + FY start year (2 digits) + 8-digit bill sequence + check.

    ``"26-27", 123`` → ``2826000001233``-style. Scanning it at the counter opens the bill (returns / reprint)
    and, unlike Code-128, every cheap scanner reads it and every ESC/POS printer draws it natively.
    """
    yy = int(fin_year.split("-")[0])
    if seq_no <= 0 or seq_no >= 10**8:
        raise ValueError(f"sequence out of range: {seq_no}")
    body = f"{RECEIPT_PREFIX}{yy:02d}{seq_no:08d}"
    return body + str(ean13_check_digit(body))


def parse_receipt_ean13(code: str) -> tuple[str, int] | None:
    """Inverse of :func:`receipt_ean13` → ``(fin_year, seq_no)`` or None."""
    code = normalise(code)
    if not (len(code) == 13 and code.startswith(RECEIPT_PREFIX) and is_valid_ean13(code)):
        return None
    yy = int(code[2:4])
    return f"{yy:02d}-{(yy + 1) % 100:02d}", int(code[4:12])


def detect_symbology(code: str) -> str:
    """Best-effort classification for the ``product_barcode.symbology`` column."""
    code = normalise(code)
    if _DIGITS.match(code):
        if len(code) == 13:
            return "EAN13"
        if len(code) == 8:
            return "EAN8"
        if len(code) == 12:
            return "UPCA"
    return "CODE128" if code else "OTHER"


def validate(code: str) -> str | None:
    """Plain reason why a code is unacceptable (None when fine). Numeric codes must carry a correct check digit."""
    code = normalise(code)
    if not code:
        return "empty"
    if len(code) > 48:
        return "too long"
    sym = detect_symbology(code)
    if sym == "EAN13" and not is_valid_ean13(code):
        return "EAN-13 check digit wrong"
    if sym == "EAN8" and not is_valid_ean8(code):
        return "EAN-8 check digit wrong"
    if sym == "UPCA" and not is_valid_upca(code):
        return "UPC-A check digit wrong"
    return None
