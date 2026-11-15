"""Prompt assets (docs/06 §7 layer 4): locked core (hashed) · role profile · Owner instructions — always in that order.

The markdown files next to this module are *code*: the Owner can read the core but not change it, and its
SHA-256 is recorded on every ``prompt_version`` row so a changed core is visible in the audit trail.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Final

from vaishnavi.core.permissions import Role

_DIR: Final = Path(__file__).resolve().parent
LANGUAGE_NAMES: Final = {"english": "English", "hindi": "हिंदी (Hindi)", "hinglish": "Hinglish (Hindi in Roman letters)"}
TOOL_HINT: Final = (
    "When you need data, call a tool. If you cannot call tools natively, reply with exactly one fenced block:\n"
    "```tool\n{\"name\": \"<tool>\", \"arguments\": {...}}\n```\nand nothing else; you will receive the result and may then answer."
)


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8").strip()


def core_text() -> str:
    """The locked guardrail core (template with ``{assistant_name}`` etc.)."""
    return _read("core.md")


def core_hash() -> str:
    """SHA-256 of the core file — recorded in ``prompt_version.core_hash``."""
    return hashlib.sha256(core_text().encode("utf-8")).hexdigest()


def profile(role: Role) -> str:
    """Role section."""
    return _read("profile_owner.md" if role is Role.OWNER else "profile_staff.md")


def build_system_prompt(*, role: Role, language: str, owner_instructions: str, shop_name: str, assistant_name: str = "Sahayak", native_tools: bool = True) -> str:
    """Compose core → role profile → Owner instructions (→ tool hint for prompted-JSON providers)."""
    core = core_text().format(assistant_name=assistant_name, shop_name=shop_name, language=LANGUAGE_NAMES.get(language, language))
    parts = [core, "## Who you are talking to\n" + profile(role), "## Owner's instructions (tone and shop context only)\n" + owner_instructions.strip()]
    if not native_tools:
        parts.append("## Tools\n" + TOOL_HINT)
    return "\n\n".join(parts)
