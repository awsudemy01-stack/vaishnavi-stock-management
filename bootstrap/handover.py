"""Hand over to the application inside the venv: ``python -m vaishnavi <command>`` (stdlib only).

Everything that needs third-party code (database, doctor, secrets, server) is done by the
application, not by the installer. This module locates the venv interpreter, checks that the
``vaishnavi`` package is importable there, runs a command while streaming its output into the
install log, and passes **secrets through stdin as JSON** — never on the command line or in the
environment, where other users of the PC could read them.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bootstrap.stages import StageContext

log = logging.getLogger("bootstrap.handover")

PACKAGE = "vaishnavi"
SECRET_PREFIX = "SECRET|"
"""Lines the application prints for the owner's eyes only (e.g. a new recovery key): shown, never logged."""
DEFAULT_TIMEOUT_S = 60 * 15


@dataclass(frozen=True)
class AppResult:
    """Outcome of one application command."""

    exit_code: int
    output: str
    elapsed_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Exit code zero."""
        return self.exit_code == 0 and not self.timed_out


def venv_python_from(ctx: StageContext) -> Path | None:
    """Interpreter recorded by the venv stage (None before it ran)."""
    value = ctx.result_data("venv").get("python")
    return Path(value) if value else None


def app_available(python: Path, timeout: float = 60) -> tuple[bool, str]:
    """Can ``import vaishnavi`` inside the venv? Returns (ok, version-or-reason)."""
    code = f"import importlib.metadata as m, {PACKAGE}; print(m.version('vaishnavi-store-manager'))"
    try:
        proc = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        return False, repr(exc)
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, reason[-1] if reason else f"exit {proc.returncode}"
    return True, proc.stdout.strip()


def run_app(
    python: Path,
    args: Sequence[str],
    *,
    secrets: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    cwd: Path | None = None,
) -> AppResult:
    """Run ``python -m vaishnavi <args>``; stream output to the log (and ``on_line``)."""
    cmd = [str(python), "-m", PACKAGE, *args]
    log.info("APP_EXEC %s secrets=%d", " ".join(args), len(secrets or {}))
    started = time.perf_counter()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None)  # noqa: S603
    except OSError as exc:
        log.error("APP_EXEC_FAILED %s", exc)
        return AppResult(-1, repr(exc), int((time.perf_counter() - started) * 1000))
    assert proc.stdin is not None and proc.stdout is not None
    try:
        # Secrets (if any) go through stdin only; an empty object keeps the protocol uniform.
        proc.stdin.write(json.dumps({"secrets": secrets or {}}) + "\n")
        proc.stdin.close()
    except (OSError, ValueError) as exc:
        log.debug("stdin to app closed early: %s", exc)
    stream = proc.stdout

    def drain() -> None:  # a silent child must not block the deadline below
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip()
                if not line:
                    continue
                if line.startswith(SECRET_PREFIX):
                    log.debug("app| %s***", SECRET_PREFIX)  # owner-only text: never in the log or the result
                    if on_line:
                        try:
                            on_line(line)
                        except Exception as exc:
                            log.debug("on_line callback failed: %r", exc)
                    continue
                lines.append(line)
                log.debug("app| %s", line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception as exc:
                        log.debug("on_line callback failed: %r", exc)
        except (OSError, ValueError) as exc:
            log.debug("APP_DRAIN_END %r", exc)

    reader = threading.Thread(target=drain, name="app-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("APP_TIMEOUT args=%s after %ss", " ".join(args), timeout)
        proc.kill()
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = -1
        timed_out = True
    finally:
        reader.join(timeout=5)
        try:
            stream.close()
        except OSError:
            pass
    elapsed = int((time.perf_counter() - started) * 1000)
    log.info("APP_EXIT args=%s code=%d ms=%d timed_out=%s", " ".join(args), code, elapsed, timed_out)
    return AppResult(code, "\n".join(lines), elapsed, timed_out)
