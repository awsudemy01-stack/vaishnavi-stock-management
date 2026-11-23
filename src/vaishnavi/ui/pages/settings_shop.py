"""Settings → Shop & GST (live run 4, observation 5): shop identity and the GST rules printed on every bill.

Why a tab of its own: the onboarding wizard asked these questions once, but the Owner then had no screen
to change the GSTIN, the "registered" switch or the default GST % — which made GST look "not working".
The tab reuses the wizard's validated submit functions so the two paths can never disagree.

Per-item GST is *not* here: each product carries its own rate (Products → GST %), and the bill splits it
into CGST + SGST halves (docs/09 §5, docs/14 §3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from vaishnavi.api.system import AppState
from vaishnavi.core.permissions import Actor
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages.onboarding import StepResult, submit_shop_profile, submit_tax

log = logging.getLogger("vaishnavi.ui.pages.settings_shop")

SHOP_FIELDS: Final = ("name", "owner_name", "address_line1", "address_line2", "phone", "email", "state_code")
GST_PRESETS: Final = ("0", "3", "5", "12", "18", "28")


@dataclass(frozen=True)
class ShopModel:
    """Current values for the form."""

    shop: dict[str, str]
    gstin: str
    registered: bool
    default_rate: str
    prices_include_tax: bool
    receipt_preview: list[str]
    whatsapp_share: bool = False
    labels: dict[str, str] = field(default_factory=dict)


def build_shop_model(state: AppState, texts: UiTexts) -> ShopModel:
    """Settings → form values (plus the receipt header the customer will see)."""
    services = state.extra["services"]
    s = services.settings_service.effective()
    shop = {k: str(getattr(s.shop, k, "") or "") for k in SHOP_FIELDS}
    rate = Decimal(s.tax.default_rate_bp) / Decimal(100)
    default_rate = f"{rate.normalize():f}"
    preview = [s.shop.name]
    for line in (s.shop.address_line1, s.shop.address_line2):
        if line.strip():
            preview.append(line.strip())
    if s.shop.phone.strip():
        preview.append(f"Ph: {s.shop.phone.strip()}")
    if s.tax.registered and s.shop.gstin.strip():
        preview.append(f"GSTIN: {s.shop.gstin.strip()}")
    keys = ("title", "intro", "shop_section", "gst_section", "name", "owner_name", "address_line1", "address_line2", "phone", "email", "state_code", "state_hint", "gstin", "gstin_hint",
            "registered", "registered_hint", "default_rate", "default_rate_hint", "prices_include_tax", "inclusive_hint", "per_item_hint", "save", "saved", "preview",
            "receipt_section", "whatsapp_share", "whatsapp_share_hint")
    labels = {k: texts.t(f"shop_settings.{k}") for k in keys}
    return ShopModel(shop, s.shop.gstin, s.tax.registered, default_rate, s.tax.prices_include_tax, preview, s.receipt.whatsapp_share, labels)


def save_shop(state: AppState, actor: Actor, form: dict[str, Any]) -> StepResult:
    """Shop identity first (so the GSTIN is stored), then the GST rules — each path validated by the wizard code."""
    services = state.extra["services"]
    gstin = str(form.get("gstin") or "").strip().upper()
    profile = {k: form.get(k, "") for k in SHOP_FIELDS}
    profile["gstin"] = gstin
    profile["name_hi"] = services.settings_service.effective().shop.name_hi
    r1 = submit_shop_profile(services, actor, profile)
    if not r1.ok:
        return r1
    tax_form = {"registered": bool(form.get("registered")), "gstin": gstin, "default_rate": str(form.get("default_rate") or "0"), "prices_include_tax": bool(form.get("prices_include_tax", True))}
    r2 = submit_tax(services, actor, tax_form)
    if not r2.ok:
        return r2
    if "whatsapp_share" in form:
        r3 = _guard_set(services, actor, {"receipt.whatsapp_share": bool(form.get("whatsapp_share"))})
        if not r3.ok:
            return r3
    log.info("SHOP_SETTINGS_SAVED by=%s registered=%s gstin_set=%s rate=%s whatsapp=%s", actor.user_id, tax_form["registered"], bool(gstin), tax_form["default_rate"], bool(form.get("whatsapp_share")))
    return r2


def _guard_set(services: Any, actor: Actor, changes: dict[str, Any]) -> StepResult:
    from vaishnavi.ui.pages.onboarding import _guard  # noqa: PLC0415

    return _guard(services.settings_service.set_many, actor, changes)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """Shop & GST tab body."""
    from nicegui import ui  # noqa: PLC0415

    box = ui.column().classes("w-full gap-3")

    def render() -> None:
        m = build_shop_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["title"]).classes("font-semibold")
            ui.label(m.labels["intro"]).classes("text-sm text-gray-600")
            form: dict[str, Any] = {**m.shop, "gstin": m.gstin, "registered": m.registered, "default_rate": m.default_rate, "prices_include_tax": m.prices_include_tax, "whatsapp_share": m.whatsapp_share}
            errors: dict[str, Any] = {}
            with ui.row().classes("w-full gap-6 items-start no-wrap"):
                with ui.column().classes("vsm-card p-4 gap-2").style("flex: 1 1 0"):
                    ui.label(m.labels["shop_section"]).classes("font-semibold text-sm")
                    errors["name"] = ui.input(m.labels["name"]).bind_value(form, "name").props("outlined dense").classes("w-full")
                    ui.input(m.labels["owner_name"]).bind_value(form, "owner_name").props("outlined dense").classes("w-full")
                    errors["address_line1"] = ui.input(m.labels["address_line1"]).bind_value(form, "address_line1").props("outlined dense").classes("w-full")
                    ui.input(m.labels["address_line2"]).bind_value(form, "address_line2").props("outlined dense").classes("w-full")
                    with ui.row().classes("w-full gap-2 no-wrap"):
                        ui.input(m.labels["phone"]).bind_value(form, "phone").props("outlined dense").classes("flex-1")
                        ui.input(m.labels["email"]).bind_value(form, "email").props("outlined dense").classes("flex-1")
                with ui.column().classes("vsm-card p-4 gap-2").style("flex: 1 1 0"):
                    ui.label(m.labels["gst_section"]).classes("font-semibold text-sm")
                    ui.switch(m.labels["registered"]).bind_value(form, "registered")
                    ui.label(m.labels["registered_hint"]).classes("text-xs text-gray-500")
                    errors["gstin"] = ui.input(m.labels["gstin"], placeholder="09ABCDE1234F1Z5").bind_value(form, "gstin").props("outlined dense uppercase maxlength=15").classes("w-full vsm-mono")
                    ui.label(m.labels["gstin_hint"]).classes("text-xs text-gray-500")
                    with ui.row().classes("w-full gap-2 no-wrap items-end"):
                        ui.input(m.labels["state_code"], placeholder="09").bind_value(form, "state_code").props("outlined dense maxlength=2").classes("w-28")
                        ui.label(m.labels["state_hint"]).classes("text-xs text-gray-500 pb-2")
                    with ui.row().classes("w-full gap-2 no-wrap items-end"):
                        errors["default_rate"] = ui.select(list(GST_PRESETS), label=m.labels["default_rate"], with_input=True, new_value_mode="add-unique").bind_value(form, "default_rate").props("outlined dense").classes("w-40")
                        ui.label(m.labels["default_rate_hint"]).classes("text-xs text-gray-500 pb-2")
                    ui.switch(m.labels["prices_include_tax"]).bind_value(form, "prices_include_tax")
                    ui.label(m.labels["inclusive_hint"]).classes("text-xs text-gray-500")
                    ui.label(m.labels["per_item_hint"]).classes("text-xs text-gray-600 pt-2")
            with ui.column().classes("vsm-card p-4 gap-1 w-full"):
                ui.label(m.labels["receipt_section"]).classes("font-semibold text-sm")
                ui.switch(m.labels["whatsapp_share"]).bind_value(form, "whatsapp_share")
                ui.label(m.labels["whatsapp_share_hint"]).classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-4 w-full"):
                ui.button(m.labels["save"], on_click=lambda: save()).props("unelevated color=primary")
                with ui.column().classes("gap-0"):
                    ui.label(m.labels["preview"]).classes("text-xs text-gray-500")
                    ui.html("<pre class='vsm-mono text-xs' style='margin:0'>" + "\n".join(_esc(x) for x in m.receipt_preview) + "</pre>", sanitize=False)

            def save() -> None:
                for widget in errors.values():
                    widget.props(remove="error error-message")
                r = save_shop(state, client.actor(), form)
                if r.ok:
                    ui.notify(m.labels["saved"], type="positive")
                    render()
                    return
                for key, msg in r.problems.items():
                    if key in errors:
                        errors[key].props(f'error error-message="{msg}"')
                ui.notify(r.message or next(iter(r.problems.values()), "could not save"), type="negative")

    render()


def _esc(text: str) -> str:
    from html import escape  # noqa: PLC0415

    return escape(text)
