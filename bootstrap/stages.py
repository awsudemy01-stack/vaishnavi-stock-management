"""Ordered stage registry and runner for the installer (stdlib only).

A *stage* is one step of ``main.py`` (survey, Python environment, AI provider, …). The runner executes
stages in order, logs ``STAGE_START/OK/WARN/SKIP/FAIL`` with elapsed milliseconds, prints one
plain-language line per stage, persists results to ``logs/install/state.json`` so an interrupted
install resumes, and maps the overall outcome to the documented exit codes (0 OK · 1 WARN · 2 FAIL ·
3 cancelled · 4 internal error).

Rules every stage must follow:
- be idempotent: check first, act only if needed, report SKIP/OK accordingly;
- never raise for expected problems — return ``StageResult.fail(...)`` with an ``E-SYS-xxx`` code and a
  'what to do' hint. An uncaught exception is treated as an installer bug (exit 4);
- never print directly; use ``ctx.console`` so every line is also logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from bootstrap.console import Console
from bootstrap.i18n import Messages

if TYPE_CHECKING:
    from bootstrap.cli import Options
    from bootstrap.wizard import Wizard

log = logging.getLogger("bootstrap.stages")

STATE_SCHEMA = 1
KEEP_RUN_HISTORY = 20


class ExitCode(IntEnum):
    """Process exit codes of ``main.py`` (docs/17 §7, §10)."""

    OK = 0
    WARN = 1
    FAIL = 2
    CANCELLED = 3
    INTERNAL = 4


class ControlFlow(Exception):  # noqa: N818 (signal, not an error)
    """Base for exceptions the runner must let through untouched (owner quit, no terminal, …).

    ``cli.main`` turns them into the right exit code and message; they are never 'installer bugs'.
    """


class StageStatus(str, Enum):
    """Outcome of one stage."""

    OK = "OK"
    WARN = "WARN"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass(frozen=True)
class StageResult:
    """What a stage reports back; ``message`` is for the owner, ``detail`` for the log."""

    status: StageStatus
    message: str = ""
    detail: str = ""
    error_code: str = ""
    hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    @classmethod
    def ok(cls, message: str = "", **data: Any) -> StageResult:
        """Stage done; ``data`` is made available to later stages via ``ctx.results``."""
        return cls(StageStatus.OK, message, data=data)

    @classmethod
    def warn(cls, message: str, *, hint: str = "", error_code: str = "", detail: str = "", **data: Any) -> StageResult:
        """Stage done but the owner should look at something; the installer continues."""
        return cls(StageStatus.WARN, message, detail, error_code, hint, data)

    @classmethod
    def skip(cls, message: str = "", **data: Any) -> StageResult:
        """Stage not needed on this machine / already done."""
        return cls(StageStatus.SKIP, message, data=data)

    @classmethod
    def fail(cls, message: str, *, error_code: str, hint: str = "", detail: str = "", **data: Any) -> StageResult:
        """Essential step failed; the installer stops after this stage."""
        return cls(StageStatus.FAIL, message, detail, error_code, hint, data)

    def with_elapsed(self, elapsed_ms: int) -> StageResult:
        """Copy with the measured duration filled in."""
        return StageResult(self.status, self.message, self.detail, self.error_code, self.hint, dict(self.data), elapsed_ms)


@dataclass
class InstallPaths:
    """Filesystem locations the installer uses before the data location is known."""

    program_dir: Path
    logs_dir: Path
    install_log_dir: Path
    state_file: Path
    config_dir: Path

    @classmethod
    def from_program_dir(cls, program_dir: Path) -> InstallPaths:
        """Derive all paths from the folder that contains ``main.py``."""
        program_dir = program_dir.resolve()
        logs_dir = program_dir / "logs"
        install_log_dir = logs_dir / "install"
        return cls(
            program_dir=program_dir,
            logs_dir=logs_dir,
            install_log_dir=install_log_dir,
            state_file=install_log_dir / "state.json",
            config_dir=program_dir / "config",
        )

    @property
    def lock_file(self) -> Path:
        """Advisory lock taken by a running installer (``logs/install/.lock``)."""
        return self.install_log_dir / ".lock"


class InstallLock:
    """One installer at a time per program folder.

    A double-click while the autostart entry is already running (or two clicks on the icon) would
    otherwise rebuild ``.venv`` under the other process's feet and race on ``state.json``.
    The lock is advisory and released by the OS when the process dies, so a crash never leaves a
    stale lock behind.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another installer is running."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        except OSError as exc:
            log.warning("INSTALL_LOCK_UNAVAILABLE path=%s error=%s — continuing without it", self.path, exc)
            return True
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            log.warning("INSTALL_LOCK_HELD path=%s code=E-SYS-016", self.path)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        self._fd = fd
        log.debug("INSTALL_LOCK_ACQUIRED pid=%d", os.getpid())
        return True

    def release(self) -> None:
        """Release (safe to call twice)."""
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError as exc:
            log.debug("install lock release: %s", exc)
        finally:
            os.close(self._fd)
            self._fd = None
        log.debug("INSTALL_LOCK_RELEASED")


class StateStore:
    """Persists per-stage outcomes and run history to ``state.json`` (atomic writes)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {"schema": STATE_SCHEMA, "runs": [], "stages": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.debug("state: no previous state at %s", self.path)
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # A damaged state file must never block the installer; stages are idempotent anyway.
            log.warning("state: could not read %s (%s) — starting fresh", self.path, exc)
            return
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            log.warning("state: unknown schema in %s — starting fresh", self.path)
            return
        self._data = data
        self._data.setdefault("runs", [])
        self._data.setdefault("stages", {})
        log.debug("state: loaded %d stage records, %d runs", len(self._data["stages"]), len(self._data["runs"]))

    def save(self) -> None:
        """Write atomically (temp file + replace) so a power cut cannot leave a half file.

        On Windows the replace can transiently fail while antivirus / cloud-sync inspects the fresh
        temp file, so it is retried briefly; a final failure is logged, never raised.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError as exc:
                if attempt == 4:
                    log.warning("state: could not replace %s after retries: %s", self.path, exc)
                    return
                time.sleep(0.05 * (attempt + 1))
        log.debug("state: saved %s", self.path)

    # -- stage records ----------------------------------------------------------------------
    def stage_record(self, key: str) -> dict[str, Any] | None:
        """Previous outcome of a stage, if any."""
        rec = self._data["stages"].get(key)
        return dict(rec) if isinstance(rec, dict) else None

    def is_done(self, key: str, cache_key: str) -> bool:
        """True when the stage previously finished OK/WARN for the same cache key."""
        rec = self.stage_record(key)
        if rec is None:
            return False
        return rec.get("status") in (StageStatus.OK.value, StageStatus.WARN.value) and rec.get("cache_key") == cache_key

    def record_stage(self, key: str, result: StageResult, cache_key: str | None) -> None:
        """Store a stage outcome (data is kept so later runs can reuse e.g. the survey)."""
        self._data["stages"][key] = {
            "status": result.status.value,
            "at": datetime.now().isoformat(timespec="seconds"),
            "cache_key": cache_key,
            "message": result.message,
            "error_code": result.error_code,
            "elapsed_ms": result.elapsed_ms,
            "data": result.data,
        }

    def stage_data(self, key: str) -> dict[str, Any]:
        """Data a previous run of ``key`` left behind (empty if none)."""
        rec = self.stage_record(key)
        data = rec.get("data") if rec else None
        return dict(data) if isinstance(data, dict) else {}

    # -- run history ------------------------------------------------------------------------
    def start_run(self, run_id: str, argv: Sequence[str]) -> None:
        """Append a run entry; history is capped at ``KEEP_RUN_HISTORY``."""
        runs = self._data["runs"]
        runs.append({"run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"), "argv": list(argv), "exit_code": None})
        del runs[:-KEEP_RUN_HISTORY]

    def finish_run(self, run_id: str, exit_code: int) -> None:
        """Mark the run finished."""
        for run in reversed(self._data["runs"]):
            if run.get("run_id") == run_id:
                run["finished_at"] = datetime.now().isoformat(timespec="seconds")
                run["exit_code"] = int(exit_code)
                break


@dataclass
class StageContext:
    """Everything a stage may touch. Stages communicate only through ``results``/``state``."""

    options: Options
    paths: InstallPaths
    messages: Messages
    console: Console
    state: StateStore
    run_id: str
    results: dict[str, StageResult] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    """Live, non-serialisable objects shared between stages (e.g. the mini log server)."""
    wizard: Wizard | None = None
    """Question engine; attached by ``cli.main`` (None in some unit tests)."""

    def t(self, key: str, **params: Any) -> str:
        """Shortcut for translated messages."""
        return self.messages.t(key, **params)

    def result_data(self, stage_key: str) -> dict[str, Any]:
        """Data produced by an earlier stage in this run, else what the last run stored."""
        res = self.results.get(stage_key)
        if res is not None:
            return dict(res.data)
        return self.state.stage_data(stage_key)


class Stage(Protocol):
    """One installer step. Implementations live in their own ``bootstrap/<name>.py`` modules."""

    key: str
    title_key: str

    def applies(self, ctx: StageContext) -> bool:
        """False → stage is skipped as 'not needed on this computer'."""
        ...

    def cache_key(self, ctx: StageContext) -> str | None:
        """Non-None → skip when ``state`` says this exact key already finished OK/WARN."""
        ...

    def run(self, ctx: StageContext) -> StageResult:
        """Do the work (idempotently) and report."""
        ...


@dataclass
class RunOutcome:
    """Summary of a pipeline run."""

    exit_code: ExitCode
    results: dict[str, StageResult]
    stopped_at: str | None = None
    error_code: str = ""

    @property
    def warnings(self) -> list[str]:
        """Keys of stages that ended with WARN."""
        return [k for k, r in self.results.items() if r.status is StageStatus.WARN]


class StageRunner:
    """Runs a pipeline of stages with logging, console reporting and resumable state."""

    def __init__(self, ctx: StageContext) -> None:
        self.ctx = ctx

    def run(self, stages: Sequence[Stage]) -> RunOutcome:
        """Execute ``stages`` in order; stop at the first FAIL; never raise."""
        ctx = self.ctx
        total = len(stages)
        log.info("PIPELINE_START run=%s stages=%d", ctx.run_id, total)
        if total == 0:
            ctx.console.info(ctx.t("runner.nothing_registered", count=0))
        worst = StageStatus.OK
        stopped_at: str | None = None
        error_code = ""

        for index, stage in enumerate(stages, start=1):
            title = ctx.t(stage.title_key)
            log.debug("STAGE_PREPARE key=%s index=%d/%d", stage.key, index, total)

            if not stage.applies(ctx):
                result = StageResult.skip(ctx.t("runner.skip_not_applicable"))
                self._finish(stage, result, None)
                continue

            cache_key = stage.cache_key(ctx)
            if cache_key is not None and ctx.state.is_done(stage.key, cache_key):
                previous = ctx.state.stage_record(stage.key) or {}
                cached_status = StageStatus(previous.get("status", StageStatus.OK.value))
                result = StageResult(StageStatus.SKIP, ctx.t("runner.skip_cached"), data=ctx.state.stage_data(stage.key))
                if cached_status is StageStatus.WARN:
                    # The earlier warning (e.g. backups on the same disk) is still true — keep showing it.
                    result = StageResult(StageStatus.WARN, f"{ctx.t('runner.skip_cached')} — {previous.get('message', '')}".rstrip(" —"), error_code=str(previous.get("error_code") or ""), data=ctx.state.stage_data(stage.key))
                    worst = _worse(worst, StageStatus.WARN)
                self._finish(stage, result, cache_key, record=False)
                continue

            log.info("STAGE_START key=%s title=%s", stage.key, title)
            started = time.perf_counter()
            try:
                result = stage.run(ctx)
            except (KeyboardInterrupt, ControlFlow):
                elapsed = int((time.perf_counter() - started) * 1000)
                log.warning("STAGE_INTERRUPTED key=%s ms=%d", stage.key, elapsed)
                ctx.state.save()
                raise
            except Exception as exc:  # installer bug → exit 4, never a silent crash
                elapsed = int((time.perf_counter() - started) * 1000)
                log.exception("STAGE_CRASH key=%s ms=%d error=%r", stage.key, elapsed, exc)
                result = StageResult.fail(
                    ctx.t("errors.E-SYS-099"), error_code="E-SYS-099", detail=repr(exc)
                ).with_elapsed(elapsed)
                self._finish(stage, result, cache_key)
                ctx.state.save()
                return RunOutcome(ExitCode.INTERNAL, ctx.results, stopped_at=stage.key, error_code="E-SYS-099")

            result = result.with_elapsed(int((time.perf_counter() - started) * 1000))
            self._finish(stage, result, cache_key)
            worst = _worse(worst, result.status)
            if result.status is StageStatus.FAIL:
                stopped_at, error_code = stage.key, result.error_code
                break

        ctx.state.save()
        exit_code = {StageStatus.OK: ExitCode.OK, StageStatus.SKIP: ExitCode.OK, StageStatus.WARN: ExitCode.WARN, StageStatus.FAIL: ExitCode.FAIL}[worst]
        log.info("PIPELINE_END run=%s exit=%d worst=%s stopped_at=%s", ctx.run_id, exit_code, worst.value, stopped_at)
        return RunOutcome(exit_code, ctx.results, stopped_at, error_code)

    def _finish(self, stage: Stage, result: StageResult, cache_key: str | None, *, record: bool = True) -> None:
        ctx = self.ctx
        ctx.results[stage.key] = result
        if record:
            ctx.state.record_stage(stage.key, result, cache_key)
            try:
                ctx.state.save()  # checkpoint after every stage: a crash or power cut keeps the progress
            except OSError as exc:
                log.warning("state: checkpoint failed: %s", exc)
        title = ctx.t(stage.title_key)
        text = f"{title}" + (f" — {result.message}" if result.message else "")
        log.log(
            logging.WARNING if result.status in (StageStatus.WARN, StageStatus.FAIL) else logging.INFO,
            "STAGE_%s key=%s ms=%d code=%s msg=%s detail=%s",
            result.status.value, stage.key, result.elapsed_ms, result.error_code or "-", result.message, result.detail or "-",
        )
        code_suffix = f"  ({result.error_code})" if result.error_code else ""
        if result.status is StageStatus.OK:
            ctx.console.ok(text)
        elif result.status is StageStatus.WARN:
            ctx.console.warn(text + code_suffix)
        elif result.status is StageStatus.SKIP:
            ctx.console.skip(text)
        else:
            ctx.console.fail(text + code_suffix)
        if result.hint:
            ctx.console.hint(result.hint)


_SEVERITY = {StageStatus.SKIP: 0, StageStatus.OK: 0, StageStatus.WARN: 1, StageStatus.FAIL: 2}


def _worse(a: StageStatus, b: StageStatus) -> StageStatus:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def build_default_pipeline(options: Options) -> list[Stage]:
    """The ordered stages of a normal ``main.py`` run.

    Each unit of Phase 1 appends its stage here (docs/13 checklist). Order follows docs/17 §3.3:
    survey → python → venv/packages → system components → AI provider → locations → database →
    backup → autostart → doctor → start.
    """
    stages: list[Stage] = []
    from bootstrap import ai_provider, app_stages, autostart, locations, minilog, offline, pkg_manager, python_env, survey, wizard  # local imports: stage modules import this module

    stages.append(minilog.register(options))
    stages.append(wizard.register(options))
    stages.append(survey.register(options))
    stages.append(offline.register(options))
    stages.extend(python_env.register(options))
    stages.append(pkg_manager.register(options))
    stages.append(ai_provider.register(options))
    stages.append(locations.register(options))
    stages.append(autostart.register(options))
    stages.extend(app_stages.register(options))  # database → doctor → start (start omitted with --install-only)
    log.debug("pipeline: %d stages registered (install_only=%s)", len(stages), options.install_only)
    return stages
