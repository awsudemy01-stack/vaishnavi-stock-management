"""Interactive question engine for the installer (stdlib only).

Every decision that needs a human is a ``Question``: numbered options with a recommended default in
brackets, a plain-language note per option, ``b`` (back) / ``s`` (skip, where allowed) / ``q`` (quit,
resumable). Secrets use ``getpass`` and are never echoed or persisted. Non-secret answers are written
to ``config/install-answers.toml`` after *every* answer so an interrupted install loses nothing and a
replacement PC can replay them with ``--answers``; ``--yes`` accepts the recommended answer everywhere.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.stages import ControlFlow, StageContext, StageResult

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.wizard")

ANSWERS_FILE = "install-answers.toml"
BACK, SKIP, QUIT = "b", "s", "q"
SKIPPED = "__skipped__"
"""Answer value recorded when the owner skipped a skippable question."""


class WizardBack(ControlFlow):
    """Owner typed ``b`` — the caller should re-ask the previous question."""


class WizardQuit(ControlFlow):
    """Owner typed ``q`` — stop the installer (exit 3, resumable)."""


class NonInteractiveError(ControlFlow):
    """No terminal to ask on and no ``--yes`` / answer available (``E-SYS-009``)."""

    def __init__(self, key: str) -> None:
        super().__init__(f"question {key!r} needs a terminal or --yes/--answers")
        self.key = key


class NoDefaultError(ControlFlow):
    """``--yes`` was given but the question has no default to accept (``E-SYS-008``)."""

    def __init__(self, key: str) -> None:
        super().__init__(f"question {key!r} has no default for --yes")
        self.key = key


class Kind(str, Enum):
    """How a question is answered."""

    CHOICE = "choice"
    TEXT = "text"
    PATH = "path"
    SECRET = "secret"
    YESNO = "yesno"


Validator = Callable[[str], str | None]
"""Returns a plain-language problem description, or None when the value is acceptable."""


@dataclass(frozen=True)
class Option:
    """One numbered choice."""

    value: str
    label_key: str
    note_key: str = ""


@dataclass(frozen=True)
class Question:
    """A single prompt. ``*_key`` fields are message keys resolved through ``Messages``."""

    key: str
    kind: Kind
    prompt_key: str
    help_key: str = ""
    options: tuple[Option, ...] = ()
    default: str | None = None
    recommended: str | None = None
    skippable: bool = False
    allow_back: bool = True
    validate: Validator | None = None
    persist: bool = True
    sensitive: bool = False
    """True → the answer is never logged or echoed (recovery key); SECRET questions are always sensitive."""

    def __post_init__(self) -> None:
        if self.kind is Kind.CHOICE and not self.options:
            raise ValueError(f"choice question {self.key!r} needs options")
        if self.kind is Kind.SECRET:
            object.__setattr__(self, "persist", False)
            object.__setattr__(self, "sensitive", True)
        if self.sensitive and self.persist:
            object.__setattr__(self, "persist", False)
        if self.kind is Kind.YESNO and self.default not in (None, "yes", "no"):
            raise ValueError(f"yes/no question {self.key!r} default must be yes/no")


# ----------------------------------------------------------------------------- answers file
def _toml_scalar(value: str | bool | int) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


class AnswerStore:
    """Non-secret answers in ``config/install-answers.toml`` (``[answers]`` table)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.answers: dict[str, str] = {}

    @classmethod
    def load(cls, path: Path) -> AnswerStore:
        """Read an answers file; a missing or damaged file yields an empty store."""
        store = cls(path)
        if not path.exists():
            return store
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("answers: could not read %s (%s) — ignoring it", path, exc)
            return store
        table = data.get("answers", {})
        if isinstance(table, dict):
            store.answers = {str(k): str(v) for k, v in table.items()}
        log.debug("answers: loaded %d answers from %s", len(store.answers), path)
        return store

    def set(self, key: str, value: str) -> None:
        """Record and persist one answer (atomic write)."""
        self.answers[key] = value
        self.save()

    def save(self) -> None:
        """Write the file atomically; failures are logged, never fatal."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Non-secret answers of the Vaishnavi Store Manager installer.",
                "# Replay on another PC with:  python main.py --answers config/install-answers.toml",
                "# Keys/passwords are NEVER stored here - they are asked again and kept encrypted.",
                "",
                "[answers]",
            ]
            lines += [f"{k} = {_toml_scalar(v)}" for k, v in sorted(self.answers.items())]
            tmp = self.path.with_suffix(".toml.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("answers: could not save %s: %s", self.path, exc)


# ----------------------------------------------------------------------------- wizard
@dataclass
class Wizard:
    """Asks questions on the console; see module docstring for the rules."""

    console: Console
    messages: Messages
    answers: AnswerStore
    assume_yes: bool = False
    prefilled: Mapping[str, str] = field(default_factory=dict)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    getpass_fn: Callable[[str], str] = field(default_factory=lambda: getpass.getpass)
    _in_sequence: bool = field(default=False, init=False, repr=False)

    # -- public ---------------------------------------------------------------------------
    def ask(self, q: Question, *, force: bool = False) -> str:
        """Return the answer (option value / text / 'yes'|'no' / ``SKIPPED``).

        ``force=True`` (used after *back*) ignores saved answers and ``--yes`` so the question is
        really asked again; a command-line pre-fill still wins.
        """
        source, answer = self._resolve_without_asking(q, force=force)
        if answer is not None:
            log.info("WIZARD_ANSWER key=%s source=%s value=%s", q.key, source, "***" if q.sensitive else answer)
            self._persist(q, answer)
            if not q.sensitive:
                self.console.info(f"{self.messages.t(q.prompt_key)} → {self._display(q, answer)}  ({self.messages.t(f'wizard.source.{source}')})")
            return answer

        if not self._interactive():
            log.error("WIZARD_NONINTERACTIVE key=%s code=E-SYS-009", q.key)
            raise NonInteractiveError(q.key)

        self._print_question(q)
        while True:
            raw = self._read(q)
            if raw == QUIT:
                log.warning("WIZARD_QUIT key=%s", q.key)
                raise WizardQuit
            if raw == BACK and q.allow_back:
                if not self._in_sequence:
                    # Stages ask one question at a time; there is no earlier question to return to here.
                    log.info("WIZARD_BACK_NOWHERE key=%s", q.key)
                    self.console.hint(self.messages.t("wizard.no_back"))
                    continue
                log.info("WIZARD_BACK key=%s", q.key)
                raise WizardBack
            if raw == SKIP and q.skippable:
                log.info("WIZARD_ANSWER key=%s source=typed value=%s", q.key, SKIPPED)
                self._persist(q, SKIPPED)
                return SKIPPED
            answer, problem = self._interpret(q, raw)
            if problem:
                self.console.warn(problem)
                continue
            assert answer is not None
            log.info("WIZARD_ANSWER key=%s source=typed value=%s", q.key, "***" if q.sensitive else answer)
            self._persist(q, answer)
            return answer

    def ask_all(self, questions: list[Question]) -> dict[str, str]:
        """Ask in order with ``b`` moving back one question; returns key → answer."""
        answers: dict[str, str] = {}
        index = 0
        force = False
        self._in_sequence = True
        try:
            while index < len(questions):
                q = questions[index]
                try:
                    answers[q.key] = self.ask(q, force=force)
                    index += 1
                    force = False
                except WizardBack:
                    index = max(0, index - 1)
                    force = True
                    self.console.line()
        finally:
            self._in_sequence = False
        return answers

    # -- resolution without prompting ---------------------------------------------------------
    def _resolve_without_asking(self, q: Question, *, force: bool = False) -> tuple[str, str | None]:
        """Answer from a flag / the answers file / ``--yes`` — always the *normalised* value
        (``"y"`` → ``"yes"``, ``"2"`` → the option value, ``~/data`` → expanded path)."""
        if q.key in self.prefilled:
            value = self.prefilled[q.key]
            if value == SKIPPED and q.skippable:
                return "flag", value
            normalised, problem = self._interpret(q, value)
            if problem is None and normalised is not None:
                return "flag", normalised
            log.warning("wizard: ignoring invalid pre-filled value for %s", q.key)
        if force:
            return "", None
        if q.kind is not Kind.SECRET and q.key in self.answers.answers:
            value = self.answers.answers[q.key]
            if value == SKIPPED and q.skippable:
                return "answers_file", value
            normalised, problem = self._interpret(q, value)
            if problem is None and normalised is not None:
                return "answers_file", normalised
            log.warning("wizard: ignoring invalid answer from file for %s", q.key)
        if self.assume_yes and q.kind is not Kind.SECRET:
            default = q.default if q.default is not None else q.recommended
            if default is None:
                if q.skippable:
                    return "yes_flag", SKIPPED
                log.error("WIZARD_NO_DEFAULT key=%s code=E-SYS-008", q.key)
                raise NoDefaultError(q.key)
            value, problem = self._interpret(q, default)
            if problem is None and value is not None:
                return "yes_flag", value
            log.warning("wizard: --yes default for %s rejected (%s); asking instead", q.key, problem)
        return "", None

    # -- prompting ------------------------------------------------------------------------
    def _print_question(self, q: Question) -> None:
        m = self.messages
        self.console.line()
        self.console.ask(m.t(q.prompt_key))
        if q.help_key:
            self.console.hint(m.t(q.help_key))
        if q.kind is Kind.CHOICE:
            for i, opt in enumerate(q.options, start=1):
                label = m.t(opt.label_key)
                note = f"  · {m.t(opt.note_key)}" if opt.note_key else ""
                rec = f"  ({m.t('wizard.recommended')})" if opt.value == q.recommended else ""
                self.console.hint(f"[{i}] {label}{note}{rec}")
        keys = [m.t("wizard.key_quit")]
        if q.skippable:
            keys.insert(0, m.t("wizard.key_skip"))
        if q.allow_back:
            keys.insert(0, m.t("wizard.key_back"))
        self.console.hint("(" + ", ".join(keys) + ")")

    def _prompt_text(self, q: Question) -> str:
        m = self.messages
        if q.kind is Kind.CHOICE:
            default = self._option_index(q, q.default or q.recommended)
            return m.t("wizard.prompt_choice", default=f" [{default}]" if default else "")
        if q.kind is Kind.YESNO:
            return m.t("wizard.prompt_yesno", default=f" [{m.t('wizard.' + q.default)}]" if q.default else "")
        if q.kind is Kind.SECRET:
            return m.t("wizard.prompt_secret")
        return m.t("wizard.prompt_text", default=f" [{q.default}]" if q.default else "")

    def _read(self, q: Question) -> str:
        prompt = "    " + self._prompt_text(q) + " "
        if q.kind is Kind.SECRET:
            try:
                return self.getpass_fn(prompt).strip()
            except EOFError:
                return QUIT
        self.console.stream.write(prompt)
        self.console.stream.flush()
        line = self.stdin.readline()
        if line == "":  # EOF
            return QUIT
        return line.strip()

    def _interpret(self, q: Question, raw: str) -> tuple[str | None, str | None]:
        """Turn typed input into an answer value, or a plain-language problem."""
        m = self.messages
        if q.kind is Kind.CHOICE:
            if raw == "":
                raw_value = q.default or q.recommended
                if raw_value is None:
                    return None, m.t("wizard.err_choose", count=len(q.options))
                return raw_value, None
            if raw.isdigit() and 1 <= int(raw) <= len(q.options):
                return q.options[int(raw) - 1].value, None
            if raw in {o.value for o in q.options}:
                return raw, None
            return None, m.t("wizard.err_choose", count=len(q.options))
        if q.kind is Kind.YESNO:
            if raw == "" and q.default:
                return q.default, None
            low = raw.lower()
            if low in ("y", "yes", "haan", "ha", "हाँ", "हां", "1"):
                return "yes", None
            if low in ("n", "no", "nahi", "nahin", "नहीं", "0"):
                return "no", None
            return None, m.t("wizard.err_yesno")
        # TEXT / PATH / SECRET
        value = raw if raw != "" else (q.default or "")
        if value == "":
            return None, m.t("wizard.err_empty")
        if q.kind is Kind.PATH:
            value = str(Path(value).expanduser())
        if q.validate is not None:
            problem = q.validate(value)
            if problem:
                return None, problem
        return value, None

    # -- helpers --------------------------------------------------------------------------
    def _interactive(self) -> bool:
        try:
            return bool(self.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def _persist(self, q: Question, answer: str) -> None:
        if q.persist and q.kind is not Kind.SECRET:
            self.answers.set(q.key, answer)

    def _display(self, q: Question, answer: str) -> str:
        if answer == SKIPPED:
            return self.messages.t("wizard.skipped")
        if q.kind is Kind.CHOICE:
            for opt in q.options:
                if opt.value == answer:
                    return self.messages.t(opt.label_key)
        if q.kind is Kind.YESNO:
            return self.messages.t(f"wizard.{answer}")
        return answer

    @staticmethod
    def _option_index(q: Question, value: str | None) -> int | None:
        for i, opt in enumerate(q.options, start=1):
            if opt.value == value:
                return i
        return None

# ----------------------------------------------------------------------------- language stage
LANGUAGE_QUESTION = Question(
    key="language",
    kind=Kind.CHOICE,
    prompt_key="q.language.prompt",
    options=(Option("en", "q.language.en"), Option("hi", "q.language.hi")),
    default="en",
    recommended="en",
    allow_back=False,
)


@dataclass
class LanguageStage:
    """Stage 1: ask the owner's language (skipped when ``--lang`` was given explicitly)."""

    key: str = "language"
    title_key: str = "stage.language"

    def applies(self, ctx: StageContext) -> bool:
        """Always listed, so the owner sees which language is in use."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached — the answers file already remembers the choice."""
        return None

    def run(self, ctx: StageContext) -> StageResult:
        """Resolve the language, reload messages, record it for later stages."""
        if ctx.options.lang_explicit:
            lang = ctx.options.lang
            source = "flag"
        else:
            assert ctx.wizard is not None, "wizard must be attached to the context"
            lang = ctx.wizard.ask(LANGUAGE_QUESTION)
            source = "wizard"
        if lang != ctx.messages.lang:
            ctx.messages = Messages.load(lang)
            if ctx.wizard is not None:
                ctx.wizard.messages = ctx.messages
        log.info("LANGUAGE_SELECTED lang=%s source=%s", lang, source)
        return StageResult.ok(ctx.t(f"q.language.{lang}"), lang=lang)


def register(options: Options) -> LanguageStage:
    """Factory used by ``build_default_pipeline``."""
    return LanguageStage()
