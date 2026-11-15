"""Run ONE command with administrator rights, after explaining why (stdlib only).

Policy (docs/07 §4, ADR-028): elevation is requested only for system-level installs; the prompt
states the reason and the exact command; the command and its output are logged as ``ELEVATED_CMD``;
the password is never logged, never stored, never passed on a command line. Declining is a normal
outcome (``Elevation.declined``), not an error.

Mechanisms
- Windows: ``ShellExecuteW(..., "runas", ...)`` — Windows shows its own UAC dialog; our process never
  sees a password. Output is captured by redirecting the elevated ``cmd.exe`` into a log file.
- macOS / Linux: ``sudo -S`` reading the password from stdin (asked with ``getpass``); when there is
  no terminal, ``pkexec`` (GUI prompt) is used if present.
"""

from __future__ import annotations

import getpass
import logging
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger("bootstrap.elevate")

ELEVATED_TIMEOUT_S = 60 * 30
PASSWORD_ATTEMPTS = 3


class Outcome(str, Enum):
    """How an elevated run ended."""

    OK = "ok"
    FAILED = "failed"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"  # no sudo/pkexec/UAC mechanism on this system


@dataclass(frozen=True)
class Elevation:
    """Result of ``run_elevated``."""

    outcome: Outcome
    exit_code: int | None = None
    output: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the command ran and exited 0."""
        return self.outcome is Outcome.OK

    @property
    def declined(self) -> bool:
        """True when the owner chose not to give the password."""
        return self.outcome is Outcome.DECLINED


def shell_join(cmd: Sequence[str]) -> str:
    """Human-readable command line for the explanation and the log."""
    out: list[str] = []
    for part in cmd:
        out.append(f'"{part}"' if " " in part or not part else part)
    return " ".join(out)


def mechanism() -> str:
    """uac | sudo | pkexec | none — what this system offers."""
    if sys.platform.startswith("win"):
        return "uac"
    if shutil.which("sudo"):
        return "sudo"
    if shutil.which("pkexec"):
        return "pkexec"
    return "none"


def is_admin() -> bool:
    """Already running with administrator rights (then no prompt is needed)."""
    try:
        if sys.platform.startswith("win"):
            import ctypes  # noqa: PLC0415

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        import os  # noqa: PLC0415

        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception as exc:
        log.debug("is_admin probe failed: %r", exc)
        return False


# ----------------------------------------------------------------------------- Windows (UAC)
def _run_uac(cmd: Sequence[str], timeout: float) -> Elevation:
    import ctypes  # noqa: PLC0415

    with tempfile.NamedTemporaryFile("w", suffix=".log", prefix="vaishnavi-elevated-", delete=False, encoding="utf-8") as fh:
        out_path = Path(fh.name)
    # cmd.exe runs the command and captures everything into out_path, then writes the exit code.
    # /v:on enables delayed expansion: !ERRORLEVEL! is read *after* the command ran (a plain
    # %ERRORLEVEL% is expanded when the line is parsed, i.e. before the command, and is always 0).
    inner = subprocess.list2cmdline(list(cmd))
    script = f'{inner} > "{out_path}" 2>&1 & echo __EXIT__!ERRORLEVEL! >> "{out_path}"'
    log.info("ELEVATED_CMD mechanism=uac cmd=%s", inner)
    try:
        # ShellExecuteExW gives us a process handle to wait on.
        class SHELLEXECUTEINFO(ctypes.Structure):  # noqa: N801 (Win32 name)
            _fields_ = [
                ("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p),
            ]

        SEE_MASK_NOCLOSEPROCESS = 0x40
        info = SHELLEXECUTEINFO()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = "cmd.exe"
        info.lpParameters = f'/v:on /d /c "{script}"'
        info.nShow = 0  # SW_HIDE
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # 64-bit HANDLEs must not be squeezed through the default c_int conversion.
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):  # type: ignore[attr-defined]
            err = ctypes.get_last_error() or kernel32.GetLastError()
            if err == 1223:  # ERROR_CANCELLED — the owner pressed "No" on the UAC dialog
                log.info("ELEVATED_DECLINED mechanism=uac")
                return Elevation(Outcome.DECLINED, detail="UAC cancelled")
            return Elevation(Outcome.FAILED, detail=f"ShellExecuteEx error {err}")
        wait = kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        if wait != 0:
            kernel32.TerminateProcess(info.hProcess, 1)
            kernel32.CloseHandle(info.hProcess)
            return Elevation(Outcome.FAILED, detail="timeout")
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(info.hProcess)
        output = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        exit_code = int(code.value)
        for line in output.splitlines():
            if line.startswith("__EXIT__"):
                try:
                    exit_code = int(line[len("__EXIT__"):].strip())
                except ValueError:
                    pass
        output = "\n".join(l for l in output.splitlines() if not l.startswith("__EXIT__"))
        for line in output.splitlines():
            log.debug("elevated| %s", line)
        log.info("ELEVATED_EXIT code=%d", exit_code)
        return Elevation(Outcome.OK if exit_code == 0 else Outcome.FAILED, exit_code, output)
    except Exception as exc:
        log.exception("UAC elevation failed")
        return Elevation(Outcome.FAILED, detail=repr(exc))
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------------- POSIX (sudo / pkexec)
def _run_sudo(cmd: Sequence[str], timeout: float, ask_password: Callable[[str], str], prompt: str) -> Elevation:
    full = ["sudo", "-S", "-p", "", "-k", *cmd]
    log.info("ELEVATED_CMD mechanism=sudo cmd=%s", shell_join(cmd))
    for attempt in range(1, PASSWORD_ATTEMPTS + 1):
        try:
            password = ask_password(prompt)
        except (EOFError, KeyboardInterrupt):
            log.info("ELEVATED_DECLINED mechanism=sudo (no password given)")
            return Elevation(Outcome.DECLINED)
        if password == "":
            log.info("ELEVATED_DECLINED mechanism=sudo (empty password)")
            return Elevation(Outcome.DECLINED)
        try:
            proc = subprocess.run(full, input=password + "\n", capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
        except subprocess.TimeoutExpired:
            return Elevation(Outcome.FAILED, detail="timeout")
        except OSError as exc:
            return Elevation(Outcome.UNAVAILABLE, detail=repr(exc))
        output = (proc.stdout or "") + (proc.stderr or "")
        wrong_password = proc.returncode == 1 and ("incorrect password" in output.lower() or "sorry, try again" in output.lower() or "authentication failure" in output.lower())
        if wrong_password and attempt < PASSWORD_ATTEMPTS:
            log.info("ELEVATED_RETRY mechanism=sudo attempt=%d (wrong password)", attempt)
            continue
        for line in output.splitlines():
            log.debug("elevated| %s", line)
        log.info("ELEVATED_EXIT code=%d", proc.returncode)
        if wrong_password:
            return Elevation(Outcome.DECLINED, proc.returncode, output, detail="wrong password 3 times")
        return Elevation(Outcome.OK if proc.returncode == 0 else Outcome.FAILED, proc.returncode, output)
    return Elevation(Outcome.DECLINED)


def _run_pkexec(cmd: Sequence[str], timeout: float) -> Elevation:
    log.info("ELEVATED_CMD mechanism=pkexec cmd=%s", shell_join(cmd))
    try:
        proc = subprocess.run(["pkexec", *cmd], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        return Elevation(Outcome.FAILED, detail="timeout")
    except OSError as exc:
        return Elevation(Outcome.UNAVAILABLE, detail=repr(exc))
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        log.debug("elevated| %s", line)
    log.info("ELEVATED_EXIT code=%d", proc.returncode)
    if proc.returncode in (126, 127):  # polkit: dismissed / not authorised
        return Elevation(Outcome.DECLINED, proc.returncode, output)
    return Elevation(Outcome.OK if proc.returncode == 0 else Outcome.FAILED, proc.returncode, output)


# ----------------------------------------------------------------------------- public
def run_elevated(
    cmd: Sequence[str],
    *,
    ask_password: Callable[[str], str] = getpass.getpass,
    password_prompt: str = "Administrator password: ",
    timeout: float = ELEVATED_TIMEOUT_S,
    interactive: bool | None = None,
) -> Elevation:
    """Run ``cmd`` with administrator rights using the mechanism this OS offers.

    The caller has already shown the owner *why* and *what* (see ``SystemComponentsStage``). If the
    process is already elevated the command simply runs. Never raises.
    """
    if is_admin():
        log.info("ELEVATED_CMD mechanism=already-admin cmd=%s", shell_join(cmd))
        try:
            proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as exc:
            return Elevation(Outcome.FAILED, detail=repr(exc))
        output = (proc.stdout or "") + (proc.stderr or "")
        for line in output.splitlines():
            log.debug("elevated| %s", line)
        return Elevation(Outcome.OK if proc.returncode == 0 else Outcome.FAILED, proc.returncode, output)

    mech = mechanism()
    if mech == "uac":
        return _run_uac(cmd, timeout)
    if interactive is None:
        interactive = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
    if mech == "sudo" and interactive:
        return _run_sudo(cmd, timeout, ask_password, password_prompt)
    if shutil.which("pkexec"):
        return _run_pkexec(cmd, timeout)
    if mech == "sudo":
        # No terminal and no GUI agent: cannot ask for a password safely.
        return Elevation(Outcome.UNAVAILABLE, detail="sudo needs a terminal; pkexec not installed")
    return Elevation(Outcome.UNAVAILABLE, detail="no sudo/pkexec on this system")
