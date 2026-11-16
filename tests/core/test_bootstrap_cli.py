"""Flag parsing, validation and mode dispatch of the installer CLI (stdlib only)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bootstrap import cli
from bootstrap.cli import Mode, Options, parse_args
from bootstrap.stages import ExitCode, StageContext


def _parse_error(argv: list[str]) -> str:
    """Return argparse's error text for argv (argparse exits with code 2)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), unittest.TestCase().assertRaises(SystemExit) as cm:
        parse_args(argv)
    assert cm.exception.code == 2
    return err.getvalue()


class ParseArgsTest(unittest.TestCase):
    def test_defaults_are_install_and_run(self) -> None:
        opts = parse_args([])
        self.assertEqual(opts, Options(mode=Mode.INSTALL_RUN, lang=os.environ.get(cli.LANG_ENV, "en")))

    def test_each_action_flag_selects_its_mode(self) -> None:
        cases = {
            "--install-only": Mode.INSTALL_ONLY,
            "--doctor": Mode.DOCTOR,
            "--logs": Mode.LOGS,
            "--where": Mode.WHERE,
            "--restore": Mode.RESTORE,
            "--reset-owner-password": Mode.RESET_OWNER_PASSWORD,
            "--selftest": Mode.SELFTEST,
            "--uninstall-autostart": Mode.UNINSTALL_AUTOSTART,
            "--stop": Mode.STOP,
        }
        for flag, mode in cases.items():
            with self.subTest(flag=flag):
                self.assertIs(parse_args([flag]).mode, mode)
        self.assertIs(parse_args(["--make-offline-bundle", "linux"]).mode, Mode.MAKE_OFFLINE_BUNDLE)

    def test_install_only_sets_the_flag(self) -> None:
        self.assertTrue(parse_args(["--install-only"]).install_only)
        self.assertFalse(parse_args([]).install_only)

    def test_action_flags_are_mutually_exclusive(self) -> None:
        self.assertIn("not allowed with", _parse_error(["--doctor", "--logs"]))

    def test_fix_requires_doctor(self) -> None:
        self.assertIn("--fix only makes sense", _parse_error(["--fix"]))
        self.assertTrue(parse_args(["--doctor", "--fix"]).fix)

    def test_no_ai_maps_to_provider_none_and_conflicts_with_other_provider(self) -> None:
        self.assertEqual(parse_args(["--no-ai"]).ai_provider, "none")
        self.assertEqual(parse_args(["--no-ai", "--ai-provider", "none"]).ai_provider, "none")
        self.assertIn("conflicts", _parse_error(["--no-ai", "--ai-provider", "openai"]))

    def test_ai_provider_choices(self) -> None:
        self.assertEqual(parse_args(["--ai-provider", "huggingface"]).ai_provider, "huggingface")
        self.assertIn("invalid choice", _parse_error(["--ai-provider", "gemini"]))

    def test_lang_choices_and_env_default(self) -> None:
        self.assertEqual(parse_args(["--lang", "hi"]).lang, "hi")
        self.assertIn("invalid choice", _parse_error(["--lang", "fr"]))
        old = os.environ.get(cli.LANG_ENV)
        os.environ[cli.LANG_ENV] = "hi"
        try:
            self.assertEqual(parse_args([]).lang, "hi")
        finally:
            if old is None:
                del os.environ[cli.LANG_ENV]
            else:
                os.environ[cli.LANG_ENV] = old

    def test_path_flags_must_exist(self) -> None:
        self.assertIn("folder not found", _parse_error(["--offline", "/definitely/not/here"]))
        self.assertIn("file not found", _parse_error(["--answers", "/definitely/not/here.toml"]))
        with tempfile.TemporaryDirectory() as tmp:
            answers = Path(tmp) / "a.toml"
            answers.write_text("x = 1", encoding="utf-8")
            opts = parse_args(["--offline", tmp, "--answers", str(answers), "--yes", "--no-browser", "--debug"])
            self.assertEqual(opts.offline_dir, Path(tmp))
            self.assertEqual(opts.answers_path, answers)
            self.assertTrue(opts.assume_yes and opts.no_browser and opts.debug)

    def test_help_mentions_every_documented_flag(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parse_args(["--help"])
        text = out.getvalue()
        for flag in ("--install-only", "--doctor", "--fix", "--lang", "--no-browser", "--no-ai", "--ai-provider",
                     "--offline", "--update", "--restore", "--reset-owner-password", "--selftest", "--logs",
                     "--uninstall-autostart", "--where", "--answers", "--yes", "--make-offline-bundle"):
            self.assertIn(flag, text)


class MainTest(unittest.TestCase):
    """End-to-end through cli.main with a temporary program folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.program_dir = Path(self._tmp.name)
        self.out = io.StringIO()

    def tearDown(self) -> None:
        import logging

        logging.shutdown()
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.NullHandler())
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> int:
        return cli.main(argv, program_dir=self.program_dir, stdout=self.out, stdin=io.StringIO(""))

    def test_install_only_runs_pipeline_exits_ok_and_writes_log_and_state(self) -> None:
        from unittest import mock

        from bootstrap import autostart as au, locations as loc, python_env as pe

        def fake_pip(cmd: list[str], *, on_line, timeout=0, env=None) -> int:  # type: ignore[no-untyped-def]
            on_line("Successfully installed everything-1.0")
            return 0

        class FakeBackend(au.Backend):
            def is_registered(self) -> bool:
                return False

            def register(self) -> tuple[bool, str]:
                return True, "fake"

        fake_home = self.program_dir / "fake-home"
        # Everything that could touch the real machine is faked: pip, home folder, Task Scheduler,
        # system-component detection + installation (a Linux CI box must never run apt-get here).
        from bootstrap import pkg_manager as pm
        from bootstrap import app_stages
        from bootstrap.handover import AppResult

        def no_install(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("the e2e test must not install system components")

        def fake_run_app(python, args, *, secrets=None, on_line=None, timeout=0, cwd=None):  # type: ignore[no-untyped-def]
            if args[0] == "init-db":
                return AppResult(0, json.dumps({"created": True, "seeded_rows": 40, "secrets_stored": [], "owner_setup_pending": True, "migrated": False}), 5)
            return AppResult(1, " ! Backup drive not set up yet (E-BKP-001)", 5)  # doctor: WARN

        with mock.patch.object(pe, "run_streaming", fake_pip), mock.patch.object(pe, "verify_imports", return_value=[]), \
             mock.patch.object(loc.Path, "home", classmethod(lambda cls: fake_home)), \
             mock.patch.object(loc, "suggest_secondary", lambda home, s: home / "VaishnaviStore-Backups"), \
             mock.patch.object(au, "Backend", FakeBackend), \
             mock.patch.object(pm, "components_for", lambda family: []), \
             mock.patch.object(pm, "_run_plain", no_install), \
             mock.patch.object(pm.elevate, "run_elevated", no_install, create=True), \
             mock.patch.object(app_stages.handover, "run_app", fake_run_app), \
             mock.patch.object(app_stages.handover, "app_available", lambda p: (True, "0.1.0")):
            code = self._run(["--install-only", "--yes", "--ai-provider", "none"])
        # WARN, not OK: the temp program folder has no requirements.lock (E-SYS-041 by design).
        self.assertEqual(code, ExitCode.WARN)
        logs = list((self.program_dir / "logs" / "install").glob("*.log"))
        self.assertEqual(len(logs), 1)
        log_text = logs[0].read_text(encoding="utf-8")
        for marker in ("RUN_START", "PIPELINE_START", "STAGE_OK key=log_console", "STAGE_OK key=language", "STAGE_OK key=survey",
                       "key=python_check", "STAGE_OK key=venv", "key=packages", "key=system_components", "STAGE_OK key=ai_provider",
                       "key=locations", "STAGE_OK key=autostart", "STAGE_OK key=database", "STAGE_WARN key=doctor", "MINILOG_STOPPED", "RUN_END"):
            self.assertIn(marker, log_text)
        self.assertNotIn("key=start", log_text, "--install-only never starts the app")
        self.assertTrue((self.program_dir / "logs" / "install" / "state.json").exists())
        self.assertTrue((self.program_dir / ".venv").is_dir())
        self.assertTrue((self.program_dir / "fake-home" / "VaishnaviStore").is_dir())
        self.assertTrue((self.program_dir / "config" / "locations.toml").exists())
        self.assertIn("E-SYS-041", log_text)
        self.assertIn("Setup steps complete, with", self.out.getvalue())
        self.assertNotIn("Ready to open the shop", self.out.getvalue(), "no readiness claim until the app-start stage exists")
        # Nothing may leak into the real home folder of the development PC.
        self.assertFalse((Path.home() / "VaishnaviStore").exists())
        self.assertFalse((Path.home() / "VaishnaviStore-Backups").exists())

    def test_question_without_terminal_or_yes_fails_with_e_sys_009(self) -> None:
        code = self._run(["--install-only"])
        self.assertEqual(code, ExitCode.FAIL)
        self.assertIn("E-SYS-009", self.out.getvalue())

    def test_hindi_messages(self) -> None:
        self._run(["--lang", "hi", "--install-only"])
        self.assertIn("सेटअप", self.out.getvalue())

    def test_every_mode_has_a_handler(self) -> None:
        cli._import_mode_modules()
        self.assertEqual(set(Mode), set(cli.registered_modes()), "no mode may fall through to E-SYS-050 any more")

    def test_unregistered_mode_reports_not_available(self) -> None:
        saved = dict(cli._MODE_HANDLERS)
        import_modes = cli._import_mode_modules
        cli._MODE_HANDLERS.pop(Mode.RESTORE, None)
        cli._import_mode_modules = lambda: None  # type: ignore[assignment]
        try:
            code = self._run(["--restore"])
        finally:
            cli._import_mode_modules = import_modes  # type: ignore[assignment]
            cli._MODE_HANDLERS.clear()
            cli._MODE_HANDLERS.update(saved)
        self.assertEqual(code, ExitCode.FAIL)
        self.assertIn("E-SYS-050", self.out.getvalue())

    def test_keyboard_interrupt_in_handler_exits_cancelled(self) -> None:
        def boom(ctx: StageContext) -> ExitCode:
            raise KeyboardInterrupt

        saved = dict(cli._MODE_HANDLERS)
        cli._MODE_HANDLERS[Mode.WHERE] = boom
        try:
            code = self._run(["--where"])
        finally:
            cli._MODE_HANDLERS.clear()
            cli._MODE_HANDLERS.update(saved)
        self.assertEqual(code, ExitCode.CANCELLED)
        self.assertIn("E-SYS-090", self.out.getvalue())

    def test_unexpected_exception_in_handler_exits_internal_without_traceback_on_console(self) -> None:
        def boom(ctx: StageContext) -> ExitCode:
            raise RuntimeError("bug")

        saved = dict(cli._MODE_HANDLERS)
        cli._MODE_HANDLERS[Mode.WHERE] = boom
        try:
            code = self._run(["--where"])
        finally:
            cli._MODE_HANDLERS.clear()
            cli._MODE_HANDLERS.update(saved)
        self.assertEqual(code, ExitCode.INTERNAL)
        console = self.out.getvalue()
        self.assertIn("E-SYS-099", console)
        self.assertNotIn("Traceback", console)
        log_text = next((self.program_dir / "logs" / "install").glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("Traceback", log_text)
        self.assertIn("RuntimeError: bug", log_text)

    def test_secret_logged_by_a_stage_is_redacted_in_log(self) -> None:
        saved = dict(cli._MODE_HANDLERS)

        def leak(ctx: StageContext) -> ExitCode:
            import logging

            logging.getLogger("test").info("api_key=sk-SECRETSECRET1234 password=hunter2")
            return ExitCode.OK

        cli._MODE_HANDLERS[Mode.WHERE] = leak
        try:
            self._run(["--where"])
        finally:
            cli._MODE_HANDLERS.clear()
            cli._MODE_HANDLERS.update(saved)
        log_text = "".join(p.read_text(encoding="utf-8") for p in (self.program_dir / "logs" / "install").glob("*.log"))
        self.assertNotIn("SECRETSECRET", log_text)
        self.assertNotIn("hunter2", log_text)
        self.assertIn("api_key=***", log_text)

    def test_secret_inside_a_traceback_is_redacted_too(self) -> None:
        saved = dict(cli._MODE_HANDLERS)

        def leak(ctx: StageContext) -> ExitCode:
            raise RuntimeError("provider rejected token sk-TRACEBACKSECRET9876")

        cli._MODE_HANDLERS[Mode.WHERE] = leak
        try:
            self._run(["--where"])
        finally:
            cli._MODE_HANDLERS.clear()
            cli._MODE_HANDLERS.update(saved)
        log_text = "".join(p.read_text(encoding="utf-8") for p in (self.program_dir / "logs" / "install").glob("*.log"))
        self.assertIn("Traceback", log_text)
        self.assertNotIn("TRACEBACKSECRET", log_text)

    def test_saved_answers_are_used_on_a_normal_restart(self) -> None:
        answers = self.program_dir / "config" / "install-answers.toml"
        answers.parent.mkdir(parents=True)
        answers.write_text('[answers]\nlanguage = "hi"\n', encoding="utf-8")
        # No --answers flag, no --yes, no keyboard: the language question must come from the saved file.
        self._run(["--install-only"])
        text = self.out.getvalue()
        self.assertIn("from your saved answers", text)
        self.assertIn("कंप्यूटर", text, "later stages speak Hindi, the saved answer")
        log_text = "".join(p.read_text(encoding="utf-8") for p in (self.program_dir / "logs" / "install").glob("*.log"))
        self.assertIn("WIZARD_ANSWER key=language source=answers_file value=hi", log_text)

    def test_init_failure_prints_plain_message_not_traceback(self) -> None:
        from unittest import mock

        with mock.patch.object(cli.Messages, "load", side_effect=OSError("messages folder unreadable")):
            err = io.StringIO()
            with mock.patch.object(cli.sys, "stderr", err):
                code = self._run(["--where"])
        self.assertEqual(code, ExitCode.INTERNAL)
        self.assertIn("E-SYS-099", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
