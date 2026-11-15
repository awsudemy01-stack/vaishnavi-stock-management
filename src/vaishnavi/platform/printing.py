"""OS printer enumeration and readiness (stdlib): Windows ``Get-Printer`` · CUPS ``lpstat`` · network ``host:9100`` · USB by VID:PID.

Raw ESC/POS submission lives in ``hardware/printer`` (Phase 2); this module only answers *what printers
exist and is the configured one ready* — which must never block selling (docs/05 §10).
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import sys
from dataclasses import dataclass

from vaishnavi.core.settings import HardwareSettings
from vaishnavi.platform import devices

log = logging.getLogger("vaishnavi.platform.printing")

_LPSTAT_RE = re.compile(r"^printer\s+(\S+)\s+(?:is\s+)?(idle|disabled|printing|stopped)", re.IGNORECASE)
_READY_WIN = {"normal", "ok", "idle", "printing", "ready"}


@dataclass(frozen=True)
class SystemPrinter:
    """A printer known to the OS print system."""

    name: str
    ready: bool
    is_default: bool = False
    detail: str = ""
    port: str = ""


@dataclass(frozen=True)
class PrinterStatus:
    """Readiness of the printer the shop configured."""

    kind: str  # none | usb | network | system
    address: str
    ready: bool
    detail: str = ""

    @property
    def configured(self) -> bool:
        """False when the shop has not set up a printer yet (bills go to PDF)."""
        return self.kind != "none"


# ----------------------------------------------------------------------------- parsers
def parse_lpstat(text: str) -> list[SystemPrinter]:
    """``lpstat -p -d`` output."""
    printers: list[SystemPrinter] = []
    default = ""
    reasons: dict[str, str] = {}
    last = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _LPSTAT_RE.match(line.strip())
        if m:
            name, state = m.group(1), m.group(2).lower()
            printers.append(SystemPrinter(name, state in ("idle", "printing"), False, state))
            last = name
            continue
        if line.lower().startswith("system default destination:"):
            default = line.split(":", 1)[1].strip()
        elif line.strip().lower().startswith("reason:") and last:
            reasons[last] = line.split(":", 1)[1].strip()
    return [SystemPrinter(p.name, p.ready, p.name == default, (p.detail + (" — " + reasons[p.name] if p.name in reasons else "")).strip(" —"), "") for p in printers]


def parse_get_printer(text: str) -> list[SystemPrinter]:
    """``Get-Printer | Select Name,PrinterStatus,PortName,Default | ConvertTo-Json``."""
    try:
        data = json.loads(text or "[]")
    except ValueError as exc:
        log.warning("Get-Printer output not JSON: %s", exc)
        return []
    items = data if isinstance(data, list) else [data]
    out: list[SystemPrinter] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("PrinterStatus", "")).lower()
        out.append(SystemPrinter(str(item.get("Name", "")), status in _READY_WIN or status.isdigit() and status in ("0", "3"), bool(item.get("Default", False)), status, str(item.get("PortName", ""))))
    return out


# ----------------------------------------------------------------------------- live
def list_system_printers() -> list[SystemPrinter]:
    """Printers of the OS print system (empty on failure)."""
    try:
        if sys.platform.startswith("win"):
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Printer | Select-Object Name,PrinterStatus,PortName,@{n='Default';e={$_.Name -eq (Get-CimInstance Win32_Printer -Filter 'Default=TRUE').Name}} | ConvertTo-Json -Compress"]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, check=False)  # noqa: S603
            return parse_get_printer(proc.stdout) if proc.returncode == 0 else []
        proc = subprocess.run(["lpstat", "-p", "-d"], capture_output=True, text=True, timeout=15, check=False)  # noqa: S603, S607
        return parse_lpstat(proc.stdout) if proc.returncode == 0 else []
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("printer enumeration failed: %s", exc)
        return []


def network_printer_ready(address: str, *, timeout: float = 2.0) -> bool:
    """TCP connect to ``host:port`` (default 9100)."""
    host, _, port_text = (address or "").partition(":")
    if not host:
        return False
    try:
        port = int(port_text) if port_text else 9100
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError) as exc:
        log.debug("network printer %s not ready: %s", address, exc)
        return False


def printer_status(hw: HardwareSettings) -> PrinterStatus:
    """Readiness of the configured receipt printer (never raises)."""
    kind, address = hw.printer_kind, hw.printer_address
    if kind == "none":
        return PrinterStatus("none", "", False, "not set up — bills are saved as PDF")
    if kind == "network":
        ready = network_printer_ready(address)
        return PrinterStatus(kind, address, ready, "reachable" if ready else f"no answer from {address}")
    if kind == "usb":
        dev = devices.find_device(address)
        return PrinterStatus(kind, address, dev is not None, dev.name if dev else f"USB printer {address} not connected")
    if kind == "system":
        for p in list_system_printers():
            if p.name == address:
                return PrinterStatus(kind, address, p.ready, p.detail or ("ready" if p.ready else "not ready"))
        return PrinterStatus(kind, address, False, f"printer '{address}' not found in the print system")
    if kind == "file":
        from pathlib import Path  # noqa: PLC0415

        parent = Path(address).expanduser().parent
        return PrinterStatus(kind, address, parent.is_dir(), "writes jobs to a file" if parent.is_dir() else f"folder {parent} missing")
    return PrinterStatus(kind, address, False, f"unknown printer kind {kind}")
