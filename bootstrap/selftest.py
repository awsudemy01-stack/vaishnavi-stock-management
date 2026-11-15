"""``main.py --selftest`` · ``--doctor [--fix]`` · ``--where`` — the owner's first-run / support tools (stdlib only).

All three are thin: they locate the venv interpreter, hand over to ``python -m vaishnavi …`` and translate
the exit code into the installer's OK / WARN / FAIL line (``--where`` needs no app at all — it reads
``config/locations.toml`` and lists the folders, so it works even when the install never finished).
"""

from __future__ import annotations

import logging
from pathlib import Path

from bootstrap import handover, locations
from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext

log = logging.getLogger("bootstrap.selftest")


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
