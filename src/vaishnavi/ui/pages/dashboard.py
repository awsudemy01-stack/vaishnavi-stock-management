"""``/`` — the dashboard (docs/09 §2): today at a glance, trend, what needs attention, who is at the counter.

Owner: today's net sales / bills / average bill / items, a 14-day sparkline, payment mix, insight bullets,
low stock, reorder suggestions from the forecast plan, who is logged in, and the setup checklist until it is
complete. Staff: their own shift's figures, low stock (shelf duty) and counter notices — never shop totals.
View-models are pure and tested; the NiceGUI rendering is thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from vaishnavi.core.clock import format_ist, parse_iso_utc, today_ist
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Role
from vaishnavi.ui.components import charts
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.state import ClientState
from vaishnavi.ui.theme import severity_color

log = logging.getLogger("vaishnavi.ui.pages.dashboard")

TREND_DAYS: Final = 14
LIST_LIMIT: Final = 5
CHIP_ORDER: Final = ("database", "ledger", "scanner", "printer", "ai_provider", "backup", "publish")


def _qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Tile:
    """A headline number."""

    key: str
    label: str
    value: str
    hint: str = ""


@dataclass(frozen=True)
class StockRow:
    """Low-stock / reorder line."""

    product_id: int
    name: str
    stock_text: str
    qty_text: str
    note: str = ""


@dataclass(frozen=True)
class InsightView:
    """A rendered bullet."""

    kind: str
    severity: str
    text: str
    link: str
    color: str


@dataclass(frozen=True)
class ChipView:
    """One status chip."""

    name: str
    level: str
    text: str


@dataclass(frozen=True)
class DashboardModel:
    """The page."""

    role: Role
    tiles: list[Tile]
    sparkline: list[int]
    sparkline_svg: str
    by_mode: dict[str, int]
    by_mode_svg: str
    insights: list[InsightView]
    low_stock: list[StockRow]
    reorder: list[StockRow]
    logged_in: list[str]
    shift_text: str
    chips: list[ChipView]
    setup_card: Any | None
    labels: dict[str, str] = field(default_factory=dict)


def _labels(texts: UiTexts) -> dict[str, str]:
    return {k: texts.t(f"dashboard.{k}") for k in ("title", "today", "trend", "mix", "attention", "low_stock", "reorder", "logged_in", "nobody", "all_good", "open_reports", "sales", "bills", "avg_bill", "items", "shift_bills", "shift_cash", "shift_items", "stock", "minimum", "suggest", "cover_days", "status")}


def _chips(state: Any) -> list[ChipView]:
    chips = state.chips if state is not None else {}
    ordered = [n for n in CHIP_ORDER if n in chips] + [n for n in chips if n not in CHIP_ORDER]
    return [ChipView(n, chips[n][0], chips[n][1]) for n in ordered]


def _insight_views(texts: UiTexts, bullets: list[Any], names: dict[int, str]) -> list[InsightView]:
    out = []
    for b in bullets:
        params = dict(b.params)
        if "user_id" in params:
            params["user"] = names.get(params["user_id"], f"#{params['user_id']}")
        if "sign" in params:
            params["sign"] = texts.t(f"insights.sign_{params['sign']}")
        out.append(InsightView(b.kind, b.severity, texts.t(b.key, **params), b.link, severity_color(b.severity)))
    return out


def _owner(c: Any, state: Any, texts: UiTexts, client: ClientState, labels: dict[str, str]) -> DashboardModel:
    actor = client.actor()
    today = today_ist()
    summary = c.reports.sales_summary(actor, since=today.isoformat(), until=today.isoformat())
    tiles = [
        Tile("sales", labels["sales"], format_inr(summary.net_paise), texts.t("dashboard.refunds_hint", amount=format_inr(summary.refunds_paise)) if summary.refunds_paise else ""),
        Tile("bills", labels["bills"], str(summary.bills)),
        Tile("avg_bill", labels["avg_bill"], format_inr(summary.avg_bill_paise)),
        Tile("items", labels["items"], _qty(summary.items)),
    ]
    since = today - timedelta(days=TREND_DAYS - 1)
    by_day = {d.day: d.total_paise - d.refunds_paise for d in c.reports.sales_by_day(actor, since=since.isoformat(), until=today.isoformat())}
    spark = [by_day.get((since + timedelta(days=i)).isoformat(), 0) for i in range(TREND_DAYS)]
    names = c.users.display_names()
    insights = _insight_views(texts, c.insights.for_owner(actor), names)
    low = [StockRow(r.product_id, r.name, _qty(r.stock_qty), _qty(r.min_stock)) for r in c.reports.low_stock(actor)[:LIST_LIMIT]]
    try:
        plan = [r for r in c.forecast.plan(actor) if r.reorder_qty > 0]
    except VaishnaviError as exc:
        log.warning("DASHBOARD_PLAN_SKIPPED code=%s", exc.code)
        plan = []
    reorder = [StockRow(r.product_id, r.name, _qty(r.stock), _qty(r.reorder_qty), texts.t("dashboard.cover_days", days=_qty(r.days_of_cover)) if r.days_of_cover is not None else r.classification) for r in plan[:LIST_LIMIT]]
    logged_in = [names.get(s.user_id, f"#{s.user_id}") for s in c.sessions.who_is_logged_in()]
    card = c.onboarding.checklist(texts)
    return DashboardModel(Role.OWNER, tiles, spark, charts.sparkline_svg(spark), dict(summary.by_mode), charts.bars_svg(summary.by_mode), insights, low, reorder, logged_in, "", _chips(state), None if card.complete else card, labels)


def _staff(c: Any, state: Any, texts: UiTexts, client: ClientState, labels: dict[str, str]) -> DashboardModel:
    actor = client.actor()
    tiles: list[Tile] = []
    shift_text = ""
    shift = c.shifts.current(actor.user_id)
    if shift is not None:
        x = c.shifts.x_report(shift.id)
        tiles = [Tile("shift_bills", labels["shift_bills"], str(x.bills)), Tile("shift_cash", labels["shift_cash"], format_inr(x.by_mode.get("CASH", 0))), Tile("shift_items", labels["shift_items"], _qty(x.items_sold))]
        shift_text = texts.t("dashboard.shift_open", id=shift.id, time=format_ist(parse_iso_utc(shift.opened_at), "%H:%M"))
    else:
        shift_text = texts.t("dashboard.no_shift")
    names = c.users.display_names()
    insights = _insight_views(texts, c.insights.for_staff(actor), names)
    low = [StockRow(r.product_id, r.name, _qty(r.stock_qty), _qty(r.min_stock)) for r in c.reports.low_stock(actor)[:LIST_LIMIT]]
    return DashboardModel(Role.STAFF, tiles, [], "", {}, "", insights, low, [], [], shift_text, _chips(state), None, labels)


def build_dashboard(c: Any, state: Any, texts: UiTexts, client: ClientState) -> DashboardModel:
    """Role-appropriate dashboard model."""
    labels = _labels(texts)
    model = _owner(c, state, texts, client, labels) if client.role is Role.OWNER else _staff(c, state, texts, client, labels)
    log.debug("DASHBOARD_BUILT role=%s tiles=%d insights=%d low=%d reorder=%d", model.role.value, len(model.tiles), len(model.insights), len(model.low_stock), len(model.reorder))
    return model


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/")
    def _home(denied: str | None = None) -> None:
        client = load_client(app)
        decision = guards.decide("/", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/"))
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/", denied=denied):
            m = build_dashboard(services, state, texts, client)
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(m.labels["title"]).classes("text-xl font-semibold")
                if m.shift_text:
                    ui.label(m.shift_text).classes("text-sm text-gray-600")
            if m.setup_card is not None:
                _setup_card(ui, texts, m.setup_card)
            if m.tiles:
                with ui.row().classes("w-full gap-3"):
                    for t in m.tiles:
                        with ui.column().classes("vsm-card p-3 gap-0").style("min-width: 150px"):
                            ui.label(t.label).classes("text-xs uppercase text-gray-500")
                            ui.label(t.value).classes("text-2xl font-semibold")
                            if t.hint:
                                ui.label(t.hint).classes("text-xs text-gray-500")
            with ui.row().classes("w-full gap-3 items-stretch"):
                if m.role is Role.OWNER:
                    with ui.column().classes("vsm-card p-3 gap-1"):
                        ui.label(m.labels["trend"]).classes("font-semibold")
                        ui.html(m.sparkline_svg)
                        ui.label(m.labels["mix"]).classes("font-semibold mt-2")
                        ui.html(m.by_mode_svg)
                        ui.label(" · ".join(f"{k} {format_inr(v)}" for k, v in m.by_mode.items())).classes("text-xs text-gray-600")
                with ui.column().classes("vsm-card p-3 gap-1 flex-1"):
                    ui.label(m.labels["attention"]).classes("font-semibold")
                    if not m.insights:
                        ui.label(m.labels["all_good"]).classes("text-sm text-gray-600")
                    for i in m.insights:
                        with ui.row().classes("items-center gap-2 cursor-pointer").on("click", lambda _e, p=i.link: ui.navigate.to(p)):
                            ui.icon("circle").style(f"color:{i.color}; font-size: 10px")
                            ui.label(i.text).classes("text-sm")
            with ui.row().classes("w-full gap-3 items-stretch"):
                _stock_list(ui, m.labels["low_stock"], m.low_stock, m.labels["stock"], m.labels["minimum"], "/reports/low_stock")
                if m.role is Role.OWNER:
                    _stock_list(ui, m.labels["reorder"], m.reorder, m.labels["stock"], m.labels["suggest"], "/reports/forecast")
                    with ui.column().classes("vsm-card p-3 gap-1").style("min-width: 200px"):
                        ui.label(m.labels["logged_in"]).classes("font-semibold")
                        if not m.logged_in:
                            ui.label(m.labels["nobody"]).classes("text-sm text-gray-600")
                        for name in m.logged_in:
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("person").classes("text-green-700")
                                ui.label(name).classes("text-sm")
            if m.chips:
                with ui.row().classes("w-full gap-2 items-center"):
                    ui.label(m.labels["status"]).classes("text-xs uppercase text-gray-500")
                    for chip in m.chips:
                        ui.badge(f"{texts.t('chip.' + chip.name) if texts.has('chip.' + chip.name) else chip.name}: {chip.text}", color={"green": "positive", "amber": "warning", "red": "negative"}.get(chip.level, "grey")).props("outline")


def _setup_card(ui: Any, texts: UiTexts, card: Any) -> None:
    with ui.column().classes("vsm-card p-4 gap-1").style("max-width: 520px"):
        ui.label(card.title).classes("font-semibold")
        ui.linear_progress(card.done / card.total if card.total else 1.0, show_value=False)
        for item in card.items:
            icon = "check_circle" if item.done else ("remove_circle_outline" if item.skipped else "radio_button_unchecked")
            with ui.row().classes("items-center gap-2 cursor-pointer").on("click", lambda _e, p=item.path: ui.navigate.to(p)):
                ui.icon(icon).classes("text-green-700" if item.done else "text-gray-500")
                ui.label(item.title).classes("text-gray-400" if item.skipped else "")
        if card.next_path:
            ui.button(texts.t("setup.next"), icon="arrow_forward", on_click=lambda: ui.navigate.to(card.next_path)).props("unelevated color=primary")


def _stock_list(ui: Any, title: str, rows: list[StockRow], stock_label: str, qty_label: str, link: str) -> None:
    with ui.column().classes("vsm-card p-3 gap-1 flex-1"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(title).classes("font-semibold")
            ui.button(icon="open_in_new", on_click=lambda: ui.navigate.to(link)).props("flat dense")
        if not rows:
            ui.label("—").classes("text-sm text-gray-600")
        for r in rows:
            with ui.row().classes("w-full items-center justify-between gap-2 cursor-pointer").on("click", lambda _e, p=r.product_id: ui.navigate.to(f"/stock?product={p}")):
                ui.label(r.name).classes("text-sm flex-1")
                ui.label(f"{stock_label} {r.stock_text}").classes("text-xs text-gray-600")
                ui.label(f"{qty_label} {r.qty_text}").classes("text-xs font-semibold")
                if r.note:
                    ui.label(r.note).classes("text-xs text-gray-500")


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:  # pragma: no cover - import order
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
