"""``/shifts`` — open with a float, cash in / out, X report, close by denomination count, Z report (docs/05 §6).

Staff count **blind**: the expected figure appears only after the count is entered (so nobody adjusts the
count to the number on screen). The Owner sees expected cash at any time and can close any shift.
View-models are pure and tested; NiceGUI rendering is thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from typing import Any, Final

from vaishnavi.core.clock import format_ist, parse_iso_utc
from vaishnavi.core.errors import ShiftError, VaishnaviError
from vaishnavi.core.money import format_inr, parse_inr
from vaishnavi.core.permissions import Actor, PinRequired, Role
from vaishnavi.services.shift import DENOMINATIONS, count_denominations
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.shifts")

MINUS: Final = "\u2212"


def _signed(paise: int) -> str:
    return f"{MINUS}{format_inr(-paise)}" if paise < 0 else format_inr(paise)


@dataclass(frozen=True)
class CurrentShift:
    """The open shift card."""

    id: int
    cashier: str
    opened_text: str
    opening_float_text: str
    bills: int
    cash_sales_text: str
    cash_in_text: str
    cash_out_text: str
    expected_text: str | None


@dataclass(frozen=True)
class HistoryRow:
    """A closed shift."""

    id: int
    cashier: str
    opened_text: str
    closed_text: str
    expected_text: str
    counted_text: str
    variance_text: str
    note: str


@dataclass(frozen=True)
class ShiftModel:
    """The page."""

    current: CurrentShift | None
    can_open: bool
    show_expected: bool
    denominations: tuple[int, ...]
    open_shifts: list[CurrentShift]
    history: list[HistoryRow]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionOutcome:
    """Submit result."""

    ok: bool
    message: str = ""
    error_code: str | None = None
    needs_pin: bool = False
    variance_paise: int | None = None
    report_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClosePreview:
    """Live figures while counting."""

    counted_paise: int
    counted_text: str
    expected_text: str | None
    variance_text: str | None
    needs_note: bool


def _current(c: Any, names: dict[int, str], shift: Any, *, show_expected: bool) -> CurrentShift:
    x = c.shifts.x_report(shift.id)
    return CurrentShift(shift.id, names.get(shift.user_id, f"#{shift.user_id}"), format_ist(parse_iso_utc(shift.opened_at), "%d-%b %H:%M"), format_inr(shift.opening_float_paise), x.bills, format_inr(x.by_mode.get("CASH", 0)), format_inr(x.cash_in_paise), format_inr(x.cash_out_paise), format_inr(x.expected_cash_paise) if show_expected else None)


def build_shift_model(c: Any, texts: UiTexts, actor: Actor, *, shift_id: int | None = None) -> ShiftModel:
    """The cashier's shift (or ``shift_id`` for the Owner), everyone's open shifts for the Owner, and history."""
    names = c.users.display_names()
    owner = actor.role is Role.OWNER
    shift = c.shifts.get(shift_id) if shift_id is not None and owner else c.shifts.current(actor.user_id)
    current = _current(c, names, shift, show_expected=owner) if shift is not None and shift.status == "OPEN" else None
    opens = [_current(c, names, s, show_expected=True) for s in c.shifts.open_shifts()] if owner else []
    history = [HistoryRow(s.id, names.get(s.user_id, f"#{s.user_id}"), format_ist(parse_iso_utc(s.opened_at), "%d-%b %H:%M"), format_ist(parse_iso_utc(s.closed_at), "%d-%b %H:%M") if s.closed_at else "", format_inr(s.expected_cash_paise or 0), format_inr(s.counted_cash_paise or 0), _signed(s.variance_paise or 0), s.note or "") for s in c.shifts.variance_history(user_id=None if owner else actor.user_id, limit=50)]
    labels = {k: texts.t(f"shifts.{k}") for k in ("title", "open", "float", "cash_in", "cash_out", "amount", "reason", "x_report", "close", "count", "note", "expected", "counted", "variance", "history", "print", "z_report", "open_shifts", "no_shift",
                                                   "open_count_hint", "float_total", "float_total_hint")}
    return ShiftModel(current, c.shifts.current(actor.user_id) is None, owner, DENOMINATIONS, opens, history, labels)


def open_shift(c: Any, actor: Actor, *, float_text: str = "", denominations: dict[str, str] | None = None) -> ActionOutcome:
    """Count the float into the drawer — note by note when the grid is used, else the typed total (Owner only).

    Staff must fill the grid (type ``0`` in one box for an empty drawer) so an opening float is never assumed.
    """
    try:
        if denominations is not None and any(str(v).strip() for v in denominations.values()):
            c.shifts.open(actor, denominations=_counts(denominations))
        elif actor.role is Role.OWNER:
            c.shifts.open(actor, opening_float_paise=parse_inr(float_text or "0"))
        else:
            return ActionOutcome(False, "Count the notes in the drawer first (type 0 if it is empty).")
    except ShiftError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except (VaishnaviError, ValueError, InvalidOperation) as exc:
        return ActionOutcome(False, str(exc))
    return ActionOutcome(True)


def open_preview_text(denominations: dict[str, str]) -> str:
    """Live total of the opening count grid (``₹0.00`` while nothing/invalid is typed)."""
    try:
        return format_inr(count_denominations(_counts(denominations)))
    except (ValueError, InvalidOperation):
        return format_inr(0)


def cash_in(c: Any, actor: Actor, *, amount_text: str, reason: str) -> ActionOutcome:
    """Add to the drawer."""
    try:
        c.shifts.cash_in(actor, parse_inr(amount_text), reason=reason)
    except (VaishnaviError, ValueError) as exc:
        return ActionOutcome(False, getattr(exc, "user_message", lambda: str(exc))(), getattr(exc, "code", None))
    return ActionOutcome(True)


def cash_out(c: Any, actor: Actor, *, amount_text: str, reason: str, approver_id: int | None = None) -> ActionOutcome:
    """Take out (Owner PIN for staff)."""
    try:
        c.shifts.cash_out(actor, parse_inr(amount_text), reason=reason, approver_id=approver_id)
    except PinRequired as exc:
        return ActionOutcome(False, str(exc), exc.code, needs_pin=True)
    except ShiftError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except (VaishnaviError, ValueError) as exc:
        return ActionOutcome(False, str(exc))
    return ActionOutcome(True)


def x_report_lines(c: Any, actor: Actor, *, shift_id: int | None = None) -> list[str]:
    """Live report text."""
    shift = c.shifts.get(shift_id) if shift_id is not None else c.shifts.current(actor.user_id)
    if shift is None:
        return []
    return c.shifts.report_lines(c.shifts.x_report(shift.id))


def _counts(denominations: dict[str, str]) -> dict[int, int]:
    out: dict[int, int] = {}
    for value, n in denominations.items():
        text = str(n).strip()
        if text:
            out[int(value)] = int(Decimal(text))
    return out


def close_preview(c: Any, actor: Actor, denominations: dict[str, str], *, shift_id: int | None = None) -> ClosePreview:
    """Counted so far (+ expected / variance for the Owner, or once a count is entered)."""
    try:
        counted = count_denominations(_counts(denominations))
    except (ValueError, InvalidOperation):
        counted = 0
    shift = c.shifts.get(shift_id) if shift_id is not None else c.shifts.current(actor.user_id)
    expected = c.shifts.expected_cash(shift.id) if shift is not None else None
    reveal = expected is not None and (actor.role is Role.OWNER or counted > 0)
    variance = counted - expected if expected is not None else None
    return ClosePreview(counted, format_inr(counted), format_inr(expected) if reveal and expected is not None else None, _signed(variance) if reveal and variance is not None else None, bool(variance is not None and variance < 0))


def close_shift(c: Any, actor: Actor, denominations: dict[str, str], *, note: str, shift_id: int | None = None) -> ActionOutcome:
    """Close → variance + Z report lines."""
    shift = c.shifts.get(shift_id) if shift_id is not None else c.shifts.current(actor.user_id)
    if shift is None:
        return ActionOutcome(False, "no open shift", "E-SHIFT-001")
    try:
        closed = c.shifts.close(actor, shift.id, denominations=_counts(denominations), note=note)
    except ShiftError as exc:
        return ActionOutcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    except (VaishnaviError, ValueError, InvalidOperation) as exc:
        return ActionOutcome(False, str(exc))
    lines = c.shifts.report_lines(c.shifts.z_report(closed.id))
    log.info("SHIFT_PAGE_CLOSED shift=%s variance=%s", closed.id, closed.variance_paise)
    return ActionOutcome(True, variance_paise=closed.variance_paise, report_lines=lines)


def print_report(c: Any, actor: Actor, lines: list[str]) -> bool:
    """Send X/Z text to the receipt printer (False when none is ready — the screen copy stays)."""
    return bool(c.print_queue.print_lines(actor, lines))


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/shifts``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.core.permissions import Permission  # noqa: PLC0415
    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.components.pin_dialog import ask_pin  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client, save_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/shifts")
    def _shifts(then_pos: int = 0) -> None:
        # ``?then_pos=1`` (login / Pay send the cashier here first): after the drawer is opened, go straight to /pos.
        client = load_client(app)
        decision = guards.decide("/shifts", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/shifts"))
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/shifts"):
            box = ui.column().classes("w-full gap-3")

        def toast(o: ActionOutcome, ok_text: str = "") -> None:
            ui.notify(ok_text or texts.t("common.saved"), type="positive") if o.ok else ui.notify(o.message, type="negative")

        def render() -> None:
            m = build_shift_model(services, texts, actor)
            box.clear()
            with box:
                ui.label(m.labels["title"]).classes("text-xl font-semibold")
                if m.current is None:
                    ui.label(m.labels["no_shift"]).classes("text-sm text-gray-600")
                    ui.label(m.labels["open_count_hint"]).classes("text-sm")
                    open_counts = {str(d): "" for d in m.denominations}
                    open_total = ui.label(f"{m.labels['float']}: {open_preview_text(open_counts)}").classes("font-semibold text-lg")
                    with ui.grid(columns=5).classes("gap-2"):
                        for d in m.denominations:
                            ui.input(f"₹{d} ×").bind_value(open_counts, str(d)).props("dense inputmode=numeric" + (" autofocus" if d == m.denominations[0] else "")).on("update:model-value", lambda _e: open_total.set_text(f"{m.labels['float']}: {open_preview_text(open_counts)}"))
                    form = {"float": ""}
                    with ui.row().classes("items-end gap-2"):
                        if m.show_expected:
                            ui.input(m.labels["float_total"]).bind_value(form, "float").props("inputmode=decimal").classes("w-40").tooltip(m.labels["float_total_hint"])
                        ui.button(m.labels["open"], icon="lock_open", on_click=lambda: (_after_open(open_shift(services, actor, float_text=str(form["float"]), denominations=open_counts)),)).props("unelevated color=primary size=lg")
                else:
                    cur = m.current
                    with ui.row().classes("gap-6 vsm-card p-3"):
                        ui.label(f"#{cur.id} · {cur.cashier} · {cur.opened_text}").classes("font-semibold")
                        ui.label(f"{m.labels['float']}: {cur.opening_float_text}")
                        ui.label(f"Bills: {cur.bills}")
                        ui.label(f"Cash sales: {cur.cash_sales_text}")
                        ui.label(f"{m.labels['cash_in']}: {cur.cash_in_text} · {m.labels['cash_out']}: {cur.cash_out_text}")
                        if cur.expected_text:
                            ui.label(f"{m.labels['expected']}: {cur.expected_text}").classes("font-bold")
                    cash = {"amount": "", "reason": ""}
                    with ui.row().classes("items-end gap-2"):
                        ui.input(m.labels["amount"]).bind_value(cash, "amount").props("inputmode=decimal").classes("w-32")
                        ui.input(m.labels["reason"]).bind_value(cash, "reason").classes("w-64")
                        ui.button(m.labels["cash_in"], on_click=lambda: (toast(cash_in(services, actor, amount_text=str(cash["amount"]), reason=str(cash["reason"]))), render())).props("outline")

                        def do_cash_out() -> None:
                            o = cash_out(services, actor, amount_text=str(cash["amount"]), reason=str(cash["reason"]))
                            if o.needs_pin:
                                ask_pin(services, client, texts, Permission.CASH_OUT, lambda a, approver: (toast(cash_out(services, a, amount_text=str(cash["amount"]), reason=str(cash["reason"]), approver_id=approver)), render()))
                                return
                            toast(o)
                            render()

                        ui.button(m.labels["cash_out"], on_click=do_cash_out).props("outline color=warning")
                        ui.button(m.labels["x_report"], icon="receipt_long", on_click=lambda: show_lines(x_report_lines(services, actor))).props("flat")
                    ui.label(m.labels["close"]).classes("font-semibold mt-2")
                    counts = {str(d): "" for d in m.denominations}
                    preview_label = ui.label("")
                    note = {"text": ""}
                    with ui.grid(columns=5).classes("gap-2"):
                        for d in m.denominations:
                            ui.input(f"₹{d} ×").bind_value(counts, str(d)).props("dense inputmode=numeric").on("update:model-value", lambda _e: update_preview())

                    def update_preview() -> None:
                        p = close_preview(services, actor, counts)
                        text = f"{m.labels['counted']}: {p.counted_text}"
                        if p.expected_text:
                            text += f" · {m.labels['expected']}: {p.expected_text} · {m.labels['variance']}: {p.variance_text}"
                        preview_label.set_text(text)

                    ui.input(m.labels["note"]).bind_value(note, "text").classes("w-full")

                    def do_close() -> None:
                        o = close_shift(services, actor, counts, note=str(note["text"]))
                        if not o.ok:
                            ui.notify(o.message, type="negative")
                            return
                        client.shift_id = None
                        save_client(app, client)
                        show_lines(o.report_lines, title=m.labels["z_report"])
                        render()

                    ui.button(m.labels["close"], icon="lock", on_click=do_close).props("unelevated color=negative")
                if m.open_shifts and actor.role is Role.OWNER:
                    ui.label(m.labels["open_shifts"]).classes("font-semibold mt-3")
                    for s in m.open_shifts:
                        ui.label(f"#{s.id} · {s.cashier} · {s.opened_text} · {m.labels['expected']}: {s.expected_text}").classes("text-sm")
                ui.label(m.labels["history"]).classes("font-semibold mt-3")
                columns = [{"name": k, "label": m.labels.get(k, k), "field": k} for k in ("cashier", "opened_text", "closed_text", "expected_text", "counted_text", "variance_text", "note")]
                ui.table(columns=columns, rows=[h.__dict__ for h in m.history], row_key="id", pagination=10).classes("w-full text-sm")

        def _after_open(o: ActionOutcome) -> None:
            toast(o)
            if o.ok:
                shift = services.shifts.current(actor.user_id)
                client.shift_id = shift.id if shift else None
                save_client(app, client)
                if then_pos:
                    ui.navigate.to("/pos")
                    return
            render()

        def show_lines(lines: list[str], *, title: str | None = None) -> None:
            with ui.dialog() as dlg, ui.card():
                if title:
                    ui.label(title).classes("font-bold")
                ui.html(f"<pre class='vsm-mono text-xs'>{html_escape(chr(10).join(lines))}</pre>", sanitize=False)
                with ui.row():
                    ui.button(texts.t("shifts.print"), icon="print", on_click=lambda: ui.notify("printed" if print_report(services, actor, lines) else texts.t("pos.done_none"))).props("flat")
                    ui.button(texts.t("common.close") if texts.has("common.close") else "OK", on_click=dlg.close).props("unelevated color=primary")
            dlg.open()

        render()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
