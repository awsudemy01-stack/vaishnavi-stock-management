"""System API — ``/healthz``, ``/api/v1/system/{status,doctor,logs,metrics,notifications}`` (docs/07 §6, ADR-038).

The handlers are plain functions over :class:`AppState` so they are testable without a web framework;
``mount(app, state)`` attaches them to FastAPI (imported lazily, shop PC only). ``/healthz`` is what the
watchdog polls: it carries a heartbeat per loop (request handler, scheduler) and turns 503 when one is
older than 60 s — a hung event loop is then restarted (``E-SYS-020``).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from vaishnavi import __version__
from vaishnavi.core.clock import iso_utc, now_utc
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths

log = logging.getLogger("vaishnavi.api.system")

STALE_AFTER_S: Final = 60.0
LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_ALIASES: Final = {"WARN": "WARNING", "ERR": "ERROR"}


# ----------------------------------------------------------------------------- metrics
class Metrics:
    """In-process counters and small histograms (no third-party client)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {}
        self._hist: dict[str, deque[float]] = {}

    def inc(self, name: str, value: float = 1) -> None:
        """Add to a counter."""
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        """Record a sample (last 1000 kept)."""
        with self._lock:
            self._hist.setdefault(name, deque(maxlen=1000)).append(value)

    def snapshot(self) -> dict[str, Any]:
        """Counters + histogram summaries."""
        with self._lock:
            hists = {}
            for name, values in self._hist.items():
                if values:
                    ordered = sorted(values)
                    hists[name] = {"count": len(ordered), "min": ordered[0], "max": ordered[-1], "avg": sum(ordered) / len(ordered), "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]}
            return {"counters": dict(self.counters), "histograms": hists}


# ----------------------------------------------------------------------------- state
@dataclass
class AppState:
    """Everything the system endpoints read; owned by the start-up orchestrator."""

    settings: Settings
    paths: AppPaths
    config_dir: Path
    started_at: datetime = field(default_factory=now_utc)
    heartbeats: dict[str, datetime] = field(default_factory=dict)
    chips: dict[str, tuple[str, str]] = field(default_factory=dict)
    """status chips: name → (level green|amber|red, text)."""
    metrics: Metrics = field(default_factory=Metrics)
    lang: str = "en"
    scheduler_status: Any = None
    """Callable returning the scheduler job table, set by start-up."""
    doctor_runner: Any = None
    """``callable(checks, fix, lang) -> dict`` set by start-up (the doctor lives above this layer)."""
    extra: dict[str, Any] = field(default_factory=dict)

    def beat(self, loop: str) -> None:
        """Record liveness of a loop (``request`` / ``scheduler``)."""
        self.heartbeats[loop] = now_utc()


# ----------------------------------------------------------------------------- handlers
def healthz(state: AppState) -> dict[str, Any]:
    """Liveness + per-loop heartbeat ages; ``ok`` false when a loop is stale."""
    now = now_utc()
    ages = {name: round((now - at).total_seconds(), 1) for name, at in state.heartbeats.items()}
    stale = sorted(name for name, age in ages.items() if age > STALE_AFTER_S)
    ok = not stale
    return {"ok": ok, "app": "vaishnavi", "status": "ok" if ok else "stale", "version": __version__, "at": iso_utc(now), "heartbeats": ages, "stale": stale, "uptime_s": int((now - state.started_at).total_seconds())}


def http_status_for(health: dict[str, Any]) -> int:
    """200 when healthy, 503 otherwise."""
    return 200 if health.get("ok") else 503


def status(state: AppState) -> dict[str, Any]:
    """What the status page shows."""
    db = state.paths.database
    return {
        "version": __version__,
        "at": iso_utc(),
        "uptime_s": int((now_utc() - state.started_at).total_seconds()),
        "chips": {name: {"level": level, "text": text} for name, (level, text) in state.chips.items()},
        "database": {"path": str(db), "exists": db.exists(), "size_bytes": db.stat().st_size if db.exists() else 0},
        "locations": {"primary": str(state.paths.primary), "secondary": str(state.paths.secondary)},
        "ai": {"provider": state.settings.ai.provider, "enabled": state.settings.ai.enabled},
        "jobs": state.scheduler_status() if callable(state.scheduler_status) else [],
        "heartbeats": healthz(state)["heartbeats"],
    }


def doctor(state: AppState, *, checks: list[str] | None = None, fix: bool = False, lang: str | None = None) -> dict[str, Any]:
    """Run the doctor from inside the running app through the runner start-up injected (its own port is not a conflict)."""
    if not callable(state.doctor_runner):
        log.warning("DOCTOR_RUNNER_MISSING — the start-up did not inject a doctor runner")
        return {"ref": "", "at": iso_utc(), "exit_code": 2, "checks": [], "error": "doctor unavailable in this process"}
    return state.doctor_runner(checks=checks, fix=fix, lang=lang or state.lang)


def log_families(state: AppState) -> list[str]:
    """Sub-folders of the logs root that contain files."""
    root = state.paths.logs
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and any(f.is_file() for f in p.iterdir()))


def logs_tail(state: AppState, *, family: str = "app", level: str = "INFO", limit: int = 200, contains: str = "") -> dict[str, Any]:
    """Last ``limit`` lines of the family's text log at or above ``level``; sandboxed to the logs folder."""
    root = state.paths.logs.resolve()
    folder = (root / family).resolve()
    if not str(folder).startswith(str(root)) or not folder.is_dir():
        log.warning("LOGS_TAIL_REFUSED family=%r", family)
        return {"family": family, "lines": [], "file": None}
    files = sorted((p for p in folder.glob("*.log") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"family": family, "lines": [], "file": None}
    wanted = _LEVEL_ALIASES.get(level.upper(), level.upper())
    min_index = LEVELS.index(wanted) if wanted in LEVELS else 1
    out: list[str] = []
    try:
        with files[0].open("r", encoding="utf-8", errors="replace") as fh:
            lines = deque(fh, maxlen=max(limit * 20, 2000))
    except OSError as exc:
        log.warning("LOGS_TAIL_FAILED %s: %s", files[0], exc)
        return {"family": family, "lines": [], "file": str(files[0])}
    for line in lines:
        level_name = _level_of(line)
        if level_name is not None and LEVELS.index(level_name) < min_index:
            continue
        if contains and contains.lower() not in line.lower():
            continue
        out.append(line.rstrip("\n"))
    return {"family": family, "file": str(files[0]), "lines": out[-limit:]}


def _level_of(line: str) -> str | None:
    parts = line.split("|")
    if len(parts) > 1:
        token = parts[1].strip().upper()
        token = _LEVEL_ALIASES.get(token, token)
        if token in LEVELS:
            return token
    return None


def metrics(state: AppState) -> dict[str, Any]:
    """JSON metrics."""
    snap = state.metrics.snapshot()
    snap["uptime_s"] = int((now_utc() - state.started_at).total_seconds())
    return snap


def metrics_prometheus(state: AppState) -> str:
    """Prometheus text exposition (prefix ``vaishnavi_``)."""
    snap = state.metrics.snapshot()
    lines = [f"vaishnavi_uptime_seconds {int((now_utc() - state.started_at).total_seconds())}"]
    for name, value in sorted(snap["counters"].items()):
        lines.append(f"vaishnavi_{name} {value:g}")
    for name, h in sorted(snap["histograms"].items()):
        for stat in ("count", "min", "max", "avg", "p95"):
            lines.append(f"vaishnavi_{name}_{stat} {h[stat]:g}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- FastAPI mount (shop PC)
def mount(app: Any, state: AppState, *, notifications: Any = None, owner_guard: Any = None) -> None:
    """Attach the routes to a FastAPI app. ``owner_guard`` is a dependency that raises 401 for non-owners."""
    from fastapi import APIRouter, Depends, Response  # noqa: PLC0415

    router = APIRouter()
    deps = [Depends(owner_guard)] if owner_guard else []

    @router.get("/healthz")
    def _healthz() -> Response:
        import json  # noqa: PLC0415

        state.beat("request")
        health = healthz(state)
        return Response(json.dumps(health), status_code=http_status_for(health), media_type="application/json")

    @router.get("/api/v1/system/status", dependencies=deps)
    def _status() -> dict[str, Any]:
        state.beat("request")
        return status(state)

    @router.get("/api/v1/system/doctor", dependencies=deps)
    def _doctor(checks: str = "", fix: bool = False, lang: str | None = None) -> dict[str, Any]:
        return doctor(state, checks=[c for c in checks.split(",") if c] or None, fix=fix, lang=lang)

    @router.get("/api/v1/system/logs", dependencies=deps)
    def _logs(family: str = "app", level: str = "INFO", limit: int = 200, contains: str = "") -> dict[str, Any]:
        return logs_tail(state, family=family, level=level, limit=min(limit, 2000), contains=contains)

    @router.get("/api/v1/system/logs/families", dependencies=deps)
    def _families() -> list[str]:
        return log_families(state)

    @router.get("/api/v1/system/metrics", dependencies=deps)
    def _metrics(format: str = "json") -> Any:  # noqa: A002
        if format == "prometheus":
            return Response(metrics_prometheus(state), media_type="text/plain; version=0.0.4")
        return metrics(state)

    if notifications is not None:

        @router.get("/api/v1/system/notifications", dependencies=deps)
        def _notifications(user_id: int, unread_only: bool = True) -> list[dict[str, Any]]:
            return notifications.unread(user_id=user_id) if unread_only else notifications.recent(user_id=user_id)

    app.include_router(router)
    log.info("SYSTEM_API_MOUNTED")
