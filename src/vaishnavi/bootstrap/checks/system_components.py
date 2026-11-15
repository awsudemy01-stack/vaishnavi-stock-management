"""Doctor check: libusb · print system · Devanagari font — WARN E-SYS-003 per missing component, in plain words (stdlib)."""

from __future__ import annotations

import ctypes.util
import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.platform.fonts import find_devanagari_font

log = logging.getLogger("vaishnavi.bootstrap.checks.system_components")


def probe_libusb() -> bool:
    """libusb-1.0 loadable (USB printers/scanners via pyusb)."""
    if sys.platform.startswith("win"):
        return True  # Windows uses WinUSB/libusb-win32 bundled with the driver; checked at pairing time
    return bool(ctypes.util.find_library("usb-1.0") or ctypes.util.find_library("usb"))


def probe_print_system() -> bool:
    """CUPS (``lpstat``) on Unix; the Spooler service on Windows."""
    try:
        if sys.platform.startswith("win"):
            proc = subprocess.run(["sc", "query", "Spooler"], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603, S607
            return proc.returncode == 0 and "RUNNING" in proc.stdout.upper()
        return shutil.which("lpstat") is not None
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("print system probe failed: %s", exc)
        return False


def probe_devanagari_font() -> bool:
    """A Hindi-capable font exists (receipts/labels/PDFs)."""
    return find_devanagari_font() is not None


DEFAULT_PROBES: dict[str, Callable[[], bool]] = {"libusb": probe_libusb, "print_system": probe_print_system, "devanagari_font": probe_devanagari_font}


@dataclass
class SystemComponentsCheck:
    """Reports what is missing and what it means for the shop; never FAIL."""

    key: str = "system_components"
    group: str = "software"
    probes: dict[str, Callable[[], bool]] = field(default_factory=lambda: dict(DEFAULT_PROBES))

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Run each probe; one WARN line naming every missing component."""
        missing = [name for name, probe in self.probes.items() if not _safe(probe)]
        if not missing:
            return CheckResult.ok(ctx.t("doctor.system_components.ok"), missing=[])
        parts = [ctx.t(f"doctor.system_components.{name}") for name in missing]
        return CheckResult.warn(ctx.t("doctor.system_components.missing", what="; ".join(parts)), error_code="E-SYS-003", hint=ctx.t("doctor.system_components.hint"), detail=",".join(missing), missing=missing)


def _safe(probe: Callable[[], bool]) -> bool:
    try:
        return bool(probe())
    except Exception as exc:
        log.warning("component probe failed: %r", exc)
        return False


CHECKS = [SystemComponentsCheck()]
