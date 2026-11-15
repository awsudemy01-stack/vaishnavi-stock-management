"""Settings → Hardware (docs/09 §10, docs/11 §1, §3): printer discovery + selection + Test print; scanner pairing.

View-model first. OS probing is confined to ``platform/`` — the two thin wrappers here (``discover_printers``,
``list_usb_devices``) exist so tests can patch them. Saving goes through ``SettingsService`` (Owner-only,
audited) so the choice survives restarts and shows on the status chips at the next hardware poll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vaishnavi.api.system import AppState
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.core.settings import Settings
from vaishnavi.platform import devices as platform_devices
from vaishnavi.platform import printing as platform_printing
from vaishnavi.services.labels import LabelModel
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_hardware")


@dataclass(frozen=True)
class PrinterOption:
    """One discovered printer."""

    kind: str  # usb | network | system
    address: str
    label: str
    ready: bool
    detail: str = ""


@dataclass(frozen=True)
class UsbOption:
    """One USB device (for scanner pairing)."""

    vid_pid: str
    name: str


@dataclass(frozen=True)
class SelectedPrinter:
    """What is configured now."""

    kind: str
    address: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class ScannerView:
    """Pairing state."""

    paired: bool
    vid_pid: str
    present: bool
    name: str
    suffix: str


@dataclass(frozen=True)
class HardwareModel:
    """The tab."""

    printers: list[PrinterOption]
    selected_printer: SelectedPrinter
    usb_devices: list[UsbOption]
    scanner: ScannerView
    scale_port: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a save."""

    ok: bool
    message: str = ""
    error_code: str | None = None


# ----------------------------------------------------------------------------- probes (patchable)
def discover_printers(settings: Settings) -> list[PrinterOption]:
    """OS printers + the configured network printer (probed) — never raises."""
    out: list[PrinterOption] = []
    try:
        for p in platform_printing.list_system_printers():
            out.append(PrinterOption("system", p.name, f"{p.name} (system)", p.ready, p.detail))
    except Exception as exc:  # noqa: BLE001
        log.debug("printer discovery failed: %s", exc)
    hw = settings.hardware
    if hw.printer_kind == "network" and hw.printer_address:
        ready = platform_printing.network_printer_ready(hw.printer_address)
        out.append(PrinterOption("network", hw.printer_address, f"{hw.printer_address} (network)", ready, "" if ready else "no answer on port 9100"))
    return out


def list_usb_devices() -> list[UsbOption]:
    """USB devices by VID:PID — never raises."""
    try:
        return [UsbOption(d.vid_pid, d.name or d.vid_pid) for d in platform_devices.list_usb_devices()]
    except Exception as exc:  # noqa: BLE001
        log.debug("usb listing failed: %s", exc)
        return []


# ----------------------------------------------------------------------------- model
def build_hardware_model(state: AppState, texts: UiTexts) -> HardwareModel:
    """Everything the tab shows (probes run here, once per render)."""
    services = state.extra["services"]
    settings = services.settings_service.effective()
    hw = settings.hardware
    printers = discover_printers(settings)
    status = platform_printing.printer_status(hw) if hw.printer_kind != "none" else None
    selected = SelectedPrinter(hw.printer_kind, hw.printer_address, bool(status and status.ready), status.detail if status else "")
    usb = list_usb_devices()
    paired = bool(hw.scanner_vid_pid)
    match = next((u for u in usb if u.vid_pid.upper() == hw.scanner_vid_pid.upper()), None) if paired else None
    scanner = ScannerView(paired, hw.scanner_vid_pid, match is not None, match.name if match else "", hw.scanner_suffix)
    labels = {k: texts.t(f"hardware.{k}") for k in ("title", "printer", "refresh", "select", "none", "paper", "test_print", "scanner", "pair", "unpair", "present", "absent", "unpaired", "usb_devices", "scale", "saved")}
    return HardwareModel(printers, selected, usb, scanner, hw.scale_port, labels)


# ----------------------------------------------------------------------------- actions
def _save(state: AppState, actor: Actor, changes: dict[str, Any]) -> ActionResult:
    services = state.extra["services"]
    try:
        services.settings_service.set_many(actor, changes)
    except PermissionDenied as exc:
        return ActionResult(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return ActionResult(False, exc.user_message(), exc.code)
    log.info("HARDWARE_SETTINGS_SAVED keys=%s by=%s", ",".join(sorted(changes)), actor.user_id)
    return ActionResult(True)


def select_printer(state: AppState, actor: Actor, *, kind: str, address: str, paper_mm: int = 80) -> ActionResult:
    """Pick (or clear with ``kind='none'``) the receipt/label printer (``file`` = write jobs to a file, for testing)."""
    if kind not in ("none", "usb", "network", "system", "file"):
        return ActionResult(False, "unknown printer kind")
    changes = {"hardware.printer_kind": kind, "hardware.printer_address": address if kind != "none" else "", "receipt.paper_width_mm": int(paper_mm)}
    return _save(state, actor, changes)


def set_require_printer(state: AppState, actor: Actor, *, on: bool) -> ActionResult:
    """*Require printed receipt* — may only be switched **on** while a printer is configured and ready (docs/05 §10)."""
    services = state.extra["services"]
    if on and not services.hardware.may_enable_require_printer():
        log.info("REQUIRE_PRINTER_REFUSED reason=printer_not_ready by=%s", actor.user_id)
        return ActionResult(False, "a configured, ready printer is needed before receipts can be made mandatory", "E-PRN-002")
    return _save(state, actor, {"billing.require_printer": bool(on)})


@dataclass(frozen=True)
class OverrideView:
    """An active override row."""

    kind: str
    reason: str
    owner_user_id: int
    ends_text: str


@dataclass(frozen=True)
class OverrideModel:
    """Owner overrides block."""

    active: list[OverrideView]
    require_scan: bool
    require_printer: bool
    scanner_ok: bool
    printer_ready: bool
    labels: dict[str, str] = field(default_factory=dict)


def build_override_model(state: AppState, texts: UiTexts) -> OverrideModel:
    """Current overrides + the strictness switches."""
    from vaishnavi.core.clock import format_ist, parse_iso_utc  # noqa: PLC0415

    services = state.extra["services"]
    settings = services.settings_service.effective()
    hw_state = services.hardware.state()
    active = []
    for kind in ("PRINTER", "SCANNER"):
        rec = services.hardware.active_override(kind)
        if rec is not None:
            active.append(OverrideView(rec.kind, rec.reason or "", rec.owner_user_id, format_ist(parse_iso_utc(rec.expires_at), "%H:%M")))
    labels = {k: texts.t(f"hardware.{k}") for k in ("title", "saved")}
    return OverrideModel(active, settings.billing.require_scan, settings.billing.require_printer, hw_state.scanner_ok, hw_state.printer_ready, labels)


def start_override(state: AppState, actor: Actor, *, kind: str, minutes: int, reason: str, approver_id: int | None = None) -> ActionResult:
    """Owner (or PIN) allows selling without the device for a while."""
    services = state.extra["services"]
    try:
        services.hardware.start_override(actor, kind, minutes=minutes, reason=reason, approver_id=approver_id)
    except (PermissionDenied, ValueError) as exc:
        return ActionResult(False, str(exc), getattr(exc, "code", None))
    except Exception as exc:  # noqa: BLE001 - PinRequired and friends
        return ActionResult(False, str(exc), getattr(exc, "code", None))
    return ActionResult(True)


def end_override(state: AppState, actor: Actor, *, kind: str) -> ActionResult:
    """End early."""
    services = state.extra["services"]
    try:
        services.hardware.end_override(actor, kind)
    except PermissionDenied as exc:
        return ActionResult(False, str(exc), exc.code)
    return ActionResult(True)


def pair_scanner(state: AppState, actor: Actor, *, vid_pid: str) -> ActionResult:
    """Pair by VID:PID (normalised upper-case)."""
    normalised = platform_devices.normalise_vid_pid(vid_pid)
    if not normalised:
        return ActionResult(False, "not a VID:PID")
    return _save(state, actor, {"hardware.scanner_vid_pid": normalised})


def unpair_scanner(state: AppState, actor: Actor) -> ActionResult:
    """Forget the paired scanner (staff billing pauses until a scanner is paired again)."""
    return _save(state, actor, {"hardware.scanner_vid_pid": ""})


def test_print_payload(state: AppState, *, paper_mm: int = 80) -> bytes:
    """A sample receipt header + a sample label, as ESC/POS bytes."""
    from vaishnavi.hardware.labels import render_escpos  # noqa: PLC0415 - ui may import hardware
    from vaishnavi.hardware.receipts import to_printer_text  # noqa: PLC0415

    settings = state.extra["services"].settings_service.effective()
    shop = settings.shop
    out = bytearray(b"\x1b@\x1ba\x01\x1b!\x18")
    out += to_printer_text(shop.name).encode("ascii", "replace") + b"\n\x1b!\x00"
    if shop.address_line1:
        out += to_printer_text(shop.address_line1).encode("ascii", "replace") + b"\n"
    out += b"TEST PRINT OK\n" + ("Paper %d mm" % paper_mm).encode("ascii") + b"\n\n"
    sample = LabelModel(name="Sample label", name_hi="नमूना", sku="TEST-0001", product_id=1, barcode="2900000000018", price_paise=1000, mrp_paise=None)
    out += render_escpos(sample, paper_mm=paper_mm)
    return bytes(out)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """The Hardware tab body inside Settings."""
    from nicegui import ui  # noqa: PLC0415

    box = ui.column().classes("w-full gap-3")

    def render() -> None:
        m = build_hardware_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["printer"]).classes("font-semibold")
            ui.label(f"{m.selected_printer.kind} {m.selected_printer.address} · {'ready' if m.selected_printer.ready else m.selected_printer.detail or '-'}").classes("text-sm vsm-mono")
            choice = {"value": f"{m.selected_printer.kind}|{m.selected_printer.address}", "paper": 80}
            options = {"none|": m.labels["none"], **{f"{p.kind}|{p.address}": p.label + ("" if p.ready else f" — {p.detail}") for p in m.printers}}
            with ui.row().classes("gap-2 items-end"):
                ui.select(options, label=m.labels["select"]).bind_value(choice, "value").classes("w-96")
                ui.select({58: "58 mm", 80: "80 mm"}, value=80, label=m.labels["paper"]).bind_value(choice, "paper")

                def save() -> None:
                    kind, _, address = str(choice["value"]).partition("|")
                    result = select_printer(state, client.actor(), kind=kind, address=address, paper_mm=int(choice["paper"]))
                    ui.notify(m.labels["saved"] if result.ok else result.message, type="positive" if result.ok else "negative")
                    render()

                ui.button(m.labels["select"], on_click=save).props("unelevated color=primary")
                ui.button(m.labels["refresh"], icon="refresh", on_click=render).props("flat")
                def test_print() -> None:
                    from vaishnavi.hardware.printer.escpos import transport_for  # noqa: PLC0415 - ui may import hardware

                    payload = test_print_payload(state, paper_mm=int(choice["paper"]))
                    transport = transport_for(state.extra["services"].settings_service.effective().hardware)
                    if transport is None:
                        ui.download(payload, "test-print.escpos")
                        return
                    try:
                        transport.send(payload)
                        ui.notify(m.labels["saved"], type="positive")
                    except VaishnaviError as exc:
                        ui.notify(f"{exc.user_message()} {exc.what_to_do()}", type="negative")

                ui.button(m.labels["test_print"], icon="print", on_click=test_print).props("outline")
            ov = build_override_model(state, texts)
            with ui.row().classes("items-center gap-3 mt-2"):
                sw = ui.switch("Require printed receipt (strict)", value=ov.require_printer)

                def toggle(e: Any) -> None:
                    result = set_require_printer(state, client.actor(), on=bool(e.value))
                    if not result.ok:
                        ui.notify(result.message, type="warning")
                    render()

                sw.on_value_change(toggle)
                ui.label("Printer ready" if ov.printer_ready else "Printer not ready").classes("text-xs " + ("text-green-700" if ov.printer_ready else "text-amber-800"))
            if ov.active:
                for o in ov.active:
                    with ui.row().classes("items-center gap-2 text-sm"):
                        ui.label(f"Override {o.kind} until {o.ends_text} · {o.reason}").classes("text-amber-800")
                        ui.button("End now", on_click=lambda _e, k=o.kind: (end_override(state, client.actor(), kind=k), render())).props("flat dense color=negative")
            form = {"kind": "PRINTER", "minutes": 60, "reason": ""}
            with ui.row().classes("gap-2 items-end"):
                ui.select({"PRINTER": "Sell without printer", "SCANNER": "Sell without scanner"}, value="PRINTER").bind_value(form, "kind")
                ui.number("Minutes", value=60, min=1, max=720).bind_value(form, "minutes")
                ui.input("Reason").bind_value(form, "reason").classes("w-64")

                def start() -> None:
                    result = start_override(state, client.actor(), kind=str(form["kind"]), minutes=int(form["minutes"] or 0), reason=str(form["reason"]))
                    ui.notify(m.labels["saved"] if result.ok else result.message, type="positive" if result.ok else "negative")
                    render()

                ui.button("Allow for a while", on_click=start).props("outline color=warning")
            ui.label(m.labels["scanner"]).classes("font-semibold mt-3")
            if m.scanner.paired:
                ui.label(f"{m.scanner.vid_pid} · {m.labels['present'] if m.scanner.present else m.labels['absent']} {m.scanner.name}").classes("text-sm vsm-mono")
                ui.button(m.labels["unpair"], on_click=lambda: (unpair_scanner(state, client.actor()), render())).props("flat color=negative")
            else:
                ui.label(m.labels["unpaired"]).classes("text-amber-800 text-sm")
            pick = {"vid_pid": ""}
            with ui.row().classes("gap-2 items-end"):
                ui.select({u.vid_pid: f"{u.name} ({u.vid_pid})" for u in m.usb_devices}, label=m.labels["usb_devices"]).bind_value(pick, "vid_pid").classes("w-96")
                ui.button(m.labels["pair"], on_click=lambda: (pair_scanner(state, client.actor(), vid_pid=str(pick["vid_pid"])), render())).props("unelevated color=primary")

    render()
