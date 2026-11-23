"""``/reports`` — the Owner's report centre (docs/05 §9, docs/09 §6).

One generic table page per report: the service returns dataclass rows, this module turns them into
labelled columns (money as ₹, user ids as names) and the same rows export to CSV. Staff reach only
*Staff work history* for themselves. Phase 5 adds the sales / profit / forecast reports to the same
registry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc, today_ist
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
    rows: Callable[..., list[dict[str, Any]]]
    raw: Callable[..., list[Any]]
    params: bool = False
    """True → ``rows``/``raw`` take a third argument: the page's query parameters (day, to, cashier)."""


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


def month_window(params: Mapping[str, str] | None) -> tuple[str, str, str]:
    """``(first_day, last_day, label)`` of the month in the query string (``month=YYYY-MM``); default = this month."""
    text = ((params or {}).get("month") or "").strip()
    try:
        first = date.fromisoformat(f"{text}-01") if text else today_ist().replace(day=1)
    except ValueError:
        first = today_ist().replace(day=1)
    nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat(), first.strftime("%b-%Y")


def date_window(params: Mapping[str, str] | None) -> str:
    """The shop day picked on the calendar (``day=YYYY-MM-DD``); default today."""
    text = ((params or {}).get("day") or "").strip()
    try:
        return date.fromisoformat(text).isoformat() if text else today_ist().isoformat()
    except ValueError:
        return today_ist().isoformat()


def _sales_raw(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[Any]:
    first, last, _label = month_window(params)
    return c.reports.sales_by_day(actor, since=first, until=last)


def _sales(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Each shop day of the chosen month, then the month's total row."""
    days = _sales_raw(c, actor, params)
    rows = [{"day": r.day, "bills": r.bills, "items": str(r.items), "discounts": format_inr(r.discount_paise), "tax": format_inr(r.tax_paise), "total": format_inr(r.total_paise), "cash": format_inr(r.cash_paise), "upi": format_inr(r.upi_paise), "card": format_inr(r.card_paise), "refunds": format_inr(r.refunds_paise)} for r in days]
    if days:
        tot = lambda f: sum(getattr(r, f) for r in days)  # noqa: E731
        rows.append({"day": f"TOTAL · {month_window(params)[2]}", "bills": tot("bills"), "items": str(sum((r.items for r in days), Decimal(0))), "discounts": format_inr(tot("discount_paise")), "tax": format_inr(tot("tax_paise")), "total": format_inr(tot("total_paise")), "cash": format_inr(tot("cash_paise")), "upi": format_inr(tot("upi_paise")), "card": format_inr(tot("card_paise")), "refunds": format_inr(tot("refunds_paise"))})
    return rows


def _sales_date_raw(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[Any]:
    return c.reports.day_sales(actor, date_window(params)).items


def _sales_date(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Sales by date: item · quantity (in its unit) · bills · amount, then the day's money — total, cash, UPI, card, refunds."""
    report = c.reports.day_sales(actor, date_window(params))
    rows = [{"item": r.name + (" (open item)" if r.is_open_item else ""), "qty": r.qty_text, "bills": r.bills, "amount": format_inr(r.amount_paise)} for r in report.items]
    s = report.summary
    rows.append({"item": "TOTAL SALE", "qty": "", "bills": s.bills, "amount": format_inr(s.total_paise)})
    for mode, label in (("CASH", "Cash"), ("UPI", "UPI"), ("CARD", "Card")):
        if s.by_mode.get(mode):
            rows.append({"item": label, "qty": "", "bills": "", "amount": format_inr(s.by_mode[mode])})
    if s.refunds_paise:
        rows.append({"item": "Refunds", "qty": "", "bills": "", "amount": f"− {format_inr(s.refunds_paise)}"})
        rows.append({"item": "NET", "qty": "", "bills": "", "amount": format_inr(s.net_paise)})
    return rows


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


def _investment(c: Any, actor: Actor) -> list[dict[str, Any]]:
    """Per item since day one, then one subtotal per category and the grand total."""
    report = c.reports.investment(actor)
    rows = [{"product": f"#{r.product_id} {r.name}", "category": r.category or "—", "qty_in": str(r.qty_in), "opening": format_inr(r.opening_paise), "purchases": format_inr(r.purchases_paise), "corrections": format_inr(r.corrections_paise), "invested": format_inr(r.invested_paise), "stock": str(r.stock_qty), "stock_value": format_inr(r.stock_value_paise), "since": _when(r.first_at)[:11], "note": "valued at selling price (no purchase price set)" if r.estimated else ""} for r in report.rows]
    for cat in report.categories:
        rows.append({"product": f"SUBTOTAL · {cat.category}", "category": cat.category, "qty_in": str(cat.qty_in), "opening": "", "purchases": "", "corrections": "", "invested": format_inr(cat.invested_paise), "stock": "", "stock_value": format_inr(cat.stock_value_paise), "since": "", "note": f"{cat.items} item(s)"})
    rows.append({"product": "TOTAL", "category": "", "qty_in": "", "opening": format_inr(report.opening_paise), "purchases": format_inr(report.purchases_paise), "corrections": format_inr(report.corrections_paise), "invested": format_inr(report.invested_paise), "stock": "", "stock_value": format_inr(report.stock_value_paise), "since": _when(report.since)[:11] if report.since else "", "note": f"{report.items} item(s)" + (f" · {report.estimated_items} without purchase price" if report.estimated_items else "")})
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


# ----------------------------------------------------------------------------- cashier day book
def day_book_window(params: Mapping[str, str] | None) -> tuple[str, str, int | None]:
    """``(from_day, to_day, cashier_id)`` from the page's query string; defaults to today, everyone."""
    p = params or {}
    day = (p.get("day") or "").strip() or today_ist().isoformat()
    to = (p.get("to") or "").strip() or day
    for text in (day, to):
        try:
            date.fromisoformat(text)
        except ValueError:
            day = to = today_ist().isoformat()
            break
    cashier = (p.get("cashier") or "").strip()
    return day, to, int(cashier) if cashier.isdigit() else None


def _items_text(items: Any) -> str:
    parts = []
    for i in items:
        tag = " (open item)" if i.is_open_item else (" (lookup)" if i.entry_method == "LOOKUP" else "")
        disc = f" −{format_inr(i.discount_paise)}" if i.discount_paise else ""
        parts.append(f"{i.name}{tag} ×{i.qty} {i.unit} @ {format_inr(i.rate_paise)}{disc} = {format_inr(i.amount_paise)}")
    return "; ".join(parts)


def _day_book_raw(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[Any]:
    day, to, cashier = day_book_window(params)
    return c.cashier_records.day_book(actor, since_day=day, until_day=to, user_id=cashier)


def _day_book(c: Any, actor: Actor, params: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every bill of the window with its items, then one subtotal per cashier and the grand total."""
    records = _day_book_raw(c, actor, params)
    rows = [{"day": r.business_day, "time": _when(r.sold_at)[12:], "invoice": r.invoice_no, "cashier": r.cashier_name, "items_detail": _items_text(r.items), "lines": r.line_count, "qty": str(r.item_qty), "total": format_inr(r.total_paise), "mode": r.payment_modes or "—", "status": r.status, "recorded": _when(r.created_at)} for r in records]
    day, to, cashier = day_book_window(params)
    for s in c.cashier_records.day_summary(actor, since_day=day, until_day=to) if records else []:
        if cashier is not None and s.user_id != cashier:
            continue
        rows.append({"day": "", "time": "", "invoice": f"SUBTOTAL · {s.cashier_name}", "cashier": s.cashier_name, "items_detail": f"{s.bills} bill(s), {s.credit_notes} credit note(s), {s.item_qty} items · first {_when(s.first_sale_at or '')[12:]} · last {_when(s.last_sale_at or '')[12:]}", "lines": "", "qty": str(s.item_qty), "total": format_inr(s.net_paise), "mode": "", "status": f"sales {format_inr(s.sales_paise)} − refunds {format_inr(s.refunds_paise)}", "recorded": ""})
    if records:
        sales = sum(r.total_paise for r in records if r.status == "COMPLETED")
        refunds = sum(r.total_paise for r in records if r.status != "COMPLETED")
        rows.append({"day": "", "time": "", "invoice": "TOTAL", "cashier": "", "items_detail": f"{sum(1 for r in records if r.status == 'COMPLETED')} bill(s), {sum(1 for r in records if r.status != 'COMPLETED')} credit note(s)", "lines": sum(r.line_count for r in records), "qty": str(sum((r.item_qty for r in records), Decimal(0))), "total": format_inr(sales - refunds), "mode": "", "status": f"sales {format_inr(sales)} − refunds {format_inr(refunds)}", "recorded": ""})
    return rows


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of an Owner-approved early deletion."""

    ok: bool
    deleted: int = 0
    message: str = ""


def purge_day_book(c: Any, actor: Actor, *, before_day: str, reason: str, password: str) -> PurgeResult:
    """Delete day-book rows before ``before_day`` after the Owner re-enters the password (the "admin approval")."""
    if actor.role is not Role.OWNER:
        return PurgeResult(False, message="owner only")
    if not c.users.reauthenticate(actor.user_id, password or ""):
        log.warning("DAY_BOOK_PURGE_REAUTH_FAILED user=%s", actor.user_id)
        return PurgeResult(False, message="password did not match")
    try:
        gone = c.cashier_records.purge(actor, before_day=before_day, reason=reason)
    except ValueError as exc:
        return PurgeResult(False, message=str(exc))
    return PurgeResult(True, gone, f"{gone} record(s) deleted")


REPORTS: Final[tuple[ReportSpec, ...]] = (
    ReportSpec("day_book", Permission.REPORT_OWN_SHIFT, ("day", "time", "invoice", "cashier", "items_detail", "lines", "qty", "total", "mode", "status", "recorded"), _day_book, _day_book_raw, params=True),
    ReportSpec("sales_date", Permission.REPORT_SALES, ("item", "qty", "bills", "amount"), _sales_date, _sales_date_raw, params=True),
    ReportSpec("sales", Permission.REPORT_SALES, ("day", "bills", "items", "discounts", "tax", "total", "cash", "upi", "card", "refunds"), _sales, _sales_raw, params=True),
    ReportSpec("items", Permission.REPORT_SALES, ("product", "day", "qty", "amount", "bills"), _items, lambda c, a: [d for t in c.reports.top_sellers(a, limit=50) for d in c.reports.item_sales(a, t.product_id)]),
    ReportSpec("top", Permission.REPORT_SALES, ("product", "qty", "amount", "share"), _top, lambda c, a: c.reports.top_sellers(a, limit=50, by="amount")),
    ReportSpec("basket", Permission.REPORT_SALES, ("product_a", "product_b", "baskets", "share"), _basket, lambda c, a: c.reports.basket_pairs(a, limit=50)),
    ReportSpec("dead", Permission.REPORT_SALES, ("product", "stock", "value", "last_sold"), _dead, lambda c, a: c.reports.dead_stock(a)),
    ReportSpec("low_stock", Permission.VIEW_STOCK_LEVEL, ("product", "stock", "minimum", "reorder"), _low, lambda c, a: c.reports.low_stock(a)),
    ReportSpec("purchases", Permission.REPORT_PURCHASES, ("supplier", "purchases", "total"), _purchases, lambda c, a: c.reports.purchases(a)),
    ReportSpec("profit", Permission.REPORT_PROFIT, ("product", "qty", "revenue", "cost", "profit", "margin"), _profit, lambda c, a: c.reports.profit(a).rows),
    ReportSpec("investment", Permission.REPORT_PURCHASES, ("product", "category", "qty_in", "opening", "purchases", "corrections", "invested", "stock", "stock_value", "since", "note"), _investment, lambda c, a: c.reports.investment(a).rows),
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


Params = Mapping[str, str] | None
"""Page query parameters for the reports that take them (``day_book``: day · to · cashier)."""


def _call_rows(spec: ReportSpec, c: Any, actor: Actor, params: Params) -> list[dict[str, Any]]:
    return spec.rows(c, actor, params) if spec.params else spec.rows(c, actor)


def _call_raw(spec: ReportSpec, c: Any, actor: Actor, params: Params) -> list[Any]:
    return spec.raw(c, actor, params) if spec.params else spec.raw(c, actor)


def _window_text(spec: ReportSpec, params: Params) -> str:
    """Subtitle fragment naming the window of a parametrised report (so the PDF says which day / month it covers)."""
    if not spec.params:
        return ""
    if spec.key == "sales":
        return month_window(params)[2]
    if spec.key == "sales_date":
        return date.fromisoformat(date_window(params)).strftime("%d-%b-%Y")
    day, to, _cashier = day_book_window(params)
    return day if day == to else f"{day} to {to}"


def build_table(c: Any, texts: UiTexts, actor: Actor, key: str, params: Params = None) -> TableModel | None:
    """Rows for one report (None when unknown or not permitted)."""
    spec = next((s for s in REPORTS if s.key == key), None)
    if spec is None or not _allowed(spec, actor):
        return None
    rows = _call_rows(spec, c, actor, params)
    labels = {col: (texts.t(f"reports.col.{col}") if texts.has(f"reports.col.{col}") else col.replace("_", " ")) for col in spec.columns}
    log.debug("REPORT_BUILT key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    title = texts.t(f"reports.{key}.title")
    window = _window_text(spec, params)
    return TableModel(key, f"{title} — {window}" if window else title, list(spec.columns), labels, rows, {k: texts.t(f"reports.{k}") for k in ("title", "export", "back")})


def _prepared_by(c: Any, actor: Actor) -> str:
    try:
        return c.users.display_names().get(actor.user_id, f"#{actor.user_id}")
    except Exception:  # noqa: BLE001 - a missing name must not block an export
        return f"#{actor.user_id}"


def _letterhead_lines(c: Any, actor: Actor, title: str, subtitle: str) -> list[str]:
    """Shop letterhead as text rows for CSV/XLSX; empty when publishing is not wired (tests of the spine)."""
    if c.publisher is None:
        return []
    lines: list[str] = c.publisher.letterhead(title, subtitle, prepared_by=_prepared_by(c, actor)).lines()
    return lines


def _subtitle(rows: int) -> str:
    """Row count only — the letterhead already carries the date and time the document was produced."""
    return f"{rows} row{'s' if rows != 1 else ''}"


def export_csv(c: Any, actor: Actor, key: str, *, texts: UiTexts | None = None, params: Params = None) -> str:
    """Raw dataclass rows as CSV (BOM) under the shop letterhead — empty string when not permitted."""
    spec = next((s for s in REPORTS if s.key == key), None)
    if spec is None or not _allowed(spec, actor):
        return ""
    rows = _call_raw(spec, c, actor, params)
    title = texts.t(f"reports.{key}.title") if texts is not None else key
    window = _window_text(spec, params)
    text = c.reports.to_csv(rows, letterhead=_letterhead_lines(c, actor, f"{title} — {window}" if window else title, _subtitle(len(rows))))
    log.info("REPORT_EXPORTED key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    return text


def export_xlsx(c: Any, texts: UiTexts, actor: Actor, key: str, params: Params = None) -> bytes | None:
    """The displayed table as a one-sheet workbook with the letterhead above the header (None when not permitted)."""
    from vaishnavi.services.export import to_xlsx  # noqa: PLC0415

    m = build_table(c, texts, actor, key, params)
    if m is None:
        return None
    rows = [[r.get(col, "") for col in m.columns] for r in m.rows]
    log.info("REPORT_EXPORTED_XLSX key=%s rows=%d user=%s", key, len(rows), actor.user_id)
    return to_xlsx(m.title, [m.column_labels[col] for col in m.columns], rows, title_rows=_letterhead_lines(c, actor, m.title, _subtitle(len(rows))))


def _document_rows(m: TableModel) -> tuple[list[str], list[list[Any]]]:
    return [m.column_labels[col] for col in m.columns], [[r.get(col, "") for col in m.columns] for r in m.rows]


def export_document(c: Any, texts: UiTexts, actor: Actor, key: str, params: Params = None) -> Path | None:
    """Write the table as a PDF (HTML when no engine) on the shop letterhead under the reports folder — Export PDF."""
    m = build_table(c, texts, actor, key, params)
    if m is None or c.publisher is None:
        return None
    headers, rows = _document_rows(m)
    path = c.publisher.render_document(m.title, _subtitle(len(rows)), headers, rows, stem=f"report-{key}-{now_utc().strftime('%Y%m%d-%H%M%S')}", prepared_by=_prepared_by(c, actor))
    log.info("REPORT_DOCUMENT key=%s path=%s user=%s", key, path.name, actor.user_id)
    return path


def send_document(c: Any, texts: UiTexts, actor: Actor, key: str, params: Params = None) -> Any | None:
    """Export PDF → Send through the configured publishing channels (Owner)."""
    m = build_table(c, texts, actor, key, params)
    if m is None or actor.role is not Role.OWNER or c.publisher is None:
        return None
    headers, rows = _document_rows(m)
    return c.publisher.publish_table(m.title, _subtitle(len(rows)), headers, rows, stem=f"report-{key}-{now_utc().strftime('%Y%m%d-%H%M%S')}", prepared_by=_prepared_by(c, actor))


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
                rows = _call_raw(spec, c, actor, None)
            except Exception as exc:  # noqa: BLE001 - one broken report must not block the whole export
                log.error("REPORT_EXPORT_ALL_FAILED key=%s error=%s", spec.key, exc)
                zf.writestr(f"{spec.key}.ERROR.txt", f"{type(exc).__name__}: {exc}")
                continue
            zf.writestr(f"{spec.key}.csv", c.reports.to_csv(rows, letterhead=_letterhead_lines(c, actor, spec.key.replace("_", " ").title(), _subtitle(len(rows)))))
            written.append(f"{spec.key}.csv ({len(rows)} rows)")
        head = "\n".join(_letterhead_lines(c, actor, "Full export", "")) or "Vaishnavi Store Manager"
        zf.writestr("README.txt", f"{head}\n\nFull export at {iso_utc()} UTC by user #{actor.user_id}\nOne CSV per report (UTF-8 with BOM; open in Excel).\n\n" + "\n".join(written) + "\n")
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
                            ui.download.content(blob, f"vaishnavi-reports-{format_ist(now_utc(), '%Y%m%d-%H%M')}.zip")

                    ui.button(m.labels["export_all"], icon="archive", on_click=export_all).props("flat")
            with ui.grid(columns=3).classes("w-full gap-3"):
                for link in m.reports:
                    with ui.column().classes("vsm-card p-3 cursor-pointer").on("click", lambda _e, p=link.path: ui.navigate.to(p)):
                        ui.label(link.title).classes("font-semibold")
                        ui.label(link.hint).classes("text-xs text-gray-500")

    @ui.page("/reports/{key}")
    def _table(key: str, day: str = "", to: str = "", cashier: str = "", month: str = "") -> None:
        client = load_client(app)
        decision = guards.decide(f"/reports/{key}", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/reports"))
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        params: dict[str, str] = {"day": day, "to": to, "cashier": cashier, "month": month}
        with frame(state, current_path="/reports"):
            m = build_table(services, texts, actor, key, params)
            if m is None:
                ui.label("—")
                return
            with ui.row().classes("items-center gap-2 w-full"):
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/reports")).props("flat")
                ui.label(m.title).classes("text-xl font-semibold")
                ui.element("div").classes("flex-1")
                ui.button(m.labels["export"], icon="download", on_click=lambda: ui.download.content(export_csv(services, actor, key, texts=texts, params=params).encode("utf-8"), f"{key}.csv")).props("outline")
                ui.button("XLSX", icon="table_view", on_click=lambda: ui.download.content(export_xlsx(services, texts, actor, key, params) or b"", f"{key}.xlsx")).props("outline")
                if client.role is Role.OWNER:

                    def export_pdf() -> None:
                        path = export_document(services, texts, actor, key, params)
                        if path is not None:
                            ui.download.content(path.read_bytes(), path.name)

                    def send() -> None:
                        result = send_document(services, texts, actor, key, params)
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
            if key == "day_book":
                _day_book_controls(services, texts, client, actor, params)
            elif key == "sales_date":
                _date_controls(texts, params)
            elif key == "sales":
                _month_controls(texts, params)
            columns = [{"name": col, "label": m.column_labels[col], "field": col, "sortable": True} for col in m.columns]
            ui.table(columns=columns, rows=m.rows, pagination=25).classes("w-full text-sm")

    def _date_controls(texts: UiTexts, params: dict[str, str]) -> None:
        """Sales by date: the calendar picker (any day of any year) and a Today shortcut."""
        form: dict[str, Any] = {"day": date_window(params)}
        with ui.row().classes("items-end gap-3 w-full vsm-card p-3"):
            ui.input(texts.t("day_book.pick_day"), value=form["day"]).bind_value(form, "day").props("type=date outlined dense").classes("w-48")
            ui.button(texts.t("day_book.apply"), icon="event", on_click=lambda: ui.navigate.to(f"/reports/sales_date?day={form['day']}")).props("unelevated color=primary")
            ui.button(texts.t("day_book.today"), on_click=lambda: ui.navigate.to("/reports/sales_date")).props("flat")

    def _month_controls(texts: UiTexts, params: dict[str, str]) -> None:
        """Sales by month: the month picker and a This-month shortcut."""
        first, _last, _label = month_window(params)
        form: dict[str, Any] = {"month": first[:7]}
        with ui.row().classes("items-end gap-3 w-full vsm-card p-3"):
            ui.input(texts.t("day_book.pick_month"), value=form["month"]).bind_value(form, "month").props("type=month outlined dense").classes("w-48")
            ui.button(texts.t("day_book.apply"), icon="calendar_month", on_click=lambda: ui.navigate.to(f"/reports/sales?month={form['month']}")).props("unelevated color=primary")
            ui.button(texts.t("day_book.this_month"), on_click=lambda: ui.navigate.to("/reports/sales")).props("flat")

    def _day_book_controls(services: Any, texts: UiTexts, client: Any, actor: Actor, params: dict[str, str]) -> None:
        """Day / range / cashier filter, the retention line and the Owner's "delete older records" dialog."""
        from urllib.parse import urlencode  # noqa: PLC0415

        t = lambda k: texts.t(f"day_book.{k}")  # noqa: E731
        from_day, to_day, cashier_id = day_book_window(params)
        form: dict[str, Any] = {"day": from_day, "to": to_day, "cashier": str(cashier_id) if cashier_id is not None else ""}
        with ui.row().classes("items-end gap-3 w-full vsm-card p-3"):
            ui.input(t("from"), value=form["day"]).bind_value(form, "day").props("type=date outlined dense").classes("w-44")
            ui.input(t("to"), value=form["to"]).bind_value(form, "to").props("type=date outlined dense").classes("w-44")
            if client.role is Role.OWNER:
                names = services.users.display_names()
                ui.select({"": t("all_cashiers"), **{str(uid): name for uid, name in names.items()}}, label=t("cashier"), value=form["cashier"]).bind_value(form, "cashier").props("outlined dense").classes("w-48")
            ui.button(t("apply"), icon="filter_alt", on_click=lambda: ui.navigate.to("/reports/day_book?" + urlencode({k: v for k, v in form.items() if v}))).props("unelevated color=primary")
            ui.button(t("today"), on_click=lambda: ui.navigate.to("/reports/day_book")).props("flat")
        status = services.cashier_records.status()
        with ui.row().classes("items-center gap-3 w-full"):
            kept = t("kept").format(days=status.retention_days, rows=status.rows, oldest=status.oldest_day or "—", newest=status.newest_day or "—", cutoff=status.auto_delete_before)
            ui.label(kept).classes("text-xs text-gray-600")
            if client.role is Role.OWNER:

                def open_purge() -> None:
                    pform: dict[str, Any] = {"before": status.auto_delete_before, "reason": "", "password": ""}
                    with ui.dialog() as dlg, ui.card().classes("gap-2").style("min-width: 420px"):
                        ui.label(t("purge_title")).classes("text-lg font-bold")
                        ui.label(t("purge_hint")).classes("text-sm text-gray-600")
                        ui.input(t("purge_before")).bind_value(pform, "before").props("type=date outlined dense").classes("w-full")
                        ui.input(t("purge_reason")).bind_value(pform, "reason").props("outlined dense").classes("w-full")
                        ui.input(t("purge_password"), password=True).bind_value(pform, "password").props("outlined dense").classes("w-full")

                        def confirm() -> None:
                            r = purge_day_book(services, actor, before_day=str(pform["before"]), reason=str(pform["reason"]), password=str(pform["password"]))
                            if r.ok:
                                dlg.close()
                                ui.notify(t("purge_done").format(rows=r.deleted), type="positive")
                                ui.navigate.reload()
                            else:
                                ui.notify(r.message, type="negative")

                        with ui.row().classes("justify-end w-full"):
                            ui.button(texts.t("common.cancel"), on_click=dlg.close).props("flat")
                            ui.button(t("purge_confirm"), icon="delete_forever", on_click=confirm).props("unelevated color=negative")
                    dlg.open()

                ui.button(t("purge"), icon="auto_delete", on_click=open_purge).props("flat color=negative")


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
