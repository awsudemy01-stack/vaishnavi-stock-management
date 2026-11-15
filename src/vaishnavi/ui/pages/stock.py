"""Stock pages (docs/09 §6): overview, movements timeline, Owner adjustment, purchase entry, stock-take. View-models first.

Every write goes through ``InventoryService`` and comes back as an ``ActionResult`` (ok / error code / plain
message) so the NiceGUI layer never sees an exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import MoneyError, format_inr, parse_inr
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.db.records import ProductRecord, StockCountRecord
from vaishnavi.services.inventory import PurchaseLine
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.stock")


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a write from the UI."""

    ok: bool
    message: str = ""
    error_code: str | None = None
    label_count: int = 0
    ref_id: int | None = None


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> ActionResult:
    try:
        value = fn(*args, **kwargs)
    except PermissionDenied as exc:
        return ActionResult(False, str(exc), exc.code)
    except VaishnaviError as exc:
        log.info("STOCK_ACTION_REFUSED code=%s %s", exc.code, exc)
        return ActionResult(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except (ValueError, InvalidOperation, MoneyError) as exc:
        return ActionResult(False, str(exc), None)
    return ActionResult(True, "", None, getattr(value, "label_count", 0), getattr(value, "id", None))


# ----------------------------------------------------------------------------- overview
@dataclass(frozen=True)
class StockRow:
    """One product's level."""

    id: int
    sku: str
    name: str
    qty_text: str
    min_text: str
    low: bool
    value_text: str


@dataclass(frozen=True)
class OverviewModel:
    """Stock overview."""

    rows: list[StockRow]
    low_stock: list[ProductRecord]
    valuation_paise: int
    valuation_text: str
    labels: dict[str, str] = field(default_factory=dict)


def build_overview(services: AppServices, texts: UiTexts) -> OverviewModel:
    """Levels, low-stock list, valuation."""
    page = services.catalog.list_products(limit=100000)
    rows = []
    for p in page.items:
        unit_value = p.cost_paise if p.cost_paise is not None else p.price_paise
        rows.append(StockRow(p.id, p.sku, p.name, f"{p.stock_qty.normalize():f} {p.base_unit}", f"{p.min_stock.normalize():f}", p.low_stock, format_inr(int(p.stock_qty * unit_value))))
    valuation = services.inventory.valuation_paise()
    labels = {k: texts.t(f"stock.{k}") for k in ("title", "overview", "movements", "adjust", "purchase", "stock_take", "low", "valuation", "qty", "min", "value", "reason", "quantity", "supplier", "invoice", "cost", "add_line", "save", "count", "approve", "cancel", "variance")}
    return OverviewModel(rows, services.inventory.low_stock_products(), valuation, format_inr(valuation), labels)


# ----------------------------------------------------------------------------- movements
@dataclass(frozen=True)
class MovementRow:
    """Timeline entry."""

    id: int
    when_text: str
    type: str
    delta_text: str
    after_text: str
    reason: str
    user_name: str
    ref: str


@dataclass(frozen=True)
class MovementsModel:
    """Timeline."""

    product: ProductRecord | None
    rows: list[MovementRow]


def build_movements(services: AppServices, texts: UiTexts, *, product_id: int | None = None, types: tuple[str, ...] | None = None, limit: int = 200) -> MovementsModel:
    """Newest first with user names."""
    names = services.users.display_names()
    rows = [
        MovementRow(m.id, format_ist(parse_iso_utc(m.created_at)), m.type, _signed(m.qty_delta), f"{m.qty_after.normalize():f}", m.reason or "", names.get(m.user_id, f"#{m.user_id}"), f"{m.ref_type} #{m.ref_id}" if m.ref_type else "")
        for m in services.inventory.movements(product_id, types=types, limit=limit)
    ]
    return MovementsModel(services.catalog.get(product_id) if product_id else None, rows)


# ----------------------------------------------------------------------------- writes
def submit_adjustment(services: AppServices, actor: Actor, *, product_id: int, qty_text: str, reason: str) -> ActionResult:
    """``+4`` / ``-6`` with a reason."""
    try:
        delta = Decimal(qty_text.strip().replace(" ", ""))
    except InvalidOperation:
        return ActionResult(False, "quantity: not a number")
    return _wrap(services.inventory.adjust, actor, product_id, delta, reason=reason)


def submit_purchase(services: AppServices, actor: Actor, *, supplier: str, invoice_no: str | None, lines: list[dict[str, str]]) -> ActionResult:
    """Purchase entry from typed rows ``{product_id, qty, unit_cost}``."""
    parsed: list[PurchaseLine] = []
    for line in lines:
        try:
            parsed.append(PurchaseLine(int(line["product_id"]), Decimal(str(line["qty"]).strip()), parse_inr(str(line["unit_cost"]))))
        except (KeyError, ValueError, InvalidOperation, MoneyError) as exc:
            return ActionResult(False, f"line: {exc}")
    return _wrap(services.inventory.record_purchase, actor, supplier_name=supplier, invoice_no=invoice_no or None, lines=parsed)


# ----------------------------------------------------------------------------- stock-take
@dataclass(frozen=True)
class CountLine:
    """One line of the count sheet."""

    product_id: int
    sku: str
    name: str
    system_text: str
    counted_text: str
    variance_text: str
    variance_value_text: str


@dataclass(frozen=True)
class StockTakeModel:
    """The sheet."""

    count_id: int
    status: str
    started_text: str
    lines: list[CountLine]
    counted: int
    total_variance_value_text: str


def start_stock_take(services: AppServices, actor: Actor, *, scope: str = "FULL", product_ids: list[int] | None = None, category_id: int | None = None) -> StockCountRecord:
    """New draft."""
    return services.inventory.start_stock_take(actor, scope=scope, product_ids=product_ids, category_id=category_id)


def _signed(value: Decimal) -> str:
    """``+4`` / ``-1.5`` without trailing zeros."""
    text = f"{value.normalize():f}"
    return text if text.startswith("-") else f"+{text}"


def build_stock_take(services: AppServices, texts: UiTexts, *, count_id: int) -> StockTakeModel:
    """Sheet with variances."""
    lines = services.inventory.stock_take_lines(count_id)
    record = services.inventory.stock_take(count_id)
    out: list[CountLine] = []
    total_value = 0
    for line in lines:
        product = services.catalog.get(line.product_id)
        total_value += line.variance_value_paise or 0
        out.append(
            CountLine(
                line.product_id,
                product.sku if product else "",
                product.name if product else "",
                f"{line.system_qty.normalize():f}",
                f"{line.counted_qty.normalize():f}" if line.counted_qty is not None else "",
                _signed(line.variance) if line.variance is not None else "",
                format_inr(line.variance_value_paise) if line.variance_value_paise is not None else "",
            )
        )
    return StockTakeModel(count_id, record.status if record else "?", format_ist(parse_iso_utc(record.started_at)) if record else "", out, sum(1 for l in lines if l.counted_qty is not None), format_inr(total_value))


def submit_count(services: AppServices, actor: Actor, *, count_id: int, product_id: int, counted_text: str) -> ActionResult:
    """Enter one counted quantity."""
    try:
        qty = Decimal(counted_text.strip())
    except InvalidOperation:
        return ActionResult(False, "counted: not a number")
    return _wrap(services.inventory.record_count, actor, count_id, product_id, qty)


def approve_stock_take(services: AppServices, actor: Actor, *, count_id: int) -> ActionResult:
    """Apply variances."""
    return _wrap(services.inventory.approve_stock_take, actor, count_id)


def cancel_stock_take(services: AppServices, actor: Actor, *, count_id: int) -> ActionResult:
    """Discard."""
    return _wrap(services.inventory.cancel_stock_take, actor, count_id)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/stock`` (overview + low stock), ``/stock/movements``, ``/stock/purchase``, ``/stock/count/<id>``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.core.permissions import Role  # noqa: PLC0415
    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services: AppServices = state.extra["services"]

    def _enter(path: str):  # type: ignore[no-untyped-def]
        client = load_client(app)
        decision = guards.decide(path, client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, path))
            return None
        return client

    @ui.page("/stock")
    def _overview(product: str = "") -> None:
        client = _enter("/stock")
        if client is None:
            return
        texts = UiTexts.load(client.lang)
        owner = client.role is Role.OWNER
        with frame(state, current_path="/stock"):
            m = build_overview(services, texts)
            with ui.row().classes("items-center gap-3 w-full"):
                ui.label(m.labels["title"]).classes("text-xl font-semibold")
                if owner:
                    ui.label(f"{m.labels['valuation']}: {m.valuation_text}").classes("text-sm text-gray-600")
                    ui.element("div").classes("flex-1")
                    ui.button(m.labels["purchase"], icon="local_shipping", on_click=lambda: ui.navigate.to("/stock/purchase")).props("unelevated color=primary")
                    ui.button(m.labels["stock_take"], icon="fact_check", on_click=lambda: ui.navigate.to(f"/stock/count/{start_stock_take(services, client.actor()).id}")).props("outline")
            if m.low_stock:
                ui.label(f"{m.labels['low']}: " + ", ".join(p.name for p in m.low_stock)).classes("text-amber-800 text-sm")
            cols = [{"name": k, "label": texts.t(f"stock.{k}") if texts.has(f"stock.{k}") else k, "field": k} for k in ("sku", "name", "qty_text", "min_text") + (("value_text",) if owner else ())]
            table = ui.table(columns=cols, rows=[r.__dict__ for r in m.rows], row_key="id", pagination=50).classes("w-full")
            if owner:
                table.on("rowClick", lambda e: _adjust_dialog(services, client, texts, int(e.args[1]["id"])))

    @ui.page("/stock/movements")
    def _movements(product: str = "") -> None:
        client = _enter("/stock")
        if client is None:
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/stock"):
            m = build_movements(services, texts, product_id=int(product) if product else None)
            ui.label(f"{texts.t('stock.movements')}" + (f" · {m.product.name}" if m.product else "")).classes("text-xl font-semibold")
            ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("when_text", "type", "delta_text", "after_text", "reason", "user_name", "ref")], rows=[r.__dict__ for r in m.rows], pagination=50).classes("w-full text-xs")

    @ui.page("/stock/purchase")
    def _purchase() -> None:
        client = _enter("/stock")
        if client is None or client.role is not Role.OWNER:
            ui.navigate.to("/stock")
            return
        texts = UiTexts.load(client.lang)
        header = {"supplier": "", "invoice": ""}
        lines: list[dict[str, str]] = [{"product_id": "", "qty": "", "unit_cost": ""}]
        with frame(state, current_path="/stock"):
            ui.label(texts.t("stock.purchase")).classes("text-xl font-semibold")
            with ui.row().classes("gap-2"):
                ui.input(texts.t("stock.supplier")).bind_value(header, "supplier")
                ui.input(texts.t("stock.invoice")).bind_value(header, "invoice")
            grid = ui.column().classes("w-full gap-1")
            products = {str(p.id): f"{p.sku} · {p.name}" for p in services.catalog.list_products(limit=5000).items}

            def render_lines() -> None:
                grid.clear()
                with grid:
                    for line in lines:
                        with ui.row().classes("gap-2 items-end"):
                            ui.select(products, label=texts.t("products.name"), with_input=True).bind_value(line, "product_id").classes("w-96")
                            ui.input(texts.t("stock.quantity")).bind_value(line, "qty").classes("w-24")
                            ui.input(texts.t("stock.cost")).bind_value(line, "unit_cost").classes("w-28")
                    ui.button(texts.t("stock.add_line"), icon="add", on_click=lambda: (lines.append({"product_id": "", "qty": "", "unit_cost": ""}), render_lines())).props("flat")

            render_lines()

            def save() -> None:
                result = submit_purchase(services, client.actor(), supplier=header["supplier"], invoice_no=header["invoice"], lines=[l for l in lines if l["product_id"]])
                if result.ok:
                    ui.notify(texts.t("stock.purchase_saved", labels=result.label_count), type="positive")
                    ui.navigate.to(f"/labels?purchase={result.ref_id}" if result.label_count else "/stock")
                else:
                    ui.notify(result.message, type="negative")

            ui.button(texts.t("stock.save"), on_click=save).props("unelevated color=primary")

    @ui.page("/stock/count/{count_id}")
    def _count(count_id: int) -> None:
        client = _enter("/stock")
        if client is None or client.role is not Role.OWNER:
            ui.navigate.to("/stock")
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/stock"):
            body = ui.column().classes("w-full")

            def render() -> None:
                body.clear()
                m = build_stock_take(services, texts, count_id=count_id)
                with body:
                    ui.label(f"{texts.t('stock.stock_take')} #{m.count_id} · {m.status} · {m.started_text}").classes("text-xl font-semibold")
                    for line in m.lines:
                        with ui.row().classes("gap-2 items-center"):
                            ui.label(f"{line.sku} {line.name}").classes("w-80")
                            ui.label(line.system_text).classes("w-16 vsm-mono")
                            box = ui.input(texts.t("stock.count"), value=line.counted_text).classes("w-24")
                            box.on("keydown.enter", lambda _e, b=box, pid=line.product_id: (submit_count(services, client.actor(), count_id=count_id, product_id=pid, counted_text=b.value or ""), render()))
                            ui.label(f"{line.variance_text} {line.variance_value_text}").classes("w-40 vsm-mono text-sm")
                    ui.label(f"{texts.t('stock.variance')}: {m.total_variance_value_text}").classes("font-semibold")
                    if m.status == "DRAFT":
                        with ui.row().classes("gap-2"):
                            ui.button(texts.t("stock.approve"), on_click=lambda: (approve_stock_take(services, client.actor(), count_id=count_id), render())).props("unelevated color=primary")
                            ui.button(texts.t("stock.cancel"), on_click=lambda: (cancel_stock_take(services, client.actor(), count_id=count_id), ui.navigate.to("/stock"))).props("flat color=negative")

            render()


def _adjust_dialog(services: AppServices, client: Any, texts: UiTexts, product_id: int) -> None:
    from nicegui import ui  # noqa: PLC0415

    product = services.catalog.get(product_id)
    form = {"qty": "", "reason": ""}
    with ui.dialog() as dialog, ui.card().classes("vsm-card p-4 gap-2").style("width: 420px"):
        ui.label(f"{texts.t('stock.adjust')} · {product.name if product else product_id}").classes("font-semibold")
        ui.input(texts.t("stock.quantity"), placeholder="+4 / -2").bind_value(form, "qty")
        ui.input(texts.t("stock.reason")).bind_value(form, "reason")

        def go() -> None:
            result = submit_adjustment(services, client.actor(), product_id=product_id, qty_text=form["qty"], reason=form["reason"])
            if result.ok:
                dialog.close()
                ui.navigate.reload()
            else:
                ui.notify(result.message, type="negative")

        with ui.row().classes("gap-2"):
            ui.button(texts.t("common.confirm"), on_click=go).props("unelevated color=primary")
            ui.button(texts.t("common.cancel"), on_click=dialog.close).props("flat")
    dialog.open()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
