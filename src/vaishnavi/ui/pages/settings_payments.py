"""Settings → Payments (docs/09 §10, docs/05 §8): UPI id and bank details — encrypted, masked, re-auth to reveal.

Values live in ``secure_setting`` through ``SecureSettingsService`` (allow-listed keys only); the page
shows previews (``•••• 4455``), reveals a value only after the Owner re-enters the password, and switches
the receipt QR on when a UPI id is saved. Nothing here is ever logged or exposed to the assistant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.api.system import AppState
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_payments")

FIELDS: Final = ("upi_id", "payee_name", "bank_name", "account_no", "ifsc")


@dataclass(frozen=True)
class PaymentField:
    """One masked value."""

    key: str
    label: str
    preview: str
    is_set: bool


@dataclass(frozen=True)
class PaymentsModel:
    """The tab."""

    fields: list[PaymentField]
    qr_enabled: bool
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RevealResult:
    """Reveal outcome (value only after re-auth)."""

    value: str | None
    message: str = ""


@dataclass(frozen=True)
class ActionResult:
    """Save / clear outcome."""

    ok: bool
    message: str = ""
    error_code: str | None = None


def build_payments_model(state: AppState, texts: UiTexts) -> PaymentsModel:
    """Previews only — never the values."""
    services = state.extra["services"]
    previews = services.secure_settings.previews()
    rows = [PaymentField(k, texts.t(f"payments.{k}"), previews.get(f"payments.{k}", ""), f"payments.{k}" in previews) for k in FIELDS]
    labels = {k: texts.t(f"payments.{k}") for k in ("title", "intro", "save", "reveal", "clear", "owner_password", "qr_on", "saved")}
    return PaymentsModel(rows, services.settings_service.effective().receipt.show_qr_upi, labels)


def save_payments(state: AppState, actor: Actor, values: dict[str, str]) -> ActionResult:
    """Store the non-empty fields; a UPI id turns the receipt QR on."""
    services = state.extra["services"]
    clean = {k: str(v).strip() for k, v in values.items() if k in FIELDS and str(v).strip()}
    if not clean:
        return ActionResult(False, "nothing to save")
    upi = clean.get("upi_id")
    if upi and "@" not in upi:
        return ActionResult(False, "a UPI id looks like name@bank")
    try:
        for key, value in clean.items():
            services.secure_settings.set(actor, f"payments.{key}", value)
        if upi:
            services.settings_service.set_many(actor, {"receipt.show_qr_upi": True})
    except PermissionDenied as exc:
        return ActionResult(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return ActionResult(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    log.info("PAYMENT_DETAILS_SAVED keys=%s by=%s", ",".join(sorted(clean)), actor.user_id)
    return ActionResult(True)


def reveal(state: AppState, actor: Actor, key: str, *, password: str) -> RevealResult:
    """Show one value after the Owner re-enters the password."""
    services = state.extra["services"]
    if key not in FIELDS:
        return RevealResult(None, "unknown field")
    if not services.users.reauthenticate(actor.user_id, password):
        log.info("PAYMENT_REVEAL_DENIED key=%s user=%s", key, actor.user_id)
        return RevealResult(None, "password did not match")
    try:
        value = services.secure_settings.reveal(actor, f"payments.{key}")
    except (PermissionDenied, VaishnaviError) as exc:
        return RevealResult(None, str(exc))
    return RevealResult(value)


def clear_field(state: AppState, actor: Actor, key: str) -> ActionResult:
    """Forget one value."""
    services = state.extra["services"]
    if key not in FIELDS:
        return ActionResult(False, "unknown field")
    try:
        services.secure_settings.delete(actor, f"payments.{key}")
        if key == "upi_id":
            services.settings_service.set_many(actor, {"receipt.show_qr_upi": False})
    except (PermissionDenied, VaishnaviError) as exc:
        return ActionResult(False, str(exc), getattr(exc, "code", None))
    return ActionResult(True)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """Payments tab body."""
    from nicegui import ui  # noqa: PLC0415

    box = ui.column().classes("w-full gap-3")

    def render() -> None:
        m = build_payments_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["title"]).classes("font-semibold")
            ui.label(m.labels["intro"]).classes("text-sm text-gray-600")
            form: dict[str, str] = {k: "" for k in FIELDS}
            pwd = {"value": ""}
            for f in m.fields:
                with ui.row().classes("items-end gap-2 w-full"):
                    ui.input(f.label, placeholder=f.preview or "—").bind_value(form, f.key).classes("w-64")
                    if f.is_set:
                        shown = ui.label(f.preview).classes("vsm-mono text-sm w-40")

                        def do_reveal(key: str = f.key, target: Any = shown) -> None:
                            r = reveal(state, client.actor(), key, password=pwd["value"])
                            if r.value is None:
                                ui.notify(r.message, type="negative")
                            else:
                                target.set_text(r.value)

                        ui.button(m.labels["reveal"], on_click=do_reveal).props("flat dense")
                        ui.button(m.labels["clear"], on_click=lambda _e, key=f.key: (clear_field(state, client.actor(), key), render())).props("flat dense color=negative")
            with ui.row().classes("items-end gap-2"):
                ui.input(m.labels["owner_password"], password=True).bind_value(pwd, "value").classes("w-56")
                ui.button(m.labels["save"], on_click=lambda: (_toast(save_payments(state, client.actor(), form), m.labels["saved"]), render())).props("unelevated color=primary")
            ui.label(m.labels["qr_on"] + (" ✓" if m.qr_enabled else "")).classes("text-xs text-gray-500")

    def _toast(result: ActionResult, ok_text: str) -> None:
        ui.notify(ok_text if result.ok else result.message, type="positive" if result.ok else "negative")

    render()
