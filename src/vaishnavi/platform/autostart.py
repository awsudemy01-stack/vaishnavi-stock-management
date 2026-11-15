"""Is the start-at-logon entry present? (Task Scheduler · launchd · systemd --user) — read-only, from inside the app.

Registration itself is the installer's job (``bootstrap/autostart.py``); the identifiers here must stay
equal to the ones used there (``tests/core/test_doctor.py`` pins them).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("vaishnavi.platform.autostart")

TASK_NAME = "Vaishnavi Store Manager"
LAUNCH_LABEL = "com.vaishnavi.store"
SYSTEMD_UNIT = "vaishnavi.service"


def is_registered() -> bool | None:
    """True / False, or None when it cannot be determined on this OS."""
    try:
        if sys.platform.startswith("win"):
            proc = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, timeout=20, check=False)  # noqa: S603, S607
            return proc.returncode == 0
        if sys.platform == "darwin":
            return (Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist").exists()
        if sys.platform.startswith("linux"):
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            return (base / "systemd" / "user" / SYSTEMD_UNIT).exists()
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("autostart probe failed: %s", exc)
        return None
    return None
