"""Administrator-rights probe for in-app system actions (stdlib; the installer does the actual prompting)."""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("vaishnavi.platform.elevation")


def is_admin() -> bool:
    """True when the process already has administrator / root rights."""
    try:
        if sys.platform.startswith("win"):
            import ctypes  # noqa: PLC0415

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception as exc:
        log.debug("is_admin probe failed: %r", exc)
        return False
