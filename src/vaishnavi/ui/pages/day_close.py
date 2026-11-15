"""``/shifts/day-close`` — the evening wizard (docs/05 §12, docs/15 B3): close shifts → Z report → backup → digest → finish.

Each step is a pure check against the services (done / not done), each action is a thin call into the
service that owns it, and *Finish* writes one ``DAY_CLOSED`` audit row with what was done, then — when
the owner asks — requests the app to shut down so the PC can be switched off. Owner only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import InvalidOperation
from typing import Any

from vaishnavi.core.clock import format_ist, iso_utc, parse_iso_utc, today_ist
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.money import format_inr, parse_inr
from vaishnavi.core.permissions import Actor, Permission, PermissionDenied, require
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.day_close")


@dataclass(frozen=True)
class Step:
    """Wizard step."""

    key: str
    title: str
    hint: str
    done: bool
    detail: str = ""


@dataclass(frozen=True)
class OpenShiftRow:
    """An open shift to close."""

    id: int
    cashier: str
    opened_text: str
    expected_text: str


@dataclass(frozen=True)
class ZReportView:
    """A closed shift's Z report."""

    shift_id: int
    cashier: str
    variance_text: str
    lines: list[str]


@dataclass(frozen=True)
class DayCloseModel:
    """The page."""

    day: str
    steps: list[Step]
    open_shifts: list[OpenShiftRow]
    z_reports: list[ZReportView]
    backup_text: str
    digest_path: str
    publishing_on: bool
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """Action result."""

    ok: bool
    message: str = ""
    error_code: str | None = None


def _owner(actor: Actor) -> Outcome | None:
    try:
        require(actor, Permission.MANAGE_SYSTEM)
    except PermissionDenied as exc:
        return Outcome(False, str(exc), exc.code)
    return None


def build_day_close(c: Any, state: Any, texts: UiTexts, actor: Actor) -> DayCloseModel:
    """Where the evening stands."""
    require(actor, Permission.MANAGE_SYSTEM)
    today = today_ist().isoformat()
    names = c.users.display_names()
    opens = c.shifts.open_shifts()
    open_rows = [OpenShiftRow(s.id, names.get(s.user_id, f"#{s.user_id}"), format_ist(parse_iso_utc(s.opened_at), "%H:%M"), format_inr(c.shifts.expected_cash(s.id))) for s in opens]
    closed_today = [s for s in c.shifts.variance_history(limit=20) if s.closed_at and s.closed_at[:10] == today]
    z_reports = [ZReportView(s.id, names.get(s.user_id, f"#{s.user_id}"), format_inr(s.variance_paise or 0), c.shifts.report_lines(c.shifts.z_report(s.id))) for s in closed_today]
    age = c.backup.last_backup_age_hours()
    backup_done = age is not None and age < 4
    backup_text = "never" if age is None else f"{age:.1f} h ago"
    digest = state.extra.get("day_close_digest_path", "")
    pub = c.settings_service.effective().publishing
    labels = {k: texts.t(f"day_close.{k}") for k in ("title", "shifts", "shifts_hint", "zreport", "zreport_hint", "backup", "backup_hint", "digest", "digest_hint", "finish", "finish_hint", "close", "counted", "note", "run_backup", "send_digest", "shutdown", "done", "print")}
    steps = [
        Step("shifts", labels["shifts"], labels["shifts_hint"], not opens, f"{len(opens)} open" if opens else "all closed"),
        Step("zreport", labels["zreport"], labels["zreport_hint"], bool(z_reports), f"{len(z_reports)} report(s)"),
        Step("backup", labels["backup"], labels["backup_hint"], backup_done, backup_text),
        Step("digest", labels["digest"], labels["digest_hint"], bool(digest), digest.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if digest else ("publishing off — local file" if not pub.enabled else "")),
        Step("finish", labels["finish"], labels["finish_hint"], False),
    ]
    log.debug("DAY_CLOSE_MODEL open=%d z=%d backup=%s digest=%s", len(opens), len(z_reports), backup_done, bool(digest))
    return DayCloseModel(today, steps, open_rows, z_reports, backup_text, digest, pub.enabled, labels)


def close_shift(c: Any, actor: Actor, shift_id: int, *, counted_text: str, note: str) -> Outcome:
    """Owner closes any open shift by total count (the blind denomination count lives on /shifts)."""
    denied = _owner(actor)
    if denied:
        return denied
    try:
        counted = parse_inr(counted_text or "0")
        closed = c.shifts.close(actor, shift_id, counted_cash_paise=counted, note=note or "day close")
    except (VaishnaviError, ValueError, InvalidOperation) as exc:
        return Outcome(False, getattr(exc, "detail", "") or str(exc), getattr(exc, "code", None))
    log.info("DAY_CLOSE_SHIFT shift=%d variance=%s", shift_id, closed.variance_paise)
    return Outcome(True, f"shift #{shift_id} closed · variance {format_inr(closed.variance_paise or 0)}")


def run_backup(c: Any, actor: Actor) -> Outcome:
    """Snapshot now."""
    denied = _owner(actor)
    if denied:
        return denied
    try:
        snap = c.backup.snapshot("day_close")
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    return Outcome(True, f"backup {snap.backup_id}" + (" (queued for the backup drive)" if getattr(snap, "queued", False) else ""))


def send_digest(c: Any, actor: Actor, state: Any | None = None) -> Outcome:
    """Render (and, when publishing is on, deliver) today's digest."""
    denied = _owner(actor)
    if denied:
        return denied
    if c.publisher is None:
        return Outcome(False, "publishing not available")
    try:
        result = c.publisher.daily_digest()
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    if state is not None:
        state.extra["day_close_digest_path"] = str(result.path)
    text = f"{result.path.name}" + (f" · sent {', '.join(result.sent)}" if result.sent else "") + (f" · failed {', '.join(result.failed)}" if result.failed else "")
    return Outcome(not result.failed, text)


def finish(c: Any, state: Any, actor: Actor, *, shutdown: bool) -> Outcome:
    """Audit the close; optionally ask the app to stop so the PC can be switched off."""
    denied = _owner(actor)
    if denied:
        return denied
    opens = c.shifts.open_shifts()
    with c.txn(label="day_close.finish") as session:
        c.audit.record(session, "DAY_CLOSED", user_id=actor.user_id, entity="day", after={"day": today_ist().isoformat(), "open_shifts_left": len(opens), "backup_age_h": c.backup.last_backup_age_hours(), "digest": bool(state.extra.get("day_close_digest_path")), "shutdown": shutdown, "at": iso_utc()})
    state.extra.pop("day_close_digest_path", None)
    if shutdown:
        state.extra["shutdown_requested"] = True
    log.info("DAY_CLOSED by=%s open_left=%d shutdown=%s", actor.user_id, len(opens), shutdown)
    return Outcome(True, "day closed" + (" — the app will stop; you can switch the PC off" if shutdown else ""))


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: Any) -> None:
    """``/shifts/day-close``."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    services = state.extra["services"]

    @ui.page("/shifts/day-close")
    def _day_close() -> None:
        client = load_client(app)
        decision = guards.decide("/settings", client)  # owner-only like settings
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/shifts/day-close"))
            return
        texts = UiTexts.load(client.lang)
        actor = client.actor()
        with frame(state, current_path="/shifts"):
            box = ui.column().classes("w-full gap-3")

        def toast(o: Outcome) -> None:
            ui.notify(o.message or texts.t("common.saved"), type="positive" if o.ok else "negative")

        def draw() -> None:
            m = build_day_close(services, state, texts, actor)
            box.clear()
            with box:
                ui.label(f"{m.labels['title']} · {m.day}").classes("text-xl font-semibold")
                with ui.stepper().props("vertical flat").classes("w-full") as stepper:
                    for step in m.steps:
                        with ui.step(step.key, title=step.title, icon="check_circle" if step.done else "radio_button_unchecked"):
                            ui.label(step.hint).classes("text-sm text-gray-600")
                            if step.detail:
                                ui.label(step.detail).classes("text-xs text-gray-500")
                            if step.key == "shifts":
                                for s in m.open_shifts:
                                    form = {"counted": "", "note": ""}
                                    with ui.row().classes("items-end gap-2 vsm-card p-2"):
                                        ui.label(f"#{s.id} · {s.cashier} · since {s.opened_text} · expected {s.expected_text}").classes("text-sm")
                                        ui.input(m.labels["counted"]).bind_value(form, "counted").classes("w-32").props("inputmode=decimal")
                                        ui.input(m.labels["note"]).bind_value(form, "note").classes("w-48")
                                        ui.button(m.labels["close"], on_click=lambda _e, sid=s.id, f=form: (toast(close_shift(services, actor, sid, counted_text=str(f["counted"]), note=str(f["note"]))), draw())).props("unelevated color=primary")
                            elif step.key == "zreport":
                                for z in m.z_reports:
                                    with ui.expansion(f"Shift #{z.shift_id} · {z.cashier} · variance {z.variance_text}").classes("w-full"):
                                        ui.label("\n".join(z.lines)).classes("font-mono text-xs").style("white-space: pre")
                                        ui.button(m.labels["print"], icon="print", on_click=lambda _e, lines=z.lines: services.print_queue.print_lines(actor, lines)).props("flat dense")
                            elif step.key == "backup":
                                ui.button(m.labels["run_backup"], icon="backup", on_click=lambda: (toast(run_backup(services, actor)), draw())).props("unelevated color=primary")
                            elif step.key == "digest":
                                ui.button(m.labels["send_digest"], icon="summarize", on_click=lambda: (toast(send_digest(services, actor, state)), draw())).props("unelevated color=primary")
                            elif step.key == "finish":
                                with ui.row().classes("gap-2"):
                                    ui.button(m.labels["done"], icon="done_all", on_click=lambda: (toast(finish(services, state, actor, shutdown=False)), ui.navigate.to("/"))).props("outline")
                                    ui.button(m.labels["shutdown"], icon="power_settings_new", on_click=lambda: (toast(finish(services, state, actor, shutdown=True)),)).props("unelevated color=negative")
                            with ui.stepper_navigation():
                                ui.button("Next", on_click=stepper.next).props("flat")
                    first_open = next((s.key for s in m.steps if not s.done), "finish")
                    stepper.value = first_open

        draw()


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:  # pragma: no cover - import order
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
