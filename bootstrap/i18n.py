"""Plain-language messages for the installer in English and Hindi (stdlib only).

Messages live in ``bootstrap/messages/<lang>.toml`` as nested tables and are addressed by dotted keys
(``runner.header``). English is always loaded as the fallback; a missing key returns the key itself so
the owner sees *something* and the gap is logged once instead of crashing the installer.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("bootstrap.i18n")

MESSAGES_DIR = Path(__file__).resolve().parent / "messages"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "hi")


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = str(value)
    return flat


class Messages:
    """Translated strings for one language with English fallback."""

    def __init__(self, lang: str, strings: dict[str, str], fallback: dict[str, str]) -> None:
        self.lang = lang
        self._strings = strings
        self._fallback = fallback
        self._reported_missing: set[str] = set()

    @classmethod
    def load(cls, lang: str = DEFAULT_LANG, messages_dir: Path = MESSAGES_DIR) -> Messages:
        """Load ``<lang>.toml`` over ``en.toml``; unknown languages fall back to English."""
        if lang not in SUPPORTED_LANGS:
            log.warning("i18n: unsupported language %r, using %s", lang, DEFAULT_LANG)
            lang = DEFAULT_LANG
        fallback = cls._read(messages_dir / f"{DEFAULT_LANG}.toml")
        strings = fallback if lang == DEFAULT_LANG else cls._read(messages_dir / f"{lang}.toml")
        log.debug("i18n: loaded lang=%s keys=%d fallback_keys=%d", lang, len(strings), len(fallback))
        return cls(lang, strings, fallback)

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        if not path.exists():
            log.warning("i18n: message file missing: %s", path)
            return {}
        with path.open("rb") as fh:
            return _flatten(tomllib.load(fh))

    def t(self, key: str, **params: Any) -> str:
        """Return the translated, formatted string for ``key``."""
        template = self._strings.get(key) or self._fallback.get(key)
        if template is None:
            if key not in self._reported_missing:
                self._reported_missing.add(key)
                log.warning("i18n: missing message key %r (lang=%s)", key, self.lang)
            return key
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("i18n: bad placeholders for key %r: %s", key, exc)
            return template

    def has(self, key: str) -> bool:
        """True when the key exists in the current language or the fallback."""
        return key in self._strings or key in self._fallback
