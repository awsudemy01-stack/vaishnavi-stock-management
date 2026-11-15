"""The documented job table (docs/07 §7) as data, bound to real callables by the start-up orchestrator.

``build_jobs(settings)`` only *describes* the jobs (name, schedule, what to call by key); ``bind_jobs``
attaches implementations from `AppServices` so the table is testable without them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from vaishnavi.core.settings import Settings
from vaishnavi.scheduler.simple import SimpleScheduler

log = logging.getLogger("vaishnavi.scheduler.jobs")


@dataclass(frozen=True)
class JobSpec:
    """A row of the job table."""

    name: str
    action: str
    every_s: float | None = None
    daily_at: str | None = None
    run_immediately: bool = False
    note: str = ""


def build_jobs(settings: Settings) -> list[JobSpec]:
    """Phase-1 jobs (the POS-only ones — hardware poll, print queue — are listed and bound when their services exist)."""
    jobs = [
        JobSpec("heartbeat", "heartbeat", every_s=15, run_immediately=True, note="scheduler liveness for /healthz and the watchdog"),
        JobSpec("session_monitor", "session_monitor", every_s=15, note="closes work sessions whose heartbeat is > 60 s old"),
        JobSpec("hardware_poll", "hardware_poll", every_s=10, note="printer ready? new printer? paired scanner present?"),
        JobSpec("print_queue_flush", "print_queue_flush", every_s=60, note="print queued receipts once the printer is back"),
        JobSpec("alerts", "alerts", every_s=60, note="evaluate alert rules → notification centre"),
        JobSpec("backup_queue_flush", "backup_queue_flush", every_s=300, note="move queued snapshots to the backup drive when it is back"),
        JobSpec("verify_chain", "verify_chain", every_s=3600, run_immediately=True, note="hash chains + invoice gaps + stock reconciliation"),
        JobSpec("backup_nightly", "backup", daily_at=settings.backup.nightly_time, note="full snapshot + prune"),
        JobSpec("db_maintenance", "db_maintenance", daily_at=_plus_minutes(settings.backup.nightly_time, 10), note="full integrity_check; monthly VACUUM"),
        JobSpec("stock_daily_balance", "stock_daily_balance", daily_at=_plus_minutes(settings.backup.nightly_time, 5), note="end-of-day qty per product"),
        JobSpec("forecast_nightly", "forecast_nightly", daily_at=_plus_minutes(settings.backup.nightly_time, 15), note="forecast snapshot per active product (after the stock balance)"),
        JobSpec("log_prune", "log_prune", daily_at="23:30", note="retention per logging.toml"),
        JobSpec("restore_drill", "restore_drill_weekly", daily_at="20:30", note=f"weekly on weekday {settings.backup.restore_drill_weekday}"),
    ]
    if settings.backup.interval_minutes > 0:
        jobs.append(JobSpec("backup_interval", "backup", every_s=settings.backup.interval_minutes * 60, note="opt-in hot snapshots"))
    if settings.ai.enabled and settings.ai.provider != "none":
        jobs.append(JobSpec("ai_health", "ai_health", every_s=60, run_immediately=True, note="provider reachable? models present? → AI chip"))
        jobs.append(JobSpec("help_index", "help_index", every_s=6 * 3600, run_immediately=True, note="chunk + embed the user guide for the assistant (skips unchanged text)"))
    if settings.ai.enabled and settings.ai.provider == "ollama":
        jobs.append(JobSpec("ai_keepalive", "ai_keepalive", every_s=600, note="one-token chat keeps the local model resident (free)"))
    if settings.publishing.enabled and settings.publishing.channels:
        jobs.append(JobSpec("digest_daily", "digest_daily", daily_at=settings.publishing.digest_time, note="daily digest → channels"))
        jobs.append(JobSpec("weekly_report", "weekly_report", daily_at=_plus_minutes(settings.publishing.digest_time, 5), note=f"weekly owner report on weekday {settings.publishing.weekly_weekday}"))
        jobs.append(JobSpec("outbox_flush", "outbox_flush", every_s=300, note="retry queued report deliveries; publish chip"))
    return jobs


def _plus_minutes(hhmm: str, minutes: int) -> str:
    hh, mm = (int(p) for p in hhmm.split(":"))
    total = (hh * 60 + mm + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def bind_jobs(scheduler: SimpleScheduler, specs: list[JobSpec], actions: Mapping[str, Callable[[], object]]) -> list[str]:
    """Attach callables; jobs whose action has no implementation yet are skipped (logged) — not errors."""
    bound: list[str] = []
    for spec in specs:
        func = actions.get(spec.action)
        if func is None:
            log.debug("JOB_UNBOUND name=%s action=%s (no implementation in this build)", spec.name, spec.action)
            continue
        if spec.every_s is not None:
            scheduler.every(seconds=spec.every_s, name=spec.name, func=func, run_immediately=spec.run_immediately)
        elif spec.daily_at:
            scheduler.daily(at=spec.daily_at, name=spec.name, func=func)
        bound.append(spec.name)
    log.info("JOBS_BOUND %s", ",".join(bound) or "-")
    return bound
