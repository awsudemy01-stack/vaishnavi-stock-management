"""Labels page (docs/11 §2): pick products, copies and target; preview; print; history. View-model first."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vaishnavi.app_services import AppServices
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.services.labels import LabelModel
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.labels")


@dataclass(frozen=True)
class LabelsModel:
    """Page state."""

    preview: list[LabelModel]
    total_labels: int
    copies: int
    target: str
    printer_ready: bool
    printer_text: str
    history: list[dict[str, Any]]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PrintResult:
    """Outcome of the print button."""

    ok: bool
    payload: bytes = b""
    content_type: str = ""
    filename: str = ""
    message: str = ""
    error_code: str | None = None


def resolve_selection(services: AppServices, *, ids: str = "", purchase: str = "") -> list[int]:
    """Product ids for the page: ``?ids=1,2`` as given, ``?purchase=<id>`` → the PIECE products received in that purchase."""
    selected = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if purchase.strip().isdigit():
        pid = int(purchase)
        moves = services.inventory.movements(types=("PURCHASE",), limit=2000)
        for m in moves:
            if m.ref_type == "PURCHASE" and m.ref_id == pid and m.product_id not in selected:
                selected.append(m.product_id)
        log.debug("LABELS_FROM_PURCHASE purchase=%d products=%d", pid, len(selected))
    return selected


def picker_options(services: AppServices) -> dict[int, str]:
    """Active, non-system products for the on-page picker (``id → "SKU · name"``)."""
    return {p.id: f"{p.sku} · {p.name}" for p in services.catalog.list_products(limit=5000).items if not p.is_system}


def build_labels_model(services: AppServices, texts: UiTexts, *, selected: list[int], copies: int, target: str) -> LabelsModel:
    """Preview (one model per product) + totals + printer state + print log."""
    preview = services.labels.models_for(selected, copies=1)
    hw = services.settings_service.effective().hardware
    ready = hw.printer_kind != "none"
    printer_text = f"{hw.printer_kind} {hw.printer_address}".strip() if ready else texts.t("chip.hint.printer.amber")
    labels = {k: texts.t(f"labels.{k}") for k in ("title", "copies", "target", "escpos", "sheet", "print", "preview", "history", "no_printer", "select_hint", "products", "nothing_selected")}
    return LabelsModel(preview, len(preview) * max(1, copies), copies, target, ready, printer_text, services.labels.recent(limit=20), labels)


def print_selected(services: AppServices, actor: Actor, *, selected: list[int], copies: int, target: str, reason: str = "NEW", paper_mm: int = 80, layout: str = "65-up") -> PrintResult:
    """Render the job; the caller sends ESC/POS bytes to the printer or downloads the sheet."""
    if not selected:
        return PrintResult(False, message="nothing selected")
    try:
        job = services.labels.print_job(actor, product_ids=selected, copies=copies, target=target, reason=reason, paper_mm=paper_mm, layout=layout)
    except PermissionDenied as exc:
        return PrintResult(False, message=str(exc), error_code=exc.code)
    except VaishnaviError as exc:
        return PrintResult(False, message=exc.user_message(), error_code=exc.code)
    except ValueError as exc:
        return PrintResult(False, message=str(exc))
    return PrintResult(True, job.payload, job.content_type, job.filename)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/labels?ids=1,2``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services: AppServices = state.extra["services"]

    @ui.page("/labels")
    def _labels(ids: str = "", purchase: str = "") -> None:
        client = load_client(app)
        decision = guards.decide("/products", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/labels"))
            return
        texts = UiTexts.load(client.lang)
        selected = resolve_selection(services, ids=ids, purchase=purchase)
        opts: dict[str, Any] = {"copies": 1, "target": "ESCPOS", "selected": list(selected)}
        log.debug("LABELS_PAGE ids=%r purchase=%r selected=%s", ids, purchase, selected)
        with frame(state, current_path="/products"):
            m = build_labels_model(services, texts, selected=selected, copies=1, target="ESCPOS")
            ui.label(m.labels["title"]).classes("text-xl font-semibold")
            if not selected:
                ui.label(m.labels["select_hint"]).classes("text-gray-500")
            preview_box = ui.column().classes("w-full gap-1")

            def current_ids() -> list[int]:
                return [int(x) for x in (opts["selected"] or [])]

            def render_preview() -> None:
                from vaishnavi.hardware.labels import barcode_svg  # noqa: PLC0415

                pm = build_labels_model(services, texts, selected=current_ids(), copies=int(opts["copies"] or 1), target=str(opts["target"]))
                preview_box.clear()
                with preview_box:
                    ui.label(f"{pm.labels['preview']} · {pm.total_labels}").classes("font-semibold mt-2")
                    with ui.row().classes("gap-2 flex-wrap"):
                        for lm in pm.preview[:24]:
                            with ui.card().classes("vsm-card p-2 items-center").style("width: 190px"):
                                ui.label(lm.name).classes("text-sm font-semibold truncate w-full text-center")
                                ui.label(lm.code_line).classes("text-xs vsm-mono")
                                ui.html(barcode_svg(lm.barcode, width_mm=34, height_mm=9), sanitize=False)
                                ui.label(lm.price_text).classes("font-bold")

            # The owner picks products right here (the grid's "Labels" button arrives with nothing selected).
            ui.select(picker_options(services), multiple=True, with_input=True, label=m.labels["products"], value=list(selected), on_change=lambda e: (opts.__setitem__("selected", e.value or []), render_preview())).classes("w-full").props("use-chips clearable")
            with ui.row().classes("gap-2 items-end"):
                ui.number(m.labels["copies"], value=1, min=1, max=500, on_change=lambda _e: render_preview()).bind_value(opts, "copies")
                ui.select({"ESCPOS": m.labels["escpos"], "PDF": m.labels["sheet"]}, value="ESCPOS", label=m.labels["target"]).bind_value(opts, "target")
                ui.label(m.printer_text).classes("text-sm " + ("text-green-700" if m.printer_ready else "text-amber-700"))

                def go() -> None:
                    chosen = current_ids()
                    if not chosen:
                        ui.notify(m.labels["nothing_selected"], type="warning")
                        return
                    result = print_selected(services, client.actor(), selected=chosen, copies=int(opts["copies"] or 1), target=str(opts["target"]))
                    if not result.ok:
                        ui.notify(result.message, type="negative")
                        return
                    log.info("LABELS_OUTPUT products=%d bytes=%d type=%s file=%s", len(chosen), len(result.payload), result.content_type, result.filename)
                    if result.content_type.startswith("application/octet-stream"):
                        sent = _send_to_printer(state, result.payload)
                        ui.notify(texts.t("labels.sent") if sent else texts.t("labels.no_printer"), type="positive" if sent else "warning")
                        if not sent:
                            ui.download.content(result.payload, result.filename)
                    else:
                        ui.download.content(result.payload, result.filename)

                ui.button(m.labels["print"], icon="print", on_click=go).props("unelevated color=primary")
            render_preview()
            ui.label(m.labels["history"]).classes("font-semibold mt-3")
            ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("at", "sku", "name", "copies", "target", "reason")], rows=m.history).classes("w-full text-xs")


def _send_to_printer(state: Any, payload: bytes) -> bool:
    """Route ESC/POS bytes through the configured printer transport (``hardware/printer/escpos``)."""
    sender = state.extra.get("print_bytes")
    if not callable(sender):
        from vaishnavi.hardware.printer.escpos import transport_for  # noqa: PLC0415 - ui may import hardware

        services = state.extra.get("services")
        hw = services.settings_service.effective().hardware if services is not None else state.settings.hardware
        transport = transport_for(hw)
        if transport is None:
            return False
        sender = transport.send
    try:
        sender(payload)
        return True
    except Exception as exc:  # noqa: BLE001 - the owner gets the file instead
        log.warning("LABEL_PRINT_FAILED %r", exc)
        return False


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
