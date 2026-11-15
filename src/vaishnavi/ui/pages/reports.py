"""``/reports`` — the Owner's report centre (docs/05 §9, docs/09 §6).

One generic table page per report: the service returns dataclass rows, this module turns them into
labelled columns (money as ₹, user ids as names) and the same rows export to CSV. Staff reach only
*Staff work history* for themselves. Phase 5 adds the sales / profit / forecast reports to the same
registry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Grant, Permission, Role, grant_for
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.reports")


@dataclass(frozen=True)
class ReportSpec:
    """Registry entry."""

    key: str
    permission: Permission
    columns: tuple[str, ...]
    rows: Callable[[Any, Actor], list[dict[str, Any]]]
    raw: Callable[[Any, Actor], list[Any]]


@dataclass(frozen=True)
class ReportLink:
    """Index card."""

    key: str
    title: str
    hint: str
    path: str


@dataclass(frozen=True)
class IndexModel:
    """``/reports``."""

    reports: list[ReportLink]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TableModel:
    """One report."""

    key: str
    title: str
    columns: list[str]
    column_labels: dict[str, str]
    rows: list[dict[str, Any]]
    labels: dict[str, str] = field(default_factory=dict)


def _names(c: Any) -> dict[int, str]:
    return c.users.display_names()


def _who(names: dict[int, str], uid: int | None) -> str:
    return "" if uid is None else names.get(uid, f"#{uid}")


def _when(text: str) -> str:
    try:
        return format_ist(parse_iso_utc(text))
    except (ValueError, TypeError):
        return text


# ----------------------------------------------------------------------------- row builders
def _discounts(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"cashier": _who(names, r.user_id), "lines": r.lines, "bills": r.bills, "adhoc": format_inr(r.adhoc_paise), "reasons": "; ".join(f"{k} ×{v}" for k, v in r.reasons.items())} for r in c.reports.discount_audit(actor)]


def _manual(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"cashier": _who(names, r.user_id), "total_lines": r.total_lines, "lookup": r.lookup_lines, "open_items": r.open_item_lines, "open_item_value": format_inr(r.open_item_paise), "ratio": f"{r.manual_ratio:.0%}"} for r in c.reports.manual_lines(actor)]


def _voids(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"when": _when(r.at), "credit_note": r.credit_note, "original": r.original_invoice, "kind": r.status, "by": _who(names, r.user_id), "approved_by": _who(names, r.approved_by), "amount": format_inr(r.total_paise), "reason": r.reason} for r in c.reports.voids(actor)]


def _overrides(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"when": _when(r.at), "action": r.action, "by": _who(names, r.user_id), "owner": _who(names, r.actor_user_id), "entity": f"{r.entity or ''} {r.entity_id or ''}".strip(), "detail": r.detail} for r in c.reports.overrides(actor)]


def _shrinkage(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product": f"#{r.product_id} {r.name}", "damage": str(r.damage_qty), "adjustments": str(r.adjustment_qty), "stock_take": str(r.stock_take_qty), "value": format_inr(r.value_paise)} for r in c.reports.shrinkage(actor)]


def _cashiers(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"cashier": _who(names, r.user_id), "bills": r.bills, "sales": format_inr(r.sales_paise), "avg_bill": format_inr(r.avg_bill_paise), "discounts": format_inr(r.discount_paise), "voids": r.voids, "manual_lines": r.manual_lines, "shifts": r.shifts_closed, "variance": format_inr(r.variance_paise)} for r in c.reports.cashier_performance(actor)]


def _tax(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"rate": f"{Decimal(r.rate_bp) / 100:g}%", "taxable": format_inr(r.taxable_paise), "cgst": format_inr(r.cgst_paise), "sgst": format_inr(r.sgst_paise), "bills": r.bills} for r in c.reports.tax_summary(actor)]


def _variance(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"closed": _when(s.closed_at or ""), "cashier": _who(names, s.user_id), "expected": format_inr(s.expected_cash_paise or 0), "counted": format_inr(s.counted_cash_paise or 0), "variance": format_inr(s.variance_paise or 0), "note": s.note or ""} for s in c.shifts.variance_history(limit=200)]


def _attendance(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    rows = c.sessions.history(actor, limit=200)
    return [{"user": _who(names, s.user_id), "login": _when(s.login_at), "logout": _when(s.logout_at) if s.logout_at else "", "duration": f"{s.duration_seconds // 3600}h {(s.duration_seconds % 3600) // 60:02d}m", "ended_by": s.end_reason or "", "bills": s.bills, "sales": format_inr(s.sales_paise), "confirmed": "yes" if s.confirmed_at else ""} for s in rows]


def _integrity(c: Any, actor: Actor) -> list[dict[str, Any]]:
    view = c.reports.integrity(actor)
    rows = [{"check": r.check, "ok": "ok" if r.ok else "FAILED", "code": r.code, "detail": r.detail} for r in view.checks]
    rows.append({"check": "HEAD", "ok": "", "code": "", "detail": view.chain_head})
    return rows


def _audit(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"when": _when(e.at), "action": e.action, "user": _who(names, e.user_id), "actor": _who(names, e.actor_user_id), "entity": f"{e.entity or ''} {e.entity_id or ''}".strip(), "after": (e.after or "")[:120]} for e in c.reports.audit_trail(actor, limit=300)]


def _sales(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"day": r.day, "bills": r.bills, "items": str(r.items), "discounts": format_inr(r.discount_paise), "tax": format_inr(r.tax_paise), "total": format_inr(r.total_paise), "cash": format_inr(r.cash_paise), "upi": format_inr(r.upi_paise), "card": format_inr(r.card_paise), "refunds": format_inr(r.refunds_paise)} for r in c.reports.sales_by_day(actor)]


def _items(c: Any, actor: Actor) -> list[dict[str, Any]]:
    rows = []
    for t in c.reports.top_sellers(actor, limit=50):
        for d in c.reports.item_sales(actor, t.product_id):
            rows.append({"product": f"#{t.product_id} {t.name}", "day": d.day, "qty": str(d.qty), "amount": format_inr(d.amount_paise), "bills": d.bills})
    return rows


def _top(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product": f"#{t.product_id} {t.name}", "qty": str(t.qty), "amount": format_inr(t.amount_paise), "share": f"{t.share_pct}%"} for t in c.reports.top_sellers(actor, limit=50, by="amount")]


def _basket(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product_a": f"#{p.product_a} {p.name_a}", "product_b": f"#{p.product_b} {p.name_b}", "baskets": p.baskets, "share": f"{p.share_pct}%"} for p in c.reports.basket_pairs(actor, limit=50)]


def _chat_security(c: Any, actor: Actor) -> list[dict[str, Any]]:
    names = _names(c)
    return [{"when": _when(e.at), "user": _who(names, e.user_id), "category": e.category, "severity": e.severity, "layer": e.layer, "excerpt": e.message_excerpt or "", "alerted": "yes" if e.owner_alerted else ""} for e in c.chat.security_events(limit=300)]


def _dead(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product": f"#{d.product_id} {d.name}", "stock": str(d.stock_qty), "value": format_inr(d.value_paise), "last_sold": _when(d.last_sold_at) if d.last_sold_at else "never"} for d in c.reports.dead_stock(actor)]


def _low(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product": f"#{r.product_id} {r.name}", "stock": str(r.stock_qty), "minimum": str(r.min_stock), "reorder": str(r.reorder_qty)} for r in c.reports.low_stock(actor)]


def _purchases(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"supplier": p.supplier, "purchases": p.purchases, "total": format_inr(p.total_paise)} for p in c.reports.purchases(actor)]


def _profit(c: Any, actor: Actor) -> list[dict[str, Any]]:
    report = c.reports.profit(actor)
    rows = [{"product": f"#{r.product_id} {r.name}", "qty": str(r.qty), "revenue": format_inr(r.revenue_paise), "cost": format_inr(r.cost_paise), "profit": format_inr(r.profit_paise), "margin": f"{r.margin_pct}%"} for r in report.rows]
    rows.append({"product": "TOTAL", "qty": "", "revenue": format_inr(report.revenue_paise), "cost": format_inr(report.cost_paise), "profit": format_inr(report.profit_paise), "margin": f"{int(round(report.profit_paise * 100 / report.revenue_paise)) if report.revenue_paise else 0}% · {report.unknown_cost_lines} lines without cost"})
    return rows


def _heatmap(c: Any, actor: Actor) -> list[dict[str, Any]]:
    heat = c.reports.hourly_heatmap(actor)
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    rows = []
    for wd in range(7):
        row: dict[str, Any] = {"weekday": days[wd]}
        for hour in range(6, 23):
            row[f"h{hour:02d}"] = heat.get((wd, hour), 0) or ""
        rows.append(row)
    return rows


def _fmt_dec(value: Any) -> str:
    return "" if value is None else str(value)


def _forecast(c: Any, actor: Actor) -> list[dict[str, Any]]:
    return [{"product": f"#{r.product_id} {r.name}", "stock": str(r.stock), "rate": _fmt_dec(r.daily_rate), "cover": _fmt_dec(r.days_of_cover), "stockout": r.stockout_date or "", "reorder": str(r.reorder_qty), "confidence": r.confidence} for r in c.forecast.plan(actor)]


def _demand(c: Any, actor: Actor) -> list[dict[str, Any]]:
    rows = c.forecast.plan(actor)
    order = {"FAST": 0, "STEADY": 1, "SLOW": 2, "NEW": 3, "DEAD": 4}
    rows.sort(key=lambda r: (order.get(r.classification, 9), -(r.daily_rate or 0), r.name))
    return [{"product": f"#{r.product_id} {r.name}", "class": r.classification, "rate": _fmt_dec(r.daily_rate), "stock": str(r.stock), "cover": _fmt_dec(r.days_of_cover), "confidence": r.confidence} for r in rows]


REPORTS: Final[tuple[ReportSpec, ...]] = (
    ReportSpec("sales", Permission.REPORT_SALES, ("day", "bills", "items", "discounts", "tax", "total", "cash", "upi", "card", "refunds"), _sales, lambda c, a: c.reports.sales_by_day(a)),
    ReportSpec("items", Permission.REPORT_SALES, ("product", "day", "qty", "amount", "bills"), _items, lambda c, a: [d for t in c.reports.top_sellers(a, limit=50) for d in c.reports.item_sales(a, t.product_id)]),
    ReportSpec("top", Permission.REPORT_SALES, ("product", "qty", "amount", "share"), _top, lambda c, a: c.reports.top_sellers(a, limit=50, by="amount")),
    ReportSpec("basket", Permission.REPORT_SALES, ("product_a", "product_b", "baskets", "share"), _basket, lambda c, a: c.reports.basket_pairs(a, limit=50)),
    ReportSpec("dead", Permission.REPORT_SALES, ("product", "stock", "value", "last_sold"), _dead, lambda c, a: c.reports.dead_stock(a)),
    ReportSpec("low_stock", Permission.VIEW_STOCK_LEVEL, ("product", "stock", "minimum", "reorder"), _low, lambda c, a: c.reports.low_stock(a)),
    ReportSpec("purchases", Permission.REPORT_PURCHASES, ("supplier", "purchases", "total"), _purchases, lambda c, a: c.reports.purchases(a)),
    ReportSpec("profit", Permission.REPORT_PROFIT, ("product", "qty", "revenue", "cost", "profit", "margin"), _profit, lambda c, a: c.reports.profit(a).rows),
    ReportSpec("heatmap", Permission.REPORT_SALES, ("weekday", *[f"h{h:02d}" for h in range(6, 23)]), _heatmap, lambda c, a: [{"weekday": k[0], "hour": k[1], "bills": v} for k, v in sorted(c.reports.hourly_heatmap(a).items())]),
    ReportSpec("forecast", Permission.REPORT_FORECAST, ("product", "stock", "rate", "cover", "stockout", "reorder", "confidence"), _forecast, lambda c, a: c.forecast.plan(a)),
    ReportSpec("demand", Permission.REPORT_FORECAST, ("product", "class", "rate", "stock", "cover", "confidence"), _demand, lambda c, a: c.forecast.plan(a)),
    ReportSpec("discounts", Permission.REPORT_AUDIT, ("cashier", "lines", "bills", "adhoc", "reasons"), _discounts, lambda c, a: c.reports.discount_audit(a)),
    ReportSpec("manual_lines", Permission.REPORT_AUDIT, ("cashier", "total_lines", "lookup", "open_items", "open_item_value", "ratio"), _manual, lambda c, a: c.reports.manual_lines(a)),
    ReportSpec("voids", Permission.REPORT_AUDIT, ("when", "credit_note", "original", "kind", "by", "approved_by", "amount", "reason"), _voids, lambda c, a: c.reports.voids(a)),
    ReportSpec("overrides", Permission.REPORT_AUDIT, ("when", "action", "by", "owner", "entity", "detail"), _overrides, lambda c, a: c.reports.overrides(a)),
    ReportSpec("shrinkage", Permission.REPORT_AUDIT, ("product", "damage", "adjustments", "stock_take", "value"), _shrinkage, lambda c, a: c.reports.shrinkage(a)),
    ReportSpec("cashiers", Permission.REPORT_AUDIT, ("cashier", "bills", "sales", "avg_bill", "discounts", "voids", "manual_lines", "shifts", "variance"), _cashiers, lambda c, a: c.reports.cashier_performance(a)),
    ReportSpec("variance", Permission.REPORT_AUDIT, ("closed", "cashier", "expected", "counted", "variance", "note"), _variance, lambda c, a: c.shifts.variance_history(limit=200)),
    ReportSpec("tax", Permission.REPORT_SALES, ("rate", "taxable", "cgst", "sgst", "bills"), _tax, lambda c, a: c.reports.tax_summary(a)),
    ReportSpec("attendance", Permission.REPORT_OWN_SHIFT, ("user", "login", "logout", "duration", "ended_by", "bills", "sales", "confirmed"), _attendance, lambda c, a: c.sessions.history(a, limit=200)),
    ReportSpec("integrity", Permission.REPORT_AUDIT, ("check", "ok", "code", "detail"), _integrity, lambda c, a: c.reports.integrity(a).checks),
    ReportSpec("audit", Permission.REPORT_AUDIT, ("when", "action", "user", "actor", "entity", "after"), _audit, lambda c, a: c.reports.audit_trail(a, limit=300)),
    ReportSpec("chat_security", Permission.REPORT_AUDIT, ("when", "user", "category", "severity", "layer", "excerpt", "alerted"), _chat_security, lambda c, a: c.chat.security_events(limit=300)),
)


def _allowed(spec: ReportSpec, actor: Actor) -> bool:
    return grant_for(spec.permission, actor.role) is not Grant.DENY


def build_index(c: Any, texts: UiTexts, actor: Actor) -> IndexModel:
    """Cards the role may open."""
    links = [ReportLink(s.key, texts.t(f"reports.{s.key}.title"), texts.t(f"reports.{s.key}.hint"), f"/reports/{s.key}") for s in REPORTS if _allowed(s, actor)]
    return IndexModel(links, {k: texts.t(f"reports.{k}") for k in ("title", "export", "export_all", "back")})


def build_table(c: Any, texts: UiTexts, actor: Actor, key: str) -> TableModel | None:
    """Rows for one report (None when unknown or not permitted)."""
    spec = next((s for s in REPORTS if s.key == key), None)
    if spec is None or not _allowed(spec, actor):
        return None
    rows = spec.rows(c, actor)
    labels = {col: (texts.t(f"reports.col.{col}") if texts.has(f"reports.col.{col}") else col.replace("_", " ")) for col in spec.columns}
    log.debug("REPORT_BUILT key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    return TableModel(key, texts.t(f"reports.{key}.title"), list(spec.columns), labels, rows, {k: texts.t(f"reports.{k}") for k in ("title", "export", "back")})


def export_csv(c: Any, actor: Actor, key: str) -> str:
    """Raw dataclass rows as CSV (BOM) — empty string when not permitted."""
    spec = next((s for s in REPORTS if s.key == key), None)
    if spec is None or not _allowed(spec, actor):
        return ""
    rows = spec.raw(c, actor)
    text = c.reports.to_csv(rows)
    log.info("REPORT_EXPORTED key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    return text


def export_xlsx(c: Any, texts: UiTexts, actor: Actor, key: str) -> bytes | None:
    """The displayed table as a one-sheet workbook (None when not permitted)."""
    from vaishnavi.services.export import to_xlsx  # noqa: PLC0415

    m = build_table(c, texts, actor, key)
    if m is None:
        return None
    rows = [[r.get(col, "") for col in m.columns] for r in m.rows]
    log.info("REPORT_EXPORTED_XLSX key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    return to_xlsx(m.title, [m.column_labels[col] for col in m.columns], rows)


def _document_rows(m: TableModel) -> tuple[list[str], list[list[Any]]]:
    return [m.column_labels[col] for col in m.columns], [[r.get(col, "") for col in m.columns] for r in m.rows]


def export_document(c: Any, texts: UiTexts, actor: Actor, key: str) -> Path | None:
    """Write the table as a PDF (HTML when no engine) under the reports folder — Export PDF."""
    m = build_table(c, texts, actor, key)
    if m is None or c.publisher is None:
        return None
    headers, rows = _document_rows(m)
    path = c.publisher.render_document(m.title, f"{format_ist(now_utc(), '%d-%b-%Y %H:%M')} · {len(rows)} rows", headers, rows, stem=f"report-{key}-{now_utc().strftime('%Y%m%d-%H%M%S')}")
    log.info("REPORT_DOCUMENT key=%s path=%s user=%s", key, path.name, actor.user_id)
    return path


def send_document(c: Any, texts: UiTexts, actor: Actor, key: str) -> Any | None:
    """Export PDF → Send through the configured publishing channels (Owner)."""
    m = build_table(c, texts, actor, key)
    if m is None or actor.role is not Role.OWNER or c.publisher is None:
        return None
    headers, rows = _document_rows(m)
    return c.publisher.publish_table(m.title, f"{format_ist(now_utc(), '%d-%b-%Y %H:%M')} · {len(rows)} rows", headers, rows, stem=f"report-{key}-{now_utc().strftime('%Y%m%d-%H%M%S')}")


def export_all_zip(c: Any, actor: Actor) -> bytes | None:
    """Every report the Owner may open, one CSV each, in a ZIP (Excel-friendly) — None for staff."""
    if actor.role is not Role.OWNER:
        log.warning("REPORT_EXPORT_ALL_DENIED user=%s role=%s", actor.user_id, actor.role.value)
        return None
    import io  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from vaishnavi.core.clock import iso_utc  # noqa: PLC0415

    buf = io.BytesIO()
    written: list[str] = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for spec in REPORTS:
            if not _allowed(spec, actor):
                continue
            try:
                rows = spec.raw(c, actor)
            except Exception as exc:  # noqa: BLE001 - one broken report must not block the whole export
                log.error("REPORT_EXPORT_ALL_FAILED key=%s error=%s", spec.key, exc)
                zf.writestr(f"{spec.key}.ERROR.txt", f"{type(exc).__name__}: {exc}")
                continue
            zf.writestr(f"{spec.key}.csv", c.reports.to_csv(rows))
            written.append(f"{spec.key}.csv ({len(rows)} rows)")
        zf.writestr("README.txt", f"Vaishnavi Store Manager — full export at {iso_utc()} UTC by user #{actor.user_id}\nOne CSV per report (UTF-8 with BOM; open in Excel).\n\n" + "\n".join(written) + "\n")
    log.info("REPORT_EXPORT_ALL files=%d user=%s bytes=%d", len(written), actor.user_id, buf.tell())
    return buf.getvalue()


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/reports`` and ``/reports/<key>``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/reports")
    def _index() -> None:
        client = load_client(app)
        decision = guards.decide("/reports", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/reports"))
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/reports"):
            m = build_index(services, texts, client.actor())
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(m.labels["title"]).classes("text-xl font-semibold")
                if client.role is Role.OWNER:

                    def export_all() -> None:
                        blob = export_all_zip(services, client.actor())
                        if blob:
                            ui.download(blob, f"vaishnavi-reports-{format_ist(now_utc(), '%Y%m%d-%H%M')}.zip")

                    ui.button(m.labels["export_all"], icon="archive", on_click=export_all).props("flat")
            with ui.grid(columns=3).classes("w-full gap-3"):
                for link in m.reports:
                    with ui.column().classes("vsm-card p-3 cursor-pointer").on("click", lambda _e, p=link.path: ui.navigate.to(p)):
                        ui.label(link.title).classes("font-semibold")
                        ui.label(link.hint).classes("text-xs text-gray-500")

    @ui.page("/reports/{key}")
    def _table(key: str) -> None:
        client = load_client(app)
        decision = guards.decide(f"/reports/{key}", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/reports"))
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/reports"):
            m = build_table(services, texts, actor, key)
            if m is None:
                ui.label("—")
                return
            with ui.row().classes("items-center gap-2 w-full"):
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/reports")).props("flat")
                ui.label(m.title).classes("text-xl font-semibold")
                ui.element("div").classes("flex-1")
                ui.button(m.labels["export"], icon="download", on_click=lambda: ui.download(export_csv(services, actor, key).encode("utf-8"), f"{key}.csv")).props("outline")
                ui.button("XLSX", icon="table_view", on_click=lambda: ui.download(export_xlsx(services, texts, actor, key) or b"", f"{key}.xlsx")).props("outline")
                if client.role is Role.OWNER:

                    def export_pdf() -> None:
                        path = export_document(services, texts, actor, key)
                        if path is not None:
                            ui.download(path.read_bytes(), path.name)

                    def send() -> None:
                        result = send_document(services, texts, actor, key)
                        if result is None:
                            ui.notify("—", type="warning")
                        elif result.sent:
                            ui.notify(f"sent: {', '.join(result.sent)}", type="positive")
                        else:
                            ui.notify(texts.t("publishing.not_sent") if texts.has("publishing.not_sent") else "not sent — check Settings → Publishing", type="warning")

                    ui.button("PDF", icon="picture_as_pdf", on_click=export_pdf).props("outline")
                    ui.button(texts.t("publishing.send") if texts.has("publishing.send") else "Send", icon="send", on_click=send).props("outline")
            if client.role is Role.OWNER and key == "integrity":
                ui.button("Run checks now", icon="verified", on_click=lambda: ui.navigate.reload()).props("flat")
            columns = [{"name": col, "label": m.column_labels[col], "field": col, "sortable": True} for col in m.columns]
            ui.table(columns=columns, rows=m.rows, pagination=25).classes("w-full text-sm")


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
