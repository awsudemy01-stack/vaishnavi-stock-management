"""Status chips ``DB · Integrity · Scanner · Printer · AI · Backup · Publish`` (docs/09 §1).

``chip_views()`` turns ``AppState.chips`` (``key → (level, text)``, written by the light doctor and the
hardware poll) into ordered, translated view objects with a plain-language hint; ``chips_from_hardware()``
splits the doctor's single *hardware* result into the Scanner and Printer chips the owner wants to see.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.theme import level_color

CHIP_ORDER: Final = ("database", "ledger", "scanner", "printer", "ai_provider", "backup", "publish")
_LEVEL_RANK: Final = {"green": 0, "grey": 1, "amber": 2, "red": 3}


@dataclass(frozen=True)
class ChipView:
    """What the top bar renders for one chip."""

    key: str
    label: str
    level: str
    text: str
    hint: str
    color: str

    @property
    def tooltip(self) -> str:
        """``text`` + hint on a second line."""
        return f"{self.text}\n{self.hint}" if self.hint else self.text


def chip_views(chips: Mapping[str, tuple[str, str]], texts: UiTexts) -> list[ChipView]:
    """Ordered chips; keys not yet reported are grey "not checked yet"."""
    views: list[ChipView] = []
    for key in CHIP_ORDER:
        level, text = chips.get(key, ("grey", texts.t("chip.unknown")))
        level = level if level in _LEVEL_RANK else "grey"
        hint_key = f"chip.hint.{key}.{level}"
        hint = texts.t(hint_key) if texts.has(hint_key) else ""
        views.append(ChipView(key=key, label=texts.t(f"chip.{key}"), level=level, text=text, hint=hint, color=level_color(level)))
    return views


def worst_level(views: list[ChipView]) -> str:
    """``red`` beats ``amber`` beats ``grey`` beats ``green`` — for the one-line health headline."""
    if not views:
        return "grey"
    return max((v.level for v in views), key=lambda lv: _LEVEL_RANK.get(lv, 1))


def chips_from_hardware(result: Any) -> dict[str, tuple[str, str]]:
    """Scanner + printer chips from the doctor's hardware ``CheckResult`` (``result.data`` keys of checks/hardware.py)."""
    data: Mapping[str, Any] = getattr(result, "data", {}) or {}
    chips: dict[str, tuple[str, str]] = {}

    paired, present, paused = bool(data.get("scanner_paired")), bool(data.get("scanner_present")), bool(data.get("billing_paused_for_staff"))
    if present:
        chips["scanner"] = ("green", "Scanner connected")
    elif paused:
        chips["scanner"] = ("red", "Scanner: paired scanner not connected" if paired else "Scanner: none paired")
    else:
        chips["scanner"] = ("amber", "Scanner: not connected" if paired else "Scanner: none paired")

    printer = data.get("printer") or {}
    kind, ready = str(printer.get("kind") or "none"), bool(printer.get("ready"))
    if kind == "none":
        chips["printer"] = ("amber", "Printer: not set up")
    elif ready:
        chips["printer"] = ("green", f"Printer ready ({printer.get('address') or kind})")
    else:
        chips["printer"] = ("amber", f"Printer: {printer.get('detail') or 'not connected'}")
    return chips
