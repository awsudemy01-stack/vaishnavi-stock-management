"""Open the shop UI in the browser — app/kiosk mode when a Chromium browser is present, plain tab otherwise (stdlib)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger("vaishnavi.platform.browser")

_CHROMIUM_NAMES = ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser", "brave", "brave-browser")


def _candidate_paths() -> list[str]:
    """Well-known install locations that are not on PATH."""
    if sys.platform.startswith("win"):
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("LOCALAPPDATA", "")]
        rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        return [str(Path(r) / s) for r in roots if r for s in rel]
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return []


def find_chromium() -> str | None:
    """Executable of a Chromium-based browser, or None."""
    for name in _CHROMIUM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in _candidate_paths():
        if Path(path).exists():
            return path
    return None


def kiosk_command(url: str, *, kiosk: bool = True) -> list[str] | None:
    """Command line for app mode (``--app=URL``), ``--kiosk`` when asked; None when no Chromium exists."""
    exe = find_chromium()
    if not exe:
        return None
    cmd = [exe, f"--app={url}", "--no-first-run", "--no-default-browser-check", "--disable-translate"]
    if kiosk:
        cmd.append("--start-maximized")
    return cmd


def open_url(url: str, *, kiosk: bool = True) -> bool:
    """Open the UI; True when something was launched."""
    cmd = kiosk_command(url, kiosk=kiosk)
    if cmd:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
            log.info("BROWSER_OPENED mode=app exe=%s", cmd[0])
            return True
        except OSError as exc:
            log.warning("app-mode browser failed (%s) — falling back to the default browser", exc)
    try:
        ok = webbrowser.open(url, new=1)
        log.info("BROWSER_OPENED mode=default ok=%s", ok)
        return bool(ok)
    except Exception as exc:
        log.warning("default browser failed: %r", exc)
        return False
