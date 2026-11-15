"""Settings → Backups (docs/09 §10, docs/14 §5.1, §7): locations, snapshot list, Backup now, the **Restore wizard**,
weekly restore-drill status and the plain-language "how to recover manually" card. View-model first.

The wizard is a small state machine: CHOOSE → CONFIRM (Owner password) → DONE / FAILED. Only the Owner
may construct it; the password is re-checked through ``UserService.reauthenticate`` (same lockout as
the login); the restore itself is ``BackupService.restore`` (pre-restore snapshot, verify, swap) and is
audited as ``RESTORE``. Afterwards the app must restart so the restored file is triaged like any start.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc
from vaishnavi.core.errors import AuthError, VaishnaviError, error_for
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.services.backup import Snapshot
from vaishnavi.services.user import UserService
from vaishnavi.ui.components.notification_bell import relative_time
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages.status import size_text

log = logging.getLogger("vaishnavi.ui.pages.settings_backups")


@dataclass(frozen=True)
class SnapshotRow:
    """One line of the snapshot table."""

    backup_id: str
    path: str
    taken_text: str
    age_text: str
    trigger: str
    size_text: str
    verified: bool
    queued: bool
    last_invoice: str | None
    app_version: str


@dataclass(frozen=True)
class DrillStatus:
    """Weekly restore drill."""

    status: str  # never | ok | failed
    at_text: str
    detail: str


@dataclass(frozen=True)
class BackupsModel:
    """Everything the tab shows."""

    primary: str
    secondary: str
    secondary_ready: bool
    secondary_reason: str
    same_device: bool
    schedule_text: str
    retention_text: str
    snapshots: list[SnapshotRow]
    queue_size: int
    drill: DrillStatus
    recovery_steps: list[str]
    labels: dict[str, str] = field(default_factory=dict)


def _row(s: Snapshot, texts: UiTexts) -> SnapshotRow:
    taken = parse_iso_utc(s.taken_at)
    return SnapshotRow(s.backup_id, s.path, format_ist(taken), relative_time(taken, texts=texts), s.trigger, size_text(s.size_bytes), s.verified, s.queued, s.last_invoice, s.app_version)


def _drill_status(services: AppServices, texts: UiTexts) -> DrillStatus:
    db = services.paths.database
    if not db.exists():
        return DrillStatus("never", "-", "")
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT at, ok, details FROM integrity_check WHERE kind = 'RESTORE_DRILL' ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        log.debug("drill status unreadable: %s", exc)
        return DrillStatus("never", "-", "")
    if row is None:
        return DrillStatus("never", "-", "")
    return DrillStatus("ok" if row[1] else "failed", format_ist(parse_iso_utc(str(row[0]))), str(row[2] or ""))


def build_backups_model(services: AppServices, texts: UiTexts) -> BackupsModel:
    """The pure view-model."""
    settings = services.settings.backup
    ready, reason = services.backup.secondary_ready()
    snaps = [_row(s, texts) for s in services.backup.list_snapshots()]
    schedule = texts.t("backups.schedule", time=settings.nightly_time) + (f" · {texts.t('backups.interval', n=settings.interval_minutes)}" if settings.interval_minutes else "")
    retention = texts.t("backups.retention", daily=settings.keep_daily, weekly=settings.keep_weekly, monthly=settings.keep_monthly)
    labels = {k: texts.t(f"backups.{k}") for k in ("title", "primary", "secondary", "ready", "not_ready", "backup_now", "restore", "snapshots", "verified", "queued", "drill", "recovery_title", "choose", "confirm_title", "confirm_body", "owner_password", "restoring", "done", "restart")}
    return BackupsModel(
        primary=str(services.paths.primary),
        secondary=str(services.paths.secondary),
        secondary_ready=ready,
        secondary_reason=reason,
        same_device=services.settings.locations.same_device,
        schedule_text=schedule,
        retention_text=retention,
        snapshots=snaps,
        queue_size=sum(1 for s in snaps if s.queued),
        drill=_drill_status(services, texts),
        recovery_steps=[texts.t(f"backups.recovery_step{i}") for i in range(1, 6)],
        labels=labels,
    )


def backup_now(services: AppServices, actor: Actor) -> SnapshotRow:
    """*Backup now* button (Owner)."""
    require(actor, Permission.MANAGE_SYSTEM)
    snap = services.backup.snapshot("manual")
    with services.txn(label="backup.manual") as session:
        services.audit.record(session, "BACKUP_MANUAL", user_id=actor.user_id, entity="backup", after={"backup_id": snap.backup_id, "queued": snap.queued})
    return _row(snap, UiTexts.load("en"))


# ----------------------------------------------------------------------------- restore wizard
class Step(str, Enum):
    """Wizard steps."""

    CHOOSE = "CHOOSE"
    CONFIRM = "CONFIRM"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RestoreSummary:
    """What the confirmation step shows."""

    backup_id: str
    taken_text: str
    last_invoice: str
    verified: bool
    live_last_invoice: str
    note: str


@dataclass(frozen=True)
class RestoreOutcome:
    """Result of :meth:`RestoreWizard.confirm`."""

    ok: bool
    detail: str = ""
    error_code: str | None = None
    pre_restore_backup_id: str = ""
    restart_required: bool = False


class RestoreWizard:
    """CHOOSE → CONFIRM → DONE/FAILED (Owner only)."""

    def __init__(self, services: AppServices, users: UserService, *, actor: Actor) -> None:
        require(actor, Permission.MANAGE_SYSTEM)
        self._container = services
        self._users = users
        self._actor = actor
        self.step = Step.CHOOSE
        self.snapshot: Snapshot | None = None
        self.summary: RestoreSummary | None = None
        self.outcome: RestoreOutcome | None = None

    def choices(self) -> list[Snapshot]:
        """Verified snapshots, newest first."""
        return self._container.backup.list_snapshots()

    def choose(self, backup_id: str) -> RestoreSummary:
        """Pick a snapshot → confirmation summary."""
        snap = next((s for s in self.choices() if s.backup_id == backup_id), None)
        if snap is None:
            raise ValueError(f"unknown snapshot {backup_id}")
        self.snapshot = snap
        live_invoice = self._live_last_invoice()
        note = "" if (snap.last_invoice or "") >= live_invoice else "bills after the snapshot will be missing — keep the paper receipts"
        self.summary = RestoreSummary(snap.backup_id, format_ist(parse_iso_utc(snap.taken_at)), snap.last_invoice or "-", snap.verified, live_invoice or "-", note)
        self.step = Step.CONFIRM
        log.info("RESTORE_WIZARD_CHOSEN backup_id=%s by=%s", snap.backup_id, self._actor.user_id)
        return self.summary

    def confirm(self, *, owner_password: str) -> RestoreOutcome:
        """Owner password → restore. Wrong password keeps the wizard on CONFIRM."""
        if self.step is not Step.CONFIRM or self.snapshot is None:
            raise RuntimeError("choose a snapshot first")
        try:
            ok = self._users.reauthenticate(self._actor.user_id, owner_password)
        except AuthError as exc:
            self.outcome = RestoreOutcome(False, exc.user_message(), exc.code)
            return self.outcome
        if not ok:
            log.warning("RESTORE_WIZARD_REFUSED reason=password by=%s code=E-AUTH-001", self._actor.user_id)
            self.outcome = RestoreOutcome(False, "wrong password", "E-AUTH-001")
            return self.outcome
        try:
            result = self._container.backup.restore(Path(self.snapshot.path), migrate=_migrate)
        except VaishnaviError as exc:
            log.error("RESTORE_WIZARD_FAILED code=%s %s", exc.code, exc)
            self.step = Step.FAILED
            self.outcome = RestoreOutcome(False, exc.user_message(), exc.code)
            return self.outcome
        with self._container.txn(label="restore.audit") as session:
            self._container.audit.record(session, "RESTORE", user_id=self._actor.user_id, entity="backup", after={"backup_id": result.backup_id, "pre_restore": result.pre_restore_backup_id, "detail": result.detail})
        self.step = Step.DONE
        self.outcome = RestoreOutcome(True, result.detail, None, result.pre_restore_backup_id, restart_required=True)
        log.info("RESTORE_WIZARD_DONE backup_id=%s pre_restore=%s by=%s", result.backup_id, result.pre_restore_backup_id, self._actor.user_id)
        return self.outcome

    def _live_last_invoice(self) -> str:
        db = self._container.paths.database
        if not db.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT invoice_no FROM sale ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            return str(row[0]) if row else ""
        except sqlite3.DatabaseError:
            return ""


def _migrate(staged: Path) -> None:
    """Bring an older snapshot to the current schema (Alembic when available; stamped stdlib DBs pass through)."""
    from vaishnavi.db.schema import HEAD_REVISION, current_revision  # noqa: PLC0415

    conn = sqlite3.connect(staged)
    try:
        revision = current_revision(conn)
    finally:
        conn.close()
    if revision == HEAD_REVISION:
        return
    try:
        from vaishnavi.db.migrate import upgrade_to_head  # noqa: PLC0415
    except ImportError as exc:
        raise error_for("E-DB-010", detail=f"snapshot at {revision}, alembic unavailable: {exc}") from exc
    upgrade_to_head(staged, tmp_dir=staged.parent, dry_run=False)


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: Any, client: Any, texts: UiTexts) -> None:
    """The Backups tab body inside Settings."""
    from nicegui import ui  # noqa: PLC0415

    services: AppServices = state.extra["services"]
    box = ui.column().classes("w-full gap-3")

    def render() -> None:
        m = build_backups_model(services, texts)
        box.clear()
        with box:
            with ui.grid(columns=2).classes("w-full gap-3"):
                with ui.card().classes("vsm-card"):
                    ui.label(m.labels["primary"]).classes("font-semibold")
                    ui.label(m.primary).classes("vsm-mono text-sm")
                with ui.card().classes("vsm-card"):
                    ui.label(m.labels["secondary"]).classes("font-semibold")
                    ui.label(m.secondary).classes("vsm-mono text-sm")
                    ui.badge(m.labels["ready"] if m.secondary_ready else m.labels["not_ready"], color="positive" if m.secondary_ready else "warning")
            ui.label(f"{m.schedule_text} · {m.retention_text}").classes("text-sm text-gray-600")
            with ui.row().classes("gap-2"):
                ui.button(m.labels["backup_now"], icon="backup", on_click=lambda: (backup_now(services, client.actor()), render())).props("unelevated color=primary")
                ui.button(m.labels["restore"], icon="restore", on_click=lambda: _open_wizard(state, client, texts)).props("outline")
            ui.label(f"{m.labels['drill']}: {m.drill.status} {m.drill.at_text}").classes("text-sm")
            ui.label(m.labels["snapshots"]).classes("font-semibold mt-2")
            ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("taken_text", "trigger", "size_text", "verified", "queued", "last_invoice", "app_version")], rows=[r.__dict__ for r in m.snapshots]).classes("w-full text-xs")
            with ui.expansion(m.labels["recovery_title"]).classes("w-full"):
                for i, step in enumerate(m.recovery_steps, 1):
                    ui.label(f"{i}. {step}").classes("text-sm")

    render()


def _open_wizard(state: Any, client: Any, texts: UiTexts) -> None:
    from nicegui import ui  # noqa: PLC0415

    services: AppServices = state.extra["services"]
    wizard = RestoreWizard(services, services.users, actor=client.actor())
    chosen = {"id": None}
    password = {"value": ""}
    with ui.dialog() as dialog, ui.card().classes("vsm-card p-4 gap-3").style("width: 560px"):
        body = ui.column().classes("w-full gap-2")

        def render() -> None:
            body.clear()
            with body:
                if wizard.step is Step.CHOOSE:
                    ui.label(texts.t("backups.choose")).classes("font-semibold")
                    options = {s.backup_id: f"{format_ist(parse_iso_utc(s.taken_at))} · {s.last_invoice or '-'} · {'✔' if s.verified else '?'}" for s in wizard.choices()}
                    ui.select(options, label=texts.t("backups.snapshots")).bind_value(chosen, "id").classes("w-full")
                    ui.button(texts.t("common.confirm"), on_click=lambda: (wizard.choose(str(chosen["id"])), render()) if chosen["id"] else None).props("unelevated color=primary")
                elif wizard.step is Step.CONFIRM and wizard.summary:
                    s = wizard.summary
                    ui.label(texts.t("backups.confirm_title")).classes("font-semibold")
                    ui.label(texts.t("backups.confirm_body", taken=s.taken_text, invoice=s.last_invoice, live=s.live_last_invoice))
                    if s.note:
                        ui.label(s.note).classes("text-amber-800 text-sm")
                    ui.input(texts.t("backups.owner_password"), password=True).bind_value(password, "value").classes("w-full")

                    def go() -> None:
                        outcome = wizard.confirm(owner_password=password["value"])
                        password["value"] = ""
                        if not outcome.ok:
                            ui.notify(f"{outcome.detail} ({outcome.error_code})", type="negative")
                        render()

                    ui.button(texts.t("backups.restore"), on_click=go).props("unelevated color=negative")
                elif wizard.step is Step.DONE:
                    ui.label(texts.t("backups.done")).classes("font-semibold text-green-800")
                    ui.label(texts.t("backups.restart"))
                    from vaishnavi.ui.pages.settings_system import request_restart  # noqa: PLC0415

                    ui.button(texts.t("backups.restart"), on_click=lambda: request_restart(state, client.actor(), reason="restore")).props("unelevated color=primary")
                else:
                    ui.label(wizard.outcome.detail if wizard.outcome else "?").classes("text-red-800")
                ui.button(texts.t("common.close"), on_click=dialog.close).props("flat")

        render()
    dialog.open()
