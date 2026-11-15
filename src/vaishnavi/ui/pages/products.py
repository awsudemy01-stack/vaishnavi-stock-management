"""Products page (docs/09 §5): grid, editor with Barcodes section, the *Price history* pop-up. View-models first.

``build_grid`` / ``build_editor`` / ``build_price_history`` are pure reads; ``validate_form`` and
``save_editor`` turn the owner's typed strings into a ``ProductInput`` / ``update()`` call and return
plain problems or the error code instead of raising. The NiceGUI page at the bottom only lays this out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import MoneyError, format_inr, parse_inr, rate_to_bp
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.db.records import BarcodeRecord, CategoryRecord, ProductRecord
from vaishnavi.db.seed import UNITS
from vaishnavi.services.catalog import ProductInput
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.products")

SALE_MODES = (("PIECE", "pcs"), ("WEIGHT", "g"), ("VOLUME", "ml"), ("LENGTH", "cm"))


# ----------------------------------------------------------------------------- grid
@dataclass(frozen=True)
class GridRow:
    """One product line."""

    id: int
    sku: str
    name: str
    name_hi: str
    category: str
    price_text: str
    mrp_text: str
    stock_text: str
    low_stock: bool
    active: bool
    barcode: str


@dataclass(frozen=True)
class GridModel:
    """The grid."""

    rows: list[GridRow]
    total: int
    page: int
    page_size: int
    query: str
    category_id: int | None
    categories: list[CategoryRecord]
    labels: dict[str, str] = field(default_factory=dict)


def _qty_text(qty: Decimal, unit: str) -> str:
    return f"{qty.normalize():f} {unit}"


def build_grid(services: AppServices, texts: UiTexts, *, query: str = "", category_id: int | None = None, page: int = 1, page_size: int = 50, include_archived: bool = False) -> GridModel:
    """One page of the product grid."""
    cats = services.catalog.categories()
    names = {c.id: c.name for c in cats}
    result = services.catalog.list_products(category_id=category_id, query=query, include_archived=include_archived, limit=page_size, offset=(max(1, page) - 1) * page_size)
    rows = [
        GridRow(p.id, p.sku, p.name, p.name_hi or "", names.get(p.category_id or -1, ""), format_inr(p.price_paise), format_inr(p.mrp_paise) if p.mrp_paise is not None else "", _qty_text(p.stock_qty, p.base_unit), p.low_stock, p.is_active, p.internal_barcode or "")
        for p in result.items
    ]
    labels = {k: texts.t(f"products.{k}") for k in ("title", "search", "category", "all", "new", "edit", "archive", "labels", "import", "export", "low", "archived", "empty")}
    return GridModel(rows, result.total, page, page_size, query, category_id, cats, labels)


# ----------------------------------------------------------------------------- editor
@dataclass(frozen=True)
class EditorModel:
    """Editor state."""

    product: ProductRecord | None
    form: dict[str, str]
    barcodes: list[BarcodeRecord]
    tags: list[str]
    categories: list[CategoryRecord]
    units: list[dict[str, Any]]
    sale_modes: tuple[tuple[str, str], ...]
    history_points: list[tuple[str, int]]
    labels: dict[str, str] = field(default_factory=dict)


FORM_FIELDS = ("sku", "name", "name_hi", "category_id", "price", "mrp", "cost", "tax_rate", "hsn", "unit", "sale_mode", "min_stock", "reorder_qty", "quick_pick", "tags", "manufacturer_barcode", "reason")


def _blank_form() -> dict[str, str]:
    return {k: "" for k in FORM_FIELDS} | {"sale_mode": "PIECE", "unit": "pc", "tax_rate": "0", "min_stock": "0", "reorder_qty": "0", "quick_pick": "0"}


def _form_of(product: ProductRecord, tags: list[str]) -> dict[str, str]:
    return _blank_form() | {
        "sku": product.sku, "name": product.name, "name_hi": product.name_hi or "", "category_id": str(product.category_id or ""), "price": f"{product.price_paise / 100:.2f}",
        "mrp": f"{product.mrp_paise / 100:.2f}" if product.mrp_paise is not None else "", "cost": f"{product.cost_paise / 100:.2f}" if product.cost_paise is not None else "",
        "tax_rate": f"{product.tax_rate_bp / 100:g}", "hsn": product.hsn_code or "", "unit": product.pricing_unit_label, "sale_mode": product.sale_mode,
        "min_stock": f"{product.min_stock.normalize():f}", "reorder_qty": f"{product.reorder_qty.normalize():f}", "quick_pick": "1" if product.quick_pick else "0", "tags": ", ".join(tags),
    }


def build_editor(services: AppServices, texts: UiTexts, *, product_id: int | None) -> EditorModel:
    """New (empty form) or existing product."""
    product = services.catalog.get(product_id) if product_id else None
    tags = services.catalog.tags_of(product.id) if product else []
    form = _form_of(product, tags) if product else _blank_form()
    barcodes = services.catalog.barcodes(product.id) if product else []
    points = services.prices.chart_points(product.id, field="PRICE", window="1y") if product else []
    labels = {k: texts.t(f"products.{k}") for k in ("editor_new", "editor_edit", "save", "name", "name_hi", "sku", "category", "price", "mrp", "cost", "tax_rate", "hsn", "unit", "sale_mode", "min_stock", "reorder_qty", "quick_pick", "tags", "barcodes", "add_barcode", "scan_hint", "reason", "price_history", "print_label", "archive")}
    return EditorModel(product, form, barcodes, tags, services.catalog.categories(), [dict(u) for u in UNITS], SALE_MODES, points, labels)


def validate_form(form: dict[str, str]) -> dict[str, str]:
    """Field → plain problem."""
    problems: dict[str, str] = {}
    if not (form.get("name") or "").strip():
        problems["name"] = "required"
    for key in ("price", "mrp", "cost"):
        value = (form.get(key) or "").strip()
        if key == "price" and not value:
            problems[key] = "required"
            continue
        if value:
            try:
                parse_inr(value)
            except MoneyError:
                problems[key] = "not an amount"
    try:
        rate_to_bp((form.get("tax_rate") or "0").rstrip("%"))
    except Exception:  # noqa: BLE001
        problems["tax_rate"] = "not a percentage"
    for key in ("min_stock", "reorder_qty"):
        value = (form.get(key) or "0").strip() or "0"
        try:
            if Decimal(value) < 0:
                problems[key] = "must not be negative"
        except InvalidOperation:
            problems[key] = "not a number"
    if (form.get("sale_mode") or "PIECE") not in dict(SALE_MODES):
        problems["sale_mode"] = "unknown"
    return problems


@dataclass(frozen=True)
class SaveResult:
    """Outcome of :func:`save_editor`."""

    ok: bool
    product_id: int | None = None
    problems: dict[str, str] = field(default_factory=dict)
    error_code: str | None = None
    message: str = ""


def _unit_for(label: str, sale_mode: str) -> tuple[str, Decimal]:
    unit = next((u for u in UNITS if u["label"] == label), None)
    if unit is None:
        return (dict(SALE_MODES)[sale_mode], Decimal("1"))
    return (str(unit["base_unit"]), Decimal(str(unit["factor"])))


def save_editor(services: AppServices, actor: Actor, *, product_id: int | None, row_version: int | None, form: dict[str, str]) -> SaveResult:
    """Create or update from the typed form."""
    problems = validate_form(form)
    if problems:
        return SaveResult(False, problems=problems)
    sale_mode = form.get("sale_mode") or "PIECE"
    base_unit, factor = _unit_for(form.get("unit") or "", sale_mode)
    values: dict[str, Any] = {
        "name": form["name"].strip(), "name_hi": (form.get("name_hi") or "").strip() or None, "category_id": int(form["category_id"]) if (form.get("category_id") or "").strip() else None,
        "price_paise": parse_inr(form["price"]), "mrp_paise": parse_inr(form["mrp"]) if (form.get("mrp") or "").strip() else None, "cost_paise": parse_inr(form["cost"]) if (form.get("cost") or "").strip() else None,
        "tax_rate_bp": rate_to_bp((form.get("tax_rate") or "0").rstrip("%")), "hsn_code": (form.get("hsn") or "").strip() or None, "unit": base_unit, "sale_mode": sale_mode, "base_unit": base_unit,
        "pricing_unit_qty": factor, "pricing_unit_label": form.get("unit") or dict(SALE_MODES)[sale_mode], "min_stock": Decimal((form.get("min_stock") or "0") or "0"), "reorder_qty": Decimal((form.get("reorder_qty") or "0") or "0"),
        "quick_pick": (form.get("quick_pick") or "0") in ("1", "true", "True"), "allow_decimal_qty": sale_mode != "PIECE",
    }
    tags = [t.strip() for t in (form.get("tags") or "").replace(";", ",").split(",") if t.strip()]
    reason = (form.get("reason") or "").strip() or None
    try:
        if product_id is None:
            product = services.catalog.create(actor, ProductInput(**values, sku=(form.get("sku") or "").strip() or None, manufacturer_barcode=(form.get("manufacturer_barcode") or "").strip() or None, tags=tags), reason=reason)
            return SaveResult(True, product.id)
        if row_version is None:
            raise ValueError("row_version required for an update")
        if (form.get("sku") or "").strip():
            values["sku"] = form["sku"].strip().upper()
        services.catalog.update(actor, product_id, row_version=row_version, changes=values, reason=reason)
        services.catalog.set_tags(actor, product_id, tags)
        code = (form.get("manufacturer_barcode") or "").strip()
        if code:
            services.catalog.add_barcode(actor, product_id, code, kind="MANUFACTURER")
        return SaveResult(True, product_id)
    except PermissionDenied as exc:
        return SaveResult(False, error_code=exc.code, message=str(exc))
    except VaishnaviError as exc:
        log.info("PRODUCT_SAVE_REFUSED code=%s %s", exc.code, exc)
        return SaveResult(False, error_code=exc.code, message=exc.user_message())
    except ValueError as exc:
        return SaveResult(False, problems={"form": str(exc)})


# ----------------------------------------------------------------------------- price history pop-up
@dataclass(frozen=True)
class HistoryRow:
    """One change."""

    when_text: str
    field: str
    old_text: str
    new_text: str
    by: str
    reason: str
    source: str


@dataclass(frozen=True)
class PriceHistoryModel:
    """Pop-up content."""

    product_id: int
    window_label: str
    rows: list[HistoryRow]
    chart: dict[str, Any]
    labels: dict[str, str] = field(default_factory=dict)


def _value_text(field_name: str, value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 100:g} %" if field_name == "TAX_RATE" else format_inr(value)


def build_price_history(services: AppServices, texts: UiTexts, product_id: int, *, field_name: str = "PRICE", window: str | None = "1y", since: datetime | None = None, until: datetime | None = None) -> PriceHistoryModel:
    """Rows + an ECharts step-line spec for the chosen window (1m / 6m / 1y / custom dates)."""
    rows = services.prices.history(product_id, field=field_name, window=window, since=since, until=until)  # type: ignore[arg-type]
    names = services.users.display_names()
    out = [HistoryRow(format_ist(parse_iso_utc(r.effective_from)), r.field, _value_text(r.field, r.old_value), _value_text(r.field, r.new_value), names.get(r.changed_by or -1, "—"), r.reason or "", r.source) for r in rows]
    points = services.prices.chart_points(product_id, field=field_name, window=window, since=since, until=until)  # type: ignore[arg-type]
    chart = {
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "time"},
        "yAxis": {"type": "value", "name": "₹" if field_name != "TAX_RATE" else "%"},
        "series": [{"type": "line", "step": "end", "data": [[t, (v / 100)] for t, v in points], "lineStyle": {"color": "#F57C00"}, "itemStyle": {"color": "#F57C00"}}],
    }
    label = "custom" if (since is not None or until is not None) else (window or "all")
    labels = {k: texts.t(f"products.{k}") for k in ("price_history", "window_1m", "window_6m", "window_1y", "window_custom", "from", "to")}
    return PriceHistoryModel(product_id, label, out, chart, labels)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/products`` and ``/products/<id>``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.core.permissions import Role  # noqa: PLC0415
    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services: AppServices = state.extra["services"]

    @ui.page("/products")
    def _grid(q: str = "", category: str = "", page: int = 1) -> None:
        client = load_client(app)
        decision = guards.decide("/products", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/products"))
            return
        texts = UiTexts.load(client.lang)
        can_edit = client.role is Role.OWNER
        with frame(state, current_path="/products"):
            model = build_grid(services, texts, query=q, category_id=int(category) if category else None, page=page)
            with ui.row().classes("items-end gap-2 w-full"):
                ui.label(model.labels["title"]).classes("text-xl font-semibold")
                search = ui.input(model.labels["search"], value=q).props("clearable").classes("w-64")
                search.on("keydown.enter", lambda _e: ui.navigate.to(f"/products?q={search.value or ''}&category={category}"))
                ui.select({"": model.labels["all"], **{str(c.id): c.name for c in model.categories}}, value=category, label=model.labels["category"], on_change=lambda e: ui.navigate.to(f"/products?q={q}&category={e.value or ''}")).classes("w-56")
                ui.element("div").classes("flex-1")
                if can_edit:
                    ui.button(model.labels["new"], icon="add", on_click=lambda: ui.navigate.to("/products/new")).props("unelevated color=primary")
                    ui.button(model.labels["import"], icon="upload", on_click=lambda: ui.navigate.to("/products/import")).props("outline")
                    ui.button(model.labels["export"], icon="download", on_click=lambda: ui.download.content(services.import_export.export_csv().encode("utf-8"), "products.csv")).props("outline")
                    ui.button(model.labels["labels"], icon="label", on_click=lambda: ui.navigate.to("/labels")).props("outline")
            columns = [{"name": k, "label": texts.t(f"products.{k}") if texts.has(f"products.{k}") else k, "field": k, "sortable": True} for k in ("sku", "name", "name_hi", "category", "price_text", "stock_text", "barcode")]
            table = ui.table(columns=columns, rows=[r.__dict__ for r in model.rows], row_key="id", pagination=model.page_size).classes("w-full")
            if can_edit:
                table.on("rowClick", lambda e: ui.navigate.to(f"/products/{e.args[1]['id']}"))
            ui.label(f"{model.total}").classes("text-xs text-gray-500")

    @ui.page("/products/import")
    def _import() -> None:
        client = load_client(app)
        decision = guards.decide("/products", client)
        if decision is not guards.Decision.ALLOW or client.role is not Role.OWNER:
            ui.navigate.to(guards.redirect_for(guards.Decision.FORBIDDEN, "/products"))
            return
        texts = UiTexts.load(client.lang)
        holder: dict[str, Any] = {"preview": None}
        with frame(state, current_path="/products"):
            ui.label(texts.t("products.import")).classes("text-xl font-semibold")
            ui.label(texts.t("products.import_hint")).classes("text-sm text-gray-600")
            ui.button(texts.t("products.import_template"), icon="download", on_click=lambda: ui.download.content(("\ufeffsku,name,name_hi,category,price,mrp,cost,tax_rate,hsn,unit,sale_mode,barcode,opening_stock,min_stock,tags\n").encode("utf-8"), "products-template.csv")).props("flat")
            result_box = ui.column().classes("w-full gap-2")

            async def on_upload(e: Any) -> None:
                raw = await e.file.read()  # NiceGUI >= 3: UploadEventArguments.file (async reader)
                text = raw.decode("utf-8", errors="replace")
                preview = services.import_export.preview_csv(text)
                holder["preview"] = preview
                result_box.clear()
                with result_box:
                    ui.label(texts.t("products.import_preview", ok=preview.ok_count, errors=preview.error_count)).classes("font-semibold")
                    bad = [{"line": r.line_no, "name": r.raw.get("name", ""), "errors": "; ".join(r.errors)} for r in preview.rows if not r.ok]
                    if bad:
                        ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("line", "name", "errors")], rows=bad).classes("w-full text-xs")

                    def apply() -> None:
                        res = services.import_export.apply(client.actor(), preview)
                        ui.notify(texts.t("products.import_done", created=res.created, updated=res.updated, skipped=res.skipped), type="positive")
                        ui.navigate.to("/products")

                    ui.button(texts.t("products.import_apply", count=preview.ok_count), on_click=apply).props("unelevated color=primary")

            ui.upload(label="CSV", auto_upload=True, on_upload=on_upload).props("accept=.csv").classes("w-96")

    @ui.page("/products/{product_id}")
    def _editor(product_id: str) -> None:
        client = load_client(app)
        decision = guards.decide("/products", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/products"))
            return
        if client.role is not Role.OWNER:  # staff see the grid read-only; the editor is Owner-only
            ui.navigate.to(guards.redirect_for(guards.Decision.FORBIDDEN, "/products"))
            return
        texts = UiTexts.load(client.lang)
        pid = None if product_id == "new" else int(product_id)
        with frame(state, current_path="/products"):
            model = build_editor(services, texts, product_id=pid)
            form = dict(model.form)
            ui.label(model.labels["editor_edit"] if model.product else model.labels["editor_new"]).classes("text-xl font-semibold")
            with ui.grid(columns=2).classes("w-full gap-2 max-w-4xl"):
                for key in ("name", "name_hi", "sku", "hsn"):
                    ui.input(model.labels[key]).bind_value(form, key)
                ui.select({"": "—", **{str(c.id): c.name for c in model.categories}}, label=model.labels["category"]).bind_value(form, "category_id")
                ui.select({m: m for m, _ in model.sale_modes}, label=model.labels["sale_mode"]).bind_value(form, "sale_mode")
                ui.select({u["label"]: u["label"] for u in model.units}, label=model.labels["unit"]).bind_value(form, "unit")
                for key in ("price", "mrp", "cost", "tax_rate", "min_stock", "reorder_qty"):
                    ui.input(model.labels[key]).bind_value(form, key).props("inputmode=decimal")
                ui.input(model.labels["tags"]).bind_value(form, "tags")
                ui.input(model.labels["reason"]).bind_value(form, "reason")
            if model.product:
                ui.label(model.labels["barcodes"]).classes("font-semibold mt-2")
                for b in model.barcodes:
                    ui.label(f"{b.barcode} · {b.kind}" + (f" ×{b.pack_qty.normalize():f}" if b.kind == "PACK" else "")).classes("vsm-mono text-sm")
                ui.input(model.labels["add_barcode"], placeholder=model.labels["scan_hint"]).bind_value(form, "manufacturer_barcode").classes("w-80")

            def save() -> None:
                result = save_editor(services, client.actor(), product_id=pid, row_version=model.product.row_version if model.product else None, form=form)
                if result.ok:
                    ui.notify(texts.t("common.saved"), type="positive")
                    ui.navigate.to(f"/products/{result.product_id}")
                else:
                    ui.notify(result.message or "; ".join(f"{k}: {v}" for k, v in result.problems.items()), type="negative")

            with ui.row().classes("gap-2 mt-3"):
                ui.button(model.labels["save"], on_click=save).props("unelevated color=primary")
                if model.product:
                    ui.button(model.labels["price_history"], icon="show_chart", on_click=lambda: _history_dialog(services, texts, pid)).props("outline")  # type: ignore[arg-type]
                    ui.button(model.labels["print_label"], icon="label", on_click=lambda: ui.navigate.to(f"/labels?ids={pid}")).props("outline")
                    ui.button(model.labels["archive"], icon="archive", on_click=lambda: (services.catalog.archive(client.actor(), pid), ui.navigate.to("/products"))).props("flat color=negative")  # type: ignore[arg-type]


def _history_dialog(services: AppServices, texts: UiTexts, product_id: int) -> None:
    from nicegui import ui  # noqa: PLC0415

    with ui.dialog() as dialog, ui.card().classes("vsm-card p-4").style("width: 720px"):
        body = ui.column().classes("w-full")

        def render(window: str) -> None:
            body.clear()
            m = build_price_history(services, texts, product_id, window=window)  # type: ignore[arg-type]
            with body:
                with ui.row().classes("gap-1"):
                    for w in ("1m", "6m", "1y", "all"):
                        ui.button(m.labels.get(f"window_{w}", w), on_click=lambda _e, w=w: render(w)).props("flat dense" + (" color=primary" if w == window else ""))
                ui.echart(m.chart).classes("w-full h-64")
                ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("when_text", "field", "old_text", "new_text", "by", "reason", "source")], rows=[r.__dict__ for r in m.rows]).classes("w-full text-xs")
                ui.button(texts.t("common.close"), on_click=dialog.close).props("flat")

        render("1y")
    dialog.open()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
