"""Browser ↔ server attendance endpoints (docs/05 §6a layers 2–3): ``/api/session/heartbeat`` and ``/api/session/closing``.

Plain handlers over ``WorkSessionService`` (testable without a framework) + ``mount()`` for FastAPI.
The beacon endpoint accepts ``sendBeacon`` bodies (JSON or ``text/plain``) and never fails loudly — a
closing browser cannot read the answer anyway.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from vaishnavi.services.work_session import WorkSessionService

log = logging.getLogger("vaishnavi.api.session")


def _session_id(payload: Any) -> int | None:
    try:
        return int(payload["session_id"]) if isinstance(payload, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def heartbeat(sessions: WorkSessionService, payload: Any) -> dict[str, Any]:
    """15-s heartbeat; ``alive: false`` tells the page its session is gone (return to login)."""
    sid = _session_id(payload)
    if sid is None:
        return {"ok": False, "alive": False, "error": "bad session id"}
    return {"ok": True, "alive": sessions.heartbeat(sid)}


def closing(sessions: WorkSessionService, payload: Any) -> dict[str, Any]:
    """Window-close beacon → ``WINDOW_CLOSED`` (idempotent)."""
    sid = _session_id(payload)
    if sid is None:
        return {"ok": False, "closed": False, "error": "bad session id"}
    return {"ok": True, "closed": sessions.window_closed(sid)}


def parse_body(raw: bytes) -> Any:
    """Beacon bodies arrive as JSON or as ``text/plain`` JSON; anything else → None."""
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def mount(app: Any, sessions: WorkSessionService) -> None:
    """Attach the two routes to FastAPI (shop PC only)."""
    from fastapi import APIRouter, Request  # noqa: PLC0415

    router = APIRouter()

    @router.post("/api/session/heartbeat")
    async def _heartbeat(request: Request) -> dict[str, Any]:
        return heartbeat(sessions, parse_body(await request.body()))

    @router.post("/api/session/closing")
    async def _closing(request: Request) -> dict[str, Any]:
        result = closing(sessions, parse_body(await request.body()))
        log.info("SESSION_CLOSING_BEACON %s", result)
        return result

    app.include_router(router)


HEARTBEAT_JS = """
(function () {
  const sid = %(session_id)d;
  const every = %(every_ms)d;
  let gone = false;
  async function beat() {
    if (gone) return;
    try {
      const r = await fetch('/api/session/heartbeat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: sid})});
      const j = await r.json();
      if (j && j.ok && j.alive === false) { gone = true; window.location.href = '/login'; }
    } catch (e) { /* offline for a moment — the server-side monitor decides */ }
  }
  setInterval(beat, every);
  window.addEventListener('beforeunload', function (e) {
    navigator.sendBeacon('/api/session/closing', new Blob([JSON.stringify({session_id: sid})], {type: 'text/plain'}));
    e.preventDefault(); e.returnValue = '';
  });
})();
"""
"""Injected by the shell for signed-in pages: heartbeat + the ``beforeunload`` beacon (browsers allow no custom dialog)."""
