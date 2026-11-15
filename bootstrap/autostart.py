"""Start-at-logon registration per OS, plus the ``--uninstall-autostart`` mode (stdlib only).

Windows: a Task Scheduler task (``schtasks /Create /SC ONLOGON``) running the ``.bat`` wrapper.
macOS: a LaunchAgent plist in ``~/Library/LaunchAgents`` (``RunAtLoad`` + ``KeepAlive`` on crash).
Linux: a systemd ``--user`` service (``Restart=on-failure``), enabled, with a hint to enable lingering.
All user-level — no administrator password. The watchdog behaviour (restart on crash) comes from
``KeepAlive`` / ``Restart=`` here and from the app's own ``/healthz`` hang detection later.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext, StageResult
from bootstrap.wizard import Kind, Question

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.autostart")

TASK_NAME = "Vaishnavi Store Manager"
LAUNCH_LABEL = "com.vaishnavi.store"
SYSTEMD_UNIT = "vaishnavi.service"
WRAPPER_WIN = "Start Vaishnavi.bat"
WRAPPER_MAC = "Start Vaishnavi.command"
WRAPPER_LINUX = "start-vaishnavi.sh"


def os_family() -> str:
    """windows | macos | linux | unknown."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def _run(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    log.info("EXEC %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8", errors="replace")  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, repr(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        log.debug("autostart| %s", line)
    return proc.returncode, out


# ----------------------------------------------------------------------------- per-OS backends
@dataclass
class Backend:
    """Register / query / remove the autostart entry."""

    program_dir: Path
    python: Path
    family: str = ""

    def __post_init__(self) -> None:
        self.family = self.family or os_family()

    # -- what to launch -----------------------------------------------------------------------
    def launch_command(self) -> list[str]:
        """``python main.py --no-browser`` is the safest cross-OS launch; the wrapper opens the browser."""
        return [str(self.python), str(self.program_dir / "main.py")]

    # -- public -------------------------------------------------------------------------------
    def is_registered(self) -> bool:
        """Entry exists."""
        if self.family == "windows":
            code, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME])
            return code == 0
        if self.family == "macos":
            return self._plist_path().exists()
        if self.family == "linux":
            return self._unit_path().exists()
        return False

    def register(self) -> tuple[bool, str]:
        """Create (or refresh) the entry; returns (ok, detail)."""
        if self.family == "windows":
            return self._register_windows()
        if self.family == "macos":
            return self._register_macos()
        if self.family == "linux":
            return self._register_linux()
        return False, "unsupported OS"

    def unregister(self) -> tuple[bool, str]:
        """Remove the entry; returns (ok, detail)."""
        if self.family == "windows":
            code, out = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            return code == 0, out.strip()[-200:]
        if self.family == "macos":
            p = self._plist_path()
            detail = ""
            if p.exists():
                if shutil.which("launchctl"):
                    code, out = _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)])  # type: ignore[attr-defined]
                    if code != 0:
                        detail = f"bootout: {out.strip()[-200:]}"
                p.unlink(missing_ok=True)
            return not p.exists(), detail
        if self.family == "linux":
            detail = ""
            if shutil.which("systemctl"):
                code, out = _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
                if code != 0:
                    detail = f"disable: {out.strip()[-200:]}"
            self._unit_path().unlink(missing_ok=True)
            if shutil.which("systemctl"):
                _run(["systemctl", "--user", "daemon-reload"])
            return not self._unit_path().exists(), detail
        return False, "unsupported OS"

    # -- windows ------------------------------------------------------------------------------
    def _register_windows(self) -> tuple[bool, str]:
        wrapper = self.program_dir / WRAPPER_WIN
        if wrapper.exists():
            action = f'"{wrapper}"'
        else:
            action = f'"{self.python}" "{self.program_dir / "main.py"}"'
        cmd = ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", action, "/RL", "LIMITED"]
        code, out = _run(cmd)
        return code == 0, out.strip()[-300:]

    # -- macos --------------------------------------------------------------------------------
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"

    def _register_macos(self) -> tuple[bool, str]:
        plist = {
            "Label": LAUNCH_LABEL,
            "ProgramArguments": self.launch_command(),
            "WorkingDirectory": str(self.program_dir),
            "RunAtLoad": True,
            # No KeepAlive: exit 1 is the documented WARN result (e.g. backups on the same disk) and
            # must not make launchd relaunch the installer in a loop; the app has its own watchdog.
            "ThrottleInterval": 20,
            "StandardOutPath": str(self.program_dir / "logs" / "startup" / "launchd.out.log"),
            "StandardErrorPath": str(self.program_dir / "logs" / "startup" / "launchd.err.log"),
            "EnvironmentVariables": {"PYTHONIOENCODING": "utf-8", "VAISHNAVI_AUTOSTART": "1"},
        }
        path = self._plist_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            (self.program_dir / "logs" / "startup").mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                plistlib.dump(plist, fh)
        except OSError as exc:
            return False, repr(exc)
        if not shutil.which("launchctl"):
            return False, "plist written but launchctl is not available"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])  # type: ignore[attr-defined]
        code, out = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])  # type: ignore[attr-defined]
        if code != 0:
            return False, f"plist written; launchctl bootstrap failed: {out.strip()[-200:]}"
        return True, ""

    # -- linux --------------------------------------------------------------------------------
    def _unit_path(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "systemd" / "user" / SYSTEMD_UNIT

    @staticmethod
    def _systemd_quote(arg: str) -> str:
        """Quote one ExecStart argument the way systemd expects (spaces, quotes, backslashes, ``%``)."""
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        return f'"{escaped}"'

    def _register_linux(self) -> tuple[bool, str]:
        exec_start = " ".join(self._systemd_quote(a) for a in [*self.launch_command(), "--no-browser"])
        unit = (
            "[Unit]\n"
            "Description=Vaishnavi Store Manager (billing and stock)\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={self.program_dir}\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "SuccessExitStatus=1 3\n"  # 1 = finished with warnings, 3 = owner cancelled — not crashes
            "Environment=PYTHONIOENCODING=utf-8 VAISHNAVI_AUTOSTART=1\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        path = self._unit_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(unit, encoding="utf-8")
        except OSError as exc:
            return False, repr(exc)
        if not shutil.which("systemctl"):
            return False, "unit written; systemctl not found (no systemd?)"
        _run(["systemctl", "--user", "daemon-reload"])
        code, out = _run(["systemctl", "--user", "enable", SYSTEMD_UNIT])
        if code != 0:
            return False, f"unit written; enable failed: {out.strip()[-200:]}"
        if shutil.which("loginctl"):
            _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        return True, ""


# ----------------------------------------------------------------------------- stage + mode
@dataclass
class AutostartStage:
    """Stage 10: start Vaishnavi automatically when the owner logs in."""

    key: str = "autostart"
    title_key: str = "stage.autostart"
    backend_factory: type[Backend] | None = None  # None → module-level Backend (patchable in tests)

    def applies(self, ctx: StageContext) -> bool:
        """Needs the venv interpreter."""
        return bool(ctx.result_data("venv").get("python"))

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached: registration is re-verified every run (cheap)."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Ask once (answer remembered), register, verify."""
        assert ctx.wizard is not None
        factory = self.backend_factory or Backend
        backend = factory(ctx.paths.program_dir, Path(ctx.result_data("venv")["python"]))
        if backend.is_registered():
            return StageResult.ok(ctx.t("autostart.already"), registered=True)
        answer = ctx.wizard.ask(Question(key="autostart", kind=Kind.YESNO, prompt_key="autostart.ask", help_key="autostart.help", default="yes", allow_back=False))
        if answer != "yes":
            log.info("AUTOSTART_DECLINED")
            return StageResult.ok(ctx.t("autostart.declined"), registered=False)
        ok, detail = backend.register()
        if not ok:
            return StageResult.warn(ctx.t("autostart.failed"), error_code="E-SYS-005", hint=ctx.t("autostart.failed_hint"), detail=detail, registered=False)
        log.info("AUTOSTART_REGISTERED family=%s detail=%s", backend.family, detail)
        return StageResult.ok(ctx.t("autostart.registered"), registered=True, detail=detail)


@register_mode(Mode.UNINSTALL_AUTOSTART)
def run_uninstall_autostart(ctx: StageContext) -> ExitCode:
    """``main.py --uninstall-autostart``."""
    python = Path(ctx.result_data("venv").get("python") or sys.executable)
    backend = Backend(ctx.paths.program_dir, python)
    if not backend.is_registered():
        ctx.console.info(ctx.t("autostart.not_registered"))
        return ExitCode.OK
    ok, detail = backend.unregister()
    if ok:
        ctx.console.ok(ctx.t("autostart.removed"))
        return ExitCode.OK
    ctx.console.fail(ctx.t("autostart.remove_failed") + f"  ({detail})")
    return ExitCode.FAIL


def register(options: Options) -> AutostartStage:
    """Factory used by ``build_default_pipeline``."""
    return AutostartStage()
