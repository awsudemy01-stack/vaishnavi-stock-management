"""ReportPublisher — daily digest · weekly report · ad-hoc documents → outbox → channels (docs/05 §11).

The service composes the figures from the already-authorised report/forecast/shift services into the
renderer's data classes, writes the document under ``<primary>/reports/<YYYY-MM>/``, queues one delivery
per enabled channel and tries to send immediately; ``flush()`` (job, every 5 min) retries what failed.
Renderer, outbox and channel construction are injected by `AppServices` (services never import
``publishing`` — same rule as receipts/hardware). Publishing disabled → the document is still written
locally (the Owner can open it) but nothing is queued.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc, today_ist
from vaishnavi.core.errors import PublishError, VaishnaviError
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.settings import Settings
from vaishnavi.services.common import TxnFactory

log = logging.getLogger("vaishnavi.services.publisher")

MINUS = "\u2212"
OWNER_ACTOR = Actor(user_id=1, role=Role.OWNER)
"""Scheduled publishing runs on the Owner's behalf (the seeded Owner account is always id 1)."""


class Renderer(Protocol):
    """``publishing.renderer`` surface the service needs."""

    DigestData: Any
    WeeklyData: Any

    def render_digest(self, data: Any) -> str: ...  # pragma: no cover - protocol
    def digest_summary(self, data: Any) -> list[str]: ...  # pragma: no cover - protocol
    def render_weekly(self, data: Any) -> str: ...  # pragma: no cover - protocol
    def render_table(self, title: str, subtitle: str, headers: Any, rows: Any) -> str: ...  # pragma: no cover - protocol
    def write_document(self, html: str, stem: Path, *, pdf_engine: Any) -> Path: ...  # pragma: no cover - protocol


ChannelFactory = Callable[[str, Settings, "ReportPublisher"], Any]
"""``factory(channel_name, settings, publisher)`` → Channel; raises PublishError E-PUB-001 when not configured."""


@dataclass(frozen=True)
class PublishResult:
    """What one publish run did."""

    document: str
    path: Path
    summary: list[str]
    queued: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class ReportPublisher:
    """Owner-facing report delivery."""

    def __init__(self, txn: TxnFactory, *, outbox: Any, renderer: Renderer, channel_factory: ChannelFactory, reports: Any, forecast: Any, insights: Any, shifts: Any, ledger: Any, users: Any, settings: Callable[[], Settings], paths: Any, notifications: Any, pdf_engine: Any = None) -> None:
        self._txn = txn
        self._outbox = outbox
        self._renderer = renderer
        self._channel_factory = channel_factory
        self._reports, self._forecast, self._insights, self._shifts = reports, forecast, insights, shifts
        self._ledger, self._users, self._settings, self._paths, self._notifications = ledger, users, settings, paths, notifications
        self._pdf_engine = pdf_engine
        self.smtp_factory: Any = None
        """Injectable SMTP client factory (tests); None → ``smtplib.SMTP``."""
        self.http_post: Any = None
        """Injectable HTTP POST for Telegram/WhatsApp (tests)."""

    @property
    def outbox(self) -> Any:
        """The queue (read by Settings → Publishing)."""
        return self._outbox

    # -- documents --------------------------------------------------------------------------------
    def _reports_dir(self, day: date) -> Path:
        return Path(self._paths.primary) / "reports" / day.strftime("%Y-%m")

    def _names(self) -> dict[int, str]:
        return self._users.display_names()

    def digest_data(self, day: date | None = None, actor: Actor = OWNER_ACTOR) -> Any:
        """Collect the day's figures (strings, ready to render)."""
        day = day or today_ist()
        d = day.isoformat()
        s = self._reports.sales_summary(actor, since=d, until=d)
        since14 = (day - timedelta(days=13)).isoformat()
        by_day = {r.day: r.total_paise - r.refunds_paise for r in self._reports.sales_by_day(actor, since=since14, until=d)}
        spark = [by_day.get((day - timedelta(days=13 - i)).isoformat(), 0) for i in range(14)]
        voids = len(self._reports.voids(actor, since=d, until=d))
        overrides = len(self._reports.overrides(actor, since=d, until=d))
        names = self._names()
        variance = [f"Shift #{sh.id} ({names.get(sh.user_id, '#' + str(sh.user_id))}): {MINUS if (sh.variance_paise or 0) < 0 else '+'}{format_inr(abs(sh.variance_paise or 0))} {'short' if (sh.variance_paise or 0) < 0 else 'over'}" for sh in self._shifts.variance_history(limit=20) if sh.closed_at and sh.closed_at[:10] == d and sh.variance_paise]
        low = self._reports.low_stock(actor)
        top = [(t.name, _q(t.qty), format_inr(t.amount_paise)) for t in self._reports.top_sellers(actor, since=d, until=d, limit=10, by="amount")]
        insights = [self._insight_text(i) for i in self._insights.for_owner(actor)]
        with self._txn(label="publish.chain_head") as session:
            head = self._ledger.chain_head(session)
        return self._renderer.DigestData(shop_name=self._settings().shop.name, day=day.strftime("%d-%b-%Y"), net=format_inr(s.net_paise), bills=s.bills, avg_bill=format_inr(s.avg_bill_paise), items=_q(s.items), by_mode={k: format_inr(v) for k, v in s.by_mode.items()}, tax=format_inr(s.tax_paise), discounts=format_inr(s.discount_paise), voids=voids, overrides=overrides, variance_lines=variance, low_stock=[(r.name, _q(r.stock_qty), _q(r.min_stock)) for r in low[:15]], out_of_stock=[r.name for r in low if r.stock_qty <= 0][:15], insights=insights, chain_head=head, sparkline=spark, generated_at=format_ist(now_utc(), "%d-%b %H:%M"), top=top)

    def weekly_data(self, end: date | None = None, actor: Actor = OWNER_ACTOR) -> Any:
        """Last 7 days ending ``end`` (default today)."""
        end = end or today_ist()
        start = end - timedelta(days=6)
        a, b = start.isoformat(), end.isoformat()
        s = self._reports.sales_summary(actor, since=a, until=b)
        names = self._names()
        plan = [r for r in self._forecast.plan(actor) if r.reorder_qty > 0][:20]
        return self._renderer.WeeklyData(shop_name=self._settings().shop.name, period=f"{start.strftime('%d-%b')} – {end.strftime('%d-%b-%Y')}", net=format_inr(s.net_paise), bills=s.bills, top=[(t.name, _q(t.qty), format_inr(t.amount_paise)) for t in self._reports.top_sellers(actor, since=a, until=b, limit=10, by="amount")], dead=[(r.name, _q(r.stock_qty), format_inr(r.value_paise)) for r in self._reports.dead_stock(actor)[:15]], reorder=[(r.name, _q(r.stock), _q(r.days_of_cover), _q(r.reorder_qty)) for r in plan], cashiers=[(names.get(c.user_id, f"#{c.user_id}"), str(c.bills), format_inr(c.sales_paise), format_inr(c.variance_paise)) for c in self._reports.cashier_performance(actor, since=a, until=b)], shrinkage=[(r.name, format_inr(r.value_paise)) for r in self._reports.shrinkage(actor, since=a, until=b)[:15]], by_day=[(r.day, format_inr(r.total_paise - r.refunds_paise)) for r in self._reports.sales_by_day(actor, since=a, until=b)], generated_at=format_ist(now_utc(), "%d-%b %H:%M"))

    @staticmethod
    def _insight_text(i: Any) -> str:
        p = i.params
        table = {
            "SALES_TODAY": f"Today {p.get('net')} across {p.get('bills')} bills (average {p.get('avg')})",
            "LOW_STOCK": f"{p.get('count')} item(s) at or below minimum: {p.get('names')}",
            "STOCKOUT_SOON": f"{p.get('count')} item(s) will run out within {p.get('days')} days: {p.get('names')}",
            "REORDER": f"{p.get('count')} item(s) have a suggested order quantity",
            "DEAD_STOCK": f"{p.get('count')} item(s) unsold for {p.get('days')} days — {p.get('value')} on shelves",
            "MANUAL_LINES": f"{p.get('share')}% of lines today were not scanned ({p.get('lines')} manual lines)",
            "VARIANCE": f"Last closed shift #{p.get('shift_id')} was {p.get('variance')} {p.get('sign')}",
            "PRINT_QUEUE": f"{p.get('count')} receipt(s) waiting for the printer",
        }
        return table.get(i.kind, i.kind)

    # -- publishing -------------------------------------------------------------------------------
    def daily_digest(self, day: date | None = None) -> PublishResult:
        """Render today's digest, queue and send."""
        day = day or today_ist()
        data = self.digest_data(day)
        html = self._renderer.render_digest(data)
        path = self._renderer.write_document(html, self._reports_dir(day) / f"digest-{day.isoformat()}", pdf_engine=self._pdf_engine)
        summary = self._renderer.digest_summary(data)
        return self._deliver("digest", path, summary, f"Daily digest {data.day}")

    def weekly_report(self, end: date | None = None) -> PublishResult:
        """Render the weekly owner report, queue and send."""
        end = end or today_ist()
        data = self.weekly_data(end)
        html = self._renderer.render_weekly(data)
        path = self._renderer.write_document(html, self._reports_dir(end) / f"weekly-{end.isoformat()}", pdf_engine=self._pdf_engine)
        summary = [f"Week {data.period}: {data.net} · {data.bills} bills", f"Top: {', '.join(t[0] for t in data.top[:3]) or '—'}", f"Reorder: {len(data.reorder)} item(s)"]
        return self._deliver("weekly", path, summary, f"Weekly report {data.period}")

    def publish_table(self, title: str, subtitle: str, headers: list[str], rows: list[list[Any]], *, stem: str) -> PublishResult:
        """Any report screen → document → channels (Export PDF → Send)."""
        path = self.render_document(title, subtitle, headers, rows, stem=stem)
        return self._deliver("report", path, [title, subtitle, f"{len(rows)} rows"], title)

    def render_document(self, title: str, subtitle: str, headers: list[str], rows: list[list[Any]], *, stem: str) -> Path:
        """Any table as a PDF (HTML without an engine) under the reports folder — no delivery."""
        html = self._renderer.render_table(title, subtitle, headers, rows)
        return self._renderer.write_document(html, self._reports_dir(today_ist()) / stem, pdf_engine=self._pdf_engine)

    def _deliver(self, document: str, path: Path, summary: list[str], subject: str) -> PublishResult:
        pub = self._settings().publishing
        if not pub.enabled or not pub.channels:
            log.info("PUBLISH_LOCAL_ONLY document=%s path=%s enabled=%s channels=%s", document, path.name, pub.enabled, pub.channels)
            return PublishResult(document, path, summary)
        queued, sent, failed = [], [], []
        for name in pub.channels:
            qid = self._outbox.enqueue(name, document, path)
            queued.append(name)
            if self._try_send(qid, name, path, summary, subject):
                sent.append(name)
            else:
                failed.append(name)
        return PublishResult(document, path, summary, queued, sent, failed)

    def _try_send(self, qid: int, channel_name: str, path: Path, summary: list[str], subject: str) -> bool:
        try:
            channel = self._channel_factory(channel_name, self._settings(), self)
            channel.send(path, summary=summary, subject=subject)
        except PublishError as exc:
            self._outbox.mark_failed(qid, f"{exc.code}: {exc.detail}")
            if exc.code == "E-PUB-001":
                self._notifications.notify("WARN", "PUBLISH_CONFIG", f"Report channel '{channel_name}' is not set up correctly", body=exc.detail, deep_link="/settings/publishing", source_ref=f"publish:{channel_name}")
            return False
        except Exception as exc:  # noqa: BLE001 - a channel bug must never break the day-close
            log.exception("PUBLISH_CHANNEL_CRASH channel=%s", channel_name)
            self._outbox.mark_failed(qid, f"{type(exc).__name__}: {exc}")
            return False
        self._outbox.mark_sent(qid)
        return True

    def flush(self) -> tuple[int, int]:
        """Retry due deliveries; returns ``(sent, failed)``."""
        sent = failed = 0
        for item in self._outbox.due():
            path = Path(item.file_path)
            if not path.exists():
                self._outbox.mark_failed(item.id, "document file missing")
                failed += 1
                continue
            if self._try_send(item.id, item.channel, path, [f"{item.document} {path.stem}"], f"{item.document} {path.stem}"):
                sent += 1
            else:
                failed += 1
        if sent or failed:
            log.info("PUBLISH_FLUSH sent=%d failed=%d", sent, failed)
        counts = self._outbox.counts()
        if counts["FAILED"]:
            self._notifications.notify("WARN", "PUBLISH_FAILED", f"{counts['FAILED']} report delivery(ies) gave up after repeated failures", deep_link="/settings/publishing", source_ref=f"publish_failed:{counts['FAILED']}")
        return sent, failed

    def chip(self) -> tuple[str, str]:
        """Status chip: grey off · green all sent · amber pending · red failed."""
        pub = self._settings().publishing
        if not pub.enabled or not pub.channels:
            return ("grey", "Publish: off")
        counts = self._outbox.counts()
        if counts["FAILED"]:
            return ("red", f"Publish: {counts['FAILED']} failed")
        if counts["PENDING"]:
            return ("amber", f"Publish: {counts['PENDING']} pending")
        last = self._outbox.last_sent_at()
        return ("green", f"Publish: sent {format_ist(parse_iso_utc(last), '%H:%M')}" if last else "Publish: ready")

    def test_channel(self, channel_name: str) -> PublishResult:
        """Send a tiny test document through one channel (Settings → Publishing → Test)."""
        html = self._renderer.render_table("Test message", f"{self._settings().shop.name} · {format_ist(now_utc())}", ["Check", "Result"], [["Channel", channel_name], ["Status", "working"]])
        path = self._renderer.write_document(html, self._reports_dir(today_ist()) / f"test-{channel_name}-{now_utc().strftime('%H%M%S')}", pdf_engine=self._pdf_engine)
        try:
            channel = self._channel_factory(channel_name, self._settings(), self)
            channel.send(path, summary=["Test message from Vaishnavi Store Manager"], subject="Test message")
        except VaishnaviError as exc:
            return PublishResult("test", path, [f"{exc.code}: {exc.user_message()} {exc.detail}".strip()], [channel_name], [], [channel_name])
        return PublishResult("test", path, ["ok"], [channel_name], [channel_name], [])


def _q(value: Any) -> str:
    if value is None:
        return ""
    from decimal import Decimal  # noqa: PLC0415

    text = format(Decimal(value).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
