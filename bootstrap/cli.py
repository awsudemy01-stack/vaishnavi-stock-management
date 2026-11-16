"""Command-line flags, modes and the orchestrating ``main()`` of the installer (stdlib only).

Parses the flags documented in docs/17 §5 into a frozen ``Options`` object, opens the install log,
loads messages in the owner's language, and dispatches to the handler of the selected ``Mode``.
Modes other than the default install/run are registered by the unit that implements them; an
unregistered mode prints a plain-language "not available in this version" line (exit 2).
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

from bootstrap.console import Console
from bootstrap.i18n import DEFAULT_LANG, SUPPORTED_LANGS, Messages
from bootstrap.logsetup import new_run_id, redact, setup_install_logging
from bootstrap.minilog import run_logs_mode, stop_log_console
from bootstrap.stages import (
    ExitCode,
    InstallLock,
    InstallPaths,
    StageContext,
    StageRunner,
    StateStore,
    build_default_pipeline,
)
from bootstrap.wizard import ANSWERS_FILE, AnswerStore, NoDefaultError, NonInteractiveError, Wizard, WizardBack, WizardQuit

log = logging.getLogger("bootstrap.cli")

VERSION = "0.1.0"
PROG = "main.py"
AI_PROVIDERS = ("ollama", "openai", "huggingface", "none")
BUNDLE_TARGETS = ("windows", "macos", "linux")
LANG_ENV = "VAISHNAVI_LANG"


class Mode(str, Enum):
    """What the owner asked ``main.py`` to do."""

    INSTALL_RUN = "install-run"
    INSTALL_ONLY = "install-only"
    DOCTOR = "doctor"
    LOGS = "logs"
    WHERE = "where"
    RESTORE = "restore"
    UPDATE = "update"
    RESET_OWNER_PASSWORD = "reset-owner-password"
    SELFTEST = "selftest"
    UNINSTALL_AUTOSTART = "uninstall-autostart"
    MAKE_OFFLINE_BUNDLE = "make-offline-bundle"
    STOP = "stop"


MUTATING_MODES = frozenset({Mode.INSTALL_RUN, Mode.INSTALL_ONLY, Mode.RESTORE, Mode.UPDATE, Mode.RESET_OWNER_PASSWORD, Mode.UNINSTALL_AUTOSTART})
"""Modes that change the installation — only one of them may run at a time (``InstallLock``)."""


@dataclass(frozen=True)
class Options:
    """Parsed, validated command-line options."""

    mode: Mode
    lang: str = DEFAULT_LANG
    lang_explicit: bool = False
    install_only: bool = False
    fix: bool = False
    no_browser: bool = False
    ai_provider: str | None = None
    offline_dir: Path | None = None
    update_path: Path | None = None
    answers_path: Path | None = None
    assume_yes: bool = False
    debug: bool = False
    bundle_target: str | None = None


ModeHandler = Callable[[StageContext], ExitCode]
_MODE_HANDLERS: dict[Mode, ModeHandler] = {}


def register_mode(mode: Mode) -> Callable[[ModeHandler], ModeHandler]:
    """Decorator used by the unit that implements a mode (``--logs``, ``--where``, …)."""

    def deco(fn: ModeHandler) -> ModeHandler:
        _MODE_HANDLERS[mode] = fn
        return fn

    return deco


def registered_modes() -> frozenset[Mode]:
    """Modes that have a handler in this build."""
    return frozenset(_MODE_HANDLERS)


# ----------------------------------------------------------------------------- parsing
def _existing_path(kind: str) -> Callable[[str], Path]:
    def convert(value: str) -> Path:
        path = Path(value).expanduser()
        if kind == "dir" and not path.is_dir():
            raise argparse.ArgumentTypeError(f"folder not found: {value}")
        if kind == "file" and not path.is_file():
            raise argparse.ArgumentTypeError(f"file not found: {value}")
        if kind == "any" and not path.exists():
            raise argparse.ArgumentTypeError(f"path not found: {value}")
        return path

    return convert


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser for ``main.py`` (kept in one place so ``--help`` matches docs/17 §5)."""
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Vaishnavi Store Manager — installs what is missing, checks the computer, starts the shop software.",
        epilog="Documentation: docs/17-install-and-run.md",
    )
    p.add_argument("--version", action="version", version=f"Vaishnavi Store Manager {VERSION}")

    action = p.add_mutually_exclusive_group()
    action.add_argument("--install-only", action="store_true", help="install and check, but do not start the app")
    action.add_argument("--doctor", action="store_true", help="only run the health check and show the report")
    action.add_argument("--logs", action="store_true", help="open the log console on :8081 without starting the app")
    action.add_argument("--where", action="store_true", help="print program, data, backup and log locations")
    action.add_argument("--restore", action="store_true", help="guided restore of the database from the backup location")
    action.add_argument("--update", metavar="ZIP_OR_FOLDER", type=_existing_path("any"), help="apply a new release (backup → replace → migrate → selftest; rolls back on failure)")
    action.add_argument("--reset-owner-password", action="store_true", help="reset the Owner password using the 12-word recovery key (asks for the computer's admin password)")
    action.add_argument("--selftest", action="store_true", help="run the test suite with fakes plus live checks on this PC")
    action.add_argument("--uninstall-autostart", action="store_true", help="remove the start-at-logon entry; nothing else is removed")
    action.add_argument("--stop", action="store_true", help="shut the running shop application down cleanly (then start it again with main.py)")
    action.add_argument("--make-offline-bundle", metavar="TARGET_OS", choices=BUNDLE_TARGETS, help="on a connected PC, prepare offline/ for a shop PC without internet")

    p.add_argument("--fix", action="store_true", help="with --doctor: apply the safe automatic fixes")
    p.add_argument("--lang", choices=SUPPORTED_LANGS, default=os.environ.get(LANG_ENV, DEFAULT_LANG), help="language of installer messages (default: English, or $VAISHNAVI_LANG)")
    p.add_argument("--no-browser", action="store_true", help="start without opening a browser window")
    p.add_argument("--no-ai", action="store_true", help="same as --ai-provider none")
    p.add_argument("--ai-provider", choices=AI_PROVIDERS, help="pre-answer the AI provider question")
    p.add_argument("--offline", metavar="FOLDER", type=_existing_path("dir"), help="install packages, Ollama and models from a prepared offline bundle")
    p.add_argument("--answers", metavar="FILE", type=_existing_path("file"), help="replay config/install-answers.toml from another PC (asks nothing except secrets)")
    p.add_argument("--yes", action="store_true", help="accept the recommended answer to every question")
    p.add_argument("--debug", action="store_true", help="also show DEBUG log lines on the console")
    return p


def _mode_from(ns: argparse.Namespace) -> Mode:
    if ns.install_only:
        return Mode.INSTALL_ONLY
    if ns.doctor:
        return Mode.DOCTOR
    if ns.logs:
        return Mode.LOGS
    if ns.where:
        return Mode.WHERE
    if ns.restore:
        return Mode.RESTORE
    if ns.update is not None:
        return Mode.UPDATE
    if ns.reset_owner_password:
        return Mode.RESET_OWNER_PASSWORD
    if ns.selftest:
        return Mode.SELFTEST
    if ns.uninstall_autostart:
        return Mode.UNINSTALL_AUTOSTART
    if ns.stop:
        return Mode.STOP
    if ns.make_offline_bundle is not None:
        return Mode.MAKE_OFFLINE_BUNDLE
    return Mode.INSTALL_RUN


def parse_args(argv: Sequence[str]) -> Options:
    """Parse and cross-validate flags. Raises ``SystemExit(2)`` with a readable message on error."""
    parser = build_parser()
    ns = parser.parse_args(list(argv))
    mode = _mode_from(ns)

    if ns.fix and mode is not Mode.DOCTOR:
        parser.error("--fix only makes sense together with --doctor")

    ai_provider = ns.ai_provider
    if ns.no_ai:
        if ai_provider not in (None, "none"):
            parser.error(f"--no-ai conflicts with --ai-provider {ai_provider}")
        ai_provider = "none"

    return Options(
        mode=mode,
        lang=ns.lang,
        lang_explicit=any(a == "--lang" or a.startswith("--lang=") for a in argv) or LANG_ENV in os.environ,
        install_only=mode is Mode.INSTALL_ONLY,
        fix=ns.fix,
        no_browser=ns.no_browser,
        ai_provider=ai_provider,
        offline_dir=ns.offline,
        update_path=ns.update,
        answers_path=ns.answers,
        assume_yes=ns.yes,
        debug=ns.debug,
        bundle_target=ns.make_offline_bundle,
    )


# ----------------------------------------------------------------------------- modes
@register_mode(Mode.INSTALL_RUN)
@register_mode(Mode.INSTALL_ONLY)
def run_install(ctx: StageContext) -> ExitCode:
    """Default mode: run the full pipeline and print the result line."""
    outcome = StageRunner(ctx).run(build_default_pipeline(ctx.options))
    ctx.console.rule()
    if outcome.exit_code is ExitCode.OK:
        ctx.console.ok(ctx.t("result.ok"))
    elif outcome.exit_code is ExitCode.WARN:
        ctx.console.warn(ctx.t("result.warn", count=len(outcome.warnings)))
    elif outcome.exit_code is ExitCode.FAIL:
        ctx.console.fail(ctx.t("result.fail"))
    if ctx.options.mode is Mode.INSTALL_ONLY or outcome.exit_code is not ExitCode.OK:
        stop_log_console(ctx)
    return outcome.exit_code


register_mode(Mode.LOGS)(run_logs_mode)


def _import_mode_modules() -> None:
    """Import modules that register extra modes (done lazily: they import this module)."""
    import bootstrap.autostart  # noqa: F401, PLC0415
    import bootstrap.maintenance  # noqa: F401, PLC0415
    import bootstrap.offline  # noqa: F401, PLC0415
    import bootstrap.recovery  # noqa: F401, PLC0415
    import bootstrap.selftest  # noqa: F401, PLC0415


# ----------------------------------------------------------------------------- main
def main(argv: Sequence[str], *, program_dir: str | os.PathLike[str] | None = None, stdout: TextIO | None = None, stdin: TextIO | None = None) -> int:
    """Entry called by ``main.py``; returns the process exit code and never raises (except argparse's own exits)."""
    options = parse_args(argv)  # SystemExit for --help / bad flags is intentional and readable
    try:
        return _main(argv, options, program_dir=program_dir, stdout=stdout, stdin=stdin)
    except KeyboardInterrupt:
        return int(ExitCode.CANCELLED)
    except Exception as exc:  # before logging/messages exist: plain ASCII, no traceback
        try:
            logging.getLogger("bootstrap").exception("BOOTSTRAP_INIT_FAILED %r", exc)
        except Exception:
            pass
        err = sys.stderr
        err.write("\nSetup could not start: %s: %s\n" % (type(exc).__name__, exc))
        err.write("Check that this folder is writable and complete, then run Start Vaishnavi again. (E-SYS-099)\n")
        err.flush()
        return int(ExitCode.INTERNAL)


def _main(argv: Sequence[str], options: Options, *, program_dir: str | os.PathLike[str] | None, stdout: TextIO | None, stdin: TextIO | None) -> int:
    _import_mode_modules()
    paths = InstallPaths.from_program_dir(Path(program_dir) if program_dir else Path(__file__).resolve().parent.parent)
    run_id = new_run_id()
    log_path = setup_install_logging(paths.install_log_dir, run_id, debug_console=options.debug)

    messages = Messages.load(options.lang)
    console = Console(stdout or sys.stdout)
    state = StateStore(paths.state_file)
    state.start_run(run_id, argv)
    # This PC's own answers are always loaded, so a daily restart asks nothing that was answered
    # before; --answers replays another PC's file but still writes to the local one.
    local_answers = paths.config_dir / ANSWERS_FILE
    answers = AnswerStore.load(options.answers_path or local_answers)
    answers.path = local_answers
    prefilled: dict[str, str] = {}
    if options.ai_provider:
        prefilled["ai_provider"] = options.ai_provider
    wizard = Wizard(console, messages, answers, assume_yes=options.assume_yes, prefilled=prefilled, stdin=stdin or sys.stdin)
    ctx = StageContext(options=options, paths=paths, messages=messages, console=console, state=state, run_id=run_id, wizard=wizard)

    log.info("RUN_START run=%s mode=%s argv=%s", run_id, options.mode.value, redact(" ".join(argv)))
    log.info("ENV python=%s exe=%s os=%s arch=%s program_dir=%s", platform.python_version(), sys.executable, platform.platform(), platform.machine(), paths.program_dir)

    console.header(messages.t("runner.header"), messages.t("runner.log_hint", path=_short(log_path, paths.program_dir)))

    handler = _MODE_HANDLERS.get(options.mode)
    lock = InstallLock(paths.lock_file)
    exit_code: ExitCode
    try:
        if handler is None:
            log.warning("MODE_UNAVAILABLE mode=%s code=E-SYS-050", options.mode.value)
            console.fail(messages.t("mode.unavailable", mode=f"--{options.mode.value}") + "  (E-SYS-050)")
            exit_code = ExitCode.FAIL
        elif options.mode in MUTATING_MODES and not lock.acquire():
            console.warn(messages.t("runner.already_running") + "  (E-SYS-016)")
            exit_code = ExitCode.WARN
        else:
            exit_code = handler(ctx)
    except KeyboardInterrupt:
        log.warning("RUN_CANCELLED run=%s code=E-SYS-090", run_id)
        console.line()
        console.warn(messages.t("result.cancelled") + "  (E-SYS-090)")
        exit_code = ExitCode.CANCELLED
    except (WizardQuit, WizardBack):
        log.warning("RUN_QUIT_BY_OWNER run=%s code=E-SYS-090", run_id)
        console.line()
        console.warn(ctx.messages.t("result.cancelled") + "  (E-SYS-090)")
        exit_code = ExitCode.CANCELLED
    except NonInteractiveError as exc:
        log.error("RUN_NONINTERACTIVE run=%s question=%s code=E-SYS-009", run_id, exc.key)
        console.fail(ctx.messages.t("wizard.err_noninteractive", question=exc.key) + "  (E-SYS-009)")
        exit_code = ExitCode.FAIL
    except NoDefaultError as exc:
        log.error("RUN_NO_DEFAULT run=%s question=%s code=E-SYS-008", run_id, exc.key)
        console.fail(ctx.messages.t("wizard.err_no_default", question=exc.key) + "  (E-SYS-008)")
        exit_code = ExitCode.FAIL
    except Exception as exc:  # last line of defence: the owner must never see a traceback
        log.exception("RUN_INTERNAL_ERROR run=%s code=E-SYS-099 error=%r", run_id, exc)
        console.line()
        console.fail(messages.t("result.internal", path=log_path, code="E-SYS-099"))
        exit_code = ExitCode.INTERNAL
    finally:
        lock.release()

    if exit_code is not ExitCode.OK:
        console.line(messages.t("result.details_in_log", path=log_path))
    state.finish_run(run_id, exit_code)
    try:
        state.save()
    except OSError as exc:
        log.warning("state: could not save at end of run: %s", exc)
    log.info("RUN_END run=%s exit=%d", run_id, exit_code)
    logging.shutdown()
    return int(exit_code)


def _short(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
