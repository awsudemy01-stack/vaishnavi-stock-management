"""Settings → Users (docs/09 §10): staff accounts, activate / deactivate, password reset, Owner PIN.

Every action is an ``UserService`` call (Owner-only, audited); the model is pure and tested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vaishnavi.api.system import AppState
from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied, Role
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_users")


@dataclass(frozen=True)
class UserRow:
    """One account."""

    id: int
    username: str
    display_name: str
    role: str
    active: bool
    must_change_password: bool
    last_login_text: str
    locked: bool


@dataclass(frozen=True)
class UsersModel:
    """The tab."""

    users: list[UserRow]
    owner_pin_set: bool
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    """Outcome."""

    ok: bool
    message: str = ""
    error_code: str | None = None


def build_users_model(state: AppState, texts: UiTexts) -> UsersModel:
    """All accounts (Owner view)."""
    services = state.extra["services"]
    owner = Actor(user_id=1, role=Role.OWNER)
    rows = []
    pin_set = False
    for u in services.users.list_users(owner):
        last = format_ist(parse_iso_utc(u.last_login_at)) if u.last_login_at else ""
        rows.append(UserRow(u.id, u.username, u.display_name, u.role.value, u.is_active, u.must_change_password, last, bool(u.locked_until)))
        if u.role is Role.OWNER and u.owner_pin_hash:
            pin_set = True
    labels = {k: texts.t(f"users.{k}") for k in ("title", "new_staff", "username", "display_name", "password", "create", "deactivate", "reactivate", "reset_password", "owner_pin", "owner_password", "new_pin", "set_pin", "must_change", "locked", "saved")}
    return UsersModel(rows, pin_set, labels)


def _run(fn: Any, *args: Any, **kwargs: Any) -> ActionResult:
    try:
        fn(*args, **kwargs)
    except PermissionDenied as exc:
        return ActionResult(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return ActionResult(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except ValueError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(True)


def create_staff(state: AppState, actor: Actor, *, username: str, display_name: str, password: str) -> ActionResult:
    """New STAFF account (must change password at first login)."""
    services = state.extra["services"]
    result = _run(services.users.create_user, actor, username, display_name, password)
    if result.ok:
        log.info("SETTINGS_USER_CREATED username=%s by=%s", username, actor.user_id)
    return result


def set_active(state: AppState, actor: Actor, user_id: int, *, active: bool) -> ActionResult:
    """Deactivate / reactivate."""
    return _run(state.extra["services"].users.set_active, actor, user_id, active)


def reset_password(state: AppState, actor: Actor, user_id: int, *, new_password: str) -> ActionResult:
    """Owner sets a temporary password; the user must change it."""
    return _run(state.extra["services"].users.reset_password, actor, user_id, new_password)


def rename(state: AppState, actor: Actor, user_id: int, *, display_name: str) -> ActionResult:
    """Display name."""
    return _run(state.extra["services"].users.rename, actor, user_id, display_name)


def set_owner_pin(state: AppState, actor: Actor, *, password: str, new_pin: str) -> ActionResult:
    """Owner PIN (needs the Owner password again)."""
    return _run(state.extra["services"].users.set_owner_pin, actor, password, new_pin)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """Users tab body."""
    from nicegui import ui  # noqa: PLC0415

    box = ui.column().classes("w-full gap-3")

    def toast(result: ActionResult, ok_text: str) -> None:
        ui.notify(ok_text if result.ok else result.message, type="positive" if result.ok else "negative")

    def render() -> None:
        m = build_users_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["title"]).classes("font-semibold")
            for u in m.users:
                with ui.row().classes("items-center gap-2 w-full vsm-card p-2"):
                    ui.label(f"{u.display_name} ({u.username})").classes("w-56 font-semibold")
                    ui.badge(u.role).props("outline")
                    if not u.active:
                        ui.badge("inactive", color="grey")
                    if u.must_change_password:
                        ui.badge(m.labels["must_change"], color="warning")
                    if u.locked:
                        ui.badge(m.labels["locked"], color="negative")
                    ui.label(u.last_login_text).classes("text-xs text-gray-500 flex-1")
                    if u.role != "OWNER":
                        ui.button(m.labels["reactivate"] if not u.active else m.labels["deactivate"], on_click=lambda _e, uid=u.id, a=u.active: (toast(set_active(state, client.actor(), uid, active=not a), m.labels["saved"]), render())).props("flat dense")
                        pwd = ui.input(m.labels["password"], password=True).props("dense").classes("w-40")
                        ui.button(m.labels["reset_password"], on_click=lambda _e, uid=u.id, p=pwd: toast(reset_password(state, client.actor(), uid, new_password=str(p.value or "")), m.labels["saved"])).props("flat dense")
            ui.label(m.labels["new_staff"]).classes("font-semibold mt-3")
            form = {"username": "", "display_name": "", "password": ""}
            with ui.row().classes("gap-2 items-end"):
                ui.input(m.labels["username"]).bind_value(form, "username").classes("w-40")
                ui.input(m.labels["display_name"]).bind_value(form, "display_name").classes("w-40")
                ui.input(m.labels["password"], password=True).bind_value(form, "password").classes("w-40")
                ui.button(m.labels["create"], on_click=lambda: (toast(create_staff(state, client.actor(), **form), m.labels["saved"]), render())).props("unelevated color=primary")
            ui.label(m.labels["owner_pin"] + (" ✓" if m.owner_pin_set else "")).classes("font-semibold mt-3")
            pin_form = {"password": "", "pin": ""}
            with ui.row().classes("gap-2 items-end"):
                ui.input(m.labels["owner_password"], password=True).bind_value(pin_form, "password").classes("w-48")
                ui.input(m.labels["new_pin"], password=True).bind_value(pin_form, "pin").props("inputmode=numeric").classes("w-32")
                ui.button(m.labels["set_pin"], on_click=lambda: (toast(set_owner_pin(state, client.actor(), password=pin_form["password"], new_pin=pin_form["pin"]), m.labels["saved"]), render())).props("outline")

    render()
