"""USB device enumeration by VID:PID — stdlib on every OS, ``pyusb`` when present (used for scanner pairing / presence).

Windows: ``Get-PnpDevice -PresentOnly`` (PowerShell, JSON) · Linux: ``/sys/bus/usb/devices`` · macOS:
``system_profiler SPUSBDataType -json``. Every path returns ``[]`` on failure (logged) — a hardware probe
must never crash the doctor or the POS.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("vaishnavi.platform.devices")

_VID_PID_RE = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]{1,4})\s*[:\-_ ]\s*(?:0x)?([0-9a-fA-F]{1,4})\s*$")
_WIN_ID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")
SYSFS_USB = Path("/sys/bus/usb/devices")


@dataclass(frozen=True)
class UsbDevice:
    """One attached USB device."""

    vid: str
    pid: str
    name: str = ""
    manufacturer: str = ""
    device_class: str = ""
    path: str = ""

    @property
    def vid_pid(self) -> str:
        """``VID:PID`` upper-case."""
        return f"{self.vid}:{self.pid}"


def normalise_vid_pid(text: str) -> str:
    """``'c2e:b61'`` / ``'0C2E-0B61'`` / ``'0x0c2e 0x0b61'`` → ``'0C2E:0B61'``; ``ValueError`` otherwise."""
    m = _VID_PID_RE.match(text or "")
    if not m:
        raise ValueError(f"not a VID:PID: {text!r}")
    return f"{int(m.group(1), 16):04X}:{int(m.group(2), 16):04X}"


# ----------------------------------------------------------------------------- parsers (pure)
def parse_linux_sysfs(root: Path = SYSFS_USB) -> list[UsbDevice]:
    """Walk ``<root>/*/idVendor`` entries."""
    out: list[UsbDevice] = []
    if not root.is_dir():
        return out
    for node in sorted(root.iterdir()):
        vid_file = node / "idVendor"
        if not vid_file.exists():
            continue
        try:
            vid = vid_file.read_text(encoding="utf-8").strip()
            pid = (node / "idProduct").read_text(encoding="utf-8").strip()
            name = _read(node / "product")
            manu = _read(node / "manufacturer")
            cls = _read(node / "bDeviceClass")
        except OSError as exc:
            log.debug("sysfs read failed for %s: %s", node, exc)
            continue
        out.append(UsbDevice(vid.upper().zfill(4), pid.upper().zfill(4), name, manu, cls, str(node)))
    return out


def parse_windows_pnp(text: str) -> list[UsbDevice]:
    """Output of ``Get-PnpDevice -PresentOnly | ConvertTo-Json`` (object or list)."""
    try:
        data = json.loads(text or "[]")
    except ValueError as exc:
        log.warning("Get-PnpDevice output not JSON: %s", exc)
        return []
    items = data if isinstance(data, list) else [data]
    out: list[UsbDevice] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        instance = str(item.get("InstanceId", ""))
        m = _WIN_ID_RE.search(instance)
        if not m or not instance.upper().startswith("USB"):
            continue
        out.append(UsbDevice(m.group(1).upper(), m.group(2).upper(), str(item.get("FriendlyName", "")), "", str(item.get("Class", "")), instance))
    return out


def parse_macos_profiler(text: str) -> list[UsbDevice]:
    """Output of ``system_profiler SPUSBDataType -json`` (nested hubs are walked)."""
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        log.warning("system_profiler output not JSON: %s", exc)
        return []
    out: list[UsbDevice] = []

    def walk(items: Any) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            vid, pid = _hex_prefix(item.get("vendor_id")), _hex_prefix(item.get("product_id"))
            if vid and pid:
                out.append(UsbDevice(vid, pid, str(item.get("_name", "")), str(item.get("manufacturer", "")), "", ""))
            walk(item.get("_items"))

    for bus in data.get("SPUSBDataType", []):
        walk(bus.get("_items"))
    return out


def _hex_prefix(value: Any) -> str:
    if not value:
        return ""
    m = re.match(r"\s*(?:0x)?([0-9a-fA-F]{1,4})", str(value))
    return f"{int(m.group(1), 16):04X}" if m else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ----------------------------------------------------------------------------- live enumeration
def list_usb_devices() -> list[UsbDevice]:
    """Attached USB devices (empty on failure)."""
    try:
        if sys.platform.startswith("win"):
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-PnpDevice -PresentOnly | Select-Object InstanceId,FriendlyName,Status,Class | ConvertTo-Json -Compress"]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, check=False)  # noqa: S603
            return parse_windows_pnp(proc.stdout) if proc.returncode == 0 else []
        if sys.platform == "darwin":
            proc = subprocess.run(["system_profiler", "SPUSBDataType", "-json"], capture_output=True, text=True, timeout=25, check=False)  # noqa: S603, S607
            return parse_macos_profiler(proc.stdout) if proc.returncode == 0 else []
        return parse_linux_sysfs()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("USB enumeration failed: %s", exc)
        return []


def find_device(vid_pid: str) -> UsbDevice | None:
    """The attached device with this ``VID:PID`` (any spelling), or None."""
    try:
        wanted = normalise_vid_pid(vid_pid)
    except ValueError:
        log.warning("find_device: bad id %r", vid_pid)
        return None
    for dev in list_usb_devices():
        if dev.vid_pid == wanted:
            return dev
    return None
