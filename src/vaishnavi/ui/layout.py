"""App shell view-model + thin NiceGUI renderer (docs/09 §1, mockups/login.html).

``build_shell(app_state, client)`` is pure and tested: brand, clock, chips, role-filtered rail, user badge,
language toggle, chat bubble, keyboard shortcuts. ``health_summary()`` is the plain-language *morning
health check* panel of the login page. ``render_*`` functions (bottom) are the only place NiceGUI is
touched and are imported lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi import __version__
from vaishnavi.api.system import AppState
from vaishnavi.core.clock import format_ist, now_utc
from vaishnavi.core.permissions import Role
from vaishnavi.ui import guards, theme
from vaishnavi.ui.components.status_chip import ChipView, chip_views, worst_level
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.state import ClientState


@dataclass(frozen=True)
class Brand:
    """Top-left mark."""

    mark: str
    title: str
    subtitle: str


@dataclass(frozen=True)
class NavView:
    """A rail entry with its translated label."""

    key: str
    path: str
    label: str
    icon: str
    active: bool = False


@dataclass(frozen=True)
class Badge:
    """User badge (top-right)."""

    name: str
    initials: str
    role: Role
    role_label: str
    session_since_text: str
    shift_text: str | None
    lock_label: str
    end_work_label: str


@dataclass(frozen=True)
class ChatBubble:
    """The collapsed assistant bubble, bottom-right."""

    visible: bool
    title: str
    hint: str


@dataclass(frozen=True)
class Shortcut:
    """Keyboard shortcut shown in the help overlay / footer."""

    keys: str
    label_key: str
    label: str


@dataclass(frozen=True)
class ShellModel:
    """Everything the shell renders around a page."""

    lang: str
    brand: Brand
    clock_text: str
    chips: list[ChipView]
    nav: list[NavView]
    badge: Badge | None
    chat_bubble: ChatBubble
    lang_toggle: tuple[tuple[str, str, bool], ...]
    shortcuts: list[Shortcut]
    footer_left: str
    footer_right: str
    denied_text: str | None = None


SHORTCUTS: Final = (("Ctrl+K", "shortcut.search"), ("Ctrl+/", "shortcut.chat"), ("Ctrl+Q", "shortcut.end_work"), ("F12", "shortcut.lock"), ("F1", "shortcut.help"))


def build_shell(app_state: AppState, client: ClientState, *, current_path: str = "/", denied: str | None = None) -> ShellModel:
    """The pure view-model."""
    lang = client.lang
    texts = UiTexts.load(lang)
    shop = app_state.settings.shop
    brand = Brand(mark="ॐ", title=texts.t("brand.title"), subtitle=texts.t("brand.subtitle", shop_hi=shop.name_hi or shop.name))
    nav: list[NavView] = []
    badge: Badge | None = None
    if client.user is not None:
        role = client.user.role
        nav = [NavView(i.key, i.path, texts.t(i.label_key), i.icon, active=_is_active(i.path, current_path)) for i in guards.nav_for(role)]
        since = format_ist(client.session_since, "%H:%M") if client.session_since else "--:--"
        badge = Badge(
            name=client.user.display_name,
            initials=client.initials(),
            role=role,
            role_label=texts.t(f"role.{role.value}"),
            session_since_text=texts.t("badge.session_since", time=since),
            shift_text=texts.t("badge.shift", id=client.shift_id) if client.shift_id is not None else None,
            lock_label=texts.t("badge.lock"),
            end_work_label=texts.t("badge.end_work"),
        )
    chat_hint = texts.t("chat.hint_signed_out") if client.user is None else texts.t("chat.hint_owner" if client.user.role is Role.OWNER else "chat.hint_staff")
    bubble = ChatBubble(visible=client.user is not None and app_state.settings.ai.enabled and app_state.settings.ai.provider != "none", title=texts.t("chat.title"), hint=chat_hint)
    toggle = tuple((code, texts.t(f"lang.{code}"), code == lang) for code in ("en", "hi"))
    shortcuts = [Shortcut(keys, key, texts.t(key)) for keys, key in SHORTCUTS]
    footer_left = f"{texts.t('foot.version', version=__version__)} · {texts.t('foot.help', shortcut='F1')}"
    footer_right = f"{texts.t('foot.owner_tools')} /system/logs · /system/status" if client.role is Role.OWNER else ""
    return ShellModel(
        lang=lang,
        brand=brand,
        clock_text=theme.format_clock(now_utc(), lang=lang),
        chips=chip_views(app_state.chips, texts),
        nav=nav,
        badge=badge,
        chat_bubble=bubble,
        lang_toggle=toggle,
        shortcuts=shortcuts,
        footer_left=footer_left,
        footer_right=footer_right,
        denied_text=texts.t("common.denied") if denied else None,
    )


def _is_active(item_path: str, current: str) -> bool:
    current = current.split("?", 1)[0].rstrip("/") or "/"
    if item_path == "/":
        return current == "/"
    return current == item_path or current.startswith(item_path + "/")


@dataclass(frozen=True)
class IntegrityBanner:
    """The red bar the Owner sees when a ledger check failed (docs/05 §4)."""

    text: str
    link: str


def integrity_banner(app_state: AppState, client: ClientState, texts: UiTexts) -> IntegrityBanner | None:
    """Owner-only; driven by the ``ledger`` chip the hourly job sets."""
    if client.role is not Role.OWNER:
        return None
    level, text = app_state.chips.get("ledger", ("green", ""))
    if level != "red":
        return None
    return IntegrityBanner(text=f"{texts.t('integrity.banner')} {text}", link="/reports/integrity")


# ----------------------------------------------------------------------------- health summary (login pane)
@dataclass(frozen=True)
class HealthLine:
    """One bullet of the morning health check."""

    level: str  # green | amber | red | blue
    text: str


@dataclass(frozen=True)
class HealthSummary:
    """The panel on the left of the login page."""

    level: str
    title: str
    lines: list[HealthLine]
    status_url: str
    foot: str


def health_summary(app_state: AppState, texts: UiTexts) -> HealthSummary:
    """Plain language only — technical detail stays in the Owner's console (docs/09 §2)."""
    views = chip_views(app_state.chips, texts)
    by_key = {v.key: v for v in views}
    lines: list[HealthLine] = []
    for key in ("database", "ledger", "scanner", "printer", "ai_provider", "backup"):
        v = by_key[key]
        text = f"{v.label} · {v.text}" if v.text else v.label
        if v.hint and v.level in ("amber", "red"):
            text = f"{text} — {v.hint}"
        lines.append(HealthLine(level=v.level, text=text))
    started = app_state.extra.get("started_at_text") or format_ist(app_state.started_at, "%H:%M")
    notes = sum(1 for v in views if v.level in ("amber", "red"))
    passed = sum(1 for v in views if v.level == "green")
    lines.append(HealthLine(level="blue", text=f"{texts.t('health.started', time=started)} · {texts.t('health.checks', passed=passed, notes=notes)}"))
    worst = worst_level([by_key[k] for k in ("database", "ledger", "scanner", "printer", "ai_provider", "backup")])
    title_key = {"red": "health.title_bad", "amber": "health.title_warn"}.get(worst, "health.title_ok")
    server = app_state.settings.server
    host = "127.0.0.1" if server.bind_host in ("127.0.0.1", "localhost") else _lan_ip()
    url = f"http://{host}:{server.port}/system/status"
    return HealthSummary(level=worst, title=texts.t(title_key), lines=lines, status_url=url, foot=texts.t("health.details", url=url))


def _lan_ip() -> str:
    import socket  # noqa: PLC0415

    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


# ----------------------------------------------------------------------------- NiceGUI renderer (thin, untested here)
def render_head() -> None:
    """Fonts + theme CSS + brand colours; call once per page."""
    from nicegui import ui  # noqa: PLC0415

    ui.colors(**theme.brand_colors())
    ui.add_head_html('<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&family=Noto+Sans+Devanagari:wght@400;600&family=Roboto+Mono&display=swap" rel="stylesheet">')
    ui.add_head_html(f"<style>{theme.css()}</style>")


def render_topbar(model: ShellModel, *, on_lang: Any = None, on_bell: Any = None, bell_count: int = 0, on_lock: Any = None, on_end_work: Any = None) -> None:
    """The 56 px top bar of the mock-up."""
    from nicegui import ui  # noqa: PLC0415

    with ui.element("div").classes("vsm-topbar w-full"):
        with ui.element("div").classes("vsm-brand"):
            ui.element("div").classes("om").props(f'innerHTML="{model.brand.mark}"')
            with ui.element("div"):
                ui.label(model.brand.title)
                ui.html(f"<small>{model.brand.subtitle}</small>", sanitize=False)
        ui.element("div").classes("flex-1")
        with ui.row().classes("gap-2 items-center"):
            for chip in model.chips:
                with ui.element("span").classes("chip"):
                    ui.element("i").classes(f"dot {chip.level}")
                    ui.label(chip.label)
                    ui.tooltip(chip.tooltip)
        with ui.element("div").classes("vsm-lang"):
            for code, label, on in model.lang_toggle:
                el = ui.element("span").classes("on" if on else "")
                el.props(f'innerHTML="{label}"')
                if on_lang is not None:
                    el.on("click", lambda _e, c=code: on_lang(c))
        ui.label(model.clock_text).classes("vsm-clock")
        if model.badge is not None:
            with ui.element("div").classes("vsm-badge flex items-center gap-2"):
                if on_bell is not None:
                    with ui.button(icon="notifications", on_click=on_bell).props("flat round dense").classes("vsm-touch"):
                        if bell_count:
                            ui.badge(str(bell_count), color="negative").props("floating")
                ui.element("div").classes("circ").props(f'innerHTML="{model.badge.initials}"')
                with ui.column().classes("gap-0 leading-tight"):
                    ui.label(model.badge.name).classes("text-sm font-semibold")
                    ui.label(f"{model.badge.role_label} · {model.badge.session_since_text}" + (f" · {model.badge.shift_text}" if model.badge.shift_text else "")).classes("text-xs text-gray-500")
                if on_lock is not None:
                    ui.button(icon="lock", on_click=on_lock).props("flat round dense").tooltip(model.badge.lock_label)
                if on_end_work is not None:
                    ui.button(model.badge.end_work_label, icon="logout", on_click=on_end_work).props("flat dense no-caps")


def render_rail(model: ShellModel) -> None:
    """Left navigation — the 64 px icon rail of the mock-up (icon above a tiny label; active = saffron square)."""
    from nicegui import ui  # noqa: PLC0415

    with ui.element("nav").classes("vsm-rail"):
        for item in model.nav:
            with ui.link(target=item.path).classes("active" if item.active else "").tooltip(item.label):
                ui.icon(item.icon)
                ui.label(item.label)


def render_chat_bubble(model: ShellModel, on_click: Any = None) -> None:
    """Collapsed Sahayak bubble."""
    from nicegui import ui  # noqa: PLC0415

    if not model.chat_bubble.visible:
        return
    with ui.element("div").classes("vsm-chatfab") as fab:
        ui.element("div").classes("bub").props('innerHTML="💬"')
        with ui.element("div"):
            ui.html(f"<b>{model.chat_bubble.title}</b><br><small>{model.chat_bubble.hint}</small>", sanitize=False)
    if on_click is not None:
        fab.on("click", on_click)


def render_footer(model: ShellModel) -> None:
    """Bottom strip."""
    from nicegui import ui  # noqa: PLC0415

    with ui.element("div").classes("vsm-foot w-full"):
        ui.label(model.footer_left)
        ui.label(model.footer_right)


def render_health_summary(summary: HealthSummary) -> None:
    """The panel on the login page's saffron pane."""
    from nicegui import ui  # noqa: PLC0415

    glyph = {"green": "✓", "amber": "!", "red": "✖", "blue": "i"}
    with ui.element("div").classes("rounded-xl p-4 text-white").style("background: rgba(0,0,0,.18); max-width: 470px; font-size: 13.5px"):
        ui.label(f"{glyph.get(summary.level, '•')} {summary.title}").classes("font-bold mb-2")
        for line in summary.lines:
            ui.label(f"{glyph.get(line.level, '•')} {line.text}")
        ui.html(f"<div style='margin-top:10px;opacity:.9;font-size:12px'>{summary.foot}</div>", sanitize=False)
