"""Label model and renderers (docs/11 §2; stdlib only).

* :func:`ean13_modules` — the 95-module bar pattern of an EAN-13 (for SVG on screen / A4 sheets).
* :func:`render_escpos` — one 80 mm / 58 mm thermal strip: name EN · name HI · ``CODE · #ID`` · native
  ``GS k`` EAN-13 · large ₹ price · optional MRP · cut.
* :func:`render_html_sheet` — printable A4 sticker sheet (65-up 38×21 mm or 40-up 52×30 mm) with SVG
  barcodes; the browser's print dialog produces the PDF, so no PDF library is required. When ReportLab is
  installed, :func:`render_pdf_sheet` draws the same grid natively (returns None otherwise).
"""

from __future__ import annotations

import logging
from html import escape
from typing import Final

from vaishnavi.core.barcode import is_valid_ean13, normalise
from vaishnavi.core.money import format_inr
from vaishnavi.services.labels import LabelModel

__all__ = ["LAYOUTS", "LabelModel", "barcode_svg", "ean13_modules", "render_escpos", "render_escpos_batch", "render_html_sheet", "render_pdf_sheet"]

log = logging.getLogger("vaishnavi.hardware.labels")

# EAN-13 encoding tables (GS1 General Specifications §5.1.1)
_L: Final = ("0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011")
_R: Final = tuple(p.translate(str.maketrans("01", "10")) for p in _L)  # R = L bit-inverted; G = R reversed (see _g_pattern)
_PARITY: Final = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")
LAYOUTS: Final[dict[str, dict[str, object]]] = {
    "65-up": {"cols": 5, "rows": 13, "w_mm": 38.1, "h_mm": 21.2, "font_pt": 6.5},
    "40-up": {"cols": 4, "rows": 10, "w_mm": 52.5, "h_mm": 29.7, "font_pt": 8},
}


def _g_pattern(digit: int) -> str:
    # G-code = R-code read right-to-left (GS1 table); derived instead of hand-typed.
    return _R[digit][::-1]


def ean13_modules(code: str) -> str:
    """95 characters of ``'1'`` (bar) / ``'0'`` (space): guard · 6 left digits · centre · 6 right digits · guard."""
    code = normalise(code)
    if not is_valid_ean13(code):
        raise ValueError(f"not a valid EAN-13: {code!r}")
    digits = [int(c) for c in code]
    parity = _PARITY[digits[0]]
    out = ["101"]
    for i, d in enumerate(digits[1:7]):
        out.append(_L[d] if parity[i] == "L" else _g_pattern(d))
    out.append("01010")
    for d in digits[7:]:
        out.append(_R[d])
    out.append("101")
    modules = "".join(out)
    assert len(modules) == 95
    return modules


def barcode_svg(code: str, *, width_mm: float = 30.0, height_mm: float = 10.0, text: bool = True) -> str:
    """Inline SVG of the EAN-13 (bars + human-readable digits)."""
    modules = ean13_modules(code)
    module_w = width_mm / 95
    bars = []
    x = 0.0
    for ch in modules:
        if ch == "1":
            bars.append(f'<rect x="{x:.3f}" y="0" width="{module_w:.3f}" height="{height_mm:.2f}"/>')
        x += module_w
    label = f'<text x="{width_mm / 2:.2f}" y="{height_mm + 2.6:.2f}" font-size="2.6" text-anchor="middle" font-family="Roboto Mono, monospace">{escape(code)}</text>' if text else ""
    total_h = height_mm + (3.2 if text else 0)
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" height="{total_h:.2f}mm" viewBox="0 0 {width_mm} {total_h:.2f}" shape-rendering="crispEdges">{"".join(bars)}{label}</svg>'


# ----------------------------------------------------------------------------- ESC/POS
ESC, GS = b"\x1b", b"\x1d"


def _escpos_text(text: str) -> bytes:
    return text.encode("utf-8", errors="replace")


def render_escpos(model: LabelModel, *, paper_mm: int = 80, cut: bool = True) -> bytes:
    """One label strip. Hindi is sent as UTF-8 text — printers without Devanagari fonts print the EN line; the
    receipt renderer's raster path (Phase 3) is reused once available."""
    narrow = paper_mm <= 58
    out = bytearray()
    out += ESC + b"@"  # initialise
    out += ESC + b"a\x01"  # centre
    out += ESC + b"!" + (b"\x08" if narrow else b"\x18")  # emphasised (+ double height on 80 mm)
    out += _escpos_text(model.name[: 24 if narrow else 32]) + b"\n"
    out += ESC + b"!\x00"
    if model.name_hi:
        out += _escpos_text(model.name_hi[: 24 if narrow else 32]) + b"\n"
    out += _escpos_text(model.code_line) + b"\n"
    # Native EAN-13: GS h (height), GS w (module width), GS H (HRI below), GS k 2 <data> NUL
    out += GS + b"h" + bytes([60 if narrow else 80])
    out += GS + b"w" + (b"\x02" if narrow else b"\x03")
    out += GS + b"H\x02"
    out += GS + b"k\x02" + model.barcode.encode("ascii") + b"\x00"
    out += b"\n"
    out += ESC + b"!" + (b"\x10" if narrow else b"\x30")  # double height (+ width on 80 mm)
    out += _escpos_text(model.price_text) + b"\n"
    out += ESC + b"!\x00"
    if model.mrp_paise and model.mrp_paise != model.price_paise:
        out += _escpos_text(f"MRP {format_inr(model.mrp_paise)}") + b"\n"
    out += b"\n\n"
    if cut:
        out += GS + b"V\x01"  # partial cut
    log.debug("LABEL_ESCPOS product=%s bytes=%d paper=%dmm", model.product_id, len(out), paper_mm)
    return bytes(out)


def render_escpos_batch(models: list[LabelModel], *, paper_mm: int = 80) -> bytes:
    """Several strips in one job."""
    return b"".join(render_escpos(m, paper_mm=paper_mm) for m in models)


# ----------------------------------------------------------------------------- A4 sheets
def render_html_sheet(models: list[LabelModel], *, layout: str = "65-up") -> str:
    """Printable A4 sheet(s); one ``<svg>`` per label; page breaks after each full sheet."""
    spec = LAYOUTS[layout]
    cols, rows = int(spec["cols"]), int(spec["rows"])
    w, h, font = float(spec["w_mm"]), float(spec["h_mm"]), float(spec["font_pt"])
    per_page = cols * rows
    cells: list[str] = []
    for m in models:
        bar_w = min(w - 4, 32.0)
        bar_h = max(6.0, h * 0.34)
        cells.append(
            f'<div class="label"><div class="name">{escape(m.name)}</div>'
            + (f'<div class="hi">{escape(m.name_hi)}</div>' if m.name_hi else "")
            + f'<div class="code">{escape(m.code_line)}</div>{barcode_svg(m.barcode, width_mm=bar_w, height_mm=bar_h)}<div class="price">{escape(m.price_text)}</div></div>'
        )
    pages: list[str] = []
    for start in range(0, len(cells), per_page):
        chunk = cells[start : start + per_page]
        cls = "sheet page-break" if start > 0 else "sheet"
        pages.append(f'<div class="{cls}">{"".join(chunk)}</div>')
    css = (
        f"@page {{ size: A4; margin: 8mm; }} body {{ margin: 0; font-family: 'Noto Sans', 'Noto Sans Devanagari', sans-serif; }} "
        f".sheet {{ display: grid; grid-template-columns: repeat({cols}, {w}mm); grid-auto-rows: {h}mm; gap: 1mm 2mm; }} "
        f".page-break {{ break-before: page; page-break-before: always; }} "
        f".label {{ box-sizing: border-box; padding: 1mm; overflow: hidden; text-align: center; font-size: {font}pt; line-height: 1.15; border: 0.1mm dashed #ddd; }} "
        f".name {{ font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }} .hi {{ font-size: {font - 0.5}pt; }} "
        f".code {{ font-family: 'Roboto Mono', monospace; font-size: {font - 1}pt; color: #444; }} .price {{ font-weight: 700; font-size: {font + 3}pt; }} svg {{ display: block; margin: 0.5mm auto; }}"
    )
    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Labels {layout}</title><style>{css}</style></head><body>{''.join(pages)}</body></html>"
    log.debug("LABEL_SHEET layout=%s labels=%d pages=%d", layout, len(models), len(pages))
    return html


def render_pdf_sheet(models: list[LabelModel], *, layout: str = "65-up") -> bytes | None:
    """Native PDF through ReportLab when the wheel is present; None otherwise (caller falls back to HTML)."""
    try:
        from reportlab.graphics.barcode import eanbc  # noqa: PLC0415
        from reportlab.graphics.shapes import Drawing  # noqa: PLC0415
        from reportlab.lib.pagesizes import A4  # noqa: PLC0415
        from reportlab.lib.units import mm  # noqa: PLC0415
        from reportlab.pdfgen import canvas  # noqa: PLC0415
    except ImportError:
        return None
    import io  # noqa: PLC0415

    spec = LAYOUTS[layout]
    cols, rows = int(spec["cols"]), int(spec["rows"])
    w, h, font = float(spec["w_mm"]) * mm, float(spec["h_mm"]) * mm, float(spec["font_pt"])
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    margin = 8 * mm
    per_page = cols * rows
    for i, m in enumerate(models):
        if i and i % per_page == 0:
            c.showPage()
        idx = i % per_page
        x = margin + (idx % cols) * (w + 2 * mm)
        y = page_h - margin - (idx // cols + 1) * (h + 1 * mm)
        c.setFont("Helvetica-Bold", font)
        c.drawCentredString(x + w / 2, y + h - font * 1.3, m.name[:28])
        c.setFont("Helvetica", font - 1)
        c.drawCentredString(x + w / 2, y + h - font * 2.6, m.code_line)
        bc = eanbc.Ean13BarcodeWidget(m.barcode)
        bc.barHeight = h * 0.32
        bc.barWidth = (w - 6 * mm) / 95
        d = Drawing(w, bc.barHeight)
        d.add(bc)
        d.drawOn(c, x + 3 * mm, y + font * 1.6)
        c.setFont("Helvetica-Bold", font + 3)
        c.drawCentredString(x + w / 2, y + 1.5, m.price_text.replace("\u20b9", "Rs "))
    c.save()
    return buf.getvalue()
