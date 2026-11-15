"""Application errors with codes and plain-language messages in English and Hindi (stdlib only).

Every error the owner or staff can meet is a :class:`VaishnaviError` subclass with a **code** of the
form ``E-<AREA>-<nnn>`` (docs/08 §5). The code is the key into ``core/messages/{en,hi}.toml``, which
holds two owner-facing texts per code — *what it means* (``msg``) and *what to do* (``fix``) — with
``{placeholders}`` filled from ``params``. Technical detail goes only to the log (``detail``).

Rules:
- Raise with a **registered** code; an unknown code is itself a programming error (caught in tests).
- ``str(error)`` is the technical form (for logs); ``error.user_message("hi")`` is for people.
- Never put secrets into ``params`` or ``detail`` — they end up in logs and on screen.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final

log = logging.getLogger("vaishnavi.core.errors")

MESSAGES_DIR: Final = Path(__file__).resolve().parent / "messages"
LANGS: Final = ("en", "hi")
DEFAULT_LANG: Final = "en"
CODE_RE: Final = re.compile(r"^E-[A-Z]+-\d{3}$")


class Severity:
    """How the UI should present the error (not an Enum to keep plain strings in JSON)."""

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


# ----------------------------------------------------------------------------- message catalogue
@dataclass(frozen=True)
class Message:
    """One code's texts in one language."""

    msg: str
    fix: str = ""


class Catalogue:
    """All messages for all languages, loaded once; English is the fallback."""

    def __init__(self, messages_dir: Path = MESSAGES_DIR) -> None:
        self._dir = messages_dir
        self._by_lang: dict[str, dict[str, Message]] = {}
        self._missing_reported: set[str] = set()

    def _load(self, lang: str) -> dict[str, Message]:
        if lang in self._by_lang:
            return self._by_lang[lang]
        path = self._dir / f"{lang}.toml"
        table: dict[str, Message] = {}
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("messages: cannot load %s: %s", path, exc)
            raw = {}
        for code, value in raw.get("errors", {}).items():
            if isinstance(value, dict):
                table[code] = Message(str(value.get("msg", "")), str(value.get("fix", "")))
            else:
                table[code] = Message(str(value))
        self._by_lang[lang] = table
        log.debug("messages: loaded lang=%s codes=%d", lang, len(table))
        return table

    def codes(self, lang: str = DEFAULT_LANG) -> set[str]:
        """All codes defined for a language."""
        return set(self._load(lang))

    def get(self, code: str, lang: str = DEFAULT_LANG) -> Message | None:
        """Message for a code in ``lang``, falling back to English; None when unknown everywhere."""
        lang = lang if lang in LANGS else DEFAULT_LANG
        found = self._load(lang).get(code)
        if found is None and lang != DEFAULT_LANG:
            found = self._load(DEFAULT_LANG).get(code)
        if found is None and code not in self._missing_reported:
            self._missing_reported.add(code)
            log.warning("messages: no text for error code %s", code)
        return found

    def render(self, code: str, lang: str, params: dict[str, Any]) -> tuple[str, str]:
        """(msg, fix) with placeholders filled; a bad placeholder never raises."""
        m = self.get(code, lang)
        if m is None:
            return code, ""
        return _fill(m.msg, params), _fill(m.fix, params)


def _fill(template: str, params: dict[str, Any]) -> str:
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


catalogue = Catalogue()
"""Process-wide catalogue."""


# ----------------------------------------------------------------------------- error hierarchy
class VaishnaviError(Exception):
    """Base of every application error. Subclasses fix the ``area``; instances carry the ``code``."""

    area: ClassVar[str] = "SYS"
    default_code: ClassVar[str] = "E-SYS-099"
    severity: ClassVar[str] = Severity.ERROR

    def __init__(self, error_code: str | None = None, *, detail: str = "", cause: BaseException | None = None, **params: Any) -> None:
        self.code = error_code or self.default_code
        if not CODE_RE.match(self.code):
            raise ValueError(f"malformed error code {self.code!r}")
        if not self.code.startswith(f"E-{self.area}-"):
            raise ValueError(f"code {self.code} does not belong to area {self.area}")
        self.params: dict[str, Any] = params
        self.detail = detail
        self.cause = cause
        super().__init__(self._technical())
        if cause is not None:
            self.__cause__ = cause

    def _technical(self) -> str:
        extras = " ".join(f"{k}={v!r}" for k, v in self.params.items())
        parts = [self.code, extras, self.detail]
        return " ".join(p for p in parts if p)

    def user_message(self, lang: str = DEFAULT_LANG) -> str:
        """What it means — for the owner/staff."""
        return catalogue.render(self.code, lang, self.params)[0]

    def what_to_do(self, lang: str = DEFAULT_LANG) -> str:
        """What to do — for the owner/staff ('' when the catalogue has no fix text)."""
        return catalogue.render(self.code, lang, self.params)[1]

    def as_problem(self, lang: str = DEFAULT_LANG, *, include_detail: bool = False) -> dict[str, Any]:
        """JSON-ready dict for the API / UI toast (technical detail only when asked, e.g. owner)."""
        msg, fix = catalogue.render(self.code, lang, self.params)
        problem: dict[str, Any] = {"code": self.code, "area": self.area, "severity": self.severity, "message": msg, "what_to_do": fix}
        if include_detail and self.detail:
            problem["detail"] = self.detail
        return problem


class ConfigError(VaishnaviError):
    """E-CFG — settings files."""

    area = "CFG"
    default_code = "E-CFG-001"
    severity = Severity.FATAL


class DatabaseError(VaishnaviError):
    """E-DB — integrity, migrations, identity."""

    area = "DB"
    default_code = "E-DB-003"
    severity = Severity.FATAL


class AuthError(VaishnaviError):
    """E-AUTH — login, PIN, permissions."""

    area = "AUTH"
    default_code = "E-AUTH-010"


class CatalogError(VaishnaviError):
    """E-CAT — products and barcodes."""

    area = "CAT"
    default_code = "E-CAT-004"


class InventoryError(VaishnaviError):
    """E-INV — stock rules."""

    area = "INV"
    default_code = "E-INV-003"


class BillingError(VaishnaviError):
    """E-BILL — checkout rules."""

    area = "BILL"
    default_code = "E-BILL-050"


class MeasureError(BillingError):
    """E-MEAS — quantity rules for weighed / measured items (a billing error the cashier fixes on the quantity pad)."""

    area = "MEAS"
    default_code = "E-MEAS-001"


class ShiftError(VaishnaviError):
    """E-SHIFT — shifts and cash."""

    area = "SHIFT"
    default_code = "E-SHIFT-002"


class LedgerError(VaishnaviError):
    """E-LEDGER — hash chain, invoice gaps, reconciliation."""

    area = "LEDGER"
    default_code = "E-LEDGER-001"
    severity = Severity.FATAL


class PrinterError(VaishnaviError):
    """E-PRN — receipt / label printing."""

    area = "PRN"
    default_code = "E-PRN-001"
    severity = Severity.WARN


class ScannerError(VaishnaviError):
    """E-SCN — barcode scanner."""

    area = "SCN"
    default_code = "E-SCN-002"
    severity = Severity.WARN


class ScaleError(VaishnaviError):
    """E-SCALE — weighing scale."""

    area = "SCALE"
    default_code = "E-SCALE-001"
    severity = Severity.WARN


class PublishError(VaishnaviError):
    """E-PUB — report delivery."""

    area = "PUB"
    default_code = "E-PUB-003"
    severity = Severity.WARN


class ForecastError(VaishnaviError):
    """E-FCST — forecasting."""

    area = "FCST"
    default_code = "E-FCST-001"
    severity = Severity.INFO


class AIError(VaishnaviError):
    """E-AI — assistant providers."""

    area = "AI"
    default_code = "E-AI-001"
    severity = Severity.WARN


class GuardrailError(VaishnaviError):
    """E-GRD — assistant refusals."""

    area = "GRD"
    default_code = "E-GRD-001"
    severity = Severity.INFO


class SecretsError(VaishnaviError):
    """E-SEC — secret storage."""

    area = "SEC"
    default_code = "E-SEC-001"


class BackupError(VaishnaviError):
    """E-BKP — backups and restore."""

    area = "BKP"
    default_code = "E-BKP-001"
    severity = Severity.WARN


class SystemError_(VaishnaviError):
    """E-SYS — installation / platform (named with a trailing underscore to avoid the builtin)."""

    area = "SYS"
    default_code = "E-SYS-099"


class MoneyError_(VaishnaviError):
    """E-MONEY — monetary input (``core.money.MoneyError`` is the low-level ValueError twin)."""

    area = "MONEY"
    default_code = "E-MONEY-001"


class LogApiError(VaishnaviError):
    """E-LOGAPI — log console / system API."""

    area = "LOGAPI"
    default_code = "E-LOGAPI-001"


class AlertError(VaishnaviError):
    """E-ALERT — alert rules (informational)."""

    area = "ALERT"
    default_code = "E-ALERT-001"
    severity = Severity.WARN


AREAS: Final[dict[str, type[VaishnaviError]]] = {
    cls.area: cls
    for cls in (
        ConfigError, DatabaseError, AuthError, CatalogError, InventoryError, BillingError, MeasureError, ShiftError, LedgerError,
        PrinterError, ScannerError, ScaleError, PublishError, ForecastError, AIError, GuardrailError, SecretsError,
        BackupError, SystemError_, MoneyError_, LogApiError, AlertError,
    )
}


def error_for(code: str, *, detail: str = "", cause: BaseException | None = None, **params: Any) -> VaishnaviError:
    """Build the right subclass from a bare code (used by the doctor and the handover parser)."""
    m = CODE_RE.match(code)
    if not m:
        raise ValueError(f"malformed error code {code!r}")
    area = code.split("-")[1]
    cls = AREAS.get(area, VaishnaviError)
    if cls is VaishnaviError and area != "SYS":
        raise ValueError(f"unknown error area {area!r} in {code}")
    return cls(code, detail=detail, cause=cause, **params)


def describe(code: str, lang: str = DEFAULT_LANG, params: dict[str, Any] | None = None) -> dict[str, str]:
    """{'code', 'message', 'what_to_do'} for any code — for the log console and the doctor report.

    ``params`` is a dict (not ``**kwargs``) because placeholders such as ``{code}`` would clash.
    """
    msg, fix = catalogue.render(code, lang, params or {})
    return {"code": code, "message": msg, "what_to_do": fix}
