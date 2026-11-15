"""``/system/status`` — health chips, versions, uptime, jobs, last backup, integrity, who is logged in (docs/08 §9).

View-model first (``build_status_model``); the NiceGUI page renders it and refreshes every 10 s.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vaishnavi import __version__
from vaishnavi.api import system as api_system
from vaishnavi.api.system import AppState
from vaishnavi.core.clock import format_ist, now_utc, parse_iso_utc
from vaishnavi.services.work_session import duration_text
from vaishnavi.ui.components.notification_bell import relative_time
from vaishnavi.ui.components.status_chip import ChipView, chip_views
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.status")


@dataclass(frozen=True)
class JobView:
    """One scheduler job row."""

    name: str
    schedule_text: str
    next_run_text: str
    runs: int
    failures: int
    last_ms: int | None
    last_error: str | None
    enabled: bool


@dataclass(frozen=True)
class DatabaseView:
    """The live file."""

    path: str
    exists: bool
    size_text: str
    revision: str | None


@dataclass(frozen=True)
class BackupView:
    """Newest snapshot."""

    backup_id: str
    taken_text: str
    age_text: str
    verified: bool
    queued: bool
    chain_head: str
    last_invoice: str | None
    location: str


@dataclass(frozen=True)
class IntegrityView:
    """Last ledger pass."""

    ok: bool
    at_text: str
    head: str
    codes: list[str]


@dataclass(frozen=True)
class LoggedInView:
    """Who is logged in now."""

    user_id: int
    display_name: str
    since_text: str
    last_seen_text: str
    client: str


@dataclass(frozen=True)
class StatusModel:
    """Everything the status page shows."""

    version: str
    healthy: bool
    uptime_text: str
    started_text: str
    chips: list[ChipView]
    heartbeats: dict[str, float]
    database: DatabaseView
    jobs: list[JobView]
    last_backup: BackupView | None
    queue_size: int
    integrity: IntegrityView
    logged_in: list[LoggedInView]
    ai: dict[str, Any]
    locations: dict[str, str]
    labels: dict[str, str] = field(default_factory=dict)


def size_text(size_bytes: int) -> str:
    """``412 KB`` / ``12.4 MB``."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, size_bytes // 1024)} KB"


def _schedule_text(job: dict[str, Any], texts: UiTexts) -> str:
    if job.get("every_s"):
        every = int(job["every_s"])
        return texts.t("status.every_s", n=every) if every < 120 else texts.t("status.every_min", n=every // 60)
    if job.get("daily_at"):
        return texts.t("status.daily_at", time=job["daily_at"])
    return "-"


def build_status_model(state: AppState, texts: UiTexts) -> StatusModel:
    """The pure view-model."""
    now = now_utc()
    health = api_system.healthz(state)
    status = api_system.status(state)
    services = state.extra.get("services")
    db = state.paths.database
    revision = None
    if db.exists():
        import sqlite3  # noqa: PLC0415

        from vaishnavi.db.schema import current_revision  # noqa: PLC0415

        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                revision = current_revision(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            log.debug("status: revision unreadable: %s", exc)
    database = DatabaseView(path=str(db), exists=db.exists(), size_text=size_text(status["database"]["size_bytes"]), revision=revision)

    jobs: list[JobView] = []
    for job in status.get("jobs") or []:
        next_run = job.get("next_run")
        next_text = relative_time(parse_iso_utc(str(next_run)[:19] + "Z"), now=now, texts=texts) if next_run else "-"
        jobs.append(JobView(str(job["name"]), _schedule_text(job, texts), next_text, int(job.get("runs") or 0), int(job.get("failures") or 0), job.get("last_ms"), job.get("last_error"), bool(job.get("enabled", True))))

    last_backup: BackupView | None = None
    queue_size = 0
    integrity = IntegrityView(ok=True, at_text="-", head="", codes=[])
    logged_in: list[LoggedInView] = []
    if services is not None:
        snaps = services.backup.list_snapshots()
        queue_size = sum(1 for s in snaps if s.queued)
        if snaps:
            s = snaps[0]
            taken = parse_iso_utc(s.taken_at)
            last_backup = BackupView(s.backup_id, format_ist(taken), relative_time(taken, now=now, texts=texts), s.verified, s.queued, s.chain_head, s.last_invoice, str(state.paths.backup_queue if s.queued else state.paths.snapshots))
        try:
            import sqlite3  # noqa: PLC0415

            conn = sqlite3.connect(db)
            try:
                result = services.ledger.run_all(conn, record=False)
            finally:
                conn.close()
            integrity = IntegrityView(ok=result.ok, at_text=format_ist(parse_iso_utc(result.at), "%d-%b %H:%M"), head=result.head, codes=result.codes)
        except Exception as exc:  # noqa: BLE001 - the page must render even if the ledger pass fails
            log.warning("status: integrity pass failed: %r", exc)
            integrity = IntegrityView(ok=False, at_text="-", head="", codes=["E-DB-003"])
        users = services.users.display_names()
        for row in services.sessions.who_is_logged_in():
            logged_in.append(LoggedInView(row.user_id, users.get(row.user_id, f"#{row.user_id}"), format_ist(parse_iso_utc(row.login_at), "%H:%M"), relative_time(parse_iso_utc(row.last_seen_at), now=now, texts=texts), row.client or ""))

    labels = {k: texts.t(f"status.{k}") for k in ("title", "healthy", "unhealthy", "uptime", "database", "jobs", "backup", "integrity", "logged_in", "nobody", "ai", "locations", "heartbeats")}
    return StatusModel(
        version=__version__,
        healthy=bool(health["ok"]),
        uptime_text=duration_text(int(health["uptime_s"])),
        started_text=format_ist(state.started_at),
        chips=chip_views(state.chips, texts),
        heartbeats=dict(health["heartbeats"]),
        database=database,
        jobs=jobs,
        last_backup=last_backup,
        queue_size=queue_size,
        integrity=integrity,
        logged_in=logged_in,
        ai=dict(status["ai"]),
        locations=dict(status["locations"]),
        labels=labels,
    )


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def register(state: AppState) -> None:
    """``/system/status`` (Owner only through the guard)."""
    from nicegui import app, ui  # noqa: PLC0415

    from vaishnavi.ui import guards  # noqa: PLC0415
    from vaishnavi.ui.server import frame  # noqa: PLC0415
    from vaishnavi.ui.session import load_client  # noqa: PLC0415

    @ui.page("/system/status")
    def _status() -> None:
        client = load_client(app)
        decision = guards.decide("/system/status", client)
        if decision is not guards.Decision.ALLOW:
            ui.navigate.to(guards.redirect_for(decision, "/system/status"))
            return
        texts = UiTexts.load(client.lang)
        with frame(state, current_path="/system/status"):
            box = ui.column().classes("w-full gap-3")

            def render() -> None:
                m = build_status_model(state, texts)
                box.clear()
                with box:
                    with ui.row().classes("items-center gap-3"):
                        ui.label(m.labels["title"]).classes("text-xl font-semibold")
                        ui.badge(m.labels["healthy"] if m.healthy else m.labels["unhealthy"], color="positive" if m.healthy else "negative")
                        ui.label(f"v{m.version} · {m.labels['uptime']} {m.uptime_text} · {m.started_text}").classes("text-sm text-gray-500")
                    with ui.row().classes("gap-2"):
                        for chip in m.chips:
                            with ui.element("span").classes("chip"):
                                ui.element("i").classes(f"dot {chip.level}")
                                ui.label(f"{chip.label}: {chip.text}")
                                ui.tooltip(chip.tooltip)
                    with ui.grid(columns=2).classes("w-full gap-3"):
                        with ui.card().classes("vsm-card"):
                            ui.label(m.labels["database"]).classes("font-semibold")
                            ui.label(f"{m.database.path} · {m.database.size_text} · {m.database.revision or '?'}").classes("text-sm vsm-mono")
                            ui.label(f"{m.labels['integrity']}: {'OK' if m.integrity.ok else ', '.join(m.integrity.codes)} · {m.integrity.at_text} · {m.integrity.head[:12]}").classes("text-sm")
                        with ui.card().classes("vsm-card"):
                            ui.label(m.labels["backup"]).classes("font-semibold")
                            if m.last_backup:
                                ui.label(f"{m.last_backup.taken_text} ({m.last_backup.age_text}) · {'✔' if m.last_backup.verified else '?'} · {m.last_backup.last_invoice or '-'} · queue {m.queue_size}").classes("text-sm")
                            else:
                                ui.label("-")
                        with ui.card().classes("vsm-card"):
                            ui.label(m.labels["logged_in"]).classes("font-semibold")
                            if not m.logged_in:
                                ui.label(m.labels["nobody"]).classes("text-sm text-gray-500")
                            for who in m.logged_in:
                                ui.label(f"{who.display_name} · {who.since_text} · {who.last_seen_text} · {who.client}").classes("text-sm")
                        with ui.card().classes("vsm-card"):
                            ui.label(m.labels["heartbeats"]).classes("font-semibold")
                            for loop, age in m.heartbeats.items():
                                ui.label(f"{loop}: {age:.0f} s").classes("text-sm vsm-mono")
                    ui.label(m.labels["jobs"]).classes("font-semibold mt-2")
                    ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("name", "schedule_text", "next_run_text", "runs", "failures", "last_ms", "last_error")], rows=[j.__dict__ for j in m.jobs]).classes("w-full text-xs")

            render()
            ui.timer(10.0, render)


def _register_with_server() -> None:
    try:
        from vaishnavi.ui.server import PAGE_REGISTRARS  # noqa: PLC0415
    except ImportError:
        return
    if register not in PAGE_REGISTRARS:
        PAGE_REGISTRARS.append(register)


_register_with_server()
