"""Route guards and the role-filtered navigation (docs/09 §2 "What each role sees after login").

Pure decisions: ``decide(path, client)`` → ``Decision``; ``nav_for(role)`` → the rail items a role may
use (items a role cannot use are **not rendered**; the services reject the call anyway — the guard is a
courtesy, not the security boundary). ``page()`` is the thin NiceGUI decorator built on top of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final
from urllib.parse import quote

from vaishnavi.core.permissions import Grant, Permission, Role, grant_for
from vaishnavi.ui.state import ClientState

log = logging.getLogger("vaishnavi.ui.guards")


class Decision(str, Enum):
    """What the guard says about one request."""

    ALLOW = "ALLOW"
    LOGIN = "LOGIN"  # not signed in → login page, remembering where they wanted to go
    LOCKED = "LOCKED"  # signed in but idle-locked → PIN overlay
    FORBIDDEN = "FORBIDDEN"  # signed in, role may not open this → home with a plain note (E-AUTH-010)
    REDIRECT_HOME = "REDIRECT_HOME"  # a signed-in user asked for /login
    CONFIRM_EXIT = "CONFIRM_EXIT"  # an unclean previous exit must be confirmed before anything else (E-AUTH-021)


@dataclass(frozen=True)
class NavItem:
    """One entry of the left navigation rail."""

    key: str
    path: str
    label_key: str
    icon: str
    permission: Permission | None = None
    """None → any signed-in user."""


NAV_ITEMS: Final[tuple[NavItem, ...]] = (
    NavItem("dashboard", "/", "nav.dashboard", "dashboard"),
    NavItem("pos", "/pos", "nav.pos", "point_of_sale", Permission.SELL),
    NavItem("products", "/products", "nav.products", "inventory_2", Permission.VIEW_STOCK_LEVEL),
    NavItem("stock", "/stock", "nav.stock", "warehouse", Permission.VIEW_STOCK_LEVEL),
    NavItem("sales_history", "/sales", "nav.sales_history", "receipt_long", Permission.REPORT_OWN_SHIFT),
    NavItem("reports", "/reports", "nav.reports", "bar_chart", Permission.REPORT_OWN_SHIFT),
    NavItem("shifts", "/shifts", "nav.shifts", "schedule", Permission.SHIFT_OWN),
    NavItem("settings", "/settings", "nav.settings", "settings", Permission.MANAGE_SYSTEM),
)

PUBLIC_PATHS: Final = ("/login", "/healthz", "/lock")
"""Reachable without a session (``/lock`` is for a locked session; anonymous users are sent to login)."""

ROUTE_PERMISSIONS: Final[tuple[tuple[str, Permission | None], ...]] = (
    # longest prefix first
    ("/reports/profit", Permission.REPORT_PROFIT),
    ("/reports/purchases", Permission.REPORT_PURCHASES),
    ("/reports/forecast", Permission.REPORT_FORECAST),
    ("/reports/audit", Permission.REPORT_AUDIT),
    ("/reports/sales", Permission.REPORT_SALES),
    ("/reports", Permission.REPORT_OWN_SHIFT),
    ("/settings", Permission.MANAGE_SYSTEM),
    ("/system", Permission.MANAGE_SYSTEM),
    ("/pos", Permission.SELL),
    ("/products", Permission.VIEW_STOCK_LEVEL),
    ("/stock", Permission.VIEW_STOCK_LEVEL),
    ("/sales", Permission.REPORT_OWN_SHIFT),
    ("/shifts", Permission.SHIFT_OWN),
    ("/onboarding", Permission.MANAGE_SYSTEM),
)


def route_permission(path: str) -> Permission | None:
    """Permission a path needs (prefix match, longest first); None → login is enough."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    for prefix, perm in ROUTE_PERMISSIONS:
        if clean == prefix or clean.startswith(prefix + "/"):
            return perm
    return None


def nav_for(role: Role) -> list[NavItem]:
    """Rail items the role may use (``PIN`` grants still show — the PIN dialog comes at the action)."""
    items: list[NavItem] = []
    for item in NAV_ITEMS:
        if item.permission is None or grant_for(item.permission, role) is not Grant.DENY:
            items.append(item)
    return items


def decide(path: str, client: ClientState) -> Decision:
    """The guard."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    if not client.is_authenticated:
        return Decision.ALLOW if clean in ("/login", "/healthz", "/onboarding") else Decision.LOGIN  # /onboarding checks "setup pending" itself
    if clean == "/login":
        return Decision.REDIRECT_HOME
    if client.locked:
        return Decision.ALLOW if clean in ("/lock", "/healthz") else Decision.LOCKED
    if client.pending_exit_confirmation and clean not in ("/confirm-exit", "/healthz", "/end-work"):
        return Decision.CONFIRM_EXIT
    perm = route_permission(clean)
    if perm is not None and client.role is not None and grant_for(perm, client.role) is Grant.DENY:
        log.warning("ROUTE_FORBIDDEN path=%s user=%s role=%s permission=%s code=E-AUTH-010", clean, client.user.username if client.user else "-", client.role.value, perm.value)
        return Decision.FORBIDDEN
    return Decision.ALLOW


def redirect_for(decision: Decision, path: str) -> str:
    """Where to send the browser for a non-ALLOW decision."""
    clean = path.split("?", 1)[0] or "/"
    if decision is Decision.LOGIN:
        return f"/login?next={quote(clean, safe='/')}" if clean not in ("/", "") else "/login"
    if decision is Decision.LOCKED:
        return f"/lock?next={quote(clean, safe='/')}"
    if decision is Decision.CONFIRM_EXIT:
        return "/confirm-exit"
    if decision is Decision.FORBIDDEN:
        return f"/?denied={quote(clean, safe='/')}"
    return "/"


# ----------------------------------------------------------------------------- NiceGUI glue (thin)
def page(path: str, **page_kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """``@guards.page('/pos')`` — a NiceGUI page that applies :func:`decide` before rendering.

    Imported lazily so this module stays importable (and testable) without NiceGUI.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        from nicegui import app, ui  # noqa: PLC0415

        from vaishnavi.ui.session import load_client  # noqa: PLC0415

        @ui.page(path, **page_kwargs)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            client = load_client(app)
            decision = decide(path, client)
            if decision is not Decision.ALLOW:
                ui.navigate.to(redirect_for(decision, path))
                return None
            client.touch()
            return await fn(*args, **kwargs) if _is_coroutine(fn) else fn(*args, **kwargs)

        wrapped.__name__ = fn.__name__
        return wrapped

    return deco


def _is_coroutine(fn: Callable[..., Any]) -> bool:
    import inspect  # noqa: PLC0415

    return inspect.iscoroutinefunction(fn)
