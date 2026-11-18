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
.vsm-rail {{ width: 64px; flex: 0 0 64px; background: var(--card); border-right: 1px solid var(--line); display: flex; flex-direction: column; align-items: center; padding: 10px 0; gap: 6px; }}
.vsm-rail a {{ width: 52px; min-height: 50px; border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; padding: 4px 2px; color: var(--muted); text-decoration: none; font-size: 10px; line-height: 1.1; text-align: center; }}
.vsm-rail a .q-icon {{ font-size: 22px; }}
.vsm-rail a .nicegui-label {{ max-width: 50px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.vsm-rail a:hover {{ background: #FAFAFA; color: var(--ink); }}
.vsm-rail a.active {{ background: var(--saffron-light); color: var(--saffron-dark); font-weight: 700; }}
.vsm-card {{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 12px 40px rgba(31,41,55,.08); }}
.vsm-chatfab {{ position: fixed; right: 22px; bottom: 56px; display: flex; align-items: center; gap: 10px; background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 6px 14px 6px 6px; box-shadow: 0 8px 24px rgba(31,41,55,.15); font-size: 13px; cursor: pointer; z-index: 50; }}
.vsm-chatfab .bub {{ width: 40px; height: 40px; border-radius: 50%; background: var(--saffron); color: #fff; display: grid; place-items: center; font-size: 20px; }}
.vsm-foot {{ height: 40px; border-top: 1px solid var(--line); background: var(--card); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; font-size: 12px; color: var(--muted); }}
.vsm-touch {{ min-height: 44px; min-width: 44px; }}
/* POS (docs/mockups/pos.html) */
.vsm-pos-scan {{ display: flex; align-items: center; gap: 10px; border: 2px solid var(--saffron); border-radius: 12px; background: var(--card); min-height: 52px; padding: 0 14px; box-shadow: 0 0 0 4px rgba(245,124,0,.12); flex: 1; }}
.vsm-fbtn {{ min-height: 52px; border: 1px solid var(--line); background: var(--card); border-radius: 12px; padding: 0 14px; font-size: 13px; font-weight: 600; color: #374151; display: flex; flex-direction: column; align-items: center; justify-content: center; line-height: 1.15; cursor: pointer; }}
.vsm-fbtn small {{ font-weight: 500; color: var(--muted); font-size: 10.5px; }}
.vsm-fbtn.primary {{ background: var(--saffron-light); border-color: #FFCC80; color: var(--saffron-dark); }}
.vsm-tab {{ font-size: 12px; padding: 5px 11px; border-radius: 999px; border: 1px solid var(--line); color: #374151; background: var(--card); cursor: pointer; }}
.vsm-tab.on {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
.vsm-tile {{ border: 1px solid var(--line); border-radius: 12px; padding: 8px; background: var(--card); display: flex; flex-direction: column; gap: 5px; position: relative; cursor: pointer; }}
.vsm-tile:hover {{ border-color: #FFCC80; box-shadow: 0 4px 14px rgba(245,124,0,.15); }}
.vsm-tile.locked {{ opacity: .6; cursor: default; }}
.vsm-tile.out {{ opacity: .45; }}
.vsm-tile .img {{ height: 70px; border-radius: 8px; background: linear-gradient(135deg,#FFE0B2,#FFCC80); display: grid; place-items: center; font-size: 26px; }}
.vsm-tile .nm {{ font-size: 12px; font-weight: 600; line-height: 1.2; min-height: 29px; }}
.vsm-tile .pr {{ display: flex; justify-content: space-between; font-size: 12px; }}
.vsm-tile .pr b {{ color: var(--saffron-dark); }}
.vsm-tile .pr span {{ color: var(--muted); }}
.vsm-tile .pr span.low {{ color: #B45309; font-weight: 600; }}
.vsm-tile .pr span.out {{ color: var(--bad); font-weight: 600; }}
.vsm-cart {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; display: flex; flex-direction: column; overflow: hidden; }}
.vsm-cart .hd {{ display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-bottom: 1px solid var(--line); }}
.vsm-cart table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
.vsm-cart th {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); background: #FAFAFA; }}
.vsm-cart td {{ padding: 8px 10px; border-bottom: 1px solid #F1F5F9; vertical-align: top; }}
.vsm-cart td.r, .vsm-cart th.r {{ text-align: right; }}
.vsm-cart tr.sel td {{ background: #FFF8EE; }}
.vsm-cart .nm {{ font-weight: 600; }}
.vsm-cart .sub {{ font-size: 11px; color: var(--muted); }}
.vsm-cart .id {{ font-family: {MONO_STACK}; font-size: 10.5px; color: var(--muted); }}
.vsm-cart .mark {{ color: var(--saffron-dark); font-weight: 700; }}
.vsm-cart .qty {{ display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
.vsm-cart .qty span {{ padding: 3px 8px; font-size: 13px; cursor: pointer; user-select: none; }}
.vsm-cart .qty span.off {{ color: #D1D5DB; cursor: default; }}
.vsm-cart .qty span:not(.off):hover {{ background: var(--saffron-light); color: var(--saffron-dark); }}
.vsm-cart .qty b {{ padding: 3px 10px; background: #FAFAFA; min-width: 56px; text-align: center; font-family: {MONO_STACK}; font-size: 12px; }}
.vsm-cart .disc {{ display: inline-block; border-radius: 6px; padding: 2px 7px; font-size: 11px; font-weight: 600; }}
.vsm-cart .disc small {{ display: block; font-weight: 400; font-size: 10px; color: var(--muted); }}
.vsm-cart .disc.OFFER, .vsm-cart .disc.BOTH {{ background: #E8F5E9; color: #1B5E20; }}
.vsm-cart .disc.ADHOC {{ background: #FFF3E0; color: #E65100; }}
.vsm-cart .disc.NONE {{ color: #9CA3AF; font-weight: 400; }}
.vsm-cart .del {{ color: #9CA3AF; cursor: pointer; font-size: 14px; padding: 2px 4px; }} .vsm-cart .del:hover {{ color: var(--bad); }}
.vsm-notice {{ display: flex; align-items: center; gap: 10px; background: #FFF8E1; border: 1px solid #FFE0B2; border-radius: 10px; padding: 8px 12px; font-size: 12.5px; color: #7a4b00; }}
.vsm-notice.red {{ background: #FFEBEE; border-color: #FFCDD2; color: #7f1d1d; }}
.vsm-notice .flex-1 {{ flex: 1; }}
.vsm-tiles {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; }}
.vsm-tiles .foot {{ font-size: 11.5px; color: var(--muted); }}
.vsm-keys {{ font-size: 11px; color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
.vsm-keys code {{ font-family: {MONO_STACK}; background: var(--card); border: 1px solid var(--line); padding: 1px 6px; border-radius: 5px; color: #374151; }}
.vsm-totals {{ border-top: 1px solid var(--line); padding: 10px 14px; display: grid; grid-template-columns: 1fr auto; gap: 3px 16px; font-size: 12.5px; }}
.vsm-totals .lbl {{ color: var(--muted); }} .vsm-totals .val {{ text-align: right; font-family: {MONO_STACK}; }}
.vsm-totals .big {{ font-size: 22px; font-weight: 700; color: var(--ink); padding-top: 6px; border-top: 1px dashed var(--line); margin-top: 4px; }}
.vsm-totals .big.val {{ color: var(--saffron-dark); }}
.vsm-totals .save {{ color: #1B5E20; font-weight: 600; }}
.vsm-totals .billdisc {{ color: #E65100; font-weight: 600; }}
.vsm-act {{ min-height: 52px; border-radius: 12px; border: 1px solid var(--line); background: var(--card); font-weight: 700; font-size: 13px; color: #374151; display: flex; flex-direction: column; align-items: center; justify-content: center; line-height: 1.1; cursor: pointer; }}
.vsm-act small {{ font-weight: 500; font-size: 10.5px; color: var(--muted); }}
.vsm-act.pay {{ background: var(--saffron); color: #fff; border-color: var(--saffron); box-shadow: 0 6px 16px rgba(245,124,0,.35); font-size: 16px; }}
.vsm-act.pay small {{ color: #fff3e0; }}
.vsm-act.disabled {{ opacity: .45; cursor: not-allowed; box-shadow: none; }}
"""
