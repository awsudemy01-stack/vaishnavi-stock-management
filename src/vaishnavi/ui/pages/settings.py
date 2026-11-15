"""``/settings`` — the Owner's settings page with tabs (docs/09 §10): Backups · Hardware · Users · Payments · System. NiceGUI only."""

from __future__ import annotations

import logging
from typing import Any

from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings")

TABS = ("backups", "hardware", "users", "payments", "ai", "publishing", "system")


def register(state: Any) -> None:
    """``/settings`` and ``/settings/<tab>``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.pages import settings_ai, settings_backups, settings_hardware, settings_payments, settings_publishing, settings_system, settings_users  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    renderers = {"backups": settings_backups.render_tab, "hardware": settings_hardware.render_tab, "users": settings_users.render_tab, "payments": settings_payments.render_tab, "ai": settings_ai.render_tab, "publishing": settings_publishing.render_tab, "system": settings_system.render_tab}

    def page(tab: str) -> None:
        path = "/settings" if tab == "backups" else f"/settings/{tab}"
        client = load_client(app)
        decision = guards.decide(path, client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, path))
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/settings"):
            ui.label(texts.t("settings.title")).classes("text-xl font-semibold")
            with ui.tabs(value=tab).classes("w-full") as tabs:
                for key in TABS:
                    ui.tab(key, label=texts.t(f"settings.tab_{key}"))
            with ui.tab_panels(tabs, value=tab).classes("w-full"):
                for key in TABS:
                    with ui.tab_panel(key):
                        renderers[key](state, client, texts)

    @ui.page("/settings")
    def _settings() -> None:
        page("backups")

    @ui.page("/settings/{tab}")
    def _settings_tab(tab: str) -> None:
        page(tab if tab in TABS else "backups")

    log.debug("settings pages registered: %s", TABS)


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
