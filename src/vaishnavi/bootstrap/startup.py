"""run_app() — the daily start inside the venv (docs/07 §6): lock → triage → schema → light doctor → services → scheduler → server → clean close.

Every step logs a marker (``APP_START``, ``DB_TRIAGE``, ``DOCTOR_LIGHT``, ``SCHEDULER_STARTED``,
``SERVER_READY``, ``APP_CLEAN_CLOSE``) so a bad morning can be traced line by line. The web server is
injected (``serve(state, on_ready)``) — NiceGUI on the shop PC, a stub in tests. Return codes follow
the installer's: 0 clean · 2 refused (``E-DB-003/021/030/010``).
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vaishnavi import __version__
from vaishnavi.api.system import AppState
from vaishnavi.bootstrap.doctor import DoctorContext, Status, default_checks, run_doctor, select_checks
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import format_ist, iso_utc, now_ist
from vaishnavi.core.errors import DatabaseError
from vaishnavi.core.events import DbRestoredDetected, bus
from vaishnavi.core.logging import with_correlation
from vaishnavi.core.settings import Settings
from vaishnavi.db import seed
from vaishnavi.db.lock import WriterLock
from vaishnavi.db.guardian import TriageKind
from vaishnavi.db.schema import HEAD_REVISION, apply_schema, current_revision, stamp_head
from vaishnavi.platform.paths import AppPaths
from vaishnavi.scheduler.jobs import bind_jobs, build_jobs
from vaishnavi.scheduler.simple import SimpleScheduler
from vaishnavi.ui.components.status_chip import chips_from_hardware

log = logging.getLogger("vaishnavi.bootstrap.startup")

LIGHT_CHECKS = ("config", "database", "ledger", "backup", "secrets", "hardware", "ai_provider")
Serve = Callable[[AppState, Callable[[], None]], None]


def require_primary(paths: AppPaths) -> bool:
    """The recorded data root must already exist — never re-create it (an unplugged disk would silently become an empty folder on C:).

    ``E-SYS-013`` when missing; otherwise the sub-folders beneath it are ensured.
    """
    if not paths.primary.exists():
        log.error("PRIMARY_MISSING path=%s code=E-SYS-013 (plug the disk in or fix config/locations.toml; the folder is never re-created automatically)", paths.primary)
        return False
    paths.ensure(include_secondary=False)
    return True


def ensure_database(paths: AppPaths) -> seed.SeedReport | None:
    """Create + seed + stamp a missing database (stdlib); the seed report when it was created now, else None."""
    db = paths.database
    if db.exists():
        return None
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        apply_schema(conn)
        stamp_head(conn)
        report = seed.apply_seed(conn)
    finally:
        conn.close()
    log.info("DB_CREATED path=%s seeded_rows=%d", db, report.total)
    return report


def ensure_schema_current(paths: AppPaths) -> str:
    """Upgrade an older database through Alembic (dry-run on a copy first); returns the revision now in place."""
    conn = sqlite3.connect(paths.database)
    try:
        revision = current_revision(conn)
    finally:
        conn.close()
    if revision == HEAD_REVISION:
        return revision
    if revision is None:
        # Schema present but never stamped (very early builds): if every table exists, stamp it.
        conn = sqlite3.connect(paths.database)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "db_identity" in tables and "sale" in tables:
                stamp_head(conn)
                log.warning("DB_STAMPED_HEAD revision=%s (unstamped schema)", HEAD_REVISION)
                return HEAD_REVISION
        finally:
            conn.close()
    try:
        from vaishnavi.db.migrate import upgrade_to_head  # noqa: PLC0415
    except ImportError as exc:
        log.error("DB_MIGRATION_UNAVAILABLE revision=%s head=%s (%s) code=E-DB-010", revision, HEAD_REVISION, exc)
        raise DatabaseError("E-DB-010", detail=f"alembic not importable: {exc}") from exc
    report = upgrade_to_head(paths.database, tmp_dir=paths.tmp)
    log.info("DB_MIGRATED before=%s after=%s", report.before, report.after)
    return report.after


def write_heartbeat(paths: AppPaths, state: AppState) -> None:
    """``state/heartbeat.json`` — a second liveness signal for the watchdog when HTTP is unreachable."""
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        payload = {"at": iso_utc(), "loops": {k: iso_utc(v) for k, v in state.heartbeats.items()}, "pid": __import__("os").getpid()}
        tmp = paths.state / "heartbeat.json.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(paths.state / "heartbeat.json")
    except OSError as exc:
        log.debug("heartbeat file: %s", exc)


def make_doctor_runner(settings: Settings, paths: AppPaths, config_dir: Path, *, runtime: dict[str, Any] | None = None) -> Callable[..., dict[str, Any]]:
    """The ``AppState.doctor_runner`` — runs the doctor inside the live process (``app_running`` so the port check is skipped)."""
    from vaishnavi.bootstrap.doctor import render_json  # noqa: PLC0415

    def run(*, checks: list[str] | None = None, fix: bool = False, lang: str = "en") -> dict[str, Any]:
        ctx = DoctorContext(settings=settings, paths=paths, config_dir=config_dir, lang=lang, fix=fix, runtime={"app_running": True, **(runtime or {})})
        report = run_doctor(select_checks(default_checks(), checks), ctx)
        return json.loads(render_json(report))

    return run


def _record_app_run(paths: AppPaths, *, start: bool, run_id: int | None = None, clean: bool = True) -> int | None:
    try:
        conn = sqlite3.connect(paths.database)
        try:
            if start:
                cur = conn.execute("INSERT INTO app_run(started_at, app_version, host) VALUES (?, ?, ?)", (iso_utc(), __version__, socket.gethostname()))
                conn.commit()
                return int(cur.lastrowid)
            if run_id is not None:
                conn.execute("UPDATE app_run SET stopped_at = ?, clean_stop = ? WHERE id = ?", (iso_utc(), 1 if clean else 0, run_id))
                conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        log.warning("app_run bookkeeping failed: %s", exc)
    return run_id


def _chip_for(result_status: Status, ok_text: str, bad_text: str) -> tuple[str, str]:
    if result_status is Status.PASS or result_status is Status.SKIP:
        return ("green", ok_text)
    if result_status is Status.WARN:
        return ("amber", bad_text)
    return ("red", bad_text)


def run_app(*, settings: Settings, paths: AppPaths, config_dir: Path, serve: Serve | None = None, open_browser: bool = True, lang: str = "en") -> int:
    """The daily start; see module docstring."""
    with with_correlation(user="system", role="SYSTEM"):
        log.info("APP_START version=%s db=%s provider=%s", __version__, paths.database, settings.ai.provider)
        if not require_primary(paths):
            log.error("APP_REFUSED data folder missing code=E-SYS-013")
            return 2
        lock = WriterLock(paths.data / "vaishnavi.db.lock")
        try:
            lock.acquire()
        except DatabaseError as exc:
            log.error("APP_REFUSED %s", exc)
            return 2
        run_id: int | None = None
        scheduler: SimpleScheduler | None = None
        services: AppServices | None = None
        try:
            created = ensure_database(paths) is not None
            services_probe = AppServices.build(settings, paths, config_dir)
            triage = services_probe.guardian.triage()
            log.info("DB_TRIAGE kind=%s code=%s created_now=%s", triage.kind.value, triage.code or "-", created)
            if not triage.may_start:
                log.error("APP_REFUSED triage=%s code=%s detail=%s", triage.kind.value, triage.code, triage.detail)
                return 2
            try:
                revision = ensure_schema_current(paths)
            except DatabaseError as exc:
                log.error("APP_REFUSED %s", exc)
                return 2
            log.info("DB_SCHEMA revision=%s", revision)
            if triage.kind is TriageKind.RESTORED:
                bus.publish(DbRestoredDetected(backup_id=str(triage.params.get("backup_id", "")), taken_at=str(triage.params.get("taken_at", "?"))))

            services = services_probe
            services.guardian.mark_open()
            run_id = _record_app_run(paths, start=True)
            services.sessions.app_run_id = run_id
            # Sessions left open by a previous run (power cut, crash) end at their last heartbeat — flagged for confirmation.
            recovered = services.sessions.recover_unclean()
            log.info("WORK_SESSIONS_RECOVERED count=%d", recovered)
            state = AppState(settings=settings, paths=paths, config_dir=config_dir, lang=lang)
            state.extra["services"] = services

            # Light doctor (the installer ran the full one): sets the chips; database problems stop the start.
            ctx = DoctorContext(settings=settings, paths=paths, config_dir=config_dir, lang=lang, fix=False, runtime={"app_running": True, "ledger": services.ledger, "backup": services.backup, "guardian": services.guardian})
            report = run_doctor(select_checks(default_checks(), list(LIGHT_CHECKS)), ctx)
            by_key = {r.key: r for r in report.results}
            log.info("DOCTOR_LIGHT ref=%s exit=%d %s", report.ref, report.exit_code, " ".join(f"{r.key}={r.status.value}" for r in report.results))
            for key, (ok_text, bad_text) in {"database": ("DB ok", "DB"), "ledger": ("Integrity ok", "Integrity"), "backup": ("Backup ok", "Backup"), "ai_provider": ("AI ready", "AI")}.items():
                if key in by_key:
                    r = by_key[key]
                    state.chips[key] = _chip_for(r.status, ok_text, f"{bad_text}: {r.message}")
            if settings.ai.provider == "none" or not settings.ai.enabled:
                state.chips["ai_provider"] = ("grey", "AI off")
            if "hardware" in by_key:
                state.chips.update(chips_from_hardware(by_key["hardware"]))  # one doctor result → Scanner + Printer chips
            state.extra["started_at_text"] = format_ist(state.started_at, "%H:%M")
            state.extra["storage_secret"] = _storage_secret(paths)
            state.doctor_runner = make_doctor_runner(settings, paths, config_dir, runtime={"ledger": services.ledger, "backup": services.backup, "guardian": services.guardian})
            if by_key.get("database") and by_key["database"].status is Status.FAIL:
                log.error("APP_REFUSED doctor database=%s code=%s", by_key["database"].message, by_key["database"].error_code)
                return 2
            if by_key.get("config") and by_key["config"].status is Status.FAIL:
                log.error("APP_REFUSED doctor config=%s code=%s", by_key["config"].message, by_key["config"].error_code)
                return 2

            if services.backup.due_on_start():
                try:
                    snap = services.backup.snapshot("start")
                    log.info("BACKUP_ON_START backup_id=%s queued=%s", snap.backup_id, snap.queued)
                except Exception as exc:  # never block the start on a backup problem
                    log.warning("BACKUP_ON_START_FAILED %r", exc)

            scheduler = SimpleScheduler()
            actions: dict[str, Callable[[], object]] = {
                "heartbeat": lambda: (state.beat("scheduler"), write_heartbeat(paths, state)),
                "verify_chain": lambda: _verify_chain_job(services, state),
                "db_status": lambda: _db_status_job(ctx, state),
                "backup": lambda: services.backup.snapshot("nightly"),
                "backup_queue_flush": services.backup.flush_queue,
                "db_maintenance": lambda: _db_maintenance_job(services),
                "restore_drill_weekly": lambda: _restore_drill_job(services, settings),
                "session_monitor": services.sessions.monitor,
                "stock_daily_balance": services.inventory.write_daily_balance,
                "hardware_poll": services.hardware.poll,
                "print_queue_flush": lambda: services.print_queue.print_pending() if services.print_queue.pending_count() else None,
                "forecast_nightly": services.forecast.snapshot_all,
                "ai_health": lambda: _ai_health_job(services, state),
                "ai_keepalive": lambda: _ai_keepalive_job(services),
                "help_index": lambda: services.indexer.rebuild() if services.indexer is not None else None,
                "digest_daily": lambda: _publish_job(services, state, "digest"),
                "weekly_report": lambda: _publish_job(services, state, "weekly"),
                "outbox_flush": lambda: _publish_job(services, state, "flush"),
            }
            services.chips_source = lambda: state.chips
            state.chips["publish"] = services.publisher.chip() if services.publisher is not None else ("grey", "Publish: off")
            bind_jobs(scheduler, build_jobs(settings), actions)
            state.scheduler_status = scheduler.status
            scheduler.start()
            log.info("SCHEDULER_STARTED jobs=%d", len(scheduler.status()))

            url = f"http://127.0.0.1:{settings.server.port}"
            if settings.server.https_enabled:
                from vaishnavi.platform.tls import ensure_self_signed  # noqa: PLC0415

                material = ensure_self_signed(paths.state / "tls", hosts=["127.0.0.1", "localhost", __import__("socket").gethostname()])
                if material is not None:
                    state.extra["tls"] = material
                    url = f"https://127.0.0.1:{settings.server.https_port}"
                    log.info("TLS_ENABLED port=%d cert=%s", settings.server.https_port, material[0])
                else:
                    log.warning("TLS_REQUESTED_BUT_UNAVAILABLE staying on http://127.0.0.1:%d code=E-SYS-003", settings.server.port)

            def on_ready() -> None:
                state.beat("request")
                write_heartbeat(paths, state)
                log.info("SERVER_READY url=%s", url)
                if open_browser:
                    from vaishnavi.platform.browser import open_url  # noqa: PLC0415

                    open_url(url, kiosk=settings.server.kiosk_browser)

            server = serve or _default_serve
            server(state, on_ready)
            return 0
        finally:
            if scheduler is not None:
                scheduler.stop()
            if run_id is not None:
                try:
                    assert services is not None
                    closed = services.sessions.close_all()
                    log.info("WORK_SESSIONS_CLOSED_ON_STOP count=%d", closed)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("closing work sessions failed: %r", exc)
                _record_app_run(paths, start=False, run_id=run_id, clean=True)
                try:
                    _clean_close(paths)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("clean close failed: %r", exc)
                log.info("APP_CLEAN_CLOSE")
            lock.release()


def _storage_secret(paths: AppPaths) -> str:
    """Stable secret for NiceGUI's browser-session cookies (``state/ui.secret``, owner-only file) so a restart keeps staff signed in."""
    import os  # noqa: PLC0415
    import secrets  # noqa: PLC0415

    path = paths.state / "ui.secret"
    try:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if len(value) >= 32:
                return value
        value = secrets.token_urlsafe(48)
        paths.state.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("UI_SECRET_CREATED path=%s", path)
        return value
    except OSError as exc:
        log.warning("UI_SECRET_UNAVAILABLE %s — sessions will not survive a restart", exc)
        return secrets.token_urlsafe(48)


def _clean_close(paths: AppPaths) -> None:
    from vaishnavi.db.guardian import DbGuardian  # noqa: PLC0415
    from vaishnavi.db.ledger_store import SqliteLedgerStore  # noqa: PLC0415
    from vaishnavi.services.ledger import LedgerService  # noqa: PLC0415

    DbGuardian(paths, LedgerService(SqliteLedgerStore())).mark_clean_close()


def _verify_chain_job(services: AppServices, state: AppState) -> None:
    conn = sqlite3.connect(services.paths.database)
    try:
        status = services.ledger.run_all(conn)
    finally:
        conn.close()
    state.chips["ledger"] = ("green", "Integrity ok") if status.ok else ("red", "Integrity: " + ", ".join(status.codes))
    if not status.ok:
        services.notifications.notify("HIGH", "INTEGRITY", "Integrity check failed: " + ", ".join(status.codes), deep_link="/reports/integrity", source_ref=",".join(status.codes))


def _db_status_job(ctx: DoctorContext, state: AppState) -> None:
    """Keep the DB chip honest: the start-up value said "Owner account not set up yet" until restart (live run 2)."""
    for check in select_checks(default_checks(), ["database"]):
        result = check.run(ctx)
        chip = _chip_for(result.status, "DB ok", f"DB: {result.message}")
        if state.chips.get("database") != chip:
            log.info("DB_CHIP_CHANGED status=%s code=%s message=%s", result.status.value, result.error_code or "-", result.message)
        state.chips["database"] = chip


def _ai_health_job(services: AppServices, state: AppState) -> None:
    """Keep the AI chip honest: green when the provider answers, amber when down, grey when off."""
    provider = services.ai_provider
    if provider is None or provider.name == "none":
        state.chips["ai_provider"] = ("grey", "AI off")
        return
    health = provider.health()
    if health.ok:
        state.chips["ai_provider"] = ("green", f"AI {provider.name} · {provider.chat_model}")
    else:
        state.chips["ai_provider"] = ("amber", f"AI offline ({health.error_code or '?'})")
        log.debug("AI_HEALTH provider=%s ok=False code=%s detail=%s", provider.name, health.error_code, health.detail)


def _publish_job(services: AppServices, state: AppState, kind: str) -> None:
    """Digest / weekly / flush, then refresh the publish chip; never raises into the scheduler."""
    publisher = services.publisher
    if publisher is None:
        return
    try:
        if kind == "digest":
            result = publisher.daily_digest()
            log.info("PUBLISH_DIGEST file=%s sent=%s failed=%s", result.path.name, ",".join(result.sent), ",".join(result.failed))
        elif kind == "weekly":
            if now_ist().weekday() == services.settings_service.effective().publishing.weekly_weekday:
                result = publisher.weekly_report()
                log.info("PUBLISH_WEEKLY file=%s sent=%s failed=%s", result.path.name, ",".join(result.sent), ",".join(result.failed))
        else:
            publisher.flush()
    except Exception as exc:  # noqa: BLE001 - reporting must never take the shop down
        log.exception("PUBLISH_JOB_FAILED kind=%s error=%s", kind, exc)
    state.chips["publish"] = publisher.chip()


def _ai_keepalive_job(services: AppServices) -> None:
    """A one-token chat so Ollama keeps the model loaded (``keep_alive`` is renewed per request)."""
    from vaishnavi.ai.providers.base import ChatMessage  # noqa: PLC0415

    provider = services.ai_provider
    if provider is None or provider.name != "ollama":
        return
    try:
        provider.chat([ChatMessage("user", "ping")], temperature=0.0, max_tokens=1)
        log.debug("AI_KEEPALIVE ok model=%s", provider.chat_model)
    except Exception as exc:  # noqa: BLE001 - never let a keep-alive crash the scheduler
        log.debug("AI_KEEPALIVE skipped: %s", exc)


def _db_maintenance_job(services: AppServices) -> None:
    if now_ist().day == 1:
        services.guardian.vacuum()
    else:
        conn = sqlite3.connect(services.paths.database)
        try:
            result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            conn.execute("INSERT INTO integrity_check(at, kind, ok, details) VALUES (?, 'FULL_CHECK', ?, ?)", (iso_utc(), 1 if result == "ok" else 0, result[:2000]))
            conn.commit()
        finally:
            conn.close()
        if result != "ok":
            services.notifications.notify("HIGH", "DB_DAMAGED", "Nightly database check failed", deep_link="/settings/backups", source_ref="E-DB-003")
    services.backup.prune()


def _restore_drill_job(services: AppServices, settings: Settings) -> None:
    if now_ist().weekday() != settings.backup.restore_drill_weekday:
        return
    if not services.backup.restore_drill():
        services.notifications.notify("HIGH", "RESTORE_DRILL", "The weekly restore test failed", deep_link="/settings/backups", source_ref="E-BKP-010")


def _default_serve(state: AppState, on_ready: Callable[[], None]) -> None:
    """NiceGUI server (unit 27 provides ``vaishnavi.ui.server``)."""
    try:
        from vaishnavi.ui.server import serve as ui_serve  # noqa: PLC0415
    except ImportError as exc:
        log.error("UI_UNAVAILABLE %s — the web UI is not part of this build", exc)
        raise
    ui_serve(state, on_ready)
