"""Locate a Devanagari-capable font for receipts, labels and PDFs (stdlib)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger("vaishnavi.platform.fonts")

KNOWN_FILES: tuple[str, ...] = (
    "NotoSansDevanagari-Regular.ttf",
    "NotoSansDevanagari-Regular.otf",
    "NotoSerifDevanagari-Regular.ttf",
    "Lohit-Devanagari.ttf",
    "Mangal.ttf",
    "Nirmala.ttf",
    "Nirmala.ttc",
    "NirmalaUI.ttf",
    "Devanagari Sangam MN.ttc",
    "Kohinoor Devanagari.ttc",
    "gargi.ttf",
    "Sahadeva.ttf",
)


def default_font_dirs() -> list[Path]:
    """Where fonts live on this OS."""
    if sys.platform.startswith("win"):
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return [windir / "Fonts", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"]
    if sys.platform == "darwin":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path.home() / ".local" / "share" / "fonts"]


def find_devanagari_font(dirs: Sequence[Path] | None = None) -> Path | None:
    """First known Devanagari font file found under the given (or default) folders."""
    for folder in dirs if dirs is not None else default_font_dirs():
        if not folder or not folder.is_dir():
            continue
        lower = {name.lower() for name in KNOWN_FILES}
        try:
            for p in folder.rglob("*"):
                if p.is_file() and p.name.lower() in lower:
                    log.debug("devanagari font: %s", p)
                    return p
        except OSError as exc:
            log.debug("font scan failed in %s: %s", folder, exc)
    return None
