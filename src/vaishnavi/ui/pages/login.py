"""Login page (approved mock-up ``docs/mockups/login.html``, docs/09 §2) — view-model first, NiceGUI last.

``build_login_model()`` → tiles, keypad rule, button text with the time that becomes ``login_at``, the
amber attendance banner; ``sign_in()`` → authenticate, open the work session, fill ``ClientState`` and
say where the browser goes next (``/confirm-exit`` · ``/onboarding`` · ``/change-password`` · ``/pos`` · ``/``).
The render function at the bottom only lays these out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc, to_ist, today_ist
from vaishnavi.core.errors import AuthError, VaishnaviError
from vaishnavi.core.permissions import Role
from vaishnavi.services.user import UserService
from vaishnavi.services.work_session import WorkSessionService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.state import ClientState, ClientUser, initials

log = logging.getLogger("vaishnavi.ui.pages.login")

OTHER_TILE = "__other__"
"""Username of the *Other — type username* tile."""


@dataclass(frozen=True)
class Tile:
    """One "Who is signing in?" tile."""

    username: str
    display_name: str
    initials: str
    role_label: str
    is_owner: bool
    selected: bool


@dataclass(frozen=True)
class Banner:
    """Amber attendance banner for an unconfirmed exit."""

    session_id: int
    title: str
    body: str
    left_at: str


@dataclass(frozen=True)
class LoginModel:
    """Everything the sign-in card renders."""

    tiles: list[Tile]
    selected: str | None
    show_keypad: bool
    show_username_box: bool
    button_text: str
    banner: Banner | None
    texts: dict[str, str]


def _when_text(iso: str, texts: UiTexts) -> str:
    when = to_ist(parse_iso_utc(iso))
    day = when.date()
    today = today_ist()
    if day == today:
        return f"{texts.t('login.today')} {when.strftime('%H:%M')}"
    if (today - day).days == 1:
        return f"{texts.t('login.yesterday')} {when.strftime('%H:%M')}"
    return when.strftime("%d-%b %H:%M")


def build_login_model(users: UserService, sessions: WorkSessionService, *, texts: UiTexts, selected: str | None = None) -> LoginModel:
    """The pure view-model."""
    rows = users.login_tiles()
    tiles = [Tile(u, name, initials(name), texts.t(f"role.{role.value}"), role is Role.OWNER, selected == u) for _, u, name, role in rows]
    tiles.append(Tile(OTHER_TILE, texts.t("login.other"), "…", texts.t("login.other_hint"), False, selected == OTHER_TILE))
    chosen = next((row for row in rows if row[1] == selected), None)
    banner: Banner | None = None
    if chosen is not None:
        user_id, _, name, _ = chosen
        pending = sessions.pending_confirmation(user_id=user_id)
        if pending is not None:
            left = pending.logout_at or pending.last_seen_at
            banner = Banner(session_id=pending.id, title=texts.t("login.banner_title", name=name, when=_when_text(left, texts)), body=texts.t("login.banner_body"), left_at=left)
    button = texts.t("login.button", time=format_ist(now_utc(), "%H:%M"))
    labels = {k: texts.t(f"login.{k}") for k in ("title", "hint", "who", "password", "clear", "backspace", "forgot", "owner_link", "username", "welcome", "welcome_hi", "sub")}
    return LoginModel(tiles=tiles, selected=selected, show_keypad=chosen is not None and chosen[3] is Role.STAFF, show_username_box=selected == OTHER_TILE, button_text=button, banner=banner, texts=labels)


# ----------------------------------------------------------------------------- sign-in flow
@dataclass(frozen=True)
class SignInResult:
    """Outcome of one attempt."""

    ok: bool
    next_path: str | None
    error_code: str | None = None
    message: str | None = None
    session_id: int | None = None


def sign_in(users: UserService, sessions: WorkSessionService, client: ClientState, *, username: str, password: str, client_label: str) -> SignInResult:
    """Authenticate → open the work session → fill the browser state → where to go next."""
    texts = UiTexts.load(client.lang)
    try:
        result = users.login(username, password, client=client_label)
    except AuthError as exc:
        log.info("SIGN_IN_REFUSED username=%s code=%s", username.strip().lower(), exc.code)
        next_path = "/onboarding" if exc.code == "E-AUTH-005" else None
        return SignInResult(ok=False, next_path=next_path, error_code=exc.code, message=f"{exc.user_message(client.lang)} {exc.what_to_do(client.lang)}".strip())
    except VaishnaviError as exc:
        log.error("SIGN_IN_FAILED username=%s code=%s", username, exc.code)
        return SignInResult(ok=False, next_path=None, error_code=exc.code, message=exc.user_message(client.lang))
    user = result.user
    started = sessions.start(user_id=user.id, client=client_label)
    shift_id = sessions.open_shift_id(user_id=user.id)
    client.sign_in(ClientUser(user_id=user.id, username=user.username, display_name=user.display_name, role=user.role), work_session_id=started.session_id, shift_id=shift_id)
    if started.unconfirmed is not None:
        left = started.unconfirmed.logout_at or started.unconfirmed.last_seen_at
        client.pending_exit_confirmation = {"session_id": started.unconfirmed.id, "left_at": left, "reason": started.unconfirmed.end_reason, "when_text": _when_text(left, texts)}
        next_path = "/confirm-exit"
    elif result.must_change_password:
        next_path = "/change-password"
    elif user.role is Role.OWNER:
        next_path = "/"
    elif shift_id is None:
        next_path = "/shifts?then_pos=1"  # docs/05 §6: a cashier opens the drawer with the float before selling
    else:
        next_path = "/pos"
    log.info("SIGN_IN_OK user=%s role=%s session=%s next=%s", user.username, user.role.value, started.session_id, next_path)
    return SignInResult(ok=True, next_path=next_path, session_id=started.session_id)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """Add ``/login`` and ``/confirm-exit`` to the app (called by ``ui.server.register_pages``)."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import layout  # noqa: PLC0415
    from vaishnavi.ui.session import load_client, save_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/login")
    def _login(next: str = "") -> None:  # noqa: A002 - query parameter
        client = load_client(app)
        if client.is_authenticated:
            ui.navigate.to("/")
            return
        texts = UiTexts.load(client.lang)
        layout.render_head()
        shell = layout.build_shell(state, client, current_path="/login")
        layout.render_topbar(shell, on_lang=lambda code: (_set_lang(client, code), ui.navigate.reload()))
        selected = {"username": None}
        typed = {"username": "", "password": ""}

        with ui.row().classes("w-full flex-1 no-wrap"):
            with ui.column().classes("w-1/2 p-12 text-white").style("background: linear-gradient(160deg,#FFB74D 0%,#F57C00 55%,#BF360C 100%)"):
                ui.label(texts.t("login.welcome")).classes("text-4xl font-bold")
                ui.label(texts.t("login.welcome_hi")).classes("text-2xl")
                ui.label(texts.t("login.sub")).classes("mt-3 max-w-md")
                shop = state.settings.shop
                with ui.element("div").classes("mt-6 rounded-xl p-4").style("background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.35); max-width: 470px"):
                    ui.label(f"{shop.name}  {shop.name_hi}").classes("text-xl font-bold")
                    ui.label(" · ".join(p for p in (shop.address_line1, f"GSTIN {shop.gstin}" if shop.gstin else "", f"☎ {shop.phone}" if shop.phone else "") if p)).classes("text-sm")
                ui.element("div").classes("flex-1")
                layout.render_health_summary(layout.health_summary(state, texts))

            with ui.column().classes("w-1/2 items-center p-6"):
                card = ui.column().classes("vsm-card p-6 gap-2").style("width: 520px")

                def render_card() -> None:
                    card.clear()
                    model = build_login_model(services.users, services.sessions, texts=texts, selected=selected["username"])
                    with card:
                        ui.label(model.texts["title"]).classes("text-xl font-bold")
                        ui.label(model.texts["hint"]).classes("text-sm text-gray-500")
                        ui.label(model.texts["who"]).classes("text-xs font-semibold mt-2")
                        with ui.row().classes("gap-2 w-full no-wrap"):
                            for tile in model.tiles:
                                with ui.column().classes("flex-1 items-center rounded-xl p-2 cursor-pointer vsm-touch").style(f"border: 1.5px solid {'#F57C00' if tile.selected else '#E5E7EB'}; background: {'#FFF3E0' if tile.selected else '#fff'}").on("click", lambda _e, u=tile.username: (selected.__setitem__("username", u), render_card())):
                                    ui.element("div").classes("circ").props(f'innerHTML="{tile.initials}"').style(f"background: {'#F57C00' if tile.selected else '#90A4AE'}; width:36px;height:36px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700")
                                    ui.label(tile.display_name).classes("text-sm")
                                    ui.label(tile.role_label).classes("text-xs " + ("text-orange-800 font-semibold" if tile.is_owner else "text-gray-500"))
                        if model.banner:
                            with ui.element("div").classes("rounded-lg p-2 text-xs").style("border:1px solid #FFE0B2;background:#FFF8E1;color:#7a4b00"):
                                ui.html(f"<b>{model.banner.title}</b><br>{model.banner.body}", sanitize=False)
                        if model.show_username_box:
                            ui.input(model.texts["username"]).bind_value(typed, "username").classes("w-full")
                        pw = ui.input(model.texts["password"], password=True, password_toggle_button=True).bind_value(typed, "password").classes("w-full")
                        pw.on("keydown.enter", lambda _e: submit())
                        if model.show_keypad:
                            with ui.grid(columns=6).classes("gap-1 w-full"):
                                for key in ("1", "2", "3", "4", "5", "6", "7", "8", "9", model.texts["clear"], "0", model.texts["backspace"]):
                                    ui.button(key, on_click=lambda _e, k=key: _keypad(typed, k, model.texts)).props("flat").classes("vsm-touch")
                        ui.button(model.button_text, on_click=submit).classes("w-full vsm-touch mt-2").props("unelevated color=primary")
                        with ui.row().classes("justify-between w-full text-xs text-gray-500"):
                            ui.label(model.texts["forgot"])
                            ui.link(model.texts["owner_link"], "#").on("click", lambda _e: (selected.__setitem__("username", OTHER_TILE), render_card()))

                def submit() -> None:
                    username = typed["username"].strip() if selected["username"] in (None, OTHER_TILE) else str(selected["username"])
                    if not username:
                        ui.notify(texts.t("login.pick_user"), type="warning")
                        return
                    result = sign_in(services.users, services.sessions, client, username=username, password=typed["password"], client_label=_client_label())
                    typed["password"] = ""
                    if not result.ok:
                        ui.notify(result.message or result.error_code or "?", type="negative")
                        if result.next_path:
                            ui.navigate.to(result.next_path)
                        return
                    save_client(app, client)
                    ui.navigate.to(next or result.next_path or "/")

                render_card()
        layout.render_footer(shell)

    @ui.page("/confirm-exit")
    def _confirm_exit() -> None:
        client = load_client(app)
        if not client.is_authenticated or not client.pending_exit_confirmation:
            ui.navigate.to("/")
            return
        texts = UiTexts.load(client.lang)
        pending = client.pending_exit_confirmation
        layout.render_head()
        note = {"text": ""}
        with ui.column().classes("items-center justify-center w-full h-screen"):
            with ui.column().classes("vsm-card p-6 gap-3").style("width: 520px"):
                ui.label(texts.t("login.confirm_title")).classes("text-xl font-bold")
                ui.label(texts.t("login.confirm_body", when=pending.get("when_text", pending.get("left_at", "?"))))
                ui.input(texts.t("login.confirm_note")).bind_value(note, "text").classes("w-full")

                def confirm() -> None:
                    services.sessions.confirm(int(pending["session_id"]), user_id=client.user.user_id, note=note["text"] or None)  # type: ignore[union-attr]
                    client.pending_exit_confirmation = None
                    save_client(app, client)
                    ui.navigate.to("/" if client.role is Role.OWNER else "/pos")

                ui.button(texts.t("common.confirm"), on_click=confirm).props("unelevated color=primary").classes("vsm-touch")

    def _set_lang(client: ClientState, code: str) -> None:
        client.lang = code
        save_client(app, client)

    def _client_label() -> str:
        try:
            from nicegui import context  # noqa: PLC0415

            req = context.client.request
            return f"{(req.headers.get('user-agent') or 'browser')[:40]} {req.client.host if req.client else ''}".strip()
        except Exception:  # noqa: BLE001 - label only
            return "browser"


def _keypad(typed: dict[str, str], key: str, labels: dict[str, str]) -> None:
    if key == labels["clear"]:
        typed["password"] = ""
    elif key == labels["backspace"]:
        typed["password"] = typed["password"][:-1]
    else:
        typed["password"] += key


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
