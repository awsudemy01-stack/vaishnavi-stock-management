"""``main.py --restore`` and ``main.py --update <zip-or-folder>`` — maintenance modes of the installer.

Both talk to the installed application through ``handover`` (``python -m vaishnavi …`` inside the venv) and
print plain lines, never tracebacks. ``--restore``: list snapshots → pick one → confirm → ``restore --id``.
``--update``: ``backup --trigger pre-update`` → ``bootstrap.update`` (stage old files → copy new → pip from the
lock file → ``selftest``; roll back on failure).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from bootstrap import handover, update
from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext
from bootstrap.wizard import Kind, Option, Question

log = logging.getLogger("bootstrap.maintenance")


def _python(ctx: StageContext) -> Path | None:
    python = handover.venv_python_from(ctx)
    if python is None:
        venv = ctx.paths.program_dir / ".venv"
        candidate = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        python = candidate if candidate.exists() else None
    return python


@register_mode(Mode.RESTORE)
def run_restore(ctx: StageContext) -> ExitCode:
    """Guided restore from the backup location."""
    assert ctx.wizard is not None
    console = ctx.console
    python = _python(ctx)
    if python is None:
        console.fail(ctx.t("recovery.app_missing") + "  (E-SYS-002)")
        return ExitCode.FAIL
    listing = handover.run_app(python, ["restore", "--list", "--json"], timeout=120)
    if not listing.ok:
        log.error("RESTORE_LIST_FAILED exit=%s", listing.exit_code)
        console.fail(ctx.t("restore.list_failed") + "  (E-BKP-002)")
        return ExitCode.FAIL
    try:
        snapshots = json.loads(listing.output.strip().splitlines()[-1]) if listing.output.strip() else []
    except (ValueError, IndexError):
        snapshots = []
    if not snapshots:
        console.warn(ctx.t("restore.none"))
        return ExitCode.WARN
    console.info(ctx.t("restore.intro"))
    shown = snapshots[:15]
    for i, s in enumerate(shown, start=1):
        console.hint(f"[{i}] {s['taken_at']}  {s['trigger']:<10} {s['backup_id']}  {s.get('last_invoice') or ''}")
    # Option labels are message keys; a literal id simply echoes itself (the list above carries the detail).
    options = tuple(Option(str(i), str(s["backup_id"])) for i, s in enumerate(shown, start=1))
    question = Question(key="restore_pick", kind=Kind.CHOICE, prompt_key="restore.pick", help_key="restore.pick_help", options=options, default="1", allow_back=False, persist=False)
    picked = ctx.wizard.ask(question)
    chosen = shown[int(picked) - 1]
    confirm = Question(key="restore_confirm", kind=Kind.YESNO, prompt_key="restore.confirm", help_key="restore.confirm_help", default="no", allow_back=False, persist=False)
    if ctx.wizard.ask(confirm) != "yes":
        console.warn(ctx.t("result.cancelled"))
        return ExitCode.CANCELLED
    console.busy(ctx.t("restore.working"))
    result = handover.run_app(python, ["restore", "--id", str(chosen["backup_id"]), "--yes", "--json"], timeout=600)
    if result.ok:
        console.ok(ctx.t("restore.done", backup_id=str(chosen["backup_id"])))
        console.hint(ctx.t("restore.after"))
        return ExitCode.OK
    code = "E-SYS-016" if "E-SYS-016" in result.output else "E-BKP-002"
    log.error("RESTORE_FAILED exit=%s code=%s", result.exit_code, code)
    console.fail(ctx.t("restore.failed") + f"  ({code})")
    return ExitCode.FAIL


@register_mode(Mode.UPDATE)
def run_update(ctx: StageContext) -> ExitCode:
    """Apply a release with backup, checks and rollback."""
    console = ctx.console
    source = ctx.options.update_path
    assert source is not None
    python = _python(ctx)
    try:
        plan = update.plan_update(ctx.paths.program_dir, source)
    except update.UpdateError as exc:
        log.error("UPDATE_REJECTED reason=%s code=E-SYS-060", exc)
        console.fail(ctx.t("update.rejected", reason=str(exc)) + "  (E-SYS-060)")
        return ExitCode.FAIL
    console.info(ctx.t("update.intro", current=plan.current_version, new=plan.new_version))
    if python is not None:
        console.busy(ctx.t("update.backup"))
        snap = handover.run_app(python, ["backup", "--trigger", "pre-update"], timeout=300)
        if not snap.ok:
            log.error("UPDATE_BACKUP_FAILED exit=%s", snap.exit_code)
            console.fail(ctx.t("update.backup_failed") + "  (E-BKP-003)")
            return ExitCode.FAIL

    def run_check(stage: str) -> tuple[bool, str]:
        if python is None:
            return True, "no venv yet — checks skipped"
        if stage == "install":
            lock = ctx.paths.program_dir / "requirements.lock"
            args = [str(python), "-m", "pip", "install", "--no-deps", "-e", str(ctx.paths.program_dir)] if not lock.exists() else [str(python), "-m", "pip", "install", "-r", str(lock), "-e", str(ctx.paths.program_dir)]
            try:
                proc = subprocess.run(args, capture_output=True, text=True, timeout=1800, check=False)  # noqa: S603
            except (OSError, subprocess.SubprocessError) as exc:
                return False, repr(exc)
            return proc.returncode == 0, (proc.stderr or proc.stdout or "")[-400:]
        result = handover.run_app(python, ["selftest"], timeout=1800)
        return result.ok, result.output[-400:]

    console.busy(ctx.t("update.applying"))
    result = update.apply_update(plan, run_check=run_check)
    if result.ok:
        console.ok(ctx.t("update.done", new=plan.new_version))
        return ExitCode.OK
    log.error("UPDATE_FAILED rolled_back=%s detail=%s code=E-SYS-061", result.rolled_back, result.detail[:200])
    console.fail(ctx.t("update.failed", detail=result.detail[:160]) + "  (E-SYS-061)")
    if result.rolled_back:
        console.hint(ctx.t("update.rolled_back", current=plan.current_version))
    return ExitCode.FAIL
