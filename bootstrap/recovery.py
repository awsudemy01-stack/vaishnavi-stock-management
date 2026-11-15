"""``main.py --reset-owner-password`` — the lock-out escape hatch (FR-A9, ADR-037; stdlib only).

Steps, each explained on screen:

1. ask for the 12-word recovery key (shape-checked against the shipped wordlist, not echoed);
2. ask for the new Owner password twice (masked, ≥ 6 characters);
3. **prove administrator rights** — a harmless command is run through UAC / sudo / pkexec; a declined or
   unavailable prompt stops here with ``E-SYS-004`` (nothing has been changed yet);
4. hand the key and the password to the application inside the venv through **stdin JSON** (never on
   the command line, never in the environment) — ``python -m vaishnavi reset-owner-password`` verifies
   the key, sets the password, audits, and prints the *new* recovery sheet as ``SECRET|`` lines which are
   shown to the owner and kept out of every log.
"""

from __future__ import annotations

import getpass
import logging
import re
import sys
from pathlib import Path

from bootstrap import elevate, handover
from bootstrap.cli import Mode, register_mode
from bootstrap.stages import ExitCode, StageContext
from bootstrap.wizard import Kind, Question

log = logging.getLogger("bootstrap.recovery")

WORDS = 12
MIN_PASSWORD_LEN = 6
PASSWORD_ATTEMPTS = 3
_CODE_RE = re.compile(r"\bE-[A-Z]+-\d{3}\b")


def wordlist_path(program_dir: Path) -> Path:
    """The shipped BIP-39 list inside the source tree."""
    return program_dir / "src" / "vaishnavi" / "core" / "wordlist.txt"


def load_words(program_dir: Path) -> frozenset[str]:
    """Known words (empty set when the file is missing — the shape check then only counts words)."""
    path = wordlist_path(program_dir)
    try:
        return frozenset(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))
    except OSError as exc:
        log.warning("wordlist unavailable (%s) — only counting words", exc)
        return frozenset()


def key_problem(text: str, words: frozenset[str], ctx: StageContext) -> str | None:
    """Plain-language problem with the typed key, or None."""
    typed = text.lower().split()
    if len(typed) != WORDS:
        return ctx.t("recovery.err_count", count=len(typed), need=WORDS)
    if words:
        unknown = [w for w in typed if w not in words]
        if unknown:
            return ctx.t("recovery.err_words", words=", ".join(unknown[:3]))
    return None


def ask_new_password(ctx: StageContext) -> str | None:
    """Masked prompt, twice; None when the owner gave up."""
    for _ in range(PASSWORD_ATTEMPTS):
        first = getpass.getpass("    " + ctx.t("recovery.ask_password") + " ").strip()
        if len(first) < MIN_PASSWORD_LEN:
            ctx.console.warn(ctx.t("recovery.err_short", min=MIN_PASSWORD_LEN))
            continue
        second = getpass.getpass("    " + ctx.t("recovery.ask_password_again") + " ").strip()
        if first != second:
            ctx.console.warn(ctx.t("recovery.err_mismatch"))
            continue
        return first
    return None


@register_mode(Mode.RESET_OWNER_PASSWORD)
def run_reset_owner_password(ctx: StageContext) -> ExitCode:
    """The mode handler (see module docstring)."""
    assert ctx.wizard is not None
    console = ctx.console
    console.info(ctx.t("recovery.intro"))
    words = load_words(ctx.paths.program_dir)
    question = Question(
        key="recovery_key",
        kind=Kind.TEXT,
        prompt_key="recovery.ask_key",
        help_key="recovery.help_key",
        allow_back=False,
        persist=False,
        sensitive=True,
        validate=lambda text: key_problem(text, words, ctx),
    )
    phrase = " ".join(ctx.wizard.ask(question).lower().split())
    log.info("RESET_OWNER_PASSWORD key_received words=%d", WORDS)

    password = ask_new_password(ctx)
    if password is None:
        console.fail(ctx.t("recovery.gave_up"))
        return ExitCode.CANCELLED

    console.info(ctx.t("recovery.elevation_why"))
    elevation = elevate.run_elevated([sys.executable, "-c", "pass"], password_prompt=ctx.t("recovery.sudo_prompt"))
    if not elevation.ok:
        log.warning("RESET_OWNER_PASSWORD_REFUSED reason=%s code=E-SYS-004", elevation.outcome.value)
        console.fail(ctx.t("recovery.elevation_failed") + "  (E-SYS-004)")
        return ExitCode.FAIL
    log.info("RESET_OWNER_PASSWORD elevation=ok")

    python = handover.venv_python_from(ctx)
    available, reason = handover.app_available(python) if python else (False, "venv not installed")
    if not python or not available:
        log.error("RESET_OWNER_PASSWORD_REFUSED reason=app-unavailable detail=%s code=E-SYS-002", reason)
        console.fail(ctx.t("recovery.app_missing") + "  (E-SYS-002)")
        console.hint(ctx.t("recovery.app_missing_hint"))
        return ExitCode.FAIL

    shown: list[str] = []

    def on_line(line: str) -> None:
        if line.startswith(handover.SECRET_PREFIX):
            shown.append(line[len(handover.SECRET_PREFIX):])

    console.busy(ctx.t("recovery.working"))
    result = handover.run_app(python, ["reset-owner-password"], secrets={"recovery_key": phrase, "new_password": password}, on_line=on_line, timeout=120)
    if result.ok:
        console.ok(ctx.t("recovery.done"))
        if shown:
            console.line()
            console.info(ctx.t("recovery.new_key_intro"))
            for text in shown:
                console.line("    " + text)
            console.line()
        return ExitCode.OK
    code = next(iter(_CODE_RE.findall(result.output)), "E-SYS-099")
    log.error("RESET_OWNER_PASSWORD_FAILED exit=%s code=%s", result.exit_code, code)
    console.fail(ctx.t("recovery.failed") + f"  ({code})")
    return ExitCode.FAIL
