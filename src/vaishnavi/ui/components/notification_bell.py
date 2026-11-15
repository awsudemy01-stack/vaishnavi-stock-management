"""Notification bell (docs/09 §1, ADR-038): unread badge + list with severity colour, deep link, relative time.

Staff see only what concerns them — rows addressed to them plus the broadcast kinds about the counter
(scanner, pending receipts, their own session); the Owner sees everything. The filter is a view rule;
the service still stores everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vaishnavi.core.clock import now_utc, parse_iso_utc
from vaishnavi.core.permissions import Role
from vaishnavi.services.notifications import NotificationService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.theme import severity_color

STAFF_BROADCAST_KINDS: Final = frozenset({"SCANNER_MISSING", "RECEIPTS_PENDING", "PRINTER_MISSING", "SESSION_UNCLEAN", "SETUP_REQUIRED"})
_SEVERITY_RANK: Final = {"INFO": 0, "WARN": 1, "HIGH": 2}


@dataclass(frozen=True)
class NotificationItem:
    """One row as rendered."""

    id: int
    kind: str
    severity: str
    title: str
    body: str
    deep_link: str
    when_text: str
    color: str
    user_id: int | None


@dataclass(frozen=True)
class BellView:
    """Badge + list."""

    unread_count: int
    items: list[NotificationItem]
    worst_severity: str | None

    @property
    def color(self) -> str:
        """Badge colour."""
        return severity_color(self.worst_severity) if self.worst_severity else severity_color("")


def relative_time(when: datetime, *, now: datetime | None = None, texts: UiTexts) -> str:
    """``just now`` / ``5 min ago`` / ``3 h ago`` / ``2 d ago`` in the UI language."""
    now = now or now_utc()
    seconds = max(0.0, (now - when).total_seconds())
    if seconds < 60:
        return texts.t("bell.just_now")
    if seconds < 3600:
        return texts.t("bell.minutes_ago", n=int(seconds // 60))
    if seconds < 86400:
        return texts.t("bell.hours_ago", n=int(seconds // 3600))
    return texts.t("bell.days_ago", n=int(seconds // 86400))


def visible_to(row: dict[str, Any], *, user_id: int, role: Role) -> bool:
    """The view rule described in the module docstring."""
    if role is Role.OWNER:
        return True
    if row.get("user_id") == user_id:
        return True
    return row.get("user_id") is None and str(row.get("kind")) in STAFF_BROADCAST_KINDS


def bell_view(service: NotificationService, *, user_id: int, role: Role, texts: UiTexts, now: datetime | None = None) -> BellView:
    """Build the bell for one user."""
    now = now or now_utc()
    items: list[NotificationItem] = []
    for row in service.unread(user_id=user_id):
        if not visible_to(row, user_id=user_id, role=role):
            continue
        try:
            created = parse_iso_utc(str(row.get("created_at")))
        except ValueError:
            created = now
        severity = str(row.get("severity") or "INFO")
        items.append(
            NotificationItem(
                id=int(row["id"]),
                kind=str(row.get("kind") or ""),
                severity=severity,
                title=str(row.get("title") or ""),
                body=str(row.get("body") or ""),
                deep_link=str(row.get("deep_link") or "/"),
                when_text=relative_time(created, now=now, texts=texts),
                color=severity_color(severity),
                user_id=row.get("user_id"),
            )
        )
    worst = max((i.severity for i in items), key=lambda s: _SEVERITY_RANK.get(s, 0), default=None)
    return BellView(unread_count=len(items), items=items, worst_severity=worst)
