"""Document renderer — daily digest / weekly report / any table as self-contained HTML, PDF when an engine exists.

Plain string templates (no Jinja2 wheel needed), inline CSS, the SVG sparkline from ``ui.components.charts``'
maths re-implemented here so the publishing layer does not import the UI. ``write_document`` tries the optional
PDF engine (weasyprint, when installed on the shop PC) and otherwise writes ``.html`` — phones open both.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

log = logging.getLogger("vaishnavi.publishing.renderer")

PdfEngine = Callable[[str, Path], None]
"""``engine(html, out_path)`` writes a PDF; None when unavailable."""

_CSS = (
    "body{font-family:Segoe UI,Arial,sans-serif;color:#2b2420;margin:0;padding:16px;background:#fbf7f0}"
    ".card{background:#fff;border:1px solid #e6d9c6;border-radius:8px;padding:12px 14px;margin:0 0 12px}"
    "h1{font-size:20px;margin:0 0 4px;color:#B4532A}h2{font-size:15px;margin:0 0 8px;color:#6b4a2b}"
    ".tiles{display:flex;flex-wrap:wrap;gap:10px}.tile{flex:1 1 120px;background:#fff;border:1px solid #e6d9c6;border-radius:8px;padding:10px}"
    ".tile .k{font-size:11px;text-transform:uppercase;color:#8a7866}.tile .v{font-size:20px;font-weight:600}"
    "table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid #eee}th{color:#6b4a2b}"
    ".muted{color:#8a7866;font-size:12px}.warn{color:#9a3b00}ul{margin:4px 0;padding-left:18px}"
)


@dataclass(frozen=True)
class DigestData:
    """Everything the daily digest shows (already formatted strings — the renderer never computes)."""

    shop_name: str
    day: str
    net: str
    bills: int
    avg_bill: str
    items: str
    by_mode: dict[str, str]
    tax: str
    discounts: str
    voids: int
    overrides: int
    variance_lines: list[str]
    low_stock: list[tuple[str, str, str]]
    """(name, qty, min)."""
    out_of_stock: list[str]
    insights: list[str]
    chain_head: str
    sparkline: list[int]
    generated_at: str
    staff_lines: list[str] = field(default_factory=list)
    top: list[tuple[str, str, str]] = field(default_factory=list)
    """(name, qty, amount) — what sold today."""


@dataclass(frozen=True)
class WeeklyData:
    """Weekly owner report."""

    shop_name: str
    period: str
    net: str
    bills: int
    top: list[tuple[str, str, str]]
    """(name, qty, amount)."""
    dead: list[tuple[str, str, str]]
    """(name, stock, value)."""
    reorder: list[tuple[str, str, str, str]]
    """(name, stock, cover days, order qty)."""
    cashiers: list[tuple[str, str, str, str]]
    """(name, bills, sales, variance)."""
    shrinkage: list[tuple[str, str]]
    """(name, value)."""
    by_day: list[tuple[str, str]]
    """(day, net)."""
    generated_at: str


def sparkline_svg(values: Sequence[int], *, width: int = 240, height: int = 40) -> str:
    """Same shape as the dashboard's sparkline (kept here so publishing does not import the UI)."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = float(hi - lo) or 1.0
    step = width / (len(values) - 1) if len(values) > 1 else 0
    pts = " ".join(f"{round(i * step, 1)},{round(height - 1 - ((v - lo) / span) * (height - 2), 1)}" for i, v in enumerate(values))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><polyline fill="none" stroke="#B4532A" stroke-width="2" points="{pts}"/></svg>'


def _page(title: str, body: str) -> str:
    return f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{escape(title)}</title><style>{_CSS}</style></head><body>{body}</body></html>"


def _tile(k: str, v: str) -> str:
    return f'<div class="tile"><div class="k">{escape(k)}</div><div class="v">{escape(v)}</div></div>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return '<p class="muted">—</p>'
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _list(items: Sequence[str], *, cls: str = "") -> str:
    return '<p class="muted">—</p>' if not items else "<ul>" + "".join(f'<li class="{cls}">{escape(i)}</li>' for i in items) + "</ul>"


def render_digest(d: DigestData) -> str:
    """Daily digest HTML."""
    body = (
        f'<div class="card"><h1>{escape(d.shop_name)} — Daily digest</h1><div class="muted">{escape(d.day)} · generated {escape(d.generated_at)}</div></div>'
        f'<div class="tiles">{_tile("Net sales", d.net)}{_tile("Bills", str(d.bills))}{_tile("Average bill", d.avg_bill)}{_tile("Items", d.items)}</div>'
        f'<div class="card"><h2>Payments · tax · discounts</h2>{_table(["Mode", "Amount"], list(d.by_mode.items()))}<p class="muted">Tax {escape(d.tax)} · Discounts {escape(d.discounts)}</p></div>'
        f'<div class="card"><h2>Last 14 days</h2>{sparkline_svg(d.sparkline)}</div>'
        f'<div class="card"><h2>Top sellers today</h2>{_table(["Item", "Qty", "Amount"], d.top)}</div>'
        f'<div class="card"><h2>Control</h2><p>Voids / refunds: <b>{d.voids}</b> · Owner overrides: <b>{d.overrides}</b></p>{_list(d.variance_lines, cls="warn")}{_list(d.staff_lines)}</div>'
        f'<div class="card"><h2>Low stock</h2>{_table(["Item", "Qty", "Min"], d.low_stock)}<h2>Out of stock</h2>{_list(d.out_of_stock, cls="warn")}</div>'
        f'<div class="card"><h2>Insights</h2>{_list(d.insights)}</div>'
        f'<div class="card muted">Sales chain head: <code>{escape(d.chain_head)}</code> — compare with the printed Z report.</div>'
    )
    return _page(f"{d.shop_name} — digest {d.day}", body)


def digest_summary(d: DigestData) -> list[str]:
    """The five lines sent as a chat message next to the document."""
    modes = " · ".join(f"{k} {v}" for k, v in d.by_mode.items()) or "—"
    low = ", ".join(n for n, _, _ in d.low_stock[:3]) or "none"
    return [
        f"{d.day}: {d.net} · {d.bills} bills · avg {d.avg_bill}",
        f"Payments: {modes}",
        f"Voids {d.voids} · overrides {d.overrides} · " + (d.variance_lines[0] if d.variance_lines else "no drawer variance"),
        f"Low stock: {low}" + (f" · out: {', '.join(d.out_of_stock[:3])}" if d.out_of_stock else ""),
        f"Chain head {d.chain_head[:12]}…",
    ]


def render_weekly(w: WeeklyData) -> str:
    """Weekly owner report HTML."""
    body = (
        f'<div class="card"><h1>{escape(w.shop_name)} — Weekly report</h1><div class="muted">{escape(w.period)} · generated {escape(w.generated_at)}</div></div>'
        f'<div class="tiles">{_tile("Net sales", w.net)}{_tile("Bills", str(w.bills))}</div>'
        f'<div class="card"><h2>Sales by day</h2>{_table(["Day", "Net"], w.by_day)}</div>'
        f'<div class="card"><h2>Top sellers</h2>{_table(["Item", "Qty", "Amount"], w.top)}</div>'
        f'<div class="card"><h2>Reorder plan</h2>{_table(["Item", "Stock", "Days of cover", "Order"], w.reorder)}</div>'
        f'<div class="card"><h2>Dead stock (60 days)</h2>{_table(["Item", "Stock", "Value"], w.dead)}</div>'
        f'<div class="card"><h2>Cashiers</h2>{_table(["Cashier", "Bills", "Sales", "Variance"], w.cashiers)}</div>'
        f'<div class="card"><h2>Shrinkage</h2>{_table(["Item", "Value"], w.shrinkage)}</div>'
    )
    return _page(f"{w.shop_name} — weekly {w.period}", body)


def render_table(title: str, subtitle: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Any report screen as a document (Export PDF → Send)."""
    return _page(title, f'<div class="card"><h1>{escape(title)}</h1><div class="muted">{escape(subtitle)}</div></div><div class="card">{_table(headers, rows)}</div>')


def default_pdf_engine() -> PdfEngine | None:
    """weasyprint when importable (optional extra ``pdf-html``), else None."""
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - optional dependency
        return None

    def engine(html: str, out: Path) -> None:
        HTML(string=html).write_pdf(str(out))

    return engine


def write_document(html: str, stem: Path, *, pdf_engine: PdfEngine | None) -> Path:
    """Write ``stem.pdf`` when an engine works, else ``stem.html``; returns the path."""
    stem.parent.mkdir(parents=True, exist_ok=True)
    if pdf_engine is not None:
        out = stem.with_suffix(".pdf")
        try:
            pdf_engine(html, out)
            log.info("DOC_WRITTEN path=%s kind=pdf bytes=%d", out, out.stat().st_size)
            return out
        except Exception as exc:  # noqa: BLE001 - fall back to HTML
            log.warning("PDF_ENGINE_FAILED %s — writing HTML instead", exc)
    out = stem.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    log.info("DOC_WRITTEN path=%s kind=html bytes=%d", out, out.stat().st_size)
    return out
