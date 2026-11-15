"""Interactive wizard: input interpretation, back/skip/quit, --yes, answers file, secrets, language stage."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore
from bootstrap.wizard import (
    LANGUAGE_QUESTION,
    SKIPPED,
    AnswerStore,
    Kind,
    LanguageStage,
    NoDefaultError,
    NonInteractiveError,
    Option,
    Question,
    Wizard,
    WizardBack,
    WizardQuit,
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


CHOICE = Question(
    key="ai_provider", kind=Kind.CHOICE, prompt_key="q.language.prompt",
    options=(Option("ollama", "q.language.en"), Option("openai", "q.language.hi"), Option("none", "wizard.no")),
    recommended="ollama",
)
TEXT = Question(key="shop", kind=Kind.TEXT, prompt_key="q.language.prompt", default="Vaishnavi")
PATH_Q = Question(key="primary", kind=Kind.PATH, prompt_key="q.language.prompt",
                  validate=lambda v: None if "ok" in v else "must contain ok")
SECRET = Question(key="api_key", kind=Kind.SECRET, prompt_key="q.language.prompt")
YESNO = Question(key="autostart", kind=Kind.YESNO, prompt_key="q.language.prompt", default="yes", skippable=True)


class WizardTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.answers_path = Path(self._tmp.name) / "config" / "install-answers.toml"
        self.out = io.StringIO()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def wizard(self, typed: str = "", *, yes: bool = False, prefilled: dict[str, str] | None = None,
               store: AnswerStore | None = None, tty: bool = True, secret: str = "sk-test") -> Wizard:
        stdin = _Tty(typed) if tty else io.StringIO(typed)
        return Wizard(Console(self.out, unicode=False), Messages.load("en"), store or AnswerStore(self.answers_path),
                      assume_yes=yes, prefilled=prefilled or {}, stdin=stdin, getpass_fn=lambda prompt: secret)


class InterpretationTest(WizardTestBase):
    def test_choice_by_number_value_or_enter_for_recommended(self) -> None:
        self.assertEqual(self.wizard("2\n").ask(CHOICE), "openai")
        self.assertEqual(self.wizard("none\n").ask(CHOICE), "none")
        self.assertEqual(self.wizard("\n").ask(CHOICE), "ollama")

    def test_invalid_choice_reasks_with_plain_message(self) -> None:
        self.assertEqual(self.wizard("9\nabc\n1\n").ask(CHOICE), "ollama")
        self.assertEqual(self.out.getvalue().count("Please type a number from 1 to 3"), 2)

    def test_text_default_and_empty_rejection(self) -> None:
        self.assertEqual(self.wizard("\n").ask(TEXT), "Vaishnavi")
        q = Question(key="x", kind=Kind.TEXT, prompt_key="q.language.prompt")
        self.assertEqual(self.wizard("\nhello\n").ask(q), "hello")
        self.assertIn("cannot be empty", self.out.getvalue())

    def test_path_validation_and_expansion(self) -> None:
        w = self.wizard("bad\n~/ok-folder\n")
        answer = w.ask(PATH_Q)
        self.assertIn("ok-folder", answer)
        self.assertFalse(answer.startswith("~"))
        self.assertIn("must contain ok", self.out.getvalue())

    def test_yesno_accepts_hindi_and_english_forms(self) -> None:
        for typed, expected in (("y\n", "yes"), ("haan\n", "yes"), ("हाँ\n", "yes"), ("n\n", "no"), ("nahi\n", "no"), ("\n", "yes")):
            with self.subTest(typed=typed):
                self.assertEqual(self.wizard(typed).ask(YESNO), expected)
        self.assertEqual(self.wizard("maybe\nno\n").ask(YESNO), "no")

    def test_prompt_shows_options_recommended_and_keys(self) -> None:
        self.wizard("\n").ask(CHOICE)
        text = self.out.getvalue()
        self.assertIn("[1] English", text)
        self.assertIn("(recommended)", text)
        self.assertIn("b = back", text)
        self.assertIn("q = quit", text)
        self.assertNotIn("s = skip", text)


class ControlKeysTest(WizardTestBase):
    def test_back_skip_quit(self) -> None:
        # 'b' on a stand-alone question (how stages ask) has nowhere to go: hint + re-ask, never cancel.
        self.assertEqual(self.wizard("b\n1\n").ask(CHOICE), "ollama")
        self.assertIn("no earlier question", self.out.getvalue())
        with self.assertRaises(WizardQuit):
            self.wizard("q\n").ask(CHOICE)
        self.assertEqual(self.wizard("s\n").ask(YESNO), SKIPPED)
        self.assertEqual(self.wizard("s\n1\n").ask(CHOICE), "ollama", "s on a non-skippable question is just invalid input")

    def test_back_inside_a_sequence_raises_for_ask_all(self) -> None:
        w = self.wizard("b\n")
        w._in_sequence = True
        with self.assertRaises(WizardBack):
            w.ask(CHOICE)

    def test_eof_is_quit(self) -> None:
        with self.assertRaises(WizardQuit):
            self.wizard("").ask(CHOICE)

    def test_back_is_ignored_on_first_question(self) -> None:
        self.assertEqual(self.wizard("b\n2\n").ask(LANGUAGE_QUESTION), "hi")

    def test_ask_all_moves_back_one_question(self) -> None:
        w = self.wizard("1\nb\n2\nyes\n")
        answers = w.ask_all([CHOICE, YESNO])
        self.assertEqual(answers, {"ai_provider": "openai", "autostart": "yes"})


class AnswersAndFlagsTest(WizardTestBase):
    def test_answers_are_persisted_after_each_answer_and_replayed(self) -> None:
        w = self.wizard("2\nyes\n")
        w.ask(CHOICE)
        self.assertTrue(self.answers_path.exists(), "written right after the first answer")
        w.ask(YESNO)
        text = self.answers_path.read_text(encoding="utf-8")
        self.assertIn('ai_provider = "openai"', text)
        self.assertIn('autostart = "yes"', text)
        self.assertIn("NEVER stored", text)

        replay = self.wizard("", store=AnswerStore.load(self.answers_path), tty=False)
        self.assertEqual(replay.ask(CHOICE), "openai")
        self.assertEqual(replay.ask(YESNO), "yes")
        self.assertIn("from your saved answers", self.out.getvalue())

    def test_invalid_saved_answer_is_ignored_and_asked_again(self) -> None:
        store = AnswerStore(self.answers_path)
        store.set("ai_provider", "gemini")
        self.assertEqual(self.wizard("3\n", store=store).ask(CHOICE), "none")

    def test_saved_and_prefilled_answers_are_normalised(self) -> None:
        store = AnswerStore(self.answers_path)
        store.set("ai_provider", "2")  # hand-edited file with the option number
        store.set("autostart", "y")
        w = self.wizard("", store=store, tty=False)
        self.assertEqual(w.ask(CHOICE), "openai")
        self.assertEqual(w.ask(YESNO), "yes")
        self.assertEqual(self.wizard("", prefilled={"autostart": "haan"}, tty=False).ask(YESNO), "yes")

    def test_secret_is_never_echoed_or_persisted(self) -> None:
        w = self.wizard(secret="sk-verysecret")
        self.assertEqual(w.ask(SECRET), "sk-verysecret")
        self.assertNotIn("verysecret", self.out.getvalue())
        self.assertFalse(self.answers_path.exists())
        self.assertFalse(SECRET.persist)

    def test_prefilled_flag_wins_and_is_validated(self) -> None:
        self.assertEqual(self.wizard("", prefilled={"ai_provider": "none"}, tty=False).ask(CHOICE), "none")
        self.assertEqual(self.wizard("2\n", prefilled={"ai_provider": "bogus"}).ask(CHOICE), "openai")

    def test_yes_flag_accepts_recommended_or_skips_or_fails(self) -> None:
        self.assertEqual(self.wizard("", yes=True, tty=False).ask(CHOICE), "ollama")
        self.assertEqual(self.wizard("", yes=True, tty=False).ask(TEXT), "Vaishnavi")
        no_default_skippable = Question(key="opt", kind=Kind.TEXT, prompt_key="q.language.prompt", skippable=True)
        self.assertEqual(self.wizard("", yes=True, tty=False).ask(no_default_skippable), SKIPPED)
        no_default = Question(key="must", kind=Kind.TEXT, prompt_key="q.language.prompt")
        with self.assertRaises(NoDefaultError):
            self.wizard("", yes=True, tty=False).ask(no_default)

    def test_yes_flag_still_asks_for_secrets(self) -> None:
        self.assertEqual(self.wizard("", yes=True, secret="sk-x").ask(SECRET), "sk-x")

    def test_non_interactive_without_answer_fails_clearly(self) -> None:
        with self.assertRaises(NonInteractiveError) as cm:
            self.wizard("1\n", tty=False).ask(CHOICE)
        self.assertEqual(cm.exception.key, "ai_provider")

    def test_corrupt_answers_file_is_ignored(self) -> None:
        self.answers_path.parent.mkdir(parents=True)
        self.answers_path.write_text("[answers\nbroken", encoding="utf-8")
        self.assertEqual(AnswerStore.load(self.answers_path).answers, {})

    def test_toml_escaping_roundtrip(self) -> None:
        store = AnswerStore(self.answers_path)
        store.set("primary", 'C:\\Users\\Shop\\Vaishnavi "data"')
        self.assertEqual(AnswerStore.load(self.answers_path).answers["primary"], 'C:\\Users\\Shop\\Vaishnavi "data"')


class LanguageStageTest(WizardTestBase):
    def _ctx(self, wizard: Wizard, **opts: object) -> StageContext:
        paths = InstallPaths.from_program_dir(Path(self._tmp.name))
        paths.install_log_dir.mkdir(parents=True, exist_ok=True)
        options = Options(mode=Mode.INSTALL_RUN, **opts)  # type: ignore[arg-type]
        return StageContext(options, paths, wizard.messages, wizard.console, StateStore(paths.state_file), "t", wizard=wizard)

    def test_asks_and_switches_messages_to_hindi(self) -> None:
        w = self.wizard("2\n")
        ctx = self._ctx(w)
        result = LanguageStage().run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(result.data["lang"], "hi")
        self.assertEqual(ctx.messages.lang, "hi")
        self.assertIs(w.messages, ctx.messages)
        self.assertIn('language = "hi"', self.answers_path.read_text(encoding="utf-8"))

    def test_explicit_lang_flag_skips_the_question(self) -> None:
        w = self.wizard("", tty=False)
        ctx = self._ctx(w, lang="hi", lang_explicit=True)
        result = LanguageStage().run(ctx)
        self.assertEqual(result.data["lang"], "hi")
        self.assertFalse(self.answers_path.exists())


if __name__ == "__main__":
    unittest.main()
