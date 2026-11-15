"""Bridge between NiceGUI's per-browser storage and :class:`ClientState` (thin; imported only inside the UI).

``app.storage.user`` is a JSON dict keyed by a browser cookie; we keep one entry ``"vsm"`` holding
``ClientState.to_dict()``. Reading is tolerant (garbage → anonymous); writing is explicit so a page
decides when the state changed.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from vaishnavi.ui.state import ClientState

log = logging.getLogger("vaishnavi.ui.session")

STORAGE_KEY: Final = "vsm"


def load_client(app: Any) -> ClientState:
    """Current browser's state (anonymous when nothing is stored)."""
    try:
        data = app.storage.user.get(STORAGE_KEY)
    except Exception as exc:  # storage not available (page without a client) → anonymous
        log.debug("client storage unavailable: %s", exc)
        return ClientState.anonymous()
    return ClientState.from_dict(data)


def save_client(app: Any, client: ClientState) -> None:
    """Persist after a change (sign-in, lock, language…)."""
    try:
        app.storage.user[STORAGE_KEY] = client.to_dict()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("client storage write failed: %s", exc)
