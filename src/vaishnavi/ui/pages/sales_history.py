"""``/sales`` — bills history, one bill's page (reprint · void · refund), the open-item review queue (docs/05 §7, §8c, docs/09 §4).

Staff see their own bills; the Owner sees everyone's and can filter by cashier. A void or refund by a
cashier needs the Owner PIN; the credit note prints like any receipt. View-models are framework-free
and tested; NiceGUI rendering is thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from typing import Any, Final

from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.errors import BillingError, VaishnaviError
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Permission, PinRequired, Role, grant_for, Grant
from vaishnavi.db.records import SaleView
from vaishnavi.services.billing import CheckoutResult, RefundLine
from vaishnavi.services.measure import QuantitySpec, format_qty
from vaishnavi.services.receipt import ReceiptModel
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.sales_history")

VOID_REASONS: Final = ("wrong_item", "customer_changed_mind", "damaged", "duplicate_bill", "price_error", "other")
DOT: Final = " \u00b7 "


# ----------------------------------------------------------------------------- history list
@dataclass(frozen=True)
class HistoryRow:
    """One bill in the list."""

    sale_id: int
    invoice_no: str
    time_text: str
    cashier: str
    lines: int
    total_text: str
    modes: str
    status: str
    markers: str
    printed: bool
    has_credit_notes: bool = False


@dataclass(frozen=True)
class HistoryModel:
    """The list page."""

    rows: list[HistoryRow]
    query: str
    can_filter_cashier: bool
    cashiers: dict[int, str]
    labels: dict[str, str] = field(default_factory=dict)


def _row(sale: SaleView, names: dict[int, str], *, has_notes: bool = False) -> HistoryRow:
    markers = ("\u2020" if any(i.entry_method == "LOOKUP" for i in sale.items) else "") + ("*" if any(i.is_open_item for i in sale.items) else "")
    modes = DOT.join(sorted({p.mode for p in sale.payments}))
    return HistoryRow(sale.id, sale.invoice_no, format_ist(parse_iso_utc(sale.sold_at), "%d-%b %H:%M"), names.get(sale.user_id, f"#{sale.user_id}"), len(sale.items), format_inr(sale.total_paise), modes, sale.status, markers, sale.receipt_printed, has_notes)


def build_history(c: Any, texts: UiTexts, actor: Actor, *, query: str = "", cashier_id: int | None = None, limit: int = 100) -> HistoryModel:
    """Newest first; staff are scoped to their own bills."""
    names = c.users.display_names()
    scope = actor.user_id if actor.role is not Role.OWNER else cashier_id
    sales = c.billing.search_sales(query, limit=limit, user_id=scope)
    labels = {k: texts.t(f"sales.{k}") for k in ("title", "search", "all_cashiers", "col_invoice", "col_time", "col_cashier", "col_lines", "col_total", "col_mode", "col_status", "review_queue")}
    return HistoryModel([_row(s, names) for s in sales], query, actor.role is Role.OWNER, names, labels)


# ----------------------------------------------------------------------------- one bill
@dataclass(frozen=True)
class RefundRow:
    """A line the customer may return."""

    sale_item_id: int
    name: str
    max_qty: Decimal
    unit: str
    max_text: str
    is_open_item: bool


@dataclass(frozen=True)
class DetailModel:
    """One bill with its actions."""

    sale: SaleView
    receipt: ReceiptModel
    receipt_text: str
    can_reprint: bool
    reprint_count: int
    can_void: bool
    can_refund: bool
    void_needs_pin: bool
    blocked_reason: str
    refund_lines: list[RefundRow]
    reasons: list[tuple[str, str]]
    related: list[SaleView]
    cashier: str
    labels: dict[str, str] = field(default_factory=dict)


def build_detail(c: Any, texts: UiTexts, actor: Actor, sale_id: int) -> DetailModel | None:
    """None when the bill does not exist or belongs to another cashier (staff)."""
    sale = c.billing.get_sale(sale_id)
    if sale is None or (actor.role is not Role.OWNER and sale.user_id != actor.user_id):
        return None
    model = c.receipts.build(sale)
    from vaishnavi.hardware.receipts import text_lines  # noqa: PLC0415 - ui may import hardware

    text = "\n".join(text_lines(c.receipts.escpos(model, paper_mm=80)))
    info = c.billing.void_status(sale_id, actor=actor)
    blocked = ""
    if info.reason_code:
        blocked = texts.t("sales.blocked_window") if info.reason_code == "E-BILL-090" else texts.t("sales.blocked_done")
    rows = []
    for it in sale.items:
        left = info.refundable.get(it.id, Decimal(0))
        spec = QuantitySpec(sale_mode="PIECE" if it.unit == "pcs" else "WEIGHT", base_unit=it.unit, pricing_unit_qty=it.pricing_unit_qty_snapshot, pricing_unit_label="", min_qty=Decimal(0), qty_step=Decimal(0), presets=(), allow_decimal=True)
        rows.append(RefundRow(it.id, it.open_item_name or it.name_snapshot, left, it.unit, format_qty(spec, left), it.is_open_item))
    names = c.users.display_names()
    reasons = [(texts.t(f"sales.reason_{k}"), k) for k in VOID_REASONS]
    labels = {k: texts.t(f"sales.{k}") for k in ("detail_title", "reprint", "void", "refund", "refund_qty", "reason", "note", "pin_title", "related", "back", "credit_note", "confirm_void")}
    return DetailModel(
        sale=sale, receipt=model, receipt_text=text, can_reprint=sale.status == "COMPLETED" or sale.status in ("VOIDED", "REFUND"), reprint_count=sale.reprint_count,
        can_void=info.can_void, can_refund=info.can_refund, void_needs_pin=grant_for(Permission.VOID_REFUND, actor.role) is Grant.PIN and not actor.pin_verified,
        blocked_reason=blocked, refund_lines=rows, reasons=reasons, related=info.related, cashier=names.get(sale.user_id, f"#{sale.user_id}"), labels=labels,
    )


@dataclass(frozen=True)
class VoidOutcome:
    """Result of a void / refund submit."""

    ok: bool
    result: CheckoutResult | None = None
    error_code: str | None = None
    message: str = ""
    needs_pin: bool = False


def _reason_text(texts: UiTexts | None, reason_key: str, note: str) -> str:
    label = texts.t(f"sales.reason_{reason_key}") if texts is not None and texts.has(f"sales.reason_{reason_key}") else reason_key.replace("_", " ")
    return f"{label} \u2014 {note.strip()}" if note.strip() else label


def do_void(c: Any, actor: Actor, sale_id: int, *, reason_key: str, note: str, approver_id: int | None = None, texts: UiTexts | None = None) -> VoidOutcome:
    """Full void."""
    try:
        result = c.billing.void(actor, sale_id, reason=_reason_text(texts, reason_key, note), approver_id=approver_id)
    except PinRequired as exc:
        return VoidOutcome(False, error_code=exc.code, message=str(exc), needs_pin=True)
    except BillingError as exc:
        return VoidOutcome(False, error_code=exc.code, message=f"{exc.user_message()} {exc.what_to_do()}".strip())
    except (VaishnaviError, ValueError) as exc:
        return VoidOutcome(False, error_code=getattr(exc, "code", None), message=str(exc))
    return VoidOutcome(True, result)


def do_refund(c: Any, actor: Actor, sale_id: int, quantities: dict[str, str], *, reason_key: str, note: str, approver_id: int | None = None, texts: UiTexts | None = None) -> VoidOutcome:
    """Partial refund from ``{sale_item_id: qty_text}`` (blanks / zeros skipped)."""
    lines: list[RefundLine] = []
    try:
        for sid, text in quantities.items():
            if not str(text).strip():
                continue
            qty = Decimal(str(text).strip())
            if qty > 0:
                lines.append(RefundLine(int(sid), qty))
        result = c.billing.refund(actor, sale_id, lines, reason=_reason_text(texts, reason_key, note), approver_id=approver_id)
    except PinRequired as exc:
        return VoidOutcome(False, error_code=exc.code, message=str(exc), needs_pin=True)
    except BillingError as exc:
        return VoidOutcome(False, error_code=exc.code, message=f"{exc.user_message()} {exc.what_to_do()}".strip())
    except (VaishnaviError, ValueError, InvalidOperation) as exc:
        return VoidOutcome(False, error_code=getattr(exc, "code", None), message=str(exc))
    return VoidOutcome(True, result)


def do_reprint(c: Any, actor: Actor, sale_id: int) -> VoidOutcome:
    """Reprint through the queue (counted, audited, limited)."""
    try:
        outcome = c.print_queue.reprint(actor, sale_id)
    except VaishnaviError as exc:
        return VoidOutcome(False, error_code=exc.code, message=f"{exc.user_message()} {exc.what_to_do()}".strip(), needs_pin=exc.code == "E-AUTH-011")
    return VoidOutcome(True, message="printed" if outcome.printed else (outcome.error or "saved"))


# ----------------------------------------------------------------------------- open-item review
@dataclass(frozen=True)
class ReviewModel:
    """The Owner's queue."""

    groups: list[Any]
    categories: list[tuple[int, str]]
    labels: dict[str, str] = field(default_factory=dict)


def build_review(c: Any, texts: UiTexts) -> ReviewModel:
    """Pending groups + categories for the convert form."""
    labels = {k: texts.t(f"sales.{k}") for k in ("review_queue", "convert", "dismiss", "times_sold", "typical_price", "new_name", "new_price", "category")}
    return ReviewModel(c.review.pending(), [(cat.id, cat.name) for cat in c.catalog.categories()], labels)


def convert_group(c: Any, actor: Actor, sale_item_ids: list[int], *, name: str, price_text: str, category_id: int | None) -> VoidOutcome:
    """Convert → product."""
    from vaishnavi.core.money import parse_inr  # noqa: PLC0415

    try:
        product = c.review.convert(actor, sale_item_ids, name=name, price_paise=parse_inr(price_text), category_id=category_id)
    except (VaishnaviError, ValueError) as exc:
        return VoidOutcome(False, error_code=getattr(exc, "code", None), message=str(exc))
    return VoidOutcome(True, message=f"#{product.id} {product.name}")


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/sales``, ``/sales/<id>``, ``/sales/review``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]

    def _guard(client: Any, path: str) -> bool:
        decision = guards.decide(path, client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, path))
            return False
        return True

    @ui.page("/sales")
    def _list(q: str = "", cashier: str = "") -> None:
        client = load_client(app)
        if not _guard(client, "/sales"):
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/sales"):
            m = build_history(services, texts, actor, query=q, cashier_id=int(cashier) if cashier else None)
            with ui.row().classes("items-end gap-2 w-full"):
                ui.label(m.labels["title"]).classes("text-xl font-semibold")
                box = ui.input(m.labels["search"], value=q).props("clearable").classes("w-64")
                box.on("keydown.enter", lambda _e: ui.navigate.to(f"/sales?q={box.value or ''}&cashier={cashier}"))
                if m.can_filter_cashier:
                    ui.select({"": m.labels["all_cashiers"], **{str(k): v for k, v in m.cashiers.items()}}, value=cashier, on_change=lambda e: ui.navigate.to(f"/sales?q={q}&cashier={e.value or ''}")).classes("w-48")
                    ui.element("div").classes("flex-1")
                    ui.button(m.labels["review_queue"], icon="rule", on_click=lambda: ui.navigate.to("/sales/review")).props("outline")
            columns = [{"name": k, "label": m.labels[f"col_{k}"], "field": k} for k in ("invoice", "time", "cashier", "lines", "total", "mode", "status")]
            rows = [{"id": r.sale_id, "invoice": f"{r.invoice_no} {r.markers}".strip(), "time": r.time_text, "cashier": r.cashier, "lines": r.lines, "total": r.total_text, "mode": r.modes, "status": r.status} for r in m.rows]
            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=25).classes("w-full")
            table.on("rowClick", lambda e: ui.navigate.to(f"/sales/{e.args[1]['id']}"))

    @ui.page("/sales/review")
    def _review() -> None:
        client = load_client(app)
        if not _guard(client, "/sales") or client.role is not Role.OWNER:
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/sales"):
            box = ui.column().classes("w-full gap-2")

            def render() -> None:
                m = build_review(services, texts)
                box.clear()
                with box:
                    ui.label(m.labels["review_queue"]).classes("text-xl font-semibold")
                    for g in m.groups:
                        form = {"name": g.name, "price": f"{g.typical_price_paise / 100:.2f}", "cat": None}
                        with ui.row().classes("items-end gap-2 vsm-card p-3 w-full"):
                            with ui.column().classes("gap-0 w-64"):
                                ui.label(g.name).classes("font-semibold")
                                ui.label(f"{m.labels['times_sold']}: {g.count}{DOT}{m.labels['typical_price']}: {format_inr(g.typical_price_paise)}").classes("text-xs text-gray-500")
                            ui.input(m.labels["new_name"]).bind_value(form, "name").classes("w-56")
                            ui.input(m.labels["new_price"]).bind_value(form, "price").classes("w-28")
                            ui.select({cid: name for cid, name in m.categories}, label=m.labels["category"]).bind_value(form, "cat").classes("w-48")
                            ui.button(m.labels["convert"], on_click=lambda _e, gg=g, f=form: (_notify(convert_group(services, actor, gg.sale_item_ids, name=str(f["name"]), price_text=str(f["price"]), category_id=f["cat"])), render())).props("unelevated color=primary")
                            ui.button(m.labels["dismiss"], on_click=lambda _e, gg=g: (services.review.dismiss(actor, gg.sale_item_ids), render())).props("flat")

            def _notify(o: VoidOutcome) -> None:
                ui.notify(o.message, type="positive" if o.ok else "negative")

            render()

    @ui.page("/sales/{sale_id}")
    def _detail(sale_id: int) -> None:
        client = load_client(app)
        if not _guard(client, "/sales"):
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/sales"):
            box = ui.column().classes("w-full gap-2")

            def render() -> None:
                m = build_detail(services, texts, actor, sale_id)
                box.clear()
                with box:
                    if m is None:
                        ui.label(texts.t("common.not_found") if texts.has("common.not_found") else "Not found")
                        return
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/sales")).props("flat")
                        ui.label(f"{m.labels['detail_title']} {m.sale.invoice_no}{DOT}{m.cashier}{DOT}{m.sale.status}").classes("text-xl font-semibold")
                    with ui.row().classes("gap-4 w-full no-wrap items-start"):
                        ui.html(f"<pre class='vsm-mono text-xs vsm-card p-3'>{html_escape(m.receipt_text)}</pre>", sanitize=False)
                        with ui.column().classes("gap-2 flex-1"):
                            ui.button(f"{m.labels['reprint']} ({m.reprint_count})", icon="print", on_click=lambda: (_toast(do_reprint(services, actor, sale_id)), render())).props("outline").set_enabled(m.can_reprint)
                            if m.blocked_reason:
                                ui.label(m.blocked_reason).classes("text-sm text-gray-600")
                            if m.can_void or m.can_refund:
                                form: dict[str, Any] = {"reason": m.reasons[0][1], "note": "", "pin": "", "qty": {str(r.sale_item_id): "" for r in m.refund_lines}}
                                ui.select({k: t for t, k in m.reasons}, label=m.labels["reason"]).bind_value(form, "reason")
                                ui.input(m.labels["note"]).bind_value(form, "note").classes("w-full")
                                pin = ui.input(m.labels["pin_title"], password=True).bind_value(form, "pin").props("inputmode=numeric")
                                pin.set_visibility(m.void_needs_pin)

                                def with_pin() -> tuple[Actor, int | None] | None:
                                    if not m.void_needs_pin:
                                        return actor, None
                                    try:
                                        owner = services.users.verify_owner_pin(str(form["pin"]), on_behalf_of=actor, permission=Permission.VOID_REFUND)
                                    except VaishnaviError as exc:
                                        ui.notify(f"{exc.user_message()} {exc.what_to_do()}", type="negative")
                                        return None
                                    return client.actor(pin_verified=True), owner.user_id

                                if m.can_void:
                                    def void_now() -> None:
                                        who = with_pin()
                                        if who is None:
                                            return
                                        a, approver = who
                                        o = do_void(services, a, sale_id, reason_key=str(form["reason"]), note=str(form["note"]), approver_id=approver, texts=texts)
                                        _toast(o)
                                        render()

                                    ui.button(m.labels["void"], icon="undo", on_click=void_now).props("unelevated color=negative")
                                if m.can_refund:
                                    ui.label(m.labels["refund_qty"]).classes("font-semibold mt-2")
                                    for r in m.refund_lines:
                                        if r.max_qty <= 0:
                                            continue
                                        with ui.row().classes("items-center gap-2"):
                                            ui.label(f"{r.name}{DOT}max {r.max_text}").classes("w-64")
                                            ui.input("qty").bind_value(form["qty"], str(r.sale_item_id)).props("dense inputmode=decimal").classes("w-24")

                                    def refund_now() -> None:
                                        who = with_pin()
                                        if who is None:
                                            return
                                        a, approver = who
                                        o = do_refund(services, a, sale_id, dict(form["qty"]), reason_key=str(form["reason"]), note=str(form["note"]), approver_id=approver, texts=texts)
                                        _toast(o)
                                        render()

                                    ui.button(m.labels["refund"], icon="keyboard_return", on_click=refund_now).props("outline color=negative")
                            if m.related:
                                ui.label(m.labels["related"]).classes("font-semibold mt-2")
                                for n in m.related:
                                    ui.button(f"{m.labels['credit_note']} {n.invoice_no}{DOT}{n.status}{DOT}{format_inr(n.total_paise)}", on_click=lambda _e, nid=n.id: ui.navigate.to(f"/sales/{nid}")).props("flat")

            def _toast(o: VoidOutcome) -> None:
                if o.ok and o.result is not None:
                    ui.notify(f"{o.result.invoice_no}", type="positive")
                else:
                    ui.notify(o.message or "ok", type="positive" if o.ok else "negative")

            render()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
