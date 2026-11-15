"""Output filter — layer 6 of docs/06 §7: the buffered answer is scanned before it is shown.

Blocks credential-looking strings (PIN with context, UPI ids, account numbers, IFSC, Luhn-valid card
numbers), file paths, code fences / SQL, and — the important one — **numbers that do not appear in any
tool result or in the question** (hallucinated figures). A violation replaces the answer with the refusal
template and is logged ``AI_OUTPUT_BLOCKED``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

log = logging.getLogger("vaishnavi.ai.output_filter")

_UPI = re.compile(r"\b[\w.-]{2,}@[a-z]{2,}\b", re.IGNORECASE)
_ACCOUNT = re.compile(r"\b\d{9,18}\b")
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PIN_CONTEXT = re.compile(r"\b(pin|password|passcode|otp|cvv)\b\W{0,12}(is|:|=|hai)?\W{0,5}\b\d{4,6}\b", re.IGNORECASE)
_PATH = re.compile(r"([A-Za-z]:\\[^\s]+|/(?:home|etc|usr|var|opt|Users)/[^\s]+|\b[\w-]+\.(?:py|toml|sqlite|db|env|ini|yaml|json|log)\b)")
_CODE = re.compile(r"```|\b(def|import|class|SELECT|INSERT|UPDATE|DELETE|DROP|FROM|WHERE)\b\s+\S+", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])(?:₹|rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)(?![\w])", re.IGNORECASE)
_DATE = re.compile(r"\b\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")
SMALL_NUMBER_OK: Final = 10
"""Counts up to this are allowed without a tool source ("3 items", "2 days")."""


@dataclass(frozen=True)
class FilterResult:
    """Outcome."""

    ok: bool
    reasons: list[str] = field(default_factory=list)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _numbers(text: str) -> set[str]:
    """Canonical numeric tokens (commas stripped, trailing .00 dropped) outside dates/times."""
    clean = _DATE.sub(" ", text)
    out: set[str] = set()
    for m in _NUMBER.finditer(clean):
        raw = m.group(1).replace(",", "")
        if raw.endswith(".00"):
            raw = raw[:-3]
        elif "." in raw:
            raw = raw.rstrip("0").rstrip(".")
        out.add(raw)
    return out


def _flatten(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for v in value.values():
            yield from _flatten(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten(v)
    elif value is not None:
        yield str(value)


def scan(answer: str, tool_results: list[dict[str, Any]], *, question: str) -> FilterResult:
    """Check one finished answer against the tool results that produced it."""
    reasons: list[str] = []
    if _PIN_CONTEXT.search(answer):
        reasons.append("PIN_LIKE")
    if _UPI.search(answer):
        reasons.append("UPI_ID")
    if _IFSC.search(answer):
        reasons.append("IFSC")
    for m in _CARD.finditer(answer):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            reasons.append("CARD_NUMBER")
            break
    if _ACCOUNT.search(_DATE.sub(" ", answer)) and "CARD_NUMBER" not in reasons:
        reasons.append("ACCOUNT_NUMBER")
    if _PATH.search(answer):
        reasons.append("FILE_PATH")
    if _CODE.search(answer):
        reasons.append("CODE_OR_SQL")
    allowed = _numbers(" ".join(_flatten([r.get("result") for r in tool_results]))) | _numbers(question)
    for n in sorted(_numbers(answer)):
        if n in allowed:
            continue
        try:
            if float(n) <= SMALL_NUMBER_OK:
                continue
        except ValueError:
            continue
        reasons.append(f"NUMBER_NOT_IN_TOOLS:{n}")
    if reasons:
        log.warning("AI_OUTPUT_BLOCKED reasons=%s answer_len=%d tools=%d", ",".join(reasons), len(answer), len(tool_results))
    else:
        log.debug("AI_OUTPUT_OK answer_len=%d numbers=%d", len(answer), len(_numbers(answer)))
    return FilterResult(not reasons, reasons)
