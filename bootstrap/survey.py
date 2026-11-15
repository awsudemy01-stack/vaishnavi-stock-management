"""Hardware / OS survey of the shop PC and the AI-provider recommendation derived from it (stdlib only).

Every probe is best-effort: it has a timeout, swallows its own errors into DEBUG log lines and leaves
the field at its "unknown" value. The survey can therefore never stop the installer. Results are
written to ``logs/install/survey.json`` and reused by later stages (``ctx.result_data("survey")``).
This module is allowed to inspect ``sys.platform`` (docs/03 §2, ADR-027).
"""

from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bootstrap.stages import StageContext, StageResult

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.survey")

GIB = 1024**3
PROBE_TIMEOUT_S = 8
INTERNET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53))
SURVEY_FILE = "survey.json"

# Thresholds from docs/06 §2.1–2.2 (GiB).
LOCAL_AI_MIN_RAM = 12
SMALL_MODEL_MAX_RAM = 8
LARGE_MODEL_MIN_RAM = 16
LARGE_MODEL_MIN_VRAM = 6
CPU_ONLY_OPT_IN_RAM = 32


@dataclass
class Gpu:
    """One graphics adapter."""

    name: str
    vram_gib: float | None = None
    vendor: str = "unknown"  # nvidia | amd | intel | apple | unknown


@dataclass
class Survey:
    """Everything the installer learned about the computer."""

    os_family: str = "unknown"  # windows | macos | linux | unknown
    os_name: str = ""
    os_version: str = ""
    arch: str = ""
    hostname: str = ""
    python_version: str = ""
    python_executable: str = ""
    in_venv: bool = False
    cpu_count: int | None = None
    cpu_name: str = ""
    ram_gib: float | None = None
    gpus: list[Gpu] = field(default_factory=list)
    apple_silicon: bool = False
    disk_free_program_gib: float | None = None
    disk_free_home_gib: float | None = None
    printers: list[str] = field(default_factory=list)
    serial_ports: list[str] = field(default_factory=list)
    usb_hid_devices: list[str] = field(default_factory=list)
    internet: bool | None = None
    package_managers: list[str] = field(default_factory=list)
    ollama_found: bool = False
    probe_errors: dict[str, str] = field(default_factory=dict)
    surveyed_on: str = ""

    # -- derived ----------------------------------------------------------------------------
    @property
    def nvidia_vram_gib(self) -> float:
        """Largest NVIDIA adapter memory (0 when none)."""
        return max((g.vram_gib or 0.0 for g in self.gpus if g.vendor == "nvidia"), default=0.0)

    @property
    def has_capable_gpu(self) -> bool:
        """NVIDIA ≥ 6 GiB or Apple silicon — can run the 7B model class."""
        return self.nvidia_vram_gib >= LARGE_MODEL_MIN_VRAM or self.apple_silicon

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Survey:
        """Inverse of ``to_dict`` (tolerates unknown / missing keys)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        gpus = [Gpu(**g) for g in known.pop("gpus", []) if isinstance(g, dict)]
        return cls(gpus=gpus, **known)


@dataclass(frozen=True)
class AiRecommendation:
    """Which AI provider / local model the installer should propose."""

    provider: str  # ollama | openai | none
    local_tier: str  # 1.5b | 3b | 7b
    reason_key: str  # message key explaining the recommendation
    local_possible: bool


def recommend_ai(s: Survey) -> AiRecommendation:
    """Apply the hardware table of docs/06 §2.1–2.2."""
    ram = s.ram_gib or 0.0
    if ram >= LARGE_MODEL_MIN_RAM and s.has_capable_gpu:
        tier = "7b"
    elif ram <= SMALL_MODEL_MAX_RAM:
        tier = "1.5b"
    else:
        tier = "3b"
    local_ok = ram >= LOCAL_AI_MIN_RAM or (ram >= SMALL_MODEL_MAX_RAM and s.has_capable_gpu)

    if s.ram_gib is None:
        return AiRecommendation("ollama", "3b", "survey.ai.unknown_ram", True)
    if local_ok:
        return AiRecommendation("ollama", tier, "survey.ai.local_ok", True)
    if s.internet is False:
        # Cloud is impossible without internet; a weak PC can still try the small model.
        return AiRecommendation("ollama" if ram >= 6 else "none", "1.5b", "survey.ai.weak_offline", ram >= 6)
    return AiRecommendation("openai", "1.5b", "survey.ai.weak_pc", ram >= 6)


# ----------------------------------------------------------------------------- probes
def _run(cmd: list[str], timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """Run a command and return stdout, or None on any problem (logged at DEBUG)."""
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("survey: %s failed: %s", cmd[0], exc)
        return None
    if completed.returncode != 0:
        log.debug("survey: %s exit=%d stderr=%s", cmd[0], completed.returncode, completed.stderr.strip()[:200])
        return None
    return completed.stdout


def _powershell(script: str, timeout: float = PROBE_TIMEOUT_S) -> list[str]:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def os_family() -> str:
    """windows | macos | linux | unknown."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def parse_os_release(text: str) -> str:
    """PRETTY_NAME from /etc/os-release content."""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def parse_meminfo(text: str) -> float | None:
    """MemTotal (GiB) from /proc/meminfo content."""
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return round(int(parts[1]) * 1024 / GIB, 1)
    return None


def parse_nvidia_smi(text: str) -> list[Gpu]:
    """Parse ``nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits``."""
    gpus: list[Gpu] = []
    for line in text.splitlines():
        if "," not in line:
            continue
        name, mem = (p.strip() for p in line.rsplit(",", 1))
        try:
            vram = round(float(mem) / 1024, 1)
        except ValueError:
            vram = None
        gpus.append(Gpu(name=name, vram_gib=vram, vendor="nvidia"))
    return gpus


def classify_vendor(name: str) -> str:
    """Vendor from an adapter name."""
    low = name.lower()
    if "nvidia" in low or "geforce" in low or "quadro" in low or "rtx" in low:
        return "nvidia"
    if "amd" in low or "radeon" in low:
        return "amd"
    if "intel" in low:
        return "intel"
    if "apple" in low:
        return "apple"
    return "unknown"


def _ram_gib(family: str, errors: dict[str, str]) -> float | None:
    try:
        if family == "windows":

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return round(status.ullTotalPhys / GIB, 1)
        elif family == "linux":
            return parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
        elif family == "macos":
            out = _run(["sysctl", "-n", "hw.memsize"])
            if out and out.strip().isdigit():
                return round(int(out.strip()) / GIB, 1)
    except Exception as exc:  # any probe failure is tolerated
        errors["ram"] = repr(exc)
        log.debug("survey: ram probe failed: %r", exc)
    return None


def _gpus(family: str, arch: str, errors: dict[str, str]) -> list[Gpu]:
    gpus: list[Gpu] = []
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out:
            gpus.extend(parse_nvidia_smi(out))
    try:
        names: list[str] = []
        if family == "windows":
            names = _powershell("Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name")
        elif family == "linux" and shutil.which("lspci"):
            names = [line.split(":", 2)[-1].strip() for line in (_run(["lspci"]) or "").splitlines() if "VGA" in line or "3D" in line]
        elif family == "macos" and arch in ("arm64", "aarch64"):
            names = ["Apple silicon GPU"]
        for name in names:
            vendor = classify_vendor(name)
            if vendor == "nvidia" and any(g.vendor == "nvidia" for g in gpus):
                continue  # nvidia-smi already gave the precise entry
            gpus.append(Gpu(name=name, vendor=vendor))
    except Exception as exc:
        errors["gpu"] = repr(exc)
        log.debug("survey: gpu probe failed: %r", exc)
    return gpus


def _printers(family: str, errors: dict[str, str]) -> list[str]:
    try:
        if family == "windows":
            return _powershell("Get-Printer | Select-Object -ExpandProperty Name")
        if shutil.which("lpstat"):
            out = _run(["lpstat", "-p"]) or ""
            return [line.split()[1] for line in out.splitlines() if line.startswith("printer ") and len(line.split()) > 1]
    except Exception as exc:
        errors["printers"] = repr(exc)
        log.debug("survey: printer probe failed: %r", exc)
    return []


def _serial_ports(family: str, errors: dict[str, str]) -> list[str]:
    try:
        if family == "windows":
            import winreg  # noqa: PLC0415 (Windows only)

            ports: list[str] = []
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                i = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    ports.append(str(value))
                    i += 1
            return sorted(ports)
        if family == "linux":
            return sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        if family == "macos":
            return sorted(glob.glob("/dev/tty.usb*"))
    except OSError as exc:  # e.g. the registry key does not exist when no port was ever present
        log.debug("survey: serial probe: %s", exc)
    except Exception as exc:
        errors["serial"] = repr(exc)
        log.debug("survey: serial probe failed: %r", exc)
    return []


def _usb_hid(family: str, errors: dict[str, str]) -> list[str]:
    try:
        if family == "windows":
            return _powershell("Get-PnpDevice -Class HIDClass -Status OK | Select-Object -ExpandProperty FriendlyName")[:40]
        if family == "linux" and shutil.which("lsusb"):
            return [line.split(" ", 6)[-1] for line in (_run(["lsusb"]) or "").splitlines() if " ID " in line][:40]
        if family == "macos":
            out = _run(["system_profiler", "SPUSBDataType", "-json"], timeout=20)
            if out:
                names: list[str] = []

                def walk(node: Any) -> None:
                    if isinstance(node, dict):
                        if "_name" in node and "vendor_id" in node:
                            names.append(str(node["_name"]))
                        for v in node.values():
                            walk(v)
                    elif isinstance(node, list):
                        for v in node:
                            walk(v)

                walk(json.loads(out))
                return names[:40]
    except Exception as exc:
        errors["usb"] = repr(exc)
        log.debug("survey: usb probe failed: %r", exc)
    return []


def _internet(timeout: float = 2.0) -> bool:
    for host, port in INTERNET_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _disk_free_gib(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(path).free / GIB, 1)
    except OSError:
        return None


def collect(program_dir: Path, *, probe_devices: bool = True, probe_internet: bool = True) -> Survey:
    """Run every probe and return the survey (never raises)."""
    family = os_family()
    errors: dict[str, str] = {}
    s = Survey(os_family=family, arch=platform.machine(), hostname=socket.gethostname(), surveyed_on=date.today().isoformat())
    s.python_version = platform.python_version()
    s.python_executable = sys.executable
    s.in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    s.cpu_count = os.cpu_count()
    s.cpu_name = platform.processor()

    if family == "windows":
        s.os_name, s.os_version = "Windows", f"{platform.release()} ({platform.version()})"
    elif family == "macos":
        s.os_name, s.os_version = "macOS", platform.mac_ver()[0]
    elif family == "linux":
        try:
            s.os_name = parse_os_release(Path("/etc/os-release").read_text(encoding="utf-8")) or "Linux"
        except OSError:
            s.os_name = "Linux"
        s.os_version = platform.release()

    s.ram_gib = _ram_gib(family, errors)
    s.gpus = _gpus(family, s.arch, errors)
    s.apple_silicon = family == "macos" and s.arch in ("arm64", "aarch64")
    s.disk_free_program_gib = _disk_free_gib(program_dir)
    s.disk_free_home_gib = _disk_free_gib(Path.home())
    if probe_devices:
        s.printers = _printers(family, errors)
        s.serial_ports = _serial_ports(family, errors)
        s.usb_hid_devices = _usb_hid(family, errors)
    s.internet = _internet() if probe_internet else None
    s.package_managers = [pm for pm in ("winget", "brew", "apt-get") if shutil.which(pm)]
    s.ollama_found = shutil.which("ollama") is not None
    s.probe_errors = errors
    log.info(
        "SURVEY os=%s/%s arch=%s ram=%s gpus=%d printers=%d serial=%d internet=%s pkg=%s ollama=%s errors=%d",
        s.os_name, s.os_version, s.arch, s.ram_gib, len(s.gpus), len(s.printers), len(s.serial_ports), s.internet, s.package_managers, s.ollama_found, len(errors),
    )
    log.debug("SURVEY_DETAIL %s", json.dumps(s.to_dict(), ensure_ascii=False))
    return s


def write_survey(s: Survey, path: Path) -> None:
    """Persist the survey as pretty JSON (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def summary_line(s: Survey, rec: AiRecommendation, t: Any) -> str:
    """One owner-facing line: 'Windows 11, 16 GB memory, no graphics card → local AI will work'."""
    ram = t("survey.ram", gib=int(round(s.ram_gib))) if s.ram_gib else t("survey.ram_unknown")
    if s.nvidia_vram_gib:
        gpu = t("survey.gpu_nvidia", gib=int(round(s.nvidia_vram_gib)))
    elif s.apple_silicon:
        gpu = t("survey.gpu_apple")
    else:
        gpu = t("survey.gpu_none")
    return f"{s.os_name} {s.os_version.split(' (')[0]}, {ram}, {gpu} → {t(rec.reason_key)}"


# ----------------------------------------------------------------------------- stage
@dataclass
class SurveyStage:
    """Stage 2: learn about the computer; cached per ISO week so daily starts stay fast."""

    key: str = "survey"
    title_key: str = "stage.survey"

    def applies(self, ctx: StageContext) -> bool:
        """Always."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Re-survey weekly, or when Python moved."""
        iso = date.today().isocalendar()
        return f"{sys.executable}|{iso[0]}-W{iso[1]}"

    def run(self, ctx: StageContext) -> StageResult:
        """Collect, persist to ``survey.json``, print the summary line."""
        s = collect(ctx.paths.program_dir)
        write_survey(s, ctx.paths.install_log_dir / SURVEY_FILE)
        rec = recommend_ai(s)
        log.info("AI_RECOMMENDATION provider=%s tier=%s reason=%s local_possible=%s", rec.provider, rec.local_tier, rec.reason_key, rec.local_possible)
        message = summary_line(s, rec, ctx.t)
        data = {"survey": s.to_dict(), "ai_provider": rec.provider, "ai_local_tier": rec.local_tier, "ai_local_possible": rec.local_possible, "ai_reason_key": rec.reason_key}
        if s.probe_errors:
            # Developer information only — a failed optional probe must not worry the owner.
            log.warning("SURVEY_PROBE_ERRORS code=E-SYS-010 %s", json.dumps(s.probe_errors))
        return StageResult.ok(message, **data)


def register(options: Options) -> SurveyStage:
    """Factory used by ``build_default_pipeline``."""
    return SurveyStage()
