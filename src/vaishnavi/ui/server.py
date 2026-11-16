"""NiceGUI server entry: ``serve(state, on_ready)`` called by ``bootstrap.startup.run_app`` (docs/07 §6).

Registers the shell pages, mounts the system API (``/healthz`` and friends) on NiceGUI's FastAPI app and
runs the server in the foreground; ``on_ready`` fires from NiceGUI's startup hook so the installer's
*Ready to open the shop* line appears only when the port really answers. Pages are added by their units
(login — 28, system pages — 29, …) through :func:`register_pages`; this module only knows the frame.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Any

from vaishnavi.api import session as api_session
from vaishnavi.api import system as api_system
from vaishnavi.api.system import AppState
from vaishnavi.ui import guards, layout
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.session import load_client, save_client

log = logging.getLogger("vaishnavi.ui.server")

PAGE_REGISTRARS: list[Callable[[AppState], None]] = []
"""Units append ``register(state)`` callables here (import side effect of ``vaishnavi.ui.pages.<page>``)."""

PAGE_MODULES: tuple[str, ...] = ("login", "onboarding", "dashboard", "notifications", "pos", "sales_history", "shifts", "day_close", "products", "stock", "labels", "reports", "status", "logs", "settings")

REQUIRED_ROUTES: tuple[str, ...] = tuple(item.path for item in guards.NAV_ITEMS) + ("/login", "/lock", "/end-work", "/onboarding", "/labels", "/shifts/day-close")
"""Paths the shell links to; a missing one is logged as ``E-UI-001`` right after registration (it would 404)."""


def register_pages(state: AppState) -> None:
    """Import every page module and let it register its routes. A page that fails to import is an ERROR, not silence."""
    import importlib  # noqa: PLC0415

    for name in PAGE_MODULES:
        try:
            importlib.import_module(f"vaishnavi.ui.pages.{name}")
        except Exception as exc:  # noqa: BLE001 - one broken page must not hide behind a quiet debug line
            log.error("UI_PAGE_IMPORT_FAILED page=%s error=%r code=E-UI-001", name, exc, exc_info=True)
    for registrar in PAGE_REGISTRARS:
        registrar(state)
    log.info("UI_PAGES_REGISTERED count=%d", len(PAGE_REGISTRARS))


def registered_paths(app: Any) -> list[str]:
    """Every HTTP route path NiceGUI/FastAPI currently knows (for the start-up log and the route check)."""
    return sorted({str(p) for p in (getattr(route, "path", None) for route in app.routes) if p})


def missing_routes(paths: list[str], required: tuple[str, ...] = REQUIRED_ROUTES) -> list[str]:
    """Required shell routes that are not registered."""
    have = set(paths)
    return [p for p in required if p not in have]


def match_report(app: Any, paths: tuple[str, ...] = REQUIRED_ROUTES) -> dict[str, str | None]:
    """Ask the router itself which endpoint a GET for each path would reach (``None`` → the browser would get a 404).

    A registered route can still be unreachable (shadowed, removed, cached); this answers the real question.
    """
    out: dict[str, str | None] = {}
    for path in paths:
        scope = {"type": "http", "method": "GET", "path": path, "headers": [], "root_path": "", "query_string": b""}
        hit: str | None = None
        for route in app.routes:
            try:
                match, _ = route.matches(scope)
            except Exception:  # noqa: BLE001 - foreign route types
                continue
            if getattr(match, "name", str(match)).upper().endswith("FULL"):
                endpoint = getattr(route, "endpoint", None)
                hit = f"{getattr(endpoint, '__module__', '?')}.{getattr(endpoint, '__qualname__', getattr(endpoint, '__name__', type(route).__name__))}"
                break
        out[path] = hit
    return out


def _log_routes(app: Any, state: AppState, *, stage: str = "registered") -> None:
    paths = registered_paths(app)
    pages = [p for p in paths if not p.startswith("/_nicegui") and not p.startswith("/api/")]
    missing = missing_routes(paths)
    state.extra["ui_routes"], state.extra["ui_routes_missing"] = pages, missing  # shown on /system/status
    log.info("UI_ROUTES stage=%s count=%d pages=%s", stage, len(paths), ",".join(pages))
    for path in missing:
        log.error("UI_ROUTE_MISSING path=%s code=E-UI-001 — its page module did not register (see UI_PAGE_IMPORT_FAILED above)", path)
    report = match_report(app)
    unreachable = [p for p, hit in report.items() if hit is None]
    log.info("UI_ROUTE_MATCH stage=%s %s", stage, " ".join(f"{p}->{(hit or 'NONE').rsplit('.', 1)[-1]}" for p, hit in report.items()))
    for path in unreachable:
        log.error("UI_ROUTE_UNREACHABLE path=%s code=E-UI-001 — registered=%s but no route matches a GET; the browser gets 404", path, path in paths)
    state.extra["ui_routes_missing"] = sorted(set(missing) | set(unreachable))


def _install_not_found_handler(app: Any, state: AppState) -> None:
    """Log every 404 with its path/referer (``E-UI-002``), remember the last few for the status page, show a bilingual page."""
    from fastapi import Request  # noqa: PLC0415
    from fastapi.responses import HTMLResponse  # noqa: PLC0415

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception) -> HTMLResponse:
        path = request.url.path
        if path.startswith("/_nicegui") or path.startswith("/favicon"):
            log.debug("HTTP_404_ASSET path=%s", path)
        else:
            log.warning("HTTP_404 path=%s query=%s referer=%s code=E-UI-002", path, request.url.query or "-", request.headers.get("referer", "-"))
            recent = state.extra.setdefault("recent_404", [])
            recent.append(f"{path}{'?' + request.url.query if request.url.query else ''}  ←  {request.headers.get('referer', '-')}")
            del recent[:-10]
        return HTMLResponse(status_code=404, content=not_found_html(path))


def capture_uvicorn_access() -> None:
    """Route uvicorn's access lines (``GET /shifts … 404``) into our ``http.log``.

    uvicorn's default dictConfig gives ``uvicorn.access`` its own stderr handler with ``propagate=False``, so our
    file handlers never saw a single request — the owner's ``access/http.log`` stayed empty (live run 3).
    """
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.INFO)
    log.info("UVICORN_LOGGING_CAPTURED families=access")


def not_found_html(path: str) -> str:
    """The 404 body (pure, tested)."""
    from html import escape  # noqa: PLC0415

    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>404</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#FBF7EF;color:#2B2118;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        ".c{text-align:center;max-width:520px;padding:24px}h1{color:#B4532A;font-size:48px;margin:0 0 8px}code{background:#F4EDE2;padding:2px 6px;border-radius:4px}"
        "a{display:inline-block;margin-top:18px;padding:10px 18px;background:#B4532A;color:#fff;border-radius:8px;text-decoration:none}</style></head>"
        f"<body><div class='c'><h1>404</h1><p>This page does not exist: <code>{escape(path)}</code></p>"
        "<p>यह पेज मौजूद नहीं है। बाईं ओर के मेन्यू का उपयोग करें।</p>"
        "<p style='font-size:13px;color:#7A4A1E'>If a button brought you here, tell the owner — the address is recorded in the app log (E-UI-002).</p>"
        "<a href='/'>Home · होम</a></div></body></html>"
    )


def frame(state: AppState, *, current_path: str, denied: str | None = None) -> Callable[[], Any]:
    """Render the shell around a page body; returns a context manager for the body area."""
    from nicegui import app, ui  # noqa: PLC0415

    client = load_client(app)
    model = layout.build_shell(state, client, current_path=current_path, denied=denied)
    layout.render_head()

    def set_lang(code: str) -> None:
        client.lang = code
        save_client(app, client)
        ui.navigate.reload()

    bell_count = 0
    services = state.extra.get("services")
    if client.user is not None and services is not None:
        from vaishnavi.ui.components.notification_bell import bell_view  # noqa: PLC0415

        bell_count = bell_view(services.notifications, user_id=client.user.user_id, role=client.user.role, texts=UiTexts.load(client.lang)).unread_count

    layout.render_topbar(model, on_lang=set_lang, bell_count=bell_count, on_bell=(lambda: ui.navigate.to("/notifications")) if client.user else None, on_lock=(lambda: _lock_now(state, client)) if client.user else None, on_end_work=(lambda: ui.navigate.to("/end-work")) if client.user else None)
    banner = layout.integrity_banner(state, client, UiTexts.load(client.lang))
    if banner is not None:
        with ui.row().classes("w-full items-center gap-2 p-2").style("background:#B71C1C;color:white"):
            ui.icon("report").classes("text-white")
            ui.label(banner.text).classes("flex-1 font-semibold")
            ui.button("Reports", on_click=lambda: ui.navigate.to(banner.link)).props("flat color=white")
    if model.denied_text:
        ui.notify(model.denied_text, type="warning")
    if client.user is not None and client.work_session_id is not None:
        # Layers 2–3 of docs/05 §6a: heartbeat every 15 s + the window-close beacon; plus the idle-lock timer.
        ui.add_body_html(f"<script>{api_session.HEARTBEAT_JS % {'session_id': client.work_session_id, 'every_ms': state.settings.billing.heartbeat_seconds * 1000}}</script>")
        ui.timer(30.0, lambda: _idle_check(state, client))
    body = ui.row().classes("w-full flex-1 no-wrap")
    with body:
        if model.nav:
            layout.render_rail(model)
        content = ui.column().classes("flex-1 p-4")
    def open_chat() -> None:
        from vaishnavi.ui.components.chat_widget import open_window  # noqa: PLC0415

        open_window(state, client, UiTexts.load(client.lang))

    layout.render_chat_bubble(model, on_click=open_chat if (client.user is not None and services is not None) else None)
    layout.render_footer(model)
    return content


def _lock_now(state: AppState, client: Any) -> None:
    from nicegui import app, ui  # noqa: PLC0415

    client.lock()
    save_client(app, client)
    services = state.extra.get("services")
    if services is not None and client.work_session_id is not None:
        services.sessions.record_lock(client.work_session_id)
    ui.navigate.to("/lock")


def _idle_check(state: AppState, client: Any) -> None:
    """Runs in the page's timer: idle → lock; locked too long → the session ends (``IDLE_LOCK_TIMEOUT``)."""
    from nicegui import app, ui  # noqa: PLC0415

    fresh = load_client(app)
    if not fresh.is_authenticated:
        return
    if fresh.should_lock(idle_minutes=state.settings.billing.idle_lock_minutes):
        _lock_now(state, fresh)
    elif fresh.lock_timed_out(max_locked_minutes=state.settings.billing.idle_lock_minutes * 3):
        services = state.extra.get("services")
        if services is not None and fresh.work_session_id is not None and fresh.locked_since is not None:
            services.sessions.idle_lock_timeout(fresh.work_session_id, locked_since=fresh.locked_since)
        fresh.sign_out()
        save_client(app, fresh)
        ui.navigate.to("/login")


def _register_frame_pages(state: AppState) -> None:
    """Pages owned by the shell: ``/lock``, ``/end-work``, ``/change-password`` (``/`` lives in ``pages.dashboard``)."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.core.errors import VaishnaviError  # noqa: PLC0415
    from vaishnavi.core.money import format_inr  # noqa: PLC0415
    from vaishnavi.core.permissions import Role  # noqa: PLC0415

    @ui.page("/lock")
    def _lock(next: str = "/") -> None:  # noqa: A002 - query parameter name
        client = load_client(app)
        if not client.is_authenticated:
            ui.navigate.to("/login")
            return
        services = state.extra["services"]
        texts = UiTexts.load(client.lang)
        layout.render_head()
        typed = {"password": ""}
        with ui.column().classes("items-center justify-center w-full h-screen"):
            with ui.column().classes("vsm-card p-6 gap-3 items-center").style("width: 420px"):
                ui.element("div").classes("circ").props(f'innerHTML="{client.initials()}"').style("width:56px;height:56px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;background:#F57C00;font-size:20px")
                ui.label(client.user.display_name).classes("text-xl font-bold")  # type: ignore[union-attr]
                ui.label(texts.t("lock.hint")).classes("text-sm text-gray-500")
                pw = ui.input(texts.t("login.password"), password=True, password_toggle_button=True).bind_value(typed, "password").classes("w-full")

                def unlock() -> None:
                    try:
                        ok = services.users.reauthenticate(client.user.user_id, typed["password"])  # type: ignore[union-attr]
                    except VaishnaviError as exc:
                        ui.notify(exc.user_message(client.lang), type="negative")
                        return
                    typed["password"] = ""
                    if not ok:
                        ui.notify(texts.t("lock.wrong"), type="negative")
                        return
                    if client.work_session_id is not None:
                        services.sessions.record_unlock(client.work_session_id, locked_seconds=client.locked_seconds())
                    client.unlock()
                    save_client(app, client)
                    ui.navigate.to(next)

                pw.on("keydown.enter", lambda _e: unlock())
                ui.button(texts.t("lock.unlock"), on_click=unlock).props("unelevated color=primary").classes("w-full vsm-touch")
                ui.button(texts.t("badge.end_work"), on_click=lambda: ui.navigate.to("/end-work")).props("flat no-caps")

    @ui.page("/end-work")
    def _end_work() -> None:
        client = load_client(app)
        if not client.is_authenticated or client.work_session_id is None:
            ui.navigate.to("/login")
            return
        services = state.extra["services"]
        texts = UiTexts.load(client.lang)
        layout.render_head()
        preview = services.sessions.end_work_preview(client.work_session_id)
        note = {"text": "", "pin": ""}
        with ui.column().classes("items-center justify-center w-full h-screen"):
            with ui.column().classes("vsm-card p-6 gap-2").style("width: 520px"):
                ui.label(texts.t("endwork.title", name=client.user.display_name)).classes("text-xl font-bold")  # type: ignore[union-attr]
                from vaishnavi.core.clock import format_ist, parse_iso_utc  # noqa: PLC0415

                ui.label(texts.t("endwork.exit_time", time=format_ist(parse_iso_utc(preview.now), "%H:%M"))).classes("text-lg")
                ui.label(texts.t("endwork.summary", login=format_ist(parse_iso_utc(preview.login_at), "%H:%M"), duration=preview.duration_text, bills=preview.bills, sales=format_inr(preview.sales_paise))).classes("text-sm text-gray-600")
                if preview.open_shift:
                    with ui.element("div").classes("rounded-lg p-2 text-xs").style("border:1px solid #FFE0B2;background:#FFF8E1;color:#7a4b00"):
                        ui.label(texts.t("endwork.open_shift"))
                    ui.button(texts.t("endwork.close_shift_first"), on_click=lambda: ui.navigate.to("/shifts")).props("flat no-caps")
                    if client.role is not Role.OWNER:
                        ui.input(texts.t("endwork.owner_pin"), password=True).bind_value(note, "pin").classes("w-full")
                ui.input(texts.t("endwork.note")).bind_value(note, "text").classes("w-full")

                def confirm() -> None:
                    actor = client.actor()
                    try:
                        if preview.open_shift and client.role is not Role.OWNER:
                            actor = services.users.verify_owner_pin(note["pin"], on_behalf_of=actor)
                        services.sessions.end(client.work_session_id, actor=actor, note=note["text"] or None)
                    except VaishnaviError as exc:
                        ui.notify(f"{exc.user_message(client.lang)} {exc.what_to_do(client.lang)}", type="negative")
                        return
                    client.sign_out()
                    save_client(app, client)
                    ui.navigate.to("/login")

                with ui.row().classes("w-full justify-between"):
                    ui.button(texts.t("common.cancel"), on_click=lambda: ui.navigate.to("/")).props("flat no-caps")
                    ui.button(texts.t("common.confirm"), on_click=confirm).props("unelevated color=primary").classes("vsm-touch")

    @ui.page("/change-password")
    def _change_password() -> None:
        client = load_client(app)
        if not client.is_authenticated:
            ui.navigate.to("/login")
            return
        services = state.extra["services"]
        texts = UiTexts.load(client.lang)
        layout.render_head()
        form = {"current": "", "new": "", "again": ""}
        with ui.column().classes("items-center justify-center w-full h-screen"):
            with ui.column().classes("vsm-card p-6 gap-2").style("width: 480px"):
                ui.label(texts.t("password.title")).classes("text-xl font-bold")
                ui.input(texts.t("password.current"), password=True).bind_value(form, "current").classes("w-full")
                ui.input(texts.t("password.new"), password=True).bind_value(form, "new").classes("w-full")
                ui.input(texts.t("password.again"), password=True).bind_value(form, "again").classes("w-full")

                def save() -> None:
                    if form["new"] != form["again"]:
                        ui.notify(texts.t("password.mismatch"), type="negative")
                        return
                    try:
                        services.users.change_password(client.actor(), form["current"], form["new"])
                    except VaishnaviError as exc:
                        ui.notify(f"{exc.user_message(client.lang)} {exc.what_to_do(client.lang)}", type="negative")
                        return
                    ui.notify(texts.t("password.changed"), type="positive")
                    ui.navigate.to("/" if client.role is Role.OWNER else "/pos")

                ui.button(texts.t("common.confirm"), on_click=save).props("unelevated color=primary").classes("w-full vsm-touch")


def serve(state: AppState, on_ready: Callable[[], None]) -> None:
    """Run NiceGUI in the foreground until the window/process ends."""
    from nicegui import app, ui  # noqa: PLC0415

    settings = state.settings
    services = state.extra.get("services")
    api_system.mount(app, state, notifications=getattr(services, "notifications", None))
    if services is not None:
        api_session.mount(app, services.sessions)
    _register_frame_pages(state)
    register_pages(state)
    _log_routes(app, state)
    _install_not_found_handler(app, state)

    def heartbeat() -> None:
        state.beat("request")
        if state.extra.get("shutdown_requested"):
            log.warning("APP_STOPPING restart=%s", bool(state.extra.get("restart_requested")))
            app.shutdown()

    def on_startup() -> None:
        capture_uvicorn_access()
        _log_routes(app, state, stage="serving")  # NiceGUI/uvicorn may add or reorder routes while starting
        on_ready()

    app.on_startup(on_startup)
    # app.timer, not ui.timer: a UI element in global scope makes NiceGUI >= 3 refuse to start with @ui.page routes.
    app.timer(10.0, heartbeat)
    tls = state.extra.get("tls")  # (cert_path, key_path) prepared by startup when server.https_enabled
    port = settings.server.https_port if tls else settings.server.port
    extra_kwargs = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}
    log.info("UI_SERVE host=%s port=%d https=%s kiosk=%s", settings.server.bind_host, port, bool(tls), settings.server.kiosk_browser)
    ui.run(
        host=settings.server.bind_host,
        port=port,
        title=settings.shop.name,
        favicon="🪔",
        dark=False,
        reload=False,
        show=False,  # the kiosk browser is opened by startup.on_ready (platform.browser)
        storage_secret=state.extra.get("storage_secret") or secrets.token_urlsafe(32),
        language="en-IN",
        **extra_kwargs,
    )
