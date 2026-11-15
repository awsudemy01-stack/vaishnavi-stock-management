"""Console reporter for the installer: ✔ / ! / ✖ / – lines in plain language (stdlib only).

The legacy Windows console (cp1252) cannot print the glyphs, so the reporter probes the stream's
encoding once and falls back to ASCII markers. Nothing here is OS-specific; it only asks the stream.
Every line printed is also mirrored to the log at DEBUG so the log alone tells the full story.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TextIO

log = logging.getLogger("bootstrap.console")

UNICODE_GLYPHS = {"ok": "✔", "warn": "!", "fail": "✖", "skip": "–", "info": "i", "ask": "?", "busy": "⟳"}
ASCII_GLYPHS = {"ok": "[OK]", "warn": "[! ]", "fail": "[X ]", "skip": "[- ]", "info": "[i ]", "ask": "[? ]", "busy": "[..]"}

RULE_WIDTH = 92


def stream_supports_unicode(stream: TextIO) -> bool:
    """True when the stream's encoding can represent the installer's glyphs."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "✔✖–⟳".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


@dataclass
class Console:
    """Writes human-facing lines; never used for data the program needs back."""

    stream: TextIO = sys.stdout
    unicode: bool | None = None

    def __post_init__(self) -> None:
        if self.unicode is None:
            self.unicode = stream_supports_unicode(self.stream)
        self._glyphs = UNICODE_GLYPHS if self.unicode else ASCII_GLYPHS
        log.debug("console: unicode=%s encoding=%s", self.unicode, getattr(self.stream, "encoding", None))

    # -- primitives -------------------------------------------------------------------------
    def line(self, text: str = "") -> None:
        """Print a raw line (and mirror it to the log)."""
        self._write(text + "\n")
        if text.strip():
            log.debug("console: %s", text)

    def rule(self) -> None:
        """Print a horizontal separator."""
        self._write(("─" if self.unicode else "-") * RULE_WIDTH + "\n")

    def header(self, title: str, right: str = "") -> None:
        """Print the setup banner with an optional right-aligned hint (log path)."""
        gap = max(1, RULE_WIDTH - len(title) - len(right))
        self.line(f"{title}{' ' * gap}{right}" if right else title)
        self.rule()

    def _mark(self, kind: str, text: str, indent: int = 1) -> None:
        glyph = self._glyphs[kind]
        pad = " " * indent
        self._write(f"{pad}{glyph}  {text}\n")
        log.debug("console[%s]: %s", kind, text)

    # -- semantic lines ---------------------------------------------------------------------
    def ok(self, text: str) -> None:
        """A step succeeded."""
        self._mark("ok", text)

    def warn(self, text: str) -> None:
        """A step succeeded with something the owner should look at."""
        self._mark("warn", text)

    def fail(self, text: str) -> None:
        """A step failed; the installer will stop."""
        self._mark("fail", text)

    def skip(self, text: str) -> None:
        """A step was not needed."""
        self._mark("skip", text)

    def info(self, text: str) -> None:
        """Neutral information."""
        self._mark("info", text)

    def busy(self, text: str) -> None:
        """A step is in progress (long operation)."""
        self._mark("busy", text)

    def ask(self, text: str) -> None:
        """A question is being put to the owner (the wizard prints the options beneath)."""
        self._mark("ask", text)

    def hint(self, text: str) -> None:
        """Indented 'what to do' line under a warn/fail."""
        self.line(f"      {text}")

    def progress(self, label: str, done: int, total: int) -> None:
        """Single-line progress that rewrites itself; call ``progress_end`` when finished."""
        total = max(total, 1)
        pct = min(100, int(done * 100 / total))
        filled = pct // 10
        bar = ("■" * filled + "□" * (10 - filled)) if self.unicode else ("#" * filled + "." * (10 - filled))
        self._write(f"\r {self._glyphs['busy']}  {label} {bar} {pct:3d} %")
        self.stream.flush()

    def progress_end(self) -> None:
        """Terminate a progress line."""
        self._write("\n")

    # -- internals --------------------------------------------------------------------------
    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            # Encoding probe was wrong (rare): degrade gracefully instead of crashing the installer.
            self.stream.write(text.encode("ascii", "replace").decode("ascii"))
        self.stream.flush()
