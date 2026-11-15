"""``/notifications`` — the bell's full history: unread first, severity colour, deep link, mark read / mark all.

Uses the bell's visibility rule (:func:`notification_bell.visible_to`) so staff and owner see exactly what
the badge counts. View-models are pure and tested; NiceGUI rendering is thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vaishnavi.core.clock import now_utc, parse_iso_utc
from vaishnavi.core.permissions import Role
from vaishnavi.ui.components.notification_bell import relative_time, visible_to
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.theme import severity_color

log = logging.getLogger("vaishnavi.ui.pages.notifications")


@dataclass(frozen=True)
class NotificationRow:
    """One line."""

    id: int
    kind: str
    severity: str
    title: str
    body: str
    deep_link: str
    when_text: str
    color: str
    read: bool


@dataclass(frozen=True)
class NotificationsModel:
    """The page."""

    items: list[NotificationRow]
    unread_count: int
    labels: dict[str, str] = field(default_factory=dict)


def build_notifications(c: Any, texts: UiTexts, *, user_id: int, role: Role, limit: int = 100) -> NotificationsModel:
    """Recent notifications visible to this user, newest first, unread flagged."""
    now = now_utc()
    rows: list[NotificationRow] = []
    for row in c.notifications.recent(user_id=user_id, limit=limit):
        if not visible_to(row, user_id=user_id, role=role):
            continue
        try:
            created = parse_iso_utc(str(row.get("created_at")))
        except ValueError:
            created = now
        severity = str(row.get("severity") or "INFO")
        rows.append(NotificationRow(int(row["id"]), str(row.get("kind") or ""), severity, str(row.get("title") or ""), str(row.get("body") or ""), str(row.get("deep_link") or "/"), relative_time(created, now=now, texts=texts), severity_color(severity), row.get("read_at") is not None))
    labels = {k: texts.t(f"notifications.{k}") for k in ("title", "empty", "mark_read", "mark_all", "open", "unread")}
    unread = sum(1 for r in rows if not r.read)
    log.debug("NOTIFICATIONS_PAGE user=%s role=%s rows=%d unread=%d", user_id, role.value, len(rows), unread)
    return NotificationsModel(rows, unread, labels)


def mark_read(c: Any, *, user_id: int, notification_id: int) -> bool:
    """Acknowledge one row."""
    return bool(c.notifications.mark_read(user_id=user_id, notification_id=notification_id))


def mark_all_read(c: Any, *, user_id: int, role: Role) -> int:
    """Acknowledge every unread row this user can see; returns how many."""
    count = 0
    for row in c.notifications.unread(user_id=user_id):
        if visible_to(row, user_id=user_id, role=role) and c.notifications.mark_read(user_id=user_id, notification_id=int(row["id"])):
            count += 1
    log.info("NOTIFICATIONS_MARK_ALL user=%s count=%d", user_id, count)
    return count


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/notifications``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/notifications")
    def _notifications() -> None:
        client = load_client(app)
        decision = guards.decide("/notifications", client)
        if decision is not guards.Decision.ALLOW or client.user is None:
            ui.navigate.to(guards.redirect_for(decision, "/notifications"))
            return
        texts = UiTexts.load(client.lang)
        user_id, role = client.user.user_id, client.role
        with frame(state, current_path="/notifications"):
            box = ui.column().classes("w-full gap-2")

        def render() -> None:
            m = build_notifications(services, texts, user_id=user_id, role=role)
            box.clear()
            with box:
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"{m.labels['title']} · {m.unread_count} {m.labels['unread']}").classes("text-xl font-semibold")
                    if m.unread_count:
                        ui.button(m.labels["mark_all"], icon="done_all", on_click=lambda: (mark_all_read(services, user_id=user_id, role=role), render())).props("flat")
                if not m.items:
                    ui.label(m.labels["empty"]).classes("text-sm text-gray-600")
                for item in m.items:
                    with ui.row().classes("w-full items-center gap-3 vsm-card p-2").style("" if not item.read else "opacity:0.6"):
                        ui.icon("circle").style(f"color:{item.color}; font-size: 12px")
                        with ui.column().classes("flex-1 gap-0"):
                            ui.label(item.title).classes("font-semibold" if not item.read else "")
                            if item.body:
                                ui.label(item.body).classes("text-sm text-gray-600")
                            ui.label(item.when_text).classes("text-xs text-gray-500")
                        ui.button(m.labels["open"], on_click=lambda _e, p=item.deep_link, i=item.id: (mark_read(services, user_id=user_id, notification_id=i), ui.navigate.to(p))).props("flat dense")
                        if not item.read:
                            ui.button(icon="done", on_click=lambda _e, i=item.id: (mark_read(services, user_id=user_id, notification_id=i), render())).props("flat dense")

        render()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:  # pragma: no cover - import order
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
