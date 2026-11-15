"""UI labels EN/HI (docs/09 §1 "Language toggle"): ``UiTexts.load(lang).t("nav.pos")``.

Tables live in ``en.json`` / ``hi.json`` next to this module (nested objects → dotted keys). Hindi
overlays English; a missing key renders as the key and is logged once — a missing translation never
stops the shop. ``hinglish`` (a *chat* language) maps to English labels.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("vaishnavi.ui.i18n")

I18N_DIR: Final = Path(__file__).resolve().parent
LANGS: Final = ("en", "hi")
DEFAULT_LANG: Final = "en"


def normalise_lang(lang: str | None) -> str:
    """``hi`` stays; everything else (``en``, ``hinglish``, unknown, None) → ``en``."""
    return "hi" if lang == "hi" else DEFAULT_LANG


def flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Nested JSON → ``{"nav.pos": "Billing"}``."""
    out: dict[str, str] = {}
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{dotted}."))
        else:
            out[dotted] = str(value)
    return out


class UiTexts:
    """All labels of one language with English fallback."""

    _cache: dict[str, dict[str, str]] = {}

    def __init__(self, lang: str, table: dict[str, str], fallback: dict[str, str]) -> None:
        self.lang = lang
        self._table = table
        self._fallback = fallback
        self._missing: set[str] = set()

    @classmethod
    def _load_lang(cls, lang: str) -> dict[str, str]:
        if lang in cls._cache:
            return cls._cache[lang]
        path = I18N_DIR / f"{lang}.json"
        try:
            table = flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("ui i18n: cannot load %s: %s", path, exc)
            table = {}
        cls._cache[lang] = table
        log.debug("ui i18n: loaded lang=%s keys=%d", lang, len(table))
        return table

    @classmethod
    def load(cls, lang: str | None = DEFAULT_LANG) -> UiTexts:
        """Labels for ``lang`` (normalised)."""
        lang = normalise_lang(lang)
        return cls(lang, cls._load_lang(lang), cls._load_lang(DEFAULT_LANG))

    def t(self, key: str, **params: Any) -> str:
        """Label with placeholders filled; the key itself when unknown."""
        template = self._table.get(key)
        if template is None:
            template = self._fallback.get(key)
        if template is None:
            if key not in self._missing:
                self._missing.add(key)
                log.warning("ui i18n: missing key %s (lang=%s)", key, self.lang)
            return key
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template

    def has(self, key: str) -> bool:
        """True when the key exists in this language or the fallback."""
        return key in self._table or key in self._fallback
