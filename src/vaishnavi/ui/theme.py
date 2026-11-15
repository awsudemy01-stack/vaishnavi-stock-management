"""Theme — the approved mock-up's palette, fonts and CSS as Python constants (docs/09 §1, mockups/login.html).

Pure data: NiceGUI applies ``brand_colors()`` through ``ui.colors(**…)`` and injects ``css()`` once per page.
Keeping the values here (not in a .css file) lets the matplotlib twins of the charts and the PDF renderer
use the very same colours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from vaishnavi.core.clock import to_ist

PALETTE: Final[dict[str, str]] = {
    "saffron": "#F57C00",
    "saffron_dark": "#E65100",
    "saffron_light": "#FFF3E0",
    "ink": "#1F2937",
    "muted": "#6B7280",
    "line": "#E5E7EB",
    "bg": "#F6F4EF",
    "card": "#FFFFFF",
    "ok": "#2E7D32",
    "warn": "#F9A825",
    "bad": "#C62828",
    "info": "#1565C0",
    "ok_soft": "#C8E6C9",
    "warn_soft": "#FFF59D",
    "bad_soft": "#FFCDD2",
    "info_soft": "#BBDEFB",
}

FONT_STACK: Final = '"Noto Sans", "Noto Sans Devanagari", system-ui, sans-serif'
MONO_STACK: Final = '"Roboto Mono", ui-monospace, monospace'

_LEVEL_COLOR: Final[dict[str, str]] = {"green": PALETTE["ok"], "amber": PALETTE["warn"], "red": PALETTE["bad"], "grey": PALETTE["muted"], "blue": PALETTE["info"]}
_SEVERITY_COLOR: Final[dict[str, str]] = {"INFO": PALETTE["info"], "WARN": PALETTE["warn"], "HIGH": PALETTE["bad"]}

_WEEKDAYS_HI: Final = ("सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि")


def level_color(level: str) -> str:
    """Hex colour of a status level (``green``/``amber``/``red``; anything else is neutral grey)."""
    return _LEVEL_COLOR.get(level, PALETTE["muted"])


def severity_color(severity: str) -> str:
    """Hex colour of a notification severity (``INFO``/``WARN``/``HIGH``)."""
    return _SEVERITY_COLOR.get(severity, PALETTE["muted"])


def brand_colors() -> dict[str, str]:
    """Quasar brand colours for ``ui.colors(...)``."""
    return {
        "primary": PALETTE["saffron"],
        "secondary": PALETTE["ink"],
        "accent": PALETTE["saffron_dark"],
        "positive": PALETTE["ok"],
        "negative": PALETTE["bad"],
        "warning": PALETTE["warn"],
        "info": PALETTE["info"],
        "dark": PALETTE["ink"],
    }


def format_clock(when: datetime, *, lang: str = "en") -> str:
    """Top-bar clock in shop time: ``Sat 14-Nov-2026 · 09:28`` (weekday in Hindi for ``hi``)."""
    ist = to_ist(when)
    day = _WEEKDAYS_HI[ist.weekday()] if lang == "hi" else ist.strftime("%a")
    return f"{day} {ist.strftime('%d-%b-%Y')} · {ist.strftime('%H:%M')}"


def css() -> str:
    """Global stylesheet (CSS variables + the shell classes of the mock-up)."""
    p = PALETTE
    return f"""
:root {{
  --saffron:{p["saffron"]}; --saffron-dark:{p["saffron_dark"]}; --saffron-light:{p["saffron_light"]};
  --ink:{p["ink"]}; --muted:{p["muted"]}; --line:{p["line"]}; --bg:{p["bg"]}; --card:{p["card"]};
  --ok:{p["ok"]}; --warn:{p["warn"]}; --bad:{p["bad"]}; --info:{p["info"]};
}}
html, body {{ font-family: {FONT_STACK}; color: var(--ink); background: var(--bg); }}
.vsm-mono {{ font-family: {MONO_STACK}; }}
.vsm-topbar {{ height: 56px; background: var(--card); border-bottom: 1px solid var(--line); display: flex; align-items: center; padding: 0 20px; gap: 14px; }}
.vsm-brand {{ display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 18px; }}
.vsm-brand .om {{ width: 34px; height: 34px; border-radius: 9px; background: var(--saffron); color: #fff; display: grid; place-items: center; font-size: 20px; }}
.vsm-brand small {{ display: block; font-weight: 500; font-size: 12px; color: var(--muted); }}
.chip {{ display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px; font-size: 12px; background: var(--card); color: #374151; }}
.chip .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.dot.green {{ background: var(--ok); }} .dot.amber {{ background: var(--warn); }} .dot.red {{ background: var(--bad); }} .dot.grey {{ background: var(--muted); }}
.vsm-lang {{ display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-size: 13px; }}
.vsm-lang span {{ padding: 5px 10px; color: var(--muted); cursor: pointer; }} .vsm-lang span.on {{ background: var(--ink); color: #fff; }}
.vsm-clock {{ font-family: {MONO_STACK}; font-size: 13px; color: var(--muted); }}
.vsm-badge .circ {{ width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; color: #fff; background: var(--saffron); }}
.vsm-rail a {{ display: flex; gap: 10px; align-items: center; padding: 10px 14px; border-radius: 10px; color: var(--ink); text-decoration: none; min-height: 44px; }}
.vsm-rail a.active {{ background: var(--saffron-light); color: var(--saffron-dark); font-weight: 600; }}
.vsm-card {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 12px 40px rgba(31,41,55,.08); }}
.vsm-chatfab {{ position: fixed; right: 22px; bottom: 56px; display: flex; align-items: center; gap: 10px; background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 6px 14px 6px 6px; box-shadow: 0 8px 24px rgba(31,41,55,.15); font-size: 13px; cursor: pointer; z-index: 50; }}
.vsm-chatfab .bub {{ width: 40px; height: 40px; border-radius: 50%; background: var(--saffron); color: #fff; display: grid; place-items: center; font-size: 20px; }}
.vsm-foot {{ height: 40px; border-top: 1px solid var(--line); background: var(--card); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; font-size: 12px; color: var(--muted); }}
.vsm-touch {{ min-height: 44px; min-width: 44px; }}
"""
