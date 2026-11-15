"""Owner-facing texts other than error codes (doctor titles, status words …), EN/HI (stdlib only).

Reads the non-``[errors]`` tables of ``core/messages/{lang}.toml``; dotted keys (``doctor.title.database``)
address nested tables; English is the fallback; ``{placeholders}`` are filled from keyword arguments;
a missing key renders as the key itself (never raises — a missing translation must not stop the shop).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("vaishnavi.core.i18n")

MESSAGES_DIR: Final = Path(__file__).resolve().parent / "messages"
LANGS: Final = ("en", "hi")
DEFAULT_LANG: Final = "en"


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{dotted}."))
        else:
            out[dotted] = str(value)
    return out


class Texts:
    """All texts of one language with English fallback."""

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
        path = MESSAGES_DIR / f"{lang}.toml"
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("texts: cannot load %s: %s", path, exc)
            raw = {}
        raw.pop("errors", None)
        table = _flatten(raw)
        cls._cache[lang] = table
        log.debug("texts: loaded lang=%s keys=%d", lang, len(table))
        return table

    @classmethod
    def load(cls, lang: str = DEFAULT_LANG) -> Texts:
        """Texts for ``lang`` (unknown → English)."""
        lang = lang if lang in LANGS else DEFAULT_LANG
        return cls(lang, cls._load_lang(lang), cls._load_lang(DEFAULT_LANG))

    def t(self, key: str, **params: Any) -> str:
        """Translated text with placeholders filled; the key itself when unknown."""
        template = self._table.get(key)
        if template is None:
            template = self._fallback.get(key)
        if template is None:
            if key not in self._missing:
                self._missing.add(key)
                log.warning("texts: missing key %s (lang=%s)", key, self.lang)
            return key
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template

    def has(self, key: str) -> bool:
        """True when the key exists in this language or the fallback."""
        return key in self._table or key in self._fallback
