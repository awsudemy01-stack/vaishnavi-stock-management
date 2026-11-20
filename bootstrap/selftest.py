"""``main.py --selftest`` · ``--doctor [--fix]`` · ``--where`` · ``--stop`` — the owner's first-run / support tools (stdlib only).

All are thin: they locate the venv interpreter, hand over to ``python -m vaishnavi …`` and translate
the exit code into the installer's OK / WARN / FAIL line (``--where`` needs no app at all — it reads
``config/locations.toml`` and lists the folders, so it works even when the install never finished;
``--stop`` asks the running application to shut down cleanly and waits until the port is free).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from bootstrap import handover, locations
from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext

log = logging.getLogger("bootstrap.selftest")

STOP_TIMEOUT_S = 25


def _python(ctx: StageContext) -> Path | None:
    python = handover.venv_python_from(ctx)
    available, reason = handover.app_available(python) if python else (False, "venv not installed")
    if not python or not available:
        log.error("APP_UNAVAILABLE reason=%s code=E-SYS-002", reason)
        ctx.console.fail(ctx.t("database.app_missing") + "  (E-SYS-002)")
        ctx.console.hint(ctx.t("database.app_missing_hint"))
        return None
    return python


def _map(exit_code: int) -> ExitCode:
    return {0: ExitCode.OK, 1: ExitCode.WARN}.get(exit_code, ExitCode.FAIL)


@register_mode(Mode.SELFTEST)
def run_selftest(ctx: StageContext) -> ExitCode:
    """Run the shipped tests + the live doctor inside the venv; stream the output."""
    python = _python(ctx)
    if python is None:
        return ExitCode.FAIL
    ctx.console.info(ctx.t("selftest.intro"))
    args = ["selftest", "--lang", ctx.messages.lang]
    if not ctx.console.unicode:
        args.append("--ascii")
    streamed: list[str] = []

    def on_line(line: str) -> None:
        streamed.append(line)
        ctx.console.line("   " + line.rstrip())

    result = handover.run_app(python, args, on_line=on_line, timeout=60 * 30)
    if not streamed:  # the child buffered its output → show it now
        for line in result.output.splitlines():
            ctx.console.line("   " + line.rstrip())
    if result.timed_out:
        ctx.console.fail(ctx.t("selftest.timeout"))
        return ExitCode.FAIL
    code = _map(result.exit_code)
    {ExitCode.OK: ctx.console.ok, ExitCode.WARN: ctx.console.warn, ExitCode.FAIL: ctx.console.fail}[code](ctx.t(f"selftest.{code.name.lower()}"))
    log.info("SELFTEST_MODE exit=%s", result.exit_code)
    return code


@register_mode(Mode.DOCTOR)
def run_doctor_mode(ctx: StageContext) -> ExitCode:
    """``--doctor [--fix]``: the full health check in the owner's language."""
    python = _python(ctx)
    if python is None:
        return ExitCode.FAIL
    args = ["doctor", "--lang", ctx.options.lang or ctx.messages.lang]
    if ctx.options.fix:
        args.append("--fix")
    if not ctx.console.unicode:
        args.append("--ascii")
    result = handover.run_app(python, args, timeout=600)
    for line in result.output.splitlines():
        if line.strip():
            ctx.console.line("   " + line.rstrip())
    if result.timed_out:
        ctx.console.fail(ctx.t("doctor.crashed"))
        return ExitCode.FAIL
    code = _map(result.exit_code)
    {ExitCode.OK: ctx.console.ok, ExitCode.WARN: ctx.console.warn, ExitCode.FAIL: ctx.console.fail}[code](ctx.t(f"doctor.{'ok' if code is ExitCode.OK else ('warn' if code is ExitCode.WARN else 'fail')}"))
    return code


@register_mode(Mode.WHERE)
def run_where(ctx: StageContext) -> ExitCode:
    """``--where``: print every folder the owner may need to find (no app required)."""
    console = ctx.console
    paths = ctx.paths
    console.info(ctx.t("where.program", path=str(paths.program_dir)))
    console.info(ctx.t("where.install_logs", path=str(paths.install_log_dir)))
    console.info(ctx.t("where.config", path=str(paths.config_dir)))
    loc = locations.read_locations(paths.config_dir / locations.LOCATIONS_FILE)
    if loc is None:
        console.warn(ctx.t("where.not_installed"))
        return ExitCode.WARN
    console.info(ctx.t("where.primary", path=str(loc.primary)))
    console.info(ctx.t("where.database", path=str(loc.primary / "data" / "vaishnavi.db")))
    console.info(ctx.t("where.app_logs", path=str(loc.primary / "logs")))
    console.info(ctx.t("where.receipts", path=str(loc.primary / "receipts")))
    console.info(ctx.t("where.secondary", path=str(loc.secondary / "vaishnavi" / "backups")))
    if loc.same_device:
        console.warn(ctx.t("where.same_device") + "  (E-SYS-012)")
    return ExitCode.OK


# ----------------------------------------------------------------------------- --stop
STOP_MARKER_FILE = "stop-requested"
"""Same name as ``vaishnavi.platform.watchdog.STOP_MARKER_FILE`` (the installer is stdlib-only and cannot import it)."""


def state_dir(config_dir: Path) -> Path | None:
    """``<primary>/state`` from the recorded locations (None before the locations stage ran)."""
    loc = locations.read_locations(config_dir / locations.LOCATIONS_FILE)
    return loc.primary / "state" if loc is not None else None


def write_stop_marker(config_dir: Path, *, reason: str) -> bool:
    """Tell the supervising watchdog this stop is deliberate — without it the app came straight back (live run 5)."""
    folder = state_dir(config_dir)
    if folder is None:
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / STOP_MARKER_FILE).write_text(json.dumps({"reason": reason, "at": time.time()}), encoding="utf-8")
    except OSError as exc:
        log.warning("STOP_MARKER_FAILED %s", exc)
        return False
    return True


def stop_requested(config_dir: Path) -> str | None:
    """Reason from the marker (and remove it), or None."""
    folder = state_dir(config_dir)
    if folder is None:
        return None
    path = folder / STOP_MARKER_FILE
    if not path.exists():
        return None
    reason = "stop requested"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        reason = str(data.get("reason", reason)) if isinstance(data, dict) else reason
    except (OSError, ValueError):
        pass
    try:
        path.unlink()
    except OSError:
        pass
    return reason


# ----------------------------------------------------------------------------- watchdog record
WATCHDOG_RECORD_FILE = "watchdog.json"
"""``<primary>/state/watchdog.json`` — pid of the ``main.py`` window that supervises the app, written by the start stage."""


def write_watchdog_record(config_dir: Path, *, pid: int | None = None) -> bool:
    """Record which process is the watchdog so ``--stop`` can end it — whatever code version it runs."""
    folder = state_dir(config_dir)
    if folder is None:
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / WATCHDOG_RECORD_FILE).write_text(json.dumps({"pid": pid or os.getpid(), "at": time.time(), "program": sys.argv[0]}), encoding="utf-8")
    except OSError as exc:
        log.warning("WATCHDOG_RECORD_FAILED %s", exc)
        return False
    return True


def clear_watchdog_record(config_dir: Path) -> None:
    """Remove the record (the watchdog is leaving on its own)."""
    folder = state_dir(config_dir)
    if folder is not None:
        (folder / WATCHDOG_RECORD_FILE).unlink(missing_ok=True)


def watchdog_pid(config_dir: Path) -> int | None:
    """PID from the record, or None."""
    folder = state_dir(config_dir)
    if folder is None:
        return None
    try:
        data = json.loads((folder / WATCHDOG_RECORD_FILE).read_text(encoding="utf-8"))
        pid = int(data["pid"]) if isinstance(data, dict) else 0
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if pid > 1 else None


def process_command(pid: int) -> str:
    """Command line of a live process ('' when it is gone). Guards against killing a reused pid after a reboot."""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"], check=False, capture_output=True, text=True, timeout=20).stdout
        else:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], check=False, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("process_command(%d): %s", pid, exc)
        return ""
    return out.strip()


def kill_watchdog(pid: int) -> bool:
    """End the supervising process (it holds no data; SIGTERM on POSIX is caught by the new watchdog and logged)."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def stop_watchdog(config_dir: Path, *, command_of: Callable[[int], str] = process_command, kill_fn: Callable[[int], bool] = kill_watchdog) -> tuple[str, str]:
    """``(outcome, detail)``: ``none`` (no record) · ``stale`` (pid gone or not a main.py) · ``stopped``.

    Live run 5b: the ``main.py`` window kept the *old* watchdog code in memory after the owner copied new
    files, so the stop marker meant nothing to it and it relaunched the app after every ``--stop``. Ending
    the watchdog process itself works for every version.
    """
    pid = watchdog_pid(config_dir)
    if pid is None:
        return "none", "no watchdog record"
    if pid == os.getpid():
        return "none", "record points at this process"
    command = command_of(pid)
    if not command or "main.py" not in command:
        clear_watchdog_record(config_dir)
        log.info("WATCHDOG_RECORD_STALE pid=%d command=%r", pid, command[:120])
        return "stale", f"pid {pid} is not the watchdog any more"
    if not kill_fn(pid):
        clear_watchdog_record(config_dir)
        return "stale", f"pid {pid} was already gone"
    clear_watchdog_record(config_dir)
    log.info("WATCHDOG_TERMINATED pid=%d command=%r", pid, command[:120])
    return "stopped", f"watchdog pid {pid}"


def running_pid(config_dir: Path) -> tuple[int | None, str]:
    """PID of the running application from ``<data>/state/heartbeat.json`` (fallback: the installer's start record)."""
    loc = locations.read_locations(config_dir / locations.LOCATIONS_FILE)
    if loc is not None:
        hb = loc.primary / "state" / "heartbeat.json"
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pid"):
                return int(data["pid"]), f"heartbeat {hb}"
        except (OSError, ValueError):
            pass
    return None, "no heartbeat file"


def port_in_use(host: str, port: int) -> bool:
    """True when something accepts connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
        except OSError:
            return False
        return True


def terminate(pid: int, *, windows: bool | None = None) -> bool:
    """Ask the process to exit (SIGTERM → uvicorn shuts down → the app's clean-close runs). False when it is gone already."""
    try:
        if sys.platform.startswith("win") if windows is None else windows:
            subprocess.run(["taskkill", "/PID", str(pid)], check=False, capture_output=True)  # polite first; Task Manager for stubborn cases
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def stop_app(config_dir: Path, *, host: str = "127.0.0.1", port: int = 8080, timeout_s: float = STOP_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, terminate_fn: Callable[[int], bool] = terminate, in_use: Callable[[str, int], bool] | None = None, stop_watchdog_fn: Callable[[Path], tuple[str, str]] | None = None) -> tuple[str, str]:
    """``(outcome, detail)``: ``not_running`` · ``stopped`` · ``no_pid`` · ``timeout``.

    Order matters: the watchdog is ended **first** (else it sees the app exit and relaunches), then the
    deliberate-stop marker is written (belt and braces for a watchdog this code did not record), then the
    app gets SIGTERM and we wait for the port.
    """
    busy = in_use or port_in_use
    if not busy(host, port):
        return "not_running", f"{host}:{port} is free"
    pid, source = running_pid(config_dir)
    if pid is None:
        return "no_pid", source
    log.info("APP_STOP_REQUESTED pid=%d source=%s", pid, source)
    wd_outcome, wd_detail = (stop_watchdog_fn or stop_watchdog)(config_dir)
    suffix = f" · {wd_detail} stopped" if wd_outcome == "stopped" else ""
    write_stop_marker(config_dir, reason="main.py --stop")
    if not terminate_fn(pid):
        return "not_running", f"process {pid} was already gone{suffix}"
    deadline = clock() + timeout_s
    while clock() < deadline:
        if not busy(host, port):
            log.info("APP_STOPPED pid=%d watchdog=%s", pid, wd_outcome)
            return "stopped", f"pid {pid}{suffix}"
        sleep(0.5)
    log.warning("APP_STOP_TIMEOUT pid=%d after=%ss", pid, timeout_s)
    return "timeout", f"pid {pid}{suffix}"


@register_mode(Mode.STOP)
def run_stop(ctx: StageContext) -> ExitCode:
    """``--stop``: shut the running application down cleanly (the owner's way to restart after copying new code)."""
    from bootstrap.app_stages import app_url  # noqa: PLC0415

    url = app_url(ctx.paths.config_dir)
    host, _, port_text = url.rsplit("://", 1)[-1].partition(":")
    port = int(port_text or 8080)
    outcome, detail = stop_app(ctx.paths.config_dir, host=host or "127.0.0.1", port=port)
    console = ctx.console
    if outcome == "not_running":
        console.info(ctx.t("stop.not_running", detail=detail))
        return ExitCode.OK
    if outcome == "no_pid":
        console.warn(ctx.t("stop.no_pid", port=port))
        console.hint(ctx.t("stop.no_pid_hint", port=port))
        return ExitCode.WARN
    if outcome == "stopped":
        console.ok(ctx.t("stop.stopped", detail=detail))
        return ExitCode.OK
    console.fail(ctx.t("stop.timeout", detail=detail, seconds=STOP_TIMEOUT_S))
    console.hint(ctx.t("stop.no_pid_hint", port=port))
    return ExitCode.FAIL


@register_mode(Mode.FRESH_START)
def run_fresh_start(ctx: StageContext) -> ExitCode:
    """``--fresh-start``: stop the app, confirm, ask the Owner password, hand over to ``python -m vaishnavi fresh-start``."""
    import getpass  # noqa: PLC0415

    from bootstrap import handover  # noqa: PLC0415
    from bootstrap.app_stages import app_url  # noqa: PLC0415

    console = ctx.console
    console.info(ctx.t("fresh_start.intro"))
    url = app_url(ctx.paths.config_dir)
    host, _, port_text = url.rsplit("://", 1)[-1].partition(":")
    outcome, detail = stop_app(ctx.paths.config_dir, host=host or "127.0.0.1", port=int(port_text or 8080))
    if outcome == "stopped":
        console.ok(ctx.t("stop.stopped", detail=detail))
    elif outcome != "not_running":
        console.fail(ctx.t("stop.timeout", detail=detail, seconds=STOP_TIMEOUT_S))
        return ExitCode.FAIL
    python = handover.venv_python_from(ctx)
    available, reason = handover.app_available(python) if python else (False, "venv not installed")
    if not python or not available:
        log.error("FRESH_START_REFUSED reason=app-unavailable detail=%s code=E-SYS-002", reason)
        console.fail(ctx.t("fresh_start.app_missing") + "  (E-SYS-002)")
        return ExitCode.FAIL
    console.line("    " + ctx.t("fresh_start.confirm") + ": ")
    typed = input("    > ").strip()
    if typed != "START FRESH":
        console.info(ctx.t("fresh_start.cancelled"))
        return ExitCode.CANCELLED
    password = getpass.getpass("    " + ctx.t("fresh_start.ask_password") + " ").strip()
    if not password:
        console.info(ctx.t("fresh_start.cancelled"))
        return ExitCode.CANCELLED
    console.busy(ctx.t("fresh_start.working"))
    result = handover.run_app(python, ["fresh-start", "--yes"], secrets={"owner_password": password}, timeout=300)
    archive = next((ln.split("archive=", 1)[1] for ln in result.output.splitlines() if ln.startswith("FRESH_START_OK")), "")
    if result.ok:
        console.ok(ctx.t("fresh_start.done", archive=archive or "-"))
        return ExitCode.OK
    log.error("FRESH_START_FAILED exit=%s", result.exit_code)
    last = next((ln for ln in reversed(result.output.strip().splitlines()) if ln.strip()), "")
    console.fail(ctx.t("fresh_start.failed") + (f"  {last}" if last else ""))
    return ExitCode.FAIL
