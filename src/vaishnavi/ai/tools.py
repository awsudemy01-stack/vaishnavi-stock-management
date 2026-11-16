"""ToolRegistry — the only data path from the shop to the model (docs/06 §3, §1a, §7 layers 1–2, 5).

Every tool is read-only, built on the already-authorised services, returns a small JSON-able dict with
money as ``₹`` strings and quantities as strings, and exists **only for the roles listed** — the model in a
staff session is never even told that ``sales_summary`` exists. Arguments are validated by
``tool_protocol.validate_args``; results are trimmed and, for staff, stripped of cost/margin keys. Every
call is logged ``AI_TOOL_CALL name= args= rows= ms=``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from vaishnavi.ai.providers.base import ToolSpec
from vaishnavi.ai.tool_protocol import ToolCallLog, ToolDef, ToolError, strip_keys, trim_result, validate_args
from vaishnavi.core.clock import format_ist, parse_iso_utc, today_ist
from vaishnavi.core.errors import catalogue
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, PermissionDenied, Role

log = logging.getLogger("vaishnavi.ai.tools")

ALL: Final = frozenset({Role.OWNER, Role.STAFF})
OWNER: Final = frozenset({Role.OWNER})
PERIODS: Final = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "last_7_days", "last_30_days", "last_90_days", "this_year")


def _q(value: Decimal | int | None) -> str:
    if value is None:
        return ""
    text = format(Decimal(value).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def period_bounds(period: str | None, from_: str | None, to: str | None, *, today: date | None = None) -> tuple[str, str, str]:
    """``(since, until, label)`` as YYYY-MM-DD shop days; explicit dates win; clamped to 2 years."""
    today = today or today_ist()
    if from_ or to:
        a = date.fromisoformat(from_) if from_ else today - timedelta(days=30)
        b = date.fromisoformat(to) if to else today
    else:
        p = period or "today"
        if p == "today":
            a = b = today
        elif p == "yesterday":
            a = b = today - timedelta(days=1)
        elif p == "this_week":
            a, b = today - timedelta(days=today.weekday()), today
        elif p == "last_week":
            b = today - timedelta(days=today.weekday() + 1)
            a = b - timedelta(days=6)
        elif p == "this_month":
            a, b = today.replace(day=1), today
        elif p == "last_month":
            b = today.replace(day=1) - timedelta(days=1)
            a = b.replace(day=1)
        elif p == "last_7_days":
            a, b = today - timedelta(days=6), today
        elif p == "last_30_days":
            a, b = today - timedelta(days=29), today
        elif p == "last_90_days":
            a, b = today - timedelta(days=89), today
        else:  # this_year
            a, b = today.replace(month=1, day=1), today
    if a > b:
        a, b = b, a
    a = max(a, today - timedelta(days=730))
    label = a.strftime("%d-%b-%Y") if a == b else f"{a.strftime('%d-%b-%Y')} to {b.strftime('%d-%b-%Y')}"
    return a.isoformat(), b.isoformat(), label


_PERIOD_PARAMS: Final = {"period": {"type": "string", "enum": list(PERIODS), "default": "today"}, "from": {"type": "string", "format": "date"}, "to": {"type": "string", "format": "date"}}


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


class ToolRegistry:
    """Builds the tool table from the application's services."""

    def __init__(self, *, reports: Any, forecast: Any, catalog: Any, prices: Any, backup: Any, billing: Any, users: Any, indexer: Any, chips: Callable[[], Mapping[str, tuple[str, str]]], provider_health: Callable[[], Any]) -> None:
        self._reports, self._forecast, self._catalog, self._prices = reports, forecast, catalog, prices
        self._backup, self._billing, self._users, self._indexer = backup, billing, users, indexer
        self._chips, self._provider_health = chips, provider_health
        self._tools: dict[str, ToolDef] = {}
        self._register_all()
        log.info("AI_TOOLS_READY count=%d staff=%d", len(self._tools), len(self.for_role(Role.STAFF)))

    # -- public ------------------------------------------------------------------------------------
    def for_role(self, role: Role) -> list[ToolSpec]:
        """Specs the model may see in this session."""
        return [t.spec() for t in self._tools.values() if role in t.roles]

    def names(self, role: Role) -> set[str]:
        """Names allowed for the role."""
        return {t.name for t in self._tools.values() if role in t.roles}

    def link_for(self, name: str) -> str | None:
        """Report screen behind a tool."""
        t = self._tools.get(name)
        return t.link if t else None

    def run(self, actor: Actor, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Validate → role gate → handler → strip/trim. Raises ``ToolError``."""
        tool = self._tools.get(name)
        if tool is None or actor.role not in tool.roles:
            log.warning("AI_TOOL_DENIED name=%s role=%s user=%s code=E-GRD-001", name, actor.role.value, actor.user_id)
            raise ToolError("E-GRD-001", tool=name, detail="tool not available for this role")
        clean = validate_args(tool, args)
        started = time.monotonic()
        try:
            result = tool.handler(actor, clean)
        except PermissionDenied as exc:
            raise ToolError("E-GRD-001", tool=name, detail=str(exc)) from exc
        ms = int((time.monotonic() - started) * 1000)
        if actor.role is not Role.OWNER:
            result = strip_keys(result, tool.staff_strip)
        result = trim_result(result)
        rows = len(result.get("items", [])) if isinstance(result.get("items"), list) else 1
        log.info("AI_TOOL_CALL name=%s args=%s rows=%d ms=%d user=%s role=%s", name, clean, rows, ms, actor.user_id, actor.role.value)
        return result

    def run_logged(self, actor: Actor, name: str, args: Mapping[str, Any]) -> ToolCallLog:
        """``run`` that never raises — the chat loop feeds errors back to the model as text."""
        started = time.monotonic()
        try:
            result = self.run(actor, name, args)
            return ToolCallLog(name, dict(args), len(result.get("items", [])) if isinstance(result.get("items"), list) else 1, int((time.monotonic() - started) * 1000), True, None, self.link_for(name), result)
        except ToolError as exc:
            return ToolCallLog(name, dict(args), 0, int((time.monotonic() - started) * 1000), False, f"{exc.code}: {exc.detail}", None, {"error": exc.code, "detail": exc.detail})

    # -- registration ----------------------------------------------------------------------------
    def _add(self, name: str, description: str, props: dict[str, Any], roles: frozenset[Role], handler: Callable[[Actor, dict[str, Any]], dict[str, Any]], *, required: list[str] | None = None, link: str | None = None) -> None:
        self._tools[name] = ToolDef(name, description, _schema(props, required), roles, handler, link)

    def _register_all(self) -> None:
        q = {"query": {"type": "string"}}
        self._add("get_stock", "Current stock of one item by name, code or barcode.", q, ALL, self._get_stock, required=["query"], link="/stock")
        self._add("search_products", "Find products by name/code; returns code, barcode, category, stock.", {**q, "limit": {"type": "integer", "default": 10}}, ALL, self._search_products, required=["query"], link="/products")
        self._add("item_price", "Selling price, MRP, unit and pricing unit of one item (no cost).", q, ALL, self._item_price, required=["query"])
        self._add("low_stock_items", "Items at or below their minimum stock.", {"limit": {"type": "integer", "default": 20}}, ALL, self._low_stock, link="/reports/low_stock")
        self._add("out_of_stock_items", "Items with zero stock.", {"limit": {"type": "integer", "default": 20}}, ALL, self._out_of_stock, link="/reports/low_stock")
        self._add("stock_overview", "Whole-shop stock picture: counts of items in stock / low / out, and the items with most stock.", {"limit": {"type": "integer", "default": 10}}, ALL, self._stock_overview, link="/stock")
        self._add("find_invoice", "Look up one bill by invoice number or receipt code (staff: own bills only).", {"invoice_no": {"type": "string"}}, ALL, self._find_invoice, required=["invoice_no"], link="/sales")
        self._add("shop_status", "Scanner, printer and assistant status.", {}, ALL, self._shop_status, link="/status")
        self._add("explain_error", "Plain-language meaning and fix for an E- error code shown on screen.", {"code": {"type": "string"}}, ALL, self._explain_error, required=["code"])
        self._add("explain_screen", "How to do something in the app (steps from the user guide).", q, ALL, self._explain_screen, required=["query"])
        self._add("sales_summary", "Sales totals for a period: net, bills, average bill, items, by payment mode.", {**_PERIOD_PARAMS, "group_by": {"type": "string", "enum": ["none", "day", "payment_mode"], "default": "none"}}, OWNER, self._sales_summary, link="/reports/sales")
        self._add("sales_compare", "Compare two periods' sales.", {"period_a": {"type": "string", "enum": list(PERIODS)}, "period_b": {"type": "string", "enum": list(PERIODS)}}, OWNER, self._sales_compare, required=["period_a", "period_b"], link="/reports/sales")
        self._add("demand_ranking", "Top or bottom selling items for a period.", {**_PERIOD_PARAMS, "order": {"type": "string", "enum": ["top", "bottom"], "default": "top"}, "limit": {"type": "integer", "default": 10}}, OWNER, self._demand_ranking, link="/reports/top")
        self._add("dead_stock", "Items in stock but unsold for N days, with value.", {"days": {"type": "integer", "default": 60, "minimum": 7, "maximum": 365}, "limit": {"type": "integer", "default": 20}}, OWNER, self._dead_stock, link="/reports/dead")
        self._add("forecast_sales", "Expected total demand over the horizon (sum of item forecasts).", {"horizon_days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90}}, OWNER, self._forecast_sales, link="/reports/forecast")
        self._add("forecast_item", "Demand forecast for one item with band, confidence and caveats.", {**q, "horizon_days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90}}, OWNER, self._forecast_item, required=["query"], link="/reports/forecast")
        self._add("reorder_plan", "Items to reorder: stock, daily rate, days of cover, suggested quantity.", {"horizon_days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 90}, "limit": {"type": "integer", "default": 20}}, OWNER, self._reorder_plan, link="/reports/forecast")
        self._add("festival_outlook", "Next festival window and the categories it lifts.", {"festival": {"type": "string"}}, OWNER, self._festival_outlook, link="/reports/forecast")
        self._add("cashier_summary", "Per-cashier bills, sales, discounts, voids, manual lines, variance.", {**_PERIOD_PARAMS, "user": {"type": "string"}}, OWNER, self._cashier_summary, link="/reports/cashiers")
        self._add("shrinkage_summary", "Damage, adjustments and stock-take losses, valued.", _PERIOD_PARAMS, OWNER, self._shrinkage, link="/reports/shrinkage")
        self._add("purchases_summary", "Purchases per supplier for a period.", _PERIOD_PARAMS, OWNER, self._purchases, link="/reports/purchases")
        self._add("profit_summary", "Gross profit and margin for a period.", _PERIOD_PARAMS, OWNER, self._profit, link="/reports/profit")
        self._add("price_history", "Price changes of one item.", {**q, "range": {"type": "string", "enum": ["1m", "6m", "1y"], "default": "6m"}}, OWNER, self._price_history, required=["query"], link="/products")
        self._add("backup_status", "Last backup age, queued copies, secondary drive.", {}, OWNER, self._backup_status, link="/settings/backups")
        self._add("doctor_summary", "Last health check in plain words (status chips).", {}, OWNER, self._doctor_summary, link="/status")
        self._add("log_explain", "Explain an E- code or log line in plain language.", {"code": {"type": "string"}}, OWNER, self._explain_error, required=["code"], link="/logs")
        self._add("settings_help", "What a setting does and where it is (never its value).", {"topic": {"type": "string"}}, OWNER, self._settings_help, required=["topic"], link="/settings")

    # -- helpers ---------------------------------------------------------------------------------
    def _product(self, query: str) -> Any | None:
        text = query.strip()
        if re.fullmatch(r"\d{8,14}", text) or re.fullmatch(r"[A-Z]{2,5}-\d{3,6}", text.upper()):
            hits = self._catalog.search(text, limit=1)
            if hits:
                return hits[0].product
        hits = self._catalog.search(text, limit=5)
        if not hits:
            return None
        active = [h for h in hits if h.product.is_active and not h.product.is_system]
        return (active or hits)[0].product

    def _product_dict(self, p: Any) -> dict[str, Any]:
        return {"found": True, "product_id": p.id, "name": p.name, "name_hi": p.name_hi or "", "sku": p.sku, "barcode": p.internal_barcode or "", "qty": _q(p.stock_qty), "unit": p.unit, "min_stock": _q(p.min_stock), "category": self._category_name(p.category_id), "active": bool(p.is_active)}

    def _category_name(self, category_id: int | None) -> str:
        if category_id is None:
            return ""
        try:
            cats = {c.id: c.name for c in self._catalog.categories()}
        except Exception:  # noqa: BLE001 - optional nicety
            return ""
        return cats.get(category_id, "")

    def _names(self) -> dict[int, str]:
        return self._users.display_names()

    # -- tools: all roles --------------------------------------------------------------------------
    def _get_stock(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        p = self._product(a["query"])
        return self._product_dict(p) if p else {"found": False, "query": a["query"]}

    def _search_products(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        hits = self._catalog.search(a["query"], limit=a.get("limit", 10))
        return {"items": [{"product_id": h.product.id, "name": h.product.name, "sku": h.product.sku, "barcode": h.product.internal_barcode or "", "category": self._category_name(h.product.category_id), "qty": _q(h.product.stock_qty), "price": format_inr(h.product.price_paise)} for h in hits], "count": len(hits), "query": a["query"]}

    def _item_price(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        p = self._product(a["query"])
        if not p:
            return {"found": False, "query": a["query"]}
        unit = p.pricing_unit_label or p.unit
        return {"found": True, "product_id": p.id, "name": p.name, "price": format_inr(p.price_paise), "mrp": format_inr(p.mrp_paise) if p.mrp_paise else "", "unit": unit, "pricing_unit_qty": _q(p.pricing_unit_qty), "tax_rate_pct": _q(Decimal(p.tax_rate_bp) / 100), "price_includes_tax": bool(p.price_includes_tax)}

    def _low_stock(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        rows = self._reports.low_stock(actor)
        limit = a.get("limit", 20)
        return {"items": [{"product_id": r.product_id, "name": r.name, "qty": _q(r.stock_qty), "min_stock": _q(r.min_stock), "reorder": _q(r.reorder_qty)} for r in rows[:limit]], "count": len(rows), "limit": limit}

    def _out_of_stock(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        rows = [r for r in self._reports.low_stock(actor) if r.stock_qty <= 0]
        limit = a.get("limit", 20)
        return {"items": [{"product_id": r.product_id, "name": r.name, "min_stock": _q(r.min_stock)} for r in rows[:limit]], "count": len(rows), "limit": limit}

    def _stock_overview(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        """"stock kya hai" / "stock list": the shop-wide picture (counts + the best-stocked items) instead of a guess."""
        limit = a.get("limit", 10)
        products = [p for p in self._catalog.list_products(limit=5000).items if p.is_active and not p.is_system]
        low = [p for p in products if p.stock_qty > 0 and p.min_stock > 0 and p.stock_qty <= p.min_stock]
        out = [p for p in products if p.stock_qty <= 0]
        top = sorted((p for p in products if p.stock_qty > 0), key=lambda p: p.stock_qty, reverse=True)[:limit]
        return {
            "items_total": len(products),
            "in_stock": len(products) - len(out),
            "low": len(low),
            "out": len(out),
            "top": [{"product_id": p.id, "name": p.name, "qty": _q(p.stock_qty), "unit": p.unit} for p in top],
            "out_names": [p.name for p in out[:limit]],
        }

    def _find_invoice(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        sale = self._billing.find_sale(a["invoice_no"])
        if sale is None or (actor.role is not Role.OWNER and sale.user_id != actor.user_id):
            return {"found": False, "invoice_no": a["invoice_no"]}
        return {"found": True, "sale_id": sale.id, "invoice_no": sale.invoice_no, "when": format_ist(parse_iso_utc(sale.sold_at)), "status": sale.status, "cashier": self._names().get(sale.user_id, f"#{sale.user_id}"), "total": format_inr(sale.total_paise), "savings": format_inr(sale.total_savings_paise), "items": [{"name": i.open_item_name or i.name_snapshot, "qty": _q(i.qty), "amount": format_inr(i.line_total_paise)} for i in sale.items][:20], "payments": [{"mode": p.mode, "amount": format_inr(p.amount_paise)} for p in sale.payments]}

    def _shop_status(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        chips = self._chips()
        health = self._provider_health()
        pick = lambda n: {"level": chips[n][0], "text": chips[n][1]} if n in chips else {"level": "grey", "text": "not checked yet"}  # noqa: E731
        return {"scanner": pick("scanner"), "printer": pick("printer"), "assistant": {"level": "green" if getattr(health, "ok", False) else "amber", "text": getattr(health, "detail", "")}}

    def _explain_error(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        code = a["code"].strip().upper()
        m = re.search(r"E-[A-Z]+-\d{3}", code)
        if not m or m.group(0) not in catalogue.codes("en"):
            return {"found": False, "code": code}
        c = m.group(0)
        msg_en, fix_en = catalogue.render(c, "en", {})
        msg_hi, fix_hi = catalogue.render(c, "hi", {})
        return {"found": True, "code": c, "message": msg_en, "what_to_do": fix_en, "message_hi": msg_hi, "what_to_do_hi": fix_hi}

    def _explain_screen(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        hits = self._indexer.search(a["query"], k=2) if self._indexer is not None else []
        if not hits:
            return {"found": False, "query": a["query"]}
        top = hits[0]
        steps = [ln.strip(" -*0123456789.") for ln in top.content.splitlines() if ln.strip() and not ln.startswith("#")]
        return {"found": True, "title": top.title, "steps": steps[:8], "source": top.source, "link": "/help"}

    def _settings_help(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        return self._explain_screen(actor, {"query": f"settings {a['topic']}"})

    # -- tools: owner ----------------------------------------------------------------------------
    def _sales_summary(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period"), a.get("from"), a.get("to"))
        s = self._reports.sales_summary(actor, since=since, until=until)
        cash_pct = int(round(s.by_mode.get("CASH", 0) * 100 / s.total_paise)) if s.total_paise else 0
        out: dict[str, Any] = {"period": label, "since": since, "until": until, "net": format_inr(s.net_paise), "gross": format_inr(s.total_paise), "refunds": format_inr(s.refunds_paise), "bills": s.bills, "items": _q(s.items), "avg_bill": format_inr(s.avg_bill_paise), "discounts": format_inr(s.discount_paise), "tax": format_inr(s.tax_paise), "by_mode": {k: format_inr(v) for k, v in s.by_mode.items()}, "cash_pct": cash_pct}
        if a.get("group_by") == "day":
            out["items_by_day"] = [{"day": d.day, "bills": d.bills, "net": format_inr(d.total_paise - d.refunds_paise)} for d in self._reports.sales_by_day(actor, since=since, until=until)]
        return out

    def _sales_compare(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        x = self._sales_summary(actor, {"period": a["period_a"]})
        y = self._sales_summary(actor, {"period": a["period_b"]})
        return {"period_a": x, "period_b": y}

    def _demand_ranking(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period") or "last_30_days", a.get("from"), a.get("to"))
        rows = self._reports.top_sellers(actor, since=since, until=until, limit=200)
        if a.get("order") == "bottom":
            rows = list(reversed(rows))
        limit = a.get("limit", 10)
        return {"period": label, "order": a.get("order", "top"), "items": [{"product_id": r.product_id, "name": r.name, "qty": _q(r.qty), "amount": format_inr(r.amount_paise), "share_pct": r.share_pct} for r in rows[:limit]], "count": len(rows)}

    def _dead_stock(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        rows = self._reports.dead_stock(actor, days=a.get("days", 60))
        limit = a.get("limit", 20)
        return {"days": a.get("days", 60), "items": [{"product_id": r.product_id, "name": r.name, "qty": _q(r.stock_qty), "value": format_inr(r.value_paise), "last_sold": r.last_sold_at or "never"} for r in rows[:limit]], "count": len(rows), "value_total": format_inr(sum(r.value_paise for r in rows))}

    def _forecast_item(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        p = self._product(a["query"])
        if not p:
            return {"found": False, "query": a["query"]}
        try:
            f = self._forecast.forecast(p.id, horizon_days=a.get("horizon_days", 14))
        except Exception as exc:  # noqa: BLE001 - ForecastError → caveat, not a crash
            code = getattr(exc, "code", "")
            return {"found": True, "product_id": p.id, "name": p.name, "stock": _q(p.stock_qty), "horizon_days": a.get("horizon_days", 14), "point": None, "caveats": [f"Not enough sales history yet ({code}). The forecast needs about 4 weeks of sales."], "confidence": "low"}
        return {"found": True, "product_id": p.id, "name": p.name, "stock": _q(p.stock_qty), "horizon_days": f.horizon_days, "point": _q(f.point), "low": _q(f.low), "high": _q(f.high), "daily_rate": _q(f.daily_rate), "method": f.method, "confidence": f.confidence, "classification": f.classification, "history_days": f.history_days, "caveats": list(f.caveats)}

    def _forecast_sales(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        plan = self._forecast.plan(actor)
        h = a.get("horizon_days", 14)
        rated = [r for r in plan if r.daily_rate is not None]
        total = sum((r.daily_rate * h for r in rated), Decimal(0))
        return {"horizon_days": h, "items_with_forecast": len(rated), "items_without_history": len(plan) - len(rated), "expected_units": _q(total.quantize(Decimal(1))), "caveats": ["Totals are unit counts across items; see reorder_plan for per-item quantities."]}

    def _reorder_plan(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        rows = [r for r in self._forecast.plan(actor) if r.reorder_qty > 0]
        limit = a.get("limit", 20)
        return {"items": [{"product_id": r.product_id, "name": r.name, "stock": _q(r.stock), "daily_rate": _q(r.daily_rate), "days_of_cover": _q(r.days_of_cover), "stockout_date": r.stockout_date or "", "order_qty": _q(r.reorder_qty), "classification": r.classification, "confidence": r.confidence} for r in rows[:limit]], "count": len(rows)}

    def _festival_outlook(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        festivals = self._forecast.festivals()
        today = today_ist()
        wanted = (a.get("festival") or "").lower()
        upcoming = sorted((f for f in festivals if f.end >= today and (not wanted or wanted in f.name.lower())), key=lambda f: f.start)
        if not upcoming:
            return {"next": None, "count": 0}
        f = upcoming[0]
        return {"next": {"name": f.name, "start": f.start.isoformat(), "end": f.end.isoformat(), "days_until": (f.start - today).days, "uplift": {k: str(v) for k, v in f.uplift.items()}}, "count": len(upcoming), "following": [{"name": g.name, "start": g.start.isoformat()} for g in upcoming[1:4]]}

    def _cashier_summary(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period"), a.get("from"), a.get("to"))
        names = self._names()
        rows = self._reports.cashier_performance(actor, since=since, until=until)
        wanted = (a.get("user") or "").lower()
        items = [{"cashier": names.get(r.user_id, f"#{r.user_id}"), "bills": r.bills, "sales": format_inr(r.sales_paise), "avg_bill": format_inr(r.avg_bill_paise), "discounts": format_inr(r.discount_paise), "voids": r.voids, "manual_lines": r.manual_lines, "shifts_closed": r.shifts_closed, "variance": format_inr(r.variance_paise)} for r in rows if not wanted or wanted in names.get(r.user_id, "").lower()]
        return {"period": label, "items": items, "count": len(items)}

    def _shrinkage(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period") or "last_30_days", a.get("from"), a.get("to"))
        rows = self._reports.shrinkage(actor, since=since, until=until)
        return {"period": label, "items": [{"name": r.name, "damage": _q(r.damage_qty), "adjustments": _q(r.adjustment_qty), "stock_take": _q(r.stock_take_qty), "value": format_inr(r.value_paise)} for r in rows[:50]], "count": len(rows), "value_total": format_inr(sum(r.value_paise for r in rows))}

    def _purchases(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period") or "this_month", a.get("from"), a.get("to"))
        rows = self._reports.purchases(actor, since=since, until=until)
        return {"period": label, "items": [{"supplier": r.supplier, "purchases": r.purchases, "total": format_inr(r.total_paise)} for r in rows], "count": len(rows), "total": format_inr(sum(r.total_paise for r in rows))}

    def _profit(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        since, until, label = period_bounds(a.get("period"), a.get("from"), a.get("to"))
        p = self._reports.profit(actor, since=since, until=until)
        margin = int(round(p.profit_paise * 100 / p.revenue_paise)) if p.revenue_paise else 0
        return {"period": label, "revenue": format_inr(p.revenue_paise), "cost": format_inr(p.cost_paise), "profit": format_inr(p.profit_paise), "margin_pct": margin, "lines_without_cost": p.unknown_cost_lines, "items": [{"name": r.name, "qty": _q(r.qty), "revenue": format_inr(r.revenue_paise), "profit": format_inr(r.profit_paise), "margin_pct": r.margin_pct} for r in p.rows[:20]]}

    def _price_history(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        p = self._product(a["query"])
        if not p:
            return {"found": False, "query": a["query"]}
        days = {"1m": 30, "6m": 182, "1y": 365}[a.get("range", "6m")]
        from datetime import datetime, timezone  # noqa: PLC0415

        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = self._prices.history(p.id, since=since)
        return {"found": True, "name": p.name, "current_price": format_inr(p.price_paise), "items": [{"when": format_ist(parse_iso_utc(r.effective_from)), "field": r.field, "old": r.old_value, "new": r.new_value, "reason": r.reason or ""} for r in rows[:30]], "count": len(rows)}

    def _backup_status(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        age = self._backup.last_backup_age_hours()
        ready, _why = self._backup.secondary_ready()
        return {"last_backup": "never" if age is None else f"{age:.1f} hours ago", "queued_copies": self._backup.queued_count(), "secondary_ready": bool(ready), "snapshots": len(self._backup.list_snapshots())}

    def _doctor_summary(self, actor: Actor, a: dict[str, Any]) -> dict[str, Any]:
        chips = self._chips()
        worst = "green"
        for level, _ in chips.values():
            if level == "red":
                worst = "red"
            elif level == "amber" and worst != "red":
                worst = "amber"
        return {"overall": worst, "checks": {k: {"level": v[0], "text": v[1]} for k, v in chips.items()}}
