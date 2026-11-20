"""Watchdog decisions from ``state/heartbeat.json`` (docs/07 §7) — pure, so the installer's ``--watchdog`` loop
and the ``restore`` command's "is the app running?" guard share one rule.

The app writes the file every 15 s (``bootstrap.startup.write_heartbeat``). Missing or unreadable → the app is
not running (**start**); fresh → **ok**; older than :data:`STALE_AFTER_S` → the process is hung (**restart**,
with the pid to stop first).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

log = logging.getLogger("vaishnavi.platform.watchdog")

STALE_AFTER_S: Final = 90.0
HEARTBEAT_FILE: Final = "heartbeat.json"
STOP_MARKER_FILE: Final = "stop-requested"
"""Written next to the heartbeat by whoever stops the app *on purpose* (``main.py --stop``, Settings → System →
Shut down, ``--fresh-start``). The supervising watchdog in the installer window sees it and does **not**
relaunch — otherwise every deliberate stop was followed by an automatic restart (live run 5)."""


def request_stop(state_dir: Path, *, reason: str) -> Path:
    """Leave the marker (best effort; the stop itself must not depend on it)."""
    path = Path(state_dir) / STOP_MARKER_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reason": reason, "at": time.time()}), encoding="utf-8")
        log.info("WATCHDOG_STOP_MARKER written reason=%s", reason)
    except OSError as exc:
        log.warning("WATCHDOG_STOP_MARKER_FAILED %s", exc)
    return path


def consume_stop_request(state_dir: Path) -> str | None:
    """The reason when a deliberate stop was requested (and remove the marker), else None."""
    path = Path(state_dir) / STOP_MARKER_FILE
    if not path.exists():
        return None
    reason = "stop requested"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        reason = str(data.get("reason", reason)) if isinstance(data, dict) else reason
    except (OSError, ValueError):
        pass
    try:
        path.unlink()
    except OSError as exc:
        log.debug("stop marker unlink: %s", exc)
    return reason


@dataclass(frozen=True)
class Decision:
    """What the watchdog should do."""

    action: str  # ok | start | restart
    age_s: float | None
    pid: int | None
    detail: str = ""


def _parse_at(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            text = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def read_heartbeat(state_dir: Path) -> tuple[float | None, int | None]:
    """``(epoch seconds of the last beat, pid)`` or ``(None, None)`` when absent/unreadable."""
    path = Path(state_dir) / HEARTBEAT_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    at = _parse_at(data.get("at"))
    pid = data.get("pid")
    return at, int(pid) if isinstance(pid, int) else None


def decide(state_dir: Path, *, now: float | None = None, stale_after_s: float = STALE_AFTER_S) -> Decision:
    """The rule."""
    now = time.time() if now is None else now
    at, pid = read_heartbeat(state_dir)
    if at is None:
        log.debug("WATCHDOG no heartbeat in %s -> start", state_dir)
        return Decision("start", None, None, "no heartbeat file")
    age = max(0.0, now - at)
    if age > stale_after_s:
        log.warning("WATCHDOG stale heartbeat age=%.0fs pid=%s -> restart", age, pid)
        return Decision("restart", age, pid, f"heartbeat {age:.0f}s old")
    return Decision("ok", age, pid)


def app_is_running(state_dir: Path, *, now: float | None = None) -> bool:
    """True when a fresh heartbeat exists (used to refuse a restore while the app is up)."""
    return decide(state_dir, now=now).action == "ok"
