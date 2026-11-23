"""Shop profile stage: ask who the shop is — name, owner, address, contact — and record it (stdlib only).

The application prints these on every receipt, report letterhead and PDF, so nothing about the shop is
hard-coded: this stage writes the ``[shop]`` block of ``config/settings.toml`` from the owner's answers.
Values already present (a re-install, or a hand-edited file) become the defaults, and when the block is
already complete the stage reuses it without asking. The Owner can change everything later in-app under
Settings → Shop & GST (database overrides win over this file).
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.stages import StageContext, StageResult
from bootstrap.wizard import SKIPPED, Kind, Question

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.shop_profile")

SETTINGS_FILE = "settings.toml"
DEFAULT_SHOP_NAME = "Vaishnavi Puja Store"
SHOP_KEYS = ("name", "name_hi", "owner_name", "address_line1", "address_line2", "phone", "email", "gstin", "state_code", "language")
"""Order of the keys in the written ``[shop]`` block (all strings)."""
ASKED_KEYS = ("name", "owner_name", "address_line1", "address_line2", "phone", "email")
REQUIRED_KEYS = ("name", "owner_name", "address_line1", "phone")
"""When all of these are filled the stage does not ask again."""

_PHONE = re.compile(r"^\+?\d{10,13}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ----------------------------------------------------------------------------- settings.toml
def read_shop_settings(path: Path) -> dict[str, str]:
    """Current ``[shop]`` strings (empty dict when the file or block is missing or unreadable)."""
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("settings.toml unreadable (%s) — the shop block will be rewritten", exc)
        return {}
    shop = data.get("shop", {})
    return {k: str(v) for k, v in shop.items() if isinstance(v, str)} if isinstance(shop, dict) else {}


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_shop_settings(path: Path, shop: dict[str, str]) -> None:
    """Create or replace the ``[shop]`` block of ``config/settings.toml``; every other section is kept as-is."""
    merged = {**read_shop_settings(path), **{k: v.strip() for k, v in shop.items()}}
    merged.setdefault("name", DEFAULT_SHOP_NAME)
    lines = ["[shop]"] + [f"{k} = {_toml_string(merged.get(k, ''))}" for k in SHOP_KEYS] + [f"{k} = {_toml_string(v)}" for k, v in merged.items() if k not in SHOP_KEYS]
    block = "\n".join(lines) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    kept: list[str] = []
    skipping = False
    inserted = False
    for line in existing.splitlines():
        if re.match(r"^\s*\[shop\]\s*$", line):
            skipping = True
            if not inserted:
                kept.append(block.rstrip())
                inserted = True
            continue
        if re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip()
    if not inserted:
        body = block + ("\n" + body if body else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(body.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.info("SHOP_SETTINGS_WRITTEN path=%s name=%s owner=%s phone=%s", path, merged.get("name"), merged.get("owner_name"), merged.get("phone"))


def is_complete(shop: dict[str, str]) -> bool:
    """True when every required field has a value."""
    return all((shop.get(k) or "").strip() for k in REQUIRED_KEYS)


# ----------------------------------------------------------------------------- stage
@dataclass
class ShopProfileStage:
    """Stage: shop name · owner · address · contact → ``[shop]`` in settings.toml."""

    key: str = "shop_profile"
    title_key: str = "stage.shop_profile"

    def applies(self, ctx: StageContext) -> bool:
        """Always — the letterhead needs the shop's identity on every machine."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached: the file is re-read every start (it is the source of truth)."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Reuse a complete ``[shop]`` block; otherwise ask (existing values as defaults) and write it."""
        assert ctx.wizard is not None
        path = ctx.paths.config_dir / SETTINGS_FILE
        existing = read_shop_settings(path)
        if is_complete(existing):
            log.info("SHOP_PROFILE_REUSED name=%s owner=%s", existing.get("name"), existing.get("owner_name"))
            return StageResult.ok(ctx.t("shop.reused", name=existing["name"], owner=existing["owner_name"]), **{k: existing.get(k, "") for k in ASKED_KEYS})

        ctx.console.info(ctx.t("shop.intro"))

        def phone_ok(value: str) -> str | None:
            return None if _PHONE.match(re.sub(r"[\s\-()]", "", value)) else ctx.t("shop.err_phone")

        def email_ok(value: str) -> str | None:
            return None if _EMAIL.match(value.strip()) else ctx.t("shop.err_email")

        def default(key: str, fallback: str | None = None) -> str | None:
            return (existing.get(key) or "").strip() or fallback

        questions = [
            Question(key="shop_name", kind=Kind.TEXT, prompt_key="shop.ask_name", help_key="shop.help_name", default=default("name", DEFAULT_SHOP_NAME)),
            Question(key="shop_owner", kind=Kind.TEXT, prompt_key="shop.ask_owner", help_key="shop.help_owner", default=default("owner_name"), skippable=True),
            Question(key="shop_address1", kind=Kind.TEXT, prompt_key="shop.ask_address1", help_key="shop.help_address1", default=default("address_line1"), skippable=True),
            Question(key="shop_address2", kind=Kind.TEXT, prompt_key="shop.ask_address2", default=default("address_line2"), skippable=True),
            Question(key="shop_phone", kind=Kind.TEXT, prompt_key="shop.ask_phone", help_key="shop.help_phone", default=default("phone"), validate=phone_ok, skippable=True),
            Question(key="shop_email", kind=Kind.TEXT, prompt_key="shop.ask_email", default=default("email"), validate=email_ok, skippable=True),
        ]
        answers = ctx.wizard.ask_all(questions)
        values = {
            "name": answers["shop_name"],
            "owner_name": _value(answers["shop_owner"]),
            "address_line1": _value(answers["shop_address1"]),
            "address_line2": _value(answers["shop_address2"]),
            "phone": re.sub(r"[\s\-()]", "", _value(answers["shop_phone"])),
            "email": _value(answers["shop_email"]),
        }
        write_shop_settings(path, values)
        missing = [k for k in REQUIRED_KEYS if not values.get(k)]
        msg = ctx.t("shop.saved", name=values["name"], owner=values["owner_name"] or "—", phone=values["phone"] or "—")
        if missing:
            return StageResult.warn(msg, hint=ctx.t("shop.incomplete_hint"), detail=f"missing: {', '.join(missing)}", **values)
        return StageResult.ok(msg, **values)


def _value(answer: str) -> str:
    return "" if answer == SKIPPED else answer.strip()


def register(options: Options) -> ShopProfileStage:
    """Factory used by ``build_default_pipeline``."""
    return ShopProfileStage()
