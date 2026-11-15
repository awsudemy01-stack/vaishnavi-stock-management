"""Owner-PIN dialog (docs/05 §5): the single way a staff screen gets an Owner-approved action.

``verify`` goes through ``UserService.verify_owner_pin`` (lockout after 3 wrong tries, ``OVERRIDE`` /
``OVERRIDE_FAILED`` audited with both user ids) and returns the **staff actor with ``pin_verified=True``**
plus the Owner's id as the approver — exactly what the services expect. ``ask_pin`` is the NiceGUI
dialog around it. Every PIN prompt in the app (POS discounts, void/refund, cash-out, hardware override,
open items) uses this module so the behaviour and the audit trail are identical everywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, Permission
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.components.pin_dialog")


@dataclass(frozen=True)
class PinPrompt:
    """Texts for the dialog."""

    title: str
    hint: str
    permission_text: str
    confirm: str
    cancel: str


@dataclass(frozen=True)
class PinOutcome:
    """Result of one PIN entry."""

    ok: bool
    actor: Actor | None = None
    approver_id: int | None = None
    error_code: str | None = None
    message: str = ""


def build_prompt(texts: UiTexts, permission: Permission) -> PinPrompt:
    """Dialog texts; the permission is named so the Owner knows what they are approving."""
    key = f"pin.for.{permission.value}"
    what = texts.t(key) if texts.has(key) else permission.value
    return PinPrompt(texts.t("pin.title"), texts.t("pin.hint"), f"{texts.t('pin.approving')}: {what} ({permission.value})", texts.t("common.confirm"), texts.t("common.cancel"))


def verify(services: Any, client: Any, pin: str, permission: Permission) -> PinOutcome:
    """Check the PIN typed on this staff screen."""
    staff = client.actor()
    if not (pin or "").strip():
        return PinOutcome(False, error_code="E-AUTH-002", message="enter the PIN")
    try:
        owner = services.users.verify_owner_pin(pin.strip(), on_behalf_of=staff, permission=permission)
    except VaishnaviError as exc:
        log.info("PIN_DIALOG_REJECTED permission=%s staff=%s code=%s", permission.value, staff.user_id, exc.code)
        return PinOutcome(False, error_code=exc.code, message=f"{exc.user_message(client.lang)} {exc.what_to_do(client.lang)}".strip())
    log.info("PIN_DIALOG_OK permission=%s staff=%s owner=%s", permission.value, staff.user_id, owner.user_id)
    return PinOutcome(True, client.actor(pin_verified=True), owner.user_id)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def ask_pin(services: Any, client: Any, texts: UiTexts, permission: Permission, on_ok: Callable[[Actor, int], None]) -> None:
    """Open the dialog; ``on_ok(pin_actor, approver_id)`` runs after a correct PIN."""
    from nicegui import ui  # noqa: PLC0415

    prompt = build_prompt(texts, permission)
    form = {"pin": ""}
    with ui.dialog() as dlg, ui.card().classes("gap-2").style("min-width: 360px"):
        ui.label(prompt.title).classes("text-lg font-bold")
        ui.label(prompt.hint).classes("text-sm text-gray-600")
        ui.label(prompt.permission_text).classes("text-xs vsm-mono")
        box = ui.input(prompt.title, password=True).bind_value(form, "pin").props("inputmode=numeric autofocus").classes("w-full")

        def submit() -> None:
            outcome = verify(services, client, str(form["pin"]), permission)
            if not outcome.ok:
                ui.notify(outcome.message, type="negative")
                form["pin"] = ""
                return
            dlg.close()
            assert outcome.actor is not None and outcome.approver_id is not None
            on_ok(outcome.actor, outcome.approver_id)

        box.on("keydown.enter", lambda _e: submit())
        with ui.row().classes("justify-end w-full"):
            ui.button(prompt.cancel, on_click=dlg.close).props("flat")
            ui.button(prompt.confirm, on_click=submit).props("unelevated color=primary")
    dlg.open()
