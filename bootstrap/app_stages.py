"""Installer stages that hand over to the application: database (11), doctor (12), start + watchdog (13). Stdlib only.

* **DatabaseStage** runs ``python -m vaishnavi init-db --json`` with any pending secrets on stdin. Only when
  the app confirms every key was stored encrypted does the installer delete ``config/secrets.env`` and drop
  its in-memory copy (docs/07 §5, ``E-SEC-001`` otherwise).
* **DoctorStage** runs the full ``doctor`` and echoes its plain-language report; exit 0/1/2 → OK/WARN/FAIL.
* **StartStage** launches ``python -m vaishnavi run`` detached, waits for ``/healthz`` and prints the one
  line the owner waits for: *Ready to open the shop*. With ``watchdog=True`` the installer stays as the
  watchdog (docs/07 §8): a dead or hung app is relaunched, with a cap per hour (``E-SYS-020``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bootstrap import ai_provider, handover
from bootstrap.cli import Options
from bootstrap.stages import StageContext, StageResult, StageStatus

log = logging.getLogger("bootstrap.app_stages")

DEFAULT_PORT = 8080
HEALTH_TIMEOUT_S = 90
HEALTH_POLL_S = 1.0


# ----------------------------------------------------------------------------- helpers
def web_port(config_dir: Path) -> int:
    """``[server] port`` from settings.toml (default 8080) — read with tomllib, no app import."""
    path = config_dir / "settings.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return int(data.get("server", {}).get("port", DEFAULT_PORT))
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        log.debug("settings.toml port unreadable (%s) — default %d", exc, DEFAULT_PORT)
        return DEFAULT_PORT


def app_url(config_dir: Path) -> str:
    """Local URL of the running app."""
    return f"http://127.0.0.1:{web_port(config_dir)}"


def fetch_health(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """GET ``<url>/healthz``; the JSON body, or None when unreachable / not JSON."""
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as resp:  # noqa: S310 (loopback only)
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _python(ctx: StageContext) -> Path | None:
    python = handover.venv_python_from(ctx)
    if python is None:
        return None
    ok, reason = handover.app_available(python)
    if not ok:
        log.error("APP_NOT_IMPORTABLE python=%s reason=%s code=E-SYS-002", python, reason)
        return None
    return python


def _echo(ctx: StageContext) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        text = line.rstrip()
        if text:
            ctx.console.info("   " + text)

    return on_line


# ----------------------------------------------------------------------------- database
class DatabaseStage:
    """Stage 11: create / seed / upgrade the database and commit the installer's secrets."""

    key: str = "database"
    title_key: str = "stage.database"

    def applies(self, ctx: StageContext) -> bool:
        """Needs the venv."""
        return bool(ctx.result_data("venv").get("python"))

    def cache_key(self, ctx: StageContext) -> str | None:
        """Never cached — ``init-db`` is idempotent and a pending secret must always be committed."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Hand over; delete the secrets file only after confirmation."""
        python = _python(ctx)
        if python is None:
            return StageResult.fail(ctx.t("database.app_missing"), error_code="E-SYS-002", hint=ctx.t("database.app_missing_hint"))
        pending: dict[str, str] = dict(ctx.runtime.get(ai_provider.RUNTIME_SECRETS) or {})
        result = handover.run_app(python, ["init-db", "--json"], secrets=pending, timeout=600)
        summary = _last_json(result.output)
        if result.timed_out or summary is None or (result.exit_code not in (0, 2)):
            # The app itself reports precise codes (E-DB-003/010/030) in its log; here we only know it did not finish.
            log.error("INIT_DB_FAILED exit=%s timed_out=%s tail=%s code=E-SYS-099", result.exit_code, result.timed_out, result.output[-400:])
            return StageResult.fail(ctx.t("database.failed"), error_code="E-SYS-099", hint=ctx.t("database.failed_hint"), detail=result.output[-2000:])
        stored = list(summary.get("secrets_stored") or [])
        failed = list(summary.get("secrets_failed") or [])
        if pending and not failed and set(stored) >= set(pending):
            ctx.runtime.pop(ai_provider.RUNTIME_SECRETS, None)
            ai_provider.delete_secrets_file_if_pending(ctx)
            log.info("SECRETS_COMMITTED keys=%s", ",".join(sorted(stored)))
        data = {"created": bool(summary.get("created")), "migrated": bool(summary.get("migrated")), "revision": summary.get("revision"), "owner_setup_pending": bool(summary.get("owner_setup_pending")), "secrets_stored": len(stored)}
        if failed:
            # Keep the owner's key where it is; the app will ask for it again once the secret store works.
            log.warning("SECRETS_NOT_STORED keys=%s code=E-SEC-001", ",".join(failed))
            return StageResult.warn(ctx.t("database.secrets_not_stored", count=len(failed)), hint=ctx.t("database.secrets_not_stored_hint"), error_code="E-SEC-001", **data)
        message = ctx.t("database.created" if data["created"] else ("database.migrated" if data["migrated"] else "database.ready"))
        if data["owner_setup_pending"]:
            message += " " + ctx.t("database.owner_pending")
        return StageResult.ok(message, **data)


def _last_json(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


# ----------------------------------------------------------------------------- doctor
class DoctorStage:
    """Stage 12: the full health check, shown in the owner's language."""

    key: str = "doctor"
    title_key: str = "stage.doctor"

    def applies(self, ctx: StageContext) -> bool:
        """Needs the venv."""
        return bool(ctx.result_data("venv").get("python"))

    def cache_key(self, ctx: StageContext) -> str | None:
        """Every run — that is the point of a daily check."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Run ``doctor [--fix] [--lang]`` and map its exit code."""
        python = _python(ctx)
        if python is None:
            return StageResult.fail(ctx.t("database.app_missing"), error_code="E-SYS-002", hint=ctx.t("database.app_missing_hint"))
        args = ["doctor", "--lang", ctx.messages.lang]
        if getattr(ctx.options, "fix", False):
            args.append("--fix")
        if not ctx.console.unicode:
            args.append("--ascii")
        result = handover.run_app(python, args, timeout=600)
        if result.timed_out or result.exit_code not in (0, 1, 2):
            return StageResult.fail(ctx.t("doctor.crashed"), error_code="E-SYS-099", detail=result.output[-2000:], hint=ctx.t("doctor.crashed_hint"))
        echo = _echo(ctx)
        for line in result.output.splitlines():
            echo(line)
        if result.exit_code == 0:
            return StageResult.ok(ctx.t("doctor.ok"), exit_code=0)
        if result.exit_code == 1:
            return StageResult.warn(ctx.t("doctor.warn"), hint=ctx.t("doctor.warn_hint"), exit_code=1)
        return StageResult.fail(ctx.t("doctor.fail"), error_code="E-SYS-099", hint=ctx.t("doctor.fail_hint"), exit_code=2)


# ----------------------------------------------------------------------------- start + watchdog
def launch_command(python: Path, *, no_browser: bool, lang: str) -> list[str]:
    """``python -m vaishnavi run`` argv."""
    cmd = [str(python), "-m", handover.PACKAGE, "run", "--lang", lang]
    if no_browser:
        cmd.append("--no-browser")
    return cmd


def launch_detached(cmd: list[str], log_dir: Path) -> subprocess.Popen[bytes]:
    """Start the app so that closing the installer window does not kill it; stdout/stderr → ``logs/install/app-stdout.log``."""
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "app-stdout.log", "ab")  # noqa: SIM115 - handed to the child
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)  # noqa: S603
    finally:
        out.close()


class Watchdog:
    """Keeps one app process alive (docs/07 §8). All collaborators are injected so it can be tested without time."""

    def __init__(
        self,
        *,
        launch: Callable[[], Any],
        health: Callable[[str], dict[str, Any] | None],
        url: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 15.0,
        stale_after: float = 60.0,
        startup_grace: float = 90.0,
        max_restarts_per_hour: int = 3,
        log_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.launch, self.health, self.url = launch, health, url
        self.clock, self.sleep = clock, sleep
        self.poll_s, self.stale_after, self.startup_grace = poll_s, stale_after, startup_grace
        self.max_restarts_per_hour = max_restarts_per_hour
        self._log_hook = log_hook
        self.proc: Any = None
        self.restarts: list[float] = []
        self.gave_up = False

    def _log(self, level: int, text: str) -> None:
        log.log(level, text)
        if self._log_hook:
            self._log_hook(text)

    def adopt(self, proc: Any) -> None:
        """Supervise a process launched by the caller (the start stage already waited for its health)."""
        self.proc = proc
        self._started_at = self.clock()
        self._last_ok = self.clock()

    def _start(self) -> None:
        self.proc = self.launch()
        self._started_at = self.clock()
        self._last_ok = self.clock()
        self._log(logging.INFO, f"WATCHDOG_LAUNCHED pid={getattr(self.proc, 'pid', '?')}")

    def _restart(self, reason: str) -> bool:
        now = self.clock()
        self.restarts = [t for t in self.restarts if now - t < 3600]
        if len(self.restarts) >= self.max_restarts_per_hour:
            self.gave_up = True
            self._log(logging.ERROR, f"WATCHDOG_GAVE_UP restarts_last_hour={len(self.restarts)} reason={reason} code=E-SYS-020")
            return False
        self._log(logging.ERROR, f"WATCHDOG_RESTART reason={reason} code=E-SYS-020")
        self._kill()
        self.restarts.append(now)
        self._start()
        return True

    def _kill(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError as exc:
            log.debug("kill: %s", exc)
        try:
            proc.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001 - mocks / already gone
            log.debug("wait: %s", exc)

    def run(self, max_iterations: int | None = None) -> None:
        """Supervise until told to stop (``max_iterations`` for tests) or until giving up."""
        if self.proc is None:
            self._start()
        iterations = 0
        while not self.gave_up and (max_iterations is None or iterations < max_iterations):
            iterations += 1
            self.sleep(self.poll_s)
            now = self.clock()
            if self.proc is not None and self.proc.poll() is not None:
                if not self._restart(f"exited code={self.proc.returncode}"):
                    break
                continue
            status = self.health(self.url)
            if status and status.get("ok", status.get("status") == "ok"):
                self._last_ok = now
                continue
            in_grace = now - self._started_at < self.startup_grace
            if (not in_grace or status is not None) and now - self._last_ok > self.stale_after:
                if not self._restart("health stale" if status is None else f"health not ok: {status.get('stale', '')}"):
                    break
        self._log(logging.INFO, f"WATCHDOG_STOPPED iterations={iterations} gave_up={self.gave_up}")


class StartStage:
    """Stage 13: start the app, wait until it answers, tell the owner."""

    key: str = "start"
    title_key: str = "stage.start"

    def __init__(self, *, watchdog: bool = True) -> None:
        self.watchdog = watchdog

    def applies(self, ctx: StageContext) -> bool:
        """Needs the venv; skipped under ``--install-only`` (not even in the pipeline then)."""
        return bool(ctx.result_data("venv").get("python"))

    def cache_key(self, ctx: StageContext) -> str | None:
        """Never cached."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Launch (or adopt an already running instance), wait for /healthz, print the ready line."""
        python = _python(ctx)
        if python is None:
            return StageResult.fail(ctx.t("database.app_missing"), error_code="E-SYS-002", hint=ctx.t("database.app_missing_hint"))
        url = app_url(ctx.paths.config_dir)
        existing = fetch_health(url)
        if existing and existing.get("ok"):
            log.info("APP_ALREADY_RUNNING url=%s", url)
            return StageResult.ok(ctx.t("start.ready", url=url) + " " + ctx.t("start.already_running"), url=url, adopted=True)
        no_browser = bool(getattr(ctx.options, "no_browser", False))
        cmd = launch_command(python, no_browser=no_browser, lang=ctx.messages.lang)
        log.info("APP_LAUNCH %s", " ".join(cmd))
        try:
            proc = launch_detached(cmd, ctx.paths.install_log_dir)
        except OSError as exc:
            return StageResult.fail(ctx.t("start.launch_failed"), error_code="E-SYS-099", detail=repr(exc))
        ctx.console.info(ctx.t("start.waiting"))
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.error("APP_EXITED_EARLY code=%s", proc.returncode)
                return StageResult.fail(ctx.t("start.exited", code=proc.returncode), error_code="E-SYS-099", hint=ctx.t("start.exited_hint", path=str(ctx.paths.logs_dir / "app")))
            status = fetch_health(url)
            if status and status.get("ok"):
                break
            time.sleep(HEALTH_POLL_S)
        if not (status and status.get("ok")):
            log.error("APP_NOT_HEALTHY_IN_TIME url=%s last=%s", url, status)
            return StageResult.fail(ctx.t("start.not_ready", seconds=HEALTH_TIMEOUT_S), error_code="E-SYS-099", hint=ctx.t("start.exited_hint", path=str(ctx.paths.logs_dir / "app")))
        message = ctx.t("start.ready", url=url)
        ctx.console.ok(message)
        if self.watchdog:
            ctx.console.info(ctx.t("start.watchdog_note", url=url))
            wd = Watchdog(launch=lambda: launch_detached(cmd, ctx.paths.install_log_dir), health=fetch_health, url=url)
            wd.adopt(proc)
            try:
                wd.run()
            except KeyboardInterrupt:
                log.info("WATCHDOG_INTERRUPTED (app keeps running)")
            if wd.gave_up:
                return StageResult.fail(ctx.t("start.gave_up"), error_code="E-SYS-020", hint=ctx.t("start.exited_hint", path=str(ctx.paths.logs_dir / "app")), url=url)
        return StageResult.ok(message, url=url, pid=proc.pid)


def register(options: Options) -> list[Any]:
    """Database + doctor (+ start unless ``--install-only``)."""
    stages: list[Any] = [DatabaseStage(), DoctorStage()]
    if not options.install_only:
        stages.append(StartStage(watchdog=not getattr(options, "no_watchdog", False)))
    return stages
