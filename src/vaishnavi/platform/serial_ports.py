"""Serial port discovery for weighing scales (stdlib; ``pyserial`` used when installed)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger("vaishnavi.platform.serial_ports")

_UNIX_PATTERNS = ("ttyUSB*", "ttyACM*", "tty.usb*", "cu.usb*", "ttyS[0-9]*")


def scan_dev(dev: Path = Path("/dev")) -> list[str]:
    """Serial-looking device nodes under ``dev``."""
    found: set[str] = set()
    if not dev.is_dir():
        return []
    for pattern in _UNIX_PATTERNS:
        for p in dev.glob(pattern):
            found.add(str(p))
    return sorted(found)


def _windows_ports() -> list[str]:
    import winreg  # noqa: PLC0415

    ports: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                ports.append(str(value))
                i += 1
    except OSError as exc:
        log.debug("SERIALCOMM registry key: %s", exc)
    return sorted(ports)


def list_ports() -> list[str]:
    """Available serial ports (``COM3`` / ``/dev/ttyUSB0``); empty on failure."""
    try:
        try:
            from serial.tools import list_ports as pyserial_ports  # noqa: PLC0415

            return sorted(p.device for p in pyserial_ports.comports())
        except ImportError:
            pass
        if sys.platform.startswith("win"):
            return _windows_ports()
        return scan_dev()
    except Exception as exc:  # a probe never crashes the app
        log.warning("serial port enumeration failed: %r", exc)
        return []


def port_present(port: str, ports: Sequence[str] | None = None) -> bool:
    """Is the configured port among the available ones (case-insensitive)?"""
    available = list_ports() if ports is None else ports
    return port.lower() in {p.lower() for p in available}
