"""Settings → System (docs/09 §10, docs/08 §9): doctor, version, code integrity, autostart, redacted config,
remote log access URLs, support bundle, restart / shut down. View-model first.

The doctor is reached only through ``AppState.doctor_runner`` (injected by start-up) so this page never
imports the ``bootstrap`` layer. Nothing here changes data; the two control actions (restart, shutdown)
are Owner-only, audited, and executed by the server loop — the page only *requests* them.
"""

from __future__ import annotations

import io
import json
import logging
import re
import socket
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from vaishnavi import __version__
from vaishnavi.api import system as api_system
from vaishnavi.api.system import AppState
from vaishnavi.core.clock import iso_utc, now_ist
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_system")

_SECRET_TOML_KEY: Final = re.compile(r"(?im)^(\s*(?:[\w.]*_)?(?:api_key|token|password|secret|key|pin|account_no|ifsc|upi_id)\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)")
BUNDLE_LOG_FAMILIES: Final = ("errors", "app", "startup", "install", "hardware", "ai")
"""Included in a support bundle; ``audit`` and ``access`` (who did what) are deliberately not."""


@dataclass(frozen=True)
class CodeIntegrityView:
    """From the doctor's ``code_integrity`` check."""

    status: str  # ok | dev-copy | changed | unknown
    message: str
    detail: str


@dataclass(frozen=True)
class AutostartView:
    """From the doctor's ``autostart`` check."""

    status: str  # registered | not-registered | unknown
    message: str


@dataclass(frozen=True)
class RemoteAccessView:
    """Settings → System → Remote log access."""

    enabled: bool
    urls: list[str]
    https_port: int
    note: str


@dataclass(frozen=True)
class DatabaseInfo:
    """Migration status."""

    revision: str | None
    head: str
    up_to_date: bool


@dataclass(frozen=True)
class SystemModel:
    """Everything the tab shows."""

    version: str
    python: str
    program_dir: str
    config_text: str
    code_integrity: CodeIntegrityView
    autostart: AutostartView
    remote_access: RemoteAccessView
    database: DatabaseInfo
    last_doctor: dict[str, Any] | None
    labels: dict[str, str] = field(default_factory=dict)


def redact_toml(text: str) -> str:
    """Mask every secret-looking value in a TOML text (defence in depth — keys must not be in TOML anyway, ``E-SEC-006``)."""
    return _SECRET_TOML_KEY.sub(lambda m: f'{m.group(1)}"***"', text)


def lan_ip() -> str:
    """Best-effort LAN address for the URL the owner types on a phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return str(s.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def remote_access_view(state: AppState, texts: UiTexts) -> RemoteAccessView:
    """URLs of the log console as they are reachable right now."""
    server = state.settings.server
    urls = [f"http://127.0.0.1:{server.port}/system/logs"]
    if server.lan_access:
        urls.append(f"http://{lan_ip()}:{server.port}/system/logs")
    return RemoteAccessView(enabled=server.lan_access, urls=urls, https_port=server.https_port, note=texts.t("system.remote_on" if server.lan_access else "system.remote_off"))


def _doctor_checks(state: AppState, keys: list[str]) -> dict[str, dict[str, Any]]:
    report = api_system.doctor(state, checks=keys)
    return {c["key"]: c for c in report.get("checks", [])}


def build_system_model(state: AppState, texts: UiTexts) -> SystemModel:
    """The pure view-model."""
    import sys  # noqa: PLC0415

    from vaishnavi.db.schema import HEAD_REVISION, current_revision  # noqa: PLC0415
    from vaishnavi.platform.paths import program_dir  # noqa: PLC0415

    cfg = state.config_dir / "settings.toml"
    try:
        config_text = redact_toml(cfg.read_text(encoding="utf-8")) if cfg.exists() else texts.t("system.no_config")
    except OSError as exc:
        config_text = f"({exc})"

    checks = _doctor_checks(state, ["code_integrity", "autostart"]) if callable(state.doctor_runner) else {}
    ci = checks.get("code_integrity")
    if ci is None:
        integrity = CodeIntegrityView("unknown", "", "")
    elif ci["status"] == "PASS":
        integrity = CodeIntegrityView("ok", ci["message"], ci.get("detail", ""))
    elif ci["status"] == "WARN" and not (ci.get("data") or {}).get("manifest", True):
        integrity = CodeIntegrityView("dev-copy", ci["message"], ci.get("detail", ""))
    else:
        integrity = CodeIntegrityView("changed", ci["message"], ci.get("detail", ""))
    au = checks.get("autostart")
    autostart = AutostartView("unknown", "") if au is None else AutostartView("registered" if au["status"] == "PASS" else ("not-registered" if au["status"] == "WARN" else "unknown"), au["message"])

    revision = None
    if state.paths.database.exists():
        try:
            conn = sqlite3.connect(f"file:{state.paths.database.as_posix()}?mode=ro", uri=True)
            try:
                revision = current_revision(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            log.debug("system: revision unreadable: %s", exc)
    labels = {k: texts.t(f"system.{k}") for k in ("title", "run_doctor", "version", "code_integrity", "autostart", "config", "remote", "support_bundle", "restart", "shutdown", "shutdown_confirm", "database", "up_to_date", "migration_pending")}
    return SystemModel(
        version=__version__,
        python=sys.version.split()[0],
        program_dir=str(program_dir()),
        config_text=config_text,
        code_integrity=integrity,
        autostart=autostart,
        remote_access=remote_access_view(state, texts),
        database=DatabaseInfo(revision, HEAD_REVISION, revision == HEAD_REVISION),
        last_doctor=state.extra.get("last_doctor"),
        labels=labels,
    )


def run_doctor(state: AppState, lang: str) -> dict[str, Any]:
    """*Run doctor* button: full report, remembered for the tab."""
    report = api_system.doctor(state, lang=lang)
    state.extra["last_doctor"] = report
    return report


# ----------------------------------------------------------------------------- support bundle
def build_support_bundle(state: AppState, *, max_log_bytes: int = 5 * 1024 * 1024) -> Path:
    """Zip for support: doctor report, status, metrics, redacted settings, non-personal logs (never the audit trail)."""
    exports = state.paths.exports
    exports.mkdir(parents=True, exist_ok=True)
    target = exports / f"support-bundle-{now_ist().strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doctor.json", json.dumps(api_system.doctor(state), ensure_ascii=False, indent=2, default=str))
        zf.writestr("status.json", json.dumps(api_system.status(state), ensure_ascii=False, indent=2, default=str))
        zf.writestr("metrics.json", json.dumps(api_system.metrics(state), ensure_ascii=False, indent=2, default=str))
        zf.writestr("bundle.json", json.dumps({"version": __version__, "at": iso_utc(), "lang": state.lang}, indent=2))
        for name in ("settings.toml", "locations.toml", "logging.toml", "alerts.toml"):
            path = state.config_dir / name
            if path.exists():
                try:
                    zf.writestr(f"config/{name}", redact_toml(path.read_text(encoding="utf-8")))
                except OSError as exc:
                    log.debug("bundle: %s unreadable: %s", path, exc)
        for family in BUNDLE_LOG_FAMILIES:
            folder = state.paths.logs / family
            if not folder.is_dir():
                continue
            for file in sorted(folder.glob("*.log*"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
                try:
                    data = file.read_bytes()
                except OSError:
                    continue
                zf.writestr(f"logs/{family}/{file.name}", data[-max_log_bytes:])
    log.info("SUPPORT_BUNDLE_WRITTEN path=%s size=%d", target, target.stat().st_size)
    return target


# ----------------------------------------------------------------------------- control actions (requests only)
def request_shutdown(state: AppState, actor: Actor, *, reason: str) -> None:
    """Owner asks the app to stop; the server loop performs it (``APP_SHUTDOWN_REQUESTED`` audited).

    The stop marker tells the installer-window watchdog this exit is deliberate — otherwise it relaunches the app."""
    require(actor, Permission.MANAGE_SYSTEM)
    _audit(state, actor, "APP_SHUTDOWN_REQUESTED", reason)
    state.extra["shutdown_requested"] = True
    from vaishnavi.platform.watchdog import request_stop  # noqa: PLC0415

    request_stop(state.paths.state, reason=f"owner shutdown: {reason}")
    log.warning("APP_SHUTDOWN_REQUESTED by=%s reason=%s", actor.user_id, reason)


def request_restart(state: AppState, actor: Actor, *, reason: str) -> None:
    """Owner asks for a restart (after a restore); the watchdog / autostart brings the app back."""
    require(actor, Permission.MANAGE_SYSTEM)
    _audit(state, actor, "APP_RESTART_REQUESTED", reason)
    state.extra["restart_requested"] = True
    state.extra["shutdown_requested"] = True
    log.warning("APP_RESTART_REQUESTED by=%s reason=%s", actor.user_id, reason)


def _audit(state: AppState, actor: Actor, action: str, reason: str) -> None:
    services = state.extra.get("services")
    if services is None:
        return
    with services.txn(label="system.control") as session:
        services.audit.record(session, action, user_id=actor.user_id, entity="app", after={"reason": reason})


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """The System tab body inside Settings."""
    from nicegui import app, ui  # noqa: PLC0415

    m = build_system_model(state, texts)
    with ui.column().classes("w-full gap-3"):
        ui.label(f"{m.labels['version']}: {m.version} · Python {m.python} · {m.program_dir}").classes("text-sm vsm-mono")
        with ui.row().classes("gap-2"):
            ui.button(m.labels["run_doctor"], icon="medical_services", on_click=lambda: (run_doctor(state, client.lang), ui.navigate.reload())).props("unelevated color=primary")
            ui.button(m.labels["support_bundle"], icon="archive", on_click=lambda: ui.download.file(build_support_bundle(state))).props("outline")
            ui.link("/system/logs", "/system/logs").classes("self-center")
            ui.link("/system/status", "/system/status").classes("self-center")
        if m.last_doctor:
            with ui.expansion(f"{m.labels['run_doctor']} · {m.last_doctor.get('worst')}").classes("w-full"):
                for check in m.last_doctor.get("checks", []):
                    ui.label(f"{check['status']:<5} {check['key']}: {check['message']} {check.get('hint', '')}").classes("text-xs vsm-mono")
        with ui.grid(columns=2).classes("w-full gap-3"):
            with ui.card().classes("vsm-card"):
                ui.label(m.labels["code_integrity"]).classes("font-semibold")
                ui.badge(m.code_integrity.status, color={"ok": "positive", "dev-copy": "warning", "changed": "negative"}.get(m.code_integrity.status, "grey"))
                ui.label(m.code_integrity.message).classes("text-sm")
            with ui.card().classes("vsm-card"):
                ui.label(m.labels["autostart"]).classes("font-semibold")
                ui.badge(m.autostart.status, color={"registered": "positive", "not-registered": "warning"}.get(m.autostart.status, "grey"))
                ui.label(m.autostart.message).classes("text-sm")
            with ui.card().classes("vsm-card"):
                ui.label(m.labels["database"]).classes("font-semibold")
                ui.label(f"{m.database.revision or '?'} / {m.database.head} · {m.labels['up_to_date'] if m.database.up_to_date else m.labels['migration_pending']}").classes("text-sm vsm-mono")
            with ui.card().classes("vsm-card"):
                ui.label(m.labels["remote"]).classes("font-semibold")
                ui.label(m.remote_access.note).classes("text-sm")
                for url in m.remote_access.urls:
                    ui.label(url).classes("text-sm vsm-mono")
        with ui.expansion(m.labels["config"]).classes("w-full"):
            ui.code(m.config_text, language="toml").classes("w-full text-xs")
        with ui.row().classes("gap-2 mt-4"):
            def shutdown() -> None:
                request_shutdown(state, client.actor(), reason="owner (Settings → System)")
                ui.notify(m.labels["shutdown_confirm"])
                app.shutdown()

            ui.button(m.labels["shutdown"], icon="power_settings_new", on_click=shutdown).props("outline color=negative")
