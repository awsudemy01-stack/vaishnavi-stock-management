"""Receipt renderers (hardware layer; stdlib only): ESC/POS for 80 / 58 mm thermal printers and HTML for the archive.

ESC/POS facts used here (Epson reference, honoured by every TM-compatible clone):

* ``ESC @`` initialise · ``ESC a n`` align · ``ESC ! n`` print mode (bit 3 emphasised, bit 4 double
  height, bit 5 double width) · ``ESC E n`` emphasis · ``ESC d n`` feed n lines
* ``GS h n`` / ``GS w n`` / ``GS H n`` barcode height / module width / HRI position, then
  ``GS k 2 <13 digits> NUL`` draws an **EAN-13 natively** (the receipt code)
* ``GS ( k`` function set for **QR** (model · size · error level · store · print)
* ``ESC p m t1 t2`` cash-drawer kick · ``GS V 65 n`` feed-and-partial-cut

Thermal printers speak **code pages**, not Unicode: the rupee sign becomes ``Rs``, the dagger ``^``,
typographic dashes ``-``; Devanagari is dropped unless the caller supplies a ``hindi_raster`` hook that
turns a text line into a ``GS v 0`` bitmap (Pillow-based, wired on the shop PC). The HTML keeps every
glyph. ``text_lines()`` strips the command stream back to the visible text so tests and the log console
can show what the paper will say.
"""

from __future__ import annotations

import html
import logging
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from vaishnavi.hardware.labels import barcode_svg

log = logging.getLogger("vaishnavi.hardware.receipts")

ESC: Final = b"\x1b"
GS: Final = b"\x1d"
COLUMNS: Final = {80: 48, 58: 32}
_TRANSLIT: Final = {"\u20b9": "Rs", "\u2020": "^", "\u2013": "-", "\u2014": "-", "\u00b7": "-", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."}


# ----------------------------------------------------------------------------- text helpers
def to_printer_text(text: str) -> str:
    """ASCII-safe text for a code-page printer (see module docstring)."""
    out = []
    for ch in text:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            decomposed = unicodedata.normalize("NFKD", ch)
            ascii_part = "".join(c for c in decomposed if ord(c) < 128)
            out.append(ascii_part)
    return "".join(out).replace(" ()", "").replace("()", "")


def _fit(text: str, width: int, *, align: str = "<") -> str:
    text = to_printer_text(text)[:width]
    return f"{text:{align}{width}}"


def _row(cols: list[tuple[str, int, str]]) -> str:
    """``[(text, width, align)]`` → one fixed-width line (single spaces between)."""
    return " ".join(_fit(t, w, align=a) for t, w, a in cols)


def _wrap(text: str, width: int, *, indent: str = "") -> list[str]:
    """Word-wrap printer text to the paper width (never truncates)."""
    import textwrap  # noqa: PLC0415

    plain = to_printer_text(text)
    return textwrap.wrap(plain, width=width, initial_indent=indent, subsequent_indent=indent, break_long_words=True) or [""]


def _ean13_cmd(code: str, *, narrow: bool) -> bytes:
    out = bytearray()
    out += GS + b"h" + bytes([60 if narrow else 80])
    out += GS + b"w" + (b"\x02" if narrow else b"\x03")
    out += GS + b"H\x02"
    out += GS + b"k\x02" + code.encode("ascii") + b"\x00"
    return bytes(out)


def _qr_cmd(payload: str, *, size: int = 6) -> bytes:
    data = payload.encode("utf-8")
    length = len(data) + 3
    out = bytearray()
    out += GS + b"(k\x04\x00\x31\x41\x32\x00"  # model 2
    out += GS + b"(k\x03\x00\x31\x43" + bytes([size])  # module size
    out += GS + b"(k\x03\x00\x31\x45\x30"  # error correction L
    out += GS + b"(k" + bytes([length & 0xFF, length >> 8]) + b"\x31\x50\x30" + data  # store
    out += GS + b"(k\x03\x00\x31\x51\x30"  # print
    return bytes(out)


# ----------------------------------------------------------------------------- ESC/POS
def render_escpos(model: Any, *, paper_mm: int = 80, hindi_raster: Callable[[str], bytes] | None = None, drawer_kick: bool = False) -> bytes:
    """The receipt as a byte stream for a thermal printer."""
    width = COLUMNS.get(paper_mm, 48)
    narrow = width <= 32
    rule = "-" * width
    out = bytearray()

    def line(text: str = "") -> None:
        out.extend(to_printer_text(text)[:width].encode("ascii", "replace") + b"\n")

    def wrapped(text: str, *, indent: str = "") -> None:
        for piece in _wrap(text, width, indent=indent):
            line(piece)

    def hindi(text: str) -> None:
        if text and hindi_raster is not None:
            out.extend(hindi_raster(text))

    out += ESC + b"@"
    out += ESC + b"a\x01"  # centre
    out += ESC + (b"!\x18" if narrow else b"!\x38")  # emphasised + double height (+ double width on 80 mm)
    line(model.shop_name[: width if narrow else width // 2])
    out += ESC + b"!\x00"
    hindi(model.shop_name_hi)
    for h in model.header_lines:
        wrapped(h)
    if model.copy_label:
        out += ESC + b"E\x01"
        line(f"*** {model.copy_label} ***")
        out += ESC + b"E\x00"
    out += ESC + b"a\x00"  # left
    line(rule)
    if narrow:
        line(f"Bill: {model.invoice_no}")
        line(model.datetime_text)
        line(f"Cashier: {model.cashier}  Shift: {model.shift_label}")
    else:
        line(_row([(f"Invoice: {model.invoice_no}", 30, "<"), (model.datetime_text, 17, ">")]))
        line(_row([(f"Cashier: {model.cashier}", 30, "<"), (f"Shift: {model.shift_label}", 17, ">")]))
    line(rule)
    if narrow:
        line(_row([("Item", 32, "<")]))
        line(_row([("Qty", 6, ">"), ("Price", 8, ">"), ("Disc", 6, ">"), ("Amount", 9, ">")]))
    else:
        line(_row([("Item", 16, "<"), ("Qty", 5, ">"), ("Price", 10, ">"), ("Disc", 5, ">"), ("Amount", 8, ">")]))
    for ln in model.lines:
        name = f"{ln.name} {ln.marker}".strip() if ln.marker else ln.name
        if narrow:
            wrapped(name)
            line(_row([(ln.qty_text, 6, ">"), (ln.price_text, 8, ">"), (ln.discount_text, 6, ">"), (ln.amount_text, 9, ">")]))
        elif len(to_printer_text(name)) > 16:
            wrapped(name)
            line(_row([("", 16, "<"), (ln.qty_text, 5, ">"), (ln.price_text, 10, ">"), (ln.discount_text, 5, ">"), (ln.amount_text, 8, ">")]))
        else:
            line(_row([(name, 16, "<"), (ln.qty_text, 5, ">"), (ln.price_text, 10, ">"), (ln.discount_text, 5, ">"), (ln.amount_text, 8, ">")]))
        if ln.note:
            wrapped(ln.note, indent="  ")
    line(rule)
    label_w = width - 10
    for row in model.total_rows:
        line(_row([(row.label, label_w, "<"), (row.amount_text, 9, ">")]))
    for row in model.tax_rows:
        line(_row([(row.label, label_w, "<"), (row.amount_text, 9, ">")]))
    out += ESC + b"!\x18"  # emphasised + double height
    line(_row([("TOTAL", width // 2 - 1, "<"), (model.total_text, width // 2, ">")]))
    out += ESC + b"!\x00"
    if model.saved_text:
        wrapped(f"You saved {model.saved_text} today")
    for p in model.payment_lines:
        if narrow and "  Change " in p:
            paid, change = p.split("  Change ", 1)
            wrapped(paid)
            wrapped(f"Change {change}")
        else:
            wrapped(p)
    if model.legend:
        if narrow:
            for part in model.legend.split("   "):
                wrapped(part)
        else:
            wrapped(model.legend)
    line(rule)
    out += ESC + b"a\x01"
    if model.upi_payload:
        out += _qr_cmd(model.upi_payload, size=5 if narrow else 6)
        wrapped(f"Pay by UPI: {model.upi_id}")
    for f in model.footer_lines:
        if f.startswith("Pay by UPI"):
            continue
        wrapped(f)
    out += _ean13_cmd(model.barcode, narrow=narrow)
    out += ESC + b"d\x03"  # feed
    if drawer_kick:
        out += ESC + b"p\x00\x19\xfa"
    out += GS + b"V\x41\x03"  # feed and partial cut
    log.debug("RECEIPT_ESCPOS sale=%s paper=%dmm bytes=%d lines=%d qr=%s", model.sale_id, paper_mm, len(out), len(model.lines), bool(model.upi_payload))
    return bytes(out)


def text_lines(data: bytes) -> list[str]:
    """Visible text of an ESC/POS stream (commands stripped) — for tests, logs and the on-screen preview."""
    lines: list[str] = []
    buf = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x1B:  # ESC
            cmd = data[i + 1 : i + 2]
            if cmd == b"@":
                i += 2
            elif cmd in (b"a", b"!", b"E", b"d", b"M", b"-", b"G"):
                i += 3
            elif cmd == b"p":
                i += 5
            else:
                i += 2
            continue
        if b == 0x1D:  # GS
            cmd = data[i + 1 : i + 2]
            if cmd == b"(":
                length = data[i + 3] | (data[i + 4] << 8)
                i += 5 + length
            elif cmd == b"k":
                m = data[i + 2]
                if m <= 6:
                    end = data.index(b"\x00", i + 3)
                    i = end + 1
                else:
                    i += 4 + data[i + 3]
            elif cmd == b"V":
                i += 4 if data[i + 2] in (65, 66) else 3
            elif cmd in (b"h", b"w", b"H", b"!", b"B", b"L", b"W", b"f"):
                i += 3
            else:
                i += 2
            continue
        if b == 0x0A:
            lines.append(buf.decode("ascii", "replace"))
            buf.clear()
        else:
            buf.append(b)
        i += 1
    if buf:
        lines.append(buf.decode("ascii", "replace"))
    return lines


# ----------------------------------------------------------------------------- HTML
_CSS: Final = """
@page { size: %(paper)smm auto; margin: 3mm; }
body { font-family: 'DejaVu Sans Mono', Consolas, monospace; font-size: 11px; width: %(inner)smm; margin: 0 auto; color: #000; }
h1 { font-size: 15px; text-align: center; margin: 0; }
.hi, .center { text-align: center; }
.rule { border-top: 1px dashed #000; margin: 4px 0; }
table { width: 100%%; border-collapse: collapse; }
td { padding: 1px 2px; vertical-align: top; }
td.n { text-align: right; white-space: nowrap; }
tr.note td { font-size: 9px; color: #333; padding-top: 0; }
.total td { font-weight: bold; font-size: 13px; }
.copy { text-align: center; font-weight: bold; border: 1px solid #000; padding: 2px; margin: 3px 0; }
.legend { font-size: 9px; }
.saved { font-weight: bold; text-align: center; }
.barcode { text-align: center; margin-top: 4px; }
"""


def render_html(model: Any) -> str:
    """Self-contained HTML (CSS ``@page`` sized to the paper; prints fine on A4 too)."""
    e = html.escape
    paper = int(getattr(model, "paper_mm", 80) or 80)
    parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{e(model.invoice_no)}</title><style>{_CSS % {'paper': paper, 'inner': paper - 8}}</style></head><body>",
        f"<h1>{e(model.shop_name)}</h1>",
    ]
    if model.shop_name_hi:
        parts.append(f"<div class='hi'>{e(model.shop_name_hi)}</div>")
    for h in model.header_lines:
        parts.append(f"<div class='center'>{e(h)}</div>")
    if model.copy_label:
        parts.append(f"<div class='copy'>{e(model.copy_label)}</div>")
    parts.append("<div class='rule'></div>")
    parts.append(f"<table><tr><td>Invoice: <b>{e(model.invoice_no)}</b></td><td class='n'>{e(model.datetime_text)}</td></tr>")
    parts.append(f"<tr><td>Cashier: {e(model.cashier)}</td><td class='n'>Shift: {e(model.shift_label)}</td></tr></table>")
    parts.append("<div class='rule'></div><table><tr><td>Item</td><td class='n'>Qty</td><td class='n'>Price</td><td class='n'>Disc</td><td class='n'>Amount</td></tr>")
    for ln in model.lines:
        name = e(ln.name) + (f" <b>{e(ln.marker)}</b>" if ln.marker else "")
        parts.append(f"<tr><td>{name}</td><td class='n'>{e(ln.qty_text)}</td><td class='n'>{e(ln.price_text)}</td><td class='n'>{e(ln.discount_text)}</td><td class='n'>{e(ln.amount_text)}</td></tr>")
        if ln.note:
            parts.append(f"<tr class='note'><td colspan='5'>&nbsp;&nbsp;{e(ln.note)}</td></tr>")
    parts.append("</table><div class='rule'></div><table>")
    for row in model.total_rows:
        parts.append(f"<tr><td>{e(row.label)}</td><td class='n'>{e(row.amount_text)}</td></tr>")
    for row in model.tax_rows:
        parts.append(f"<tr><td>{e(row.label)}</td><td class='n'>{e(row.amount_text)}</td></tr>")
    parts.append(f"<tr class='total'><td>TOTAL</td><td class='n'>{e(model.total_text)}</td></tr></table>")
    if model.saved_text:
        parts.append(f"<div class='saved'>You saved {e(model.saved_text)} today</div>")
    for p in model.payment_lines:
        parts.append(f"<div>{e(p)}</div>")
    if model.legend:
        parts.append(f"<div class='legend'>{e(model.legend)}</div>")
    parts.append("<div class='rule'></div>")
    if model.upi_payload:
        parts.append(f"<div class='center'><a href='{e(model.upi_payload)}'>Pay by UPI: {e(model.upi_id or '')}</a></div>")
    for f in model.footer_lines:
        if f.startswith("Pay by UPI"):
            continue
        parts.append(f"<div class='center'>{e(f)}</div>")
    parts.append(f"<div class='barcode'>{barcode_svg(model.barcode, width_mm=min(40.0, paper - 20), height_mm=10.0)}</div>")
    parts.append("</body></html>")
    text = "\n".join(parts)
    log.debug("RECEIPT_HTML sale=%s chars=%d", model.sale_id, len(text))
    return text


def render_pdf(html_text: str, path: Path) -> bool:
    """HTML → PDF when ``weasyprint`` is importable on the shop PC; False (and no file) otherwise."""
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    try:
        HTML(string=html_text).write_pdf(str(path))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("receipt PDF failed: %s", exc)
        return False
