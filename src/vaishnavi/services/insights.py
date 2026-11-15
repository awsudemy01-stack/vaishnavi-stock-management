"""Insight bullets for the dashboard and the assistant (docs/09 §2, docs/11 §3 "fast-path facts").

Each :class:`Insight` is a *fact with an i18n key* — the UI (or the chat layer) renders the sentence in the
user's language from ``key`` + ``params``; the service never produces prose. Bullets are sorted worst-first.
Everything here is composed from the already-authorised report/forecast/shift services, so the role rules
live in one place: :meth:`InsightsService.for_owner` requires ``REPORT_SALES``; :meth:`for_staff` only
surfaces what the counter needs (stock running out, receipts waiting).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from vaishnavi.core.clock import today_ist
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.services.forecast import ForecastService
from vaishnavi.services.print_queue import PrintQueueService
from vaishnavi.services.report import ReportService
from vaishnavi.services.shift import ShiftService

log = logging.getLogger("vaishnavi.services.insights")

SEVERITY_RANK: Final = {"HIGH": 0, "WARN": 1, "INFO": 2}
MANUAL_SHARE_WARN: Final = 0.15
"""Above this share of non-scanned lines in a day the owner should look (docs/05 §9)."""
STOCKOUT_SOON_DAYS: Final = 7
DEAD_STOCK_DAYS: Final = 60


@dataclass(frozen=True)
class Insight:
    """One bullet."""

    kind: str
    severity: str
    key: str
    link: str
    params: dict[str, Any] = field(default_factory=dict)


def _sorted(items: list[Insight]) -> list[Insight]:
    return sorted(items, key=lambda i: SEVERITY_RANK.get(i.severity, 9))


class InsightsService:
    """Compose report facts into ranked bullets."""

    def __init__(self, reports: ReportService, forecast: ForecastService, shifts: ShiftService, print_queue: PrintQueueService) -> None:
        self._reports = reports
        self._forecast = forecast
        self._shifts = shifts
        self._print_queue = print_queue

    # -- owner --------------------------------------------------------------------------------------
    def for_owner(self, actor: Actor) -> list[Insight]:
        """Sales today vs yesterday, low stock, stock-outs ahead, dead stock, manual lines, drawer variance, receipts."""
        require(actor, Permission.REPORT_SALES)
        out: list[Insight] = []
        today = today_ist()
        yesterday = (today - timedelta(days=1)).isoformat()
        out.extend(self._sales_today(actor, today.isoformat(), yesterday))
        out.extend(self._stock(actor, owner=True))
        out.extend(self._manual_lines(actor, today.isoformat()))
        out.extend(self._variance())
        out.extend(self._print_queue_pending())
        log.debug("INSIGHTS_OWNER count=%d kinds=%s", len(out), ",".join(i.kind for i in out))
        return _sorted(out)

    def for_staff(self, actor: Actor) -> list[Insight]:
        """What the counter needs to know — nothing about money."""
        require(actor, Permission.VIEW_STOCK_LEVEL)
        out = self._stock(actor, owner=False) + self._print_queue_pending()
        log.debug("INSIGHTS_STAFF count=%d", len(out))
        return _sorted(out)

    # -- pieces -------------------------------------------------------------------------------------
    def _sales_today(self, actor: Actor, today: str, yesterday: str) -> list[Insight]:
        now = self._reports.sales_summary(actor, since=today, until=today)
        prev = self._reports.sales_summary(actor, since=yesterday, until=yesterday)
        change = None if not prev.net_paise else int(round((now.net_paise - prev.net_paise) * 100 / prev.net_paise))
        key = "insights.sales_today" if change is None else ("insights.sales_up" if change >= 0 else "insights.sales_down")
        return [Insight("SALES_TODAY", "INFO", key, "/reports/sales", {"net": format_inr(now.net_paise), "bills": now.bills, "avg": format_inr(now.avg_bill_paise), "change": abs(change or 0)})]

    def _stock(self, actor: Actor, *, owner: bool) -> list[Insight]:
        out: list[Insight] = []
        low = self._reports.low_stock(actor)
        if low:
            names = ", ".join(r.name for r in low[:3]) + (" …" if len(low) > 3 else "")
            out.append(Insight("LOW_STOCK", "WARN", "insights.low_stock", "/reports/low_stock", {"count": len(low), "names": names}))
        if not owner:
            return out
        try:
            plan = self._forecast.plan(actor)
        except VaishnaviError as exc:  # forecast is advisory; never break the dashboard
            log.warning("INSIGHTS_FORECAST_SKIPPED code=%s", exc.code)
            plan = []
        soon = [r for r in plan if r.days_of_cover is not None and r.days_of_cover <= STOCKOUT_SOON_DAYS and r.stock > 0]
        if soon:
            out.append(Insight("STOCKOUT_SOON", "WARN", "insights.stockout_soon", "/reports/forecast", {"count": len(soon), "names": ", ".join(r.name for r in soon[:3]), "days": STOCKOUT_SOON_DAYS}))
        reorder = [r for r in plan if r.reorder_qty > 0]
        if reorder:
            out.append(Insight("REORDER", "INFO", "insights.reorder", "/reports/forecast", {"count": len(reorder)}))
        dead = self._reports.dead_stock(actor, days=DEAD_STOCK_DAYS)
        if dead:
            out.append(Insight("DEAD_STOCK", "INFO", "insights.dead_stock", "/reports/dead", {"count": len(dead), "value": format_inr(sum(d.value_paise for d in dead)), "days": DEAD_STOCK_DAYS}))
        return out

    def _manual_lines(self, actor: Actor, today: str) -> list[Insight]:
        rows = self._reports.manual_lines(actor, since=today, until=today)
        flagged = [r for r in rows if r.total_lines >= 3 and r.manual_ratio >= MANUAL_SHARE_WARN]
        if not flagged:
            return []
        worst = max(flagged, key=lambda r: r.manual_ratio)
        return [Insight("MANUAL_LINES", "WARN", "insights.manual_lines", "/reports/manual_lines", {"share": int(round(worst.manual_ratio * 100)), "lines": worst.lookup_lines + worst.open_item_lines, "user_id": worst.user_id})]

    def _variance(self) -> list[Insight]:
        closed = [s for s in self._shifts.variance_history(limit=5) if s.variance_paise]
        if not closed:
            return []
        s = closed[0]
        return [Insight("VARIANCE", "WARN", "insights.variance", "/reports/variance", {"variance": format_inr(abs(s.variance_paise or 0)), "sign": "short" if (s.variance_paise or 0) < 0 else "over", "shift_id": s.id, "user_id": s.user_id})]

    def _print_queue_pending(self) -> list[Insight]:
        pending = self._print_queue.pending_count()
        return [Insight("PRINT_QUEUE", "INFO", "insights.print_queue", "/settings/hardware", {"count": pending})] if pending else []
