"""Tiny dependency-free SVG charts for the dashboard (docs/09 §2).

Pure functions returning SVG markup strings: no JS chart library (offline-first, no CDN), nothing
to install, renders in every browser and inside the HTML report e-mail. Numbers are only ever
*shapes* here — the labelled figures sit next to the chart as text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape

PRIMARY = "#B4532A"
"""Kesari (saffron) — matches ``ui.theme``."""
MUTED = "#D9CBB5"


def _head(width: int, height: int, label: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{label}">'


def sparkline_svg(values: Sequence[int | float], *, width: int = 160, height: int = 36, color: str = PRIMARY) -> str:
    """A polyline through ``values`` spread over ``width``; flat/empty series still return an SVG."""
    pad = 1.0
    head = _head(width, height, "trend")
    if not values:
        return head + f'<line x1="0" y1="{height / 2}" x2="{width}" y2="{height / 2}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="3 3"/></svg>'
    lo, hi = min(values), max(values)
    span = float(hi - lo) or 1.0
    step = (width / (len(values) - 1)) if len(values) > 1 else 0.0
    points = []
    for i, v in enumerate(values):
        x = round(i * step, 1) if len(values) > 1 else width / 2
        y = round(height - pad - ((float(v) - lo) / span) * (height - 2 * pad), 1)
        points.append(f"{x},{y}")
    last_x, last_y = points[-1].split(",")
    return head + f'<polyline fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="{" ".join(points)}"/><circle cx="{last_x}" cy="{last_y}" r="2.5" fill="{color}"/></svg>'


def bars_svg(values: Mapping[str, int | float], *, width: int = 240, bar_height: int = 14, gap: int = 6, color: str = PRIMARY) -> str:
    """Horizontal bars whose widths are shares of the total (payment mix, top sellers)."""
    rows = [(k, v) for k, v in values.items() if v and v > 0]
    height = max(bar_height, len(rows) * (bar_height + gap) - gap) if rows else bar_height
    head = _head(width, height, "shares")
    if not rows:
        return head + f'<rect x="0" y="0" width="{width}" height="{bar_height}" rx="3" fill="{MUTED}" opacity="0.4"/></svg>'
    total = float(sum(v for _, v in rows)) or 1.0
    parts = []
    for i, (label, v) in enumerate(rows):
        w = round(width * float(v) / total, 1)
        y = i * (bar_height + gap)
        parts.append(f'<rect x="0" y="{y}" width="{w}" height="{bar_height}" rx="3" fill="{color}" opacity="{max(0.4, 1 - i * 0.15):.2f}"><title>{escape(str(label))}</title></rect>')
    return head + "".join(parts) + "</svg>"
