"""ESC/POS transports — how raw receipt bytes reach the printer (stdlib; ``pywin32`` optional on Windows).

* **network** ``host:9100`` — a plain TCP socket (every Ethernet/Wi-Fi thermal printer).
* **system** ``<OS printer name>`` — Windows: ``win32print`` RAW datatype when ``pywin32`` is installed
  (the installer adds it), else a ``copy /B`` to the shared printer as a last resort; macOS/Linux:
  ``lp -d <name> -o raw``.
* **usb** ``VID:PID`` — Linux: the ``/dev/usb/lp*`` node of that device; Windows/macOS: a USB receipt
  printer is always also installed as an OS printer, so we send through the spooler of the printer
  whose port matches, or fall back to the default printer.
* **file** — writes the job to a path (tests, the "save as .bin" debug action).

Every transport raises ``PrinterError(E-PRN-001)`` on failure; the queue turns that into a pending row.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vaishnavi.core.errors import PrinterError
from vaishnavi.core.settings import HardwareSettings

log = logging.getLogger("vaishnavi.hardware.printer.escpos")


class Transport(Protocol):
    """Accepts a complete ESC/POS job."""

    def send(self, data: bytes) -> None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class NetworkTransport:
    """``host[:port]`` (default 9100)."""

    address: str
    timeout: float = 5.0

    def send(self, data: bytes) -> None:
        """Connect, write, close."""
        host, _, port_text = self.address.partition(":")
        port = int(port_text) if port_text else 9100
        try:
            with socket.create_connection((host, port), timeout=self.timeout) as sock:
                sock.sendall(data)
        except OSError as exc:
            raise PrinterError("E-PRN-001", detail=f"network {self.address}: {exc}") from exc
        log.debug("PRINT_SENT transport=network address=%s bytes=%d", self.address, len(data))


@dataclass(frozen=True)
class FileTransport:
    """Append jobs to a file (debug / tests)."""

    path: Path

    def send(self, data: bytes) -> None:
        """Write."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as fh:
                fh.write(data)
        except OSError as exc:
            raise PrinterError("E-PRN-001", detail=f"file {self.path}: {exc}") from exc
        log.debug("PRINT_SENT transport=file path=%s bytes=%d", self.path, len(data))


@dataclass(frozen=True)
class SystemTransport:
    """OS print system, RAW."""

    printer_name: str

    def send(self, data: bytes) -> None:
        """Windows → win32print; POSIX → lp -o raw."""
        if sys.platform.startswith("win"):
            _send_win32(self.printer_name, data)
        else:
            _send_lp(self.printer_name, data)
        log.debug("PRINT_SENT transport=system printer=%s bytes=%d", self.printer_name, len(data))


@dataclass(frozen=True)
class UsbTransport:
    """``VID:PID`` — Linux device node, otherwise via the OS printer on that USB port."""

    vid_pid: str

    def send(self, data: bytes) -> None:
        """Best available path for the OS."""
        if sys.platform.startswith("linux"):
            node = _linux_lp_node(self.vid_pid)
            if node is not None:
                try:
                    with node.open("wb") as fh:
                        fh.write(data)
                except OSError as exc:
                    raise PrinterError("E-PRN-001", detail=f"usb {node}: {exc}") from exc
                log.debug("PRINT_SENT transport=usb node=%s bytes=%d", node, len(data))
                return
        name = _os_printer_for_usb(self.vid_pid)
        if name is None:
            raise PrinterError("E-PRN-001", detail=f"no OS printer found for USB {self.vid_pid}")
        SystemTransport(name).send(data)


def transport_for(hw: HardwareSettings) -> Transport | None:
    """The transport for the configured printer (None when none is configured)."""
    kind, address = hw.printer_kind, hw.printer_address
    if kind == "none" or not address:
        return None
    if kind == "network":
        return NetworkTransport(address)
    if kind == "system":
        return SystemTransport(address)
    if kind == "usb":
        return UsbTransport(address)
    if kind == "file":
        return FileTransport(Path(address))
    log.warning("unknown printer kind %r", kind)
    return None


# ----------------------------------------------------------------------------- OS helpers
def _send_win32(printer_name: str, data: bytes) -> None:
    try:
        import win32print  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        _send_copy_b(printer_name, data)
        return
    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as exc:  # noqa: BLE001
        raise PrinterError("E-PRN-001", detail=f"OpenPrinter {printer_name}: {exc}") from exc
    try:
        win32print.StartDocPrinter(handle, 1, ("Vaishnavi receipt", None, "RAW"))
        win32print.StartPagePrinter(handle)
        win32print.WritePrinter(handle, data)
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
    except Exception as exc:  # noqa: BLE001
        raise PrinterError("E-PRN-001", detail=f"WritePrinter {printer_name}: {exc}") from exc
    finally:
        win32print.ClosePrinter(handle)


def _send_copy_b(printer_name: str, data: bytes) -> None:
    """Last resort without pywin32: ``copy /B job \\\\localhost\\<share>`` needs the printer shared under its name."""
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as fh:
        fh.write(data)
        temp = fh.name
    target = rf"\\localhost\{printer_name}"
    proc = subprocess.run(["cmd", "/c", "copy", "/B", temp, target], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603, S607
    Path(temp).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise PrinterError("E-PRN-001", detail=f"copy /B to {target} failed: {proc.stderr.strip() or proc.stdout.strip()} (install pywin32 or share the printer)")


def _send_lp(printer_name: str, data: bytes) -> None:
    if shutil.which("lp") is None:
        raise PrinterError("E-PRN-001", detail="lp (CUPS) not installed")
    proc = subprocess.run(["lp", "-d", printer_name, "-o", "raw", "-s"], input=data, capture_output=True, timeout=30, check=False)  # noqa: S603, S607
    if proc.returncode != 0:
        raise PrinterError("E-PRN-001", detail=f"lp -d {printer_name}: {proc.stderr.decode(errors='replace').strip()}")


def _linux_lp_node(vid_pid: str) -> Path | None:
    """``/dev/usb/lpN`` whose sysfs device matches ``VID:PID``."""
    root = Path("/sys/class/usbmisc")
    try:
        candidates = sorted(root.glob("lp*"))
    except OSError:
        return None
    want = vid_pid.lower().replace("0x", "")
    for entry in candidates:
        dev = entry / "device"
        try:
            parent = dev.resolve().parent
            vid = (parent / "idVendor").read_text().strip().lower()
            pid = (parent / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if f"{vid}:{pid}" == want:
            node = Path("/dev/usb") / entry.name
            if node.exists():
                return node
    fallback = Path("/dev/usb/lp0")
    return fallback if fallback.exists() and len(candidates) == 1 else None


def _os_printer_for_usb(vid_pid: str) -> str | None:
    """On Windows/macOS the USB printer is also an OS printer; pick the one on a USB port, else the default."""
    from vaishnavi.platform.printing import list_system_printers  # noqa: PLC0415

    printers = list_system_printers()
    usb = [p for p in printers if "usb" in (p.port or "").lower()]
    if len(usb) == 1:
        return usb[0].name
    for p in printers:
        if p.is_default:
            return p.name
    log.debug("no OS printer matched USB %s among %s", vid_pid, [p.name for p in printers])
    return None
