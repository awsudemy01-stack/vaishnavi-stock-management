"""SimpleScheduler — interval and daily (IST) jobs in one background thread (stdlib).

Chosen over APScheduler for Phase 1 (ADR note in docs/12): the shop PC restarts every day, so
persistence of schedules is unnecessary, and a 150-line scheduler the doctor can introspect beats a
dependency the installer must verify. Jobs never raise into the loop: a failure is logged
``JOB_FAILED`` with the traceback and counted; the heartbeat job keeps ``/healthz`` honest.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vaishnavi.core.clock import IST, now_utc

log = logging.getLogger("vaishnavi.scheduler")


@dataclass
class Job:
    """One scheduled callable."""

    name: str
    func: Callable[[], object]
    interval_s: float | None = None
    daily_at: str | None = None  # "HH:MM" IST
    next_run: datetime | None = None
    runs: int = 0
    failures: int = 0
    last_run: datetime | None = None
    last_error: str = ""
    last_ms: int = 0
    enabled: bool = True


@dataclass
class SimpleScheduler:
    """Tick-based scheduler; ``start()`` runs ``run_pending`` in a daemon thread."""

    clock: Callable[[], datetime] = field(default=now_utc)
    sleep: Callable[[float], None] = field(default=time.sleep)
    tick_s: float = 1.0
    _jobs: list[Job] = field(default_factory=list)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- definition ---------------------------------------------------------------------------
    def every(self, *, seconds: float, name: str, func: Callable[[], object], run_immediately: bool = True) -> Job:
        """Repeat every ``seconds``; the first run is on the first tick unless told otherwise."""
        now = self.clock()
        job = Job(name, func, interval_s=seconds, next_run=now if run_immediately else now + timedelta(seconds=seconds))
        self._jobs.append(job)
        log.debug("JOB_ADDED name=%s every=%ss", name, seconds)
        return job

    def daily(self, *, at: str, name: str, func: Callable[[], object]) -> Job:
        """Run once a day at ``HH:MM`` shop time (IST)."""
        job = Job(name, func, daily_at=at)
        job.next_run = self._next_daily(at, self.clock())
        self._jobs.append(job)
        log.debug("JOB_ADDED name=%s daily_at=%s next=%s", name, at, job.next_run)
        return job

    @staticmethod
    def _next_daily(at: str, now: datetime) -> datetime:
        hh, mm = (int(p) for p in at.split(":"))
        local = now.astimezone(IST)
        candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(now.tzinfo)

    # -- execution ----------------------------------------------------------------------------
    def run_pending(self) -> int:
        """Run every due job once; returns how many ran."""
        now = self.clock()
        ran = 0
        for job in self._jobs:
            if not job.enabled or job.next_run is None or job.next_run > now:
                continue
            ran += 1
            started = time.perf_counter()
            try:
                job.func()
                job.runs += 1
                job.last_error = ""
                log.debug("JOB_OK name=%s ms=%d", job.name, int((time.perf_counter() - started) * 1000))
            except Exception as exc:  # the loop must survive any job
                job.failures += 1
                job.last_error = repr(exc)
                log.error("JOB_FAILED name=%s error=%r\n%s", job.name, exc, traceback.format_exc())
            job.last_ms = int((time.perf_counter() - started) * 1000)
            job.last_run = now
            if job.interval_s is not None:
                job.next_run = now + timedelta(seconds=job.interval_s)
            elif job.daily_at:
                job.next_run = self._next_daily(job.daily_at, now + timedelta(seconds=1))
        return ran

    def _loop(self) -> None:
        log.info("SCHEDULER_LOOP_START jobs=%d", len(self._jobs))
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception as exc:  # pragma: no cover - defensive
                log.error("SCHEDULER_TICK_FAILED %r", exc)
            self.sleep(self.tick_s)
        log.info("SCHEDULER_LOOP_END")

    def start(self) -> None:
        """Start the background thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vaishnavi-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- introspection ------------------------------------------------------------------------
    def status(self) -> list[dict[str, object]]:
        """For ``/system/status`` and the doctor."""
        return [
            {"name": j.name, "every_s": j.interval_s, "daily_at": j.daily_at, "next_run": j.next_run.isoformat() if j.next_run else None, "runs": j.runs, "failures": j.failures, "last_ms": j.last_ms, "last_error": j.last_error, "enabled": j.enabled}
            for j in self._jobs
        ]

    def trigger(self, name: str) -> bool:
        """Run a job now (Settings → System → run job)."""
        for job in self._jobs:
            if job.name == name:
                job.next_run = self.clock()
                return True
        return False
