"""Export helpers — a minimal, dependency-free XLSX writer (docs/13 Phase 5/7 "exports open in Excel").

An ``.xlsx`` file is a ZIP of a handful of XML parts. One sheet, inline strings, numbers as numeric
cells, no styles — exactly what Excel, LibreOffice and Google Sheets need to open the file and what the
owner needs to sort and total a report at home. CSV stays available for everything else.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections.abc import Sequence
from typing import Any
from xml.sax.saxutils import escape

log = logging.getLogger("vaishnavi.services.export")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def safe_sheet_name(name: str) -> str:
    """Excel forbids ``\\ / ? * [ ] :`` and limits names to 31 characters."""
    clean = re.sub(r"[\\/?*\[\]:]", "", name).strip() or "Sheet1"
    return clean[:31]


def _col(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value: Any) -> str:
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" t="n"><v>{value}</v></c>'
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def to_xlsx(sheet_name: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], *, title_rows: Sequence[str] = ()) -> bytes:
    """One-sheet workbook with a header row; ``title_rows`` (the shop letterhead) sit above it, then one blank row."""
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    all_rows: list[list[Any]] = [[t] for t in title_rows]
    if title_rows:
        all_rows.append([])
    all_rows += [list(headers), *[list(r) for r in rows]]
    for r_idx, row in enumerate(all_rows, start=1):
        cells = "".join(_cell(f"{_col(c_idx)}{r_idx}", v) for c_idx, v in enumerate(row))
        lines.append(f'<row r="{r_idx}">{cells}</row>')
    lines.append("</sheetData></worksheet>")
    sheet_xml = "".join(lines)
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(safe_sheet_name(sheet_name))}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    log.debug("XLSX_BUILT sheet=%s rows=%d bytes=%d", sheet_name, len(rows), buf.tell())
    return buf.getvalue()
