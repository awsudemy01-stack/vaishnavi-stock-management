"""Elevation helper and system-components stage (stdlib only; privileged commands are faked)."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bootstrap import elevate, pkg_manager as pm
from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.elevate import Elevation, Outcome
from bootstrap.i18n import Messages
from bootstrap.pkg_manager import Component, Manager, SystemComponentsStage, components_for, install_commands
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore
from bootstrap.wizard import AnswerStore, Wizard


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _ctx(tmp: Path, typed: str = "") -> tuple[StageContext, io.StringIO]:
    paths = InstallPaths.from_program_dir(tmp)
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    console, messages = Console(out, unicode=False), Messages.load("en")
    wizard = Wizard(console, messages, AnswerStore(tmp / "config" / "a.toml"), stdin=_Tty(typed))
    return StageContext(Options(mode=Mode.INSTALL_RUN), paths, messages, console, StateStore(paths.state_file), "t", wizard=wizard), out


class ElevateTest(unittest.TestCase):
    def test_shell_join_quotes_spaces(self) -> None:
        self.assertEqual(elevate.shell_join(["apt-get", "install", "a b"]), 'apt-get install "a b"')

    def test_mechanism_matches_platform(self) -> None:
        m = elevate.mechanism()
        if sys.platform.startswith("win"):
            self.assertEqual(m, "uac")
        else:
            self.assertIn(m, ("sudo", "pkexec", "none"))

    def test_already_admin_runs_directly(self) -> None:
        with mock.patch.object(elevate, "is_admin", return_value=True):
            res = elevate.run_elevated([sys.executable, "-c", "print('hi')"])
        self.assertTrue(res.ok)
        self.assertIn("hi", res.output)

    def test_sudo_declined_on_empty_password_and_eof(self) -> None:
        with mock.patch.object(elevate, "is_admin", return_value=False), mock.patch.object(elevate, "mechanism", return_value="sudo"):
            self.assertTrue(elevate.run_elevated(["true"], ask_password=lambda p: "", interactive=True).declined)

            def eof(prompt: str) -> str:
                raise EOFError

            self.assertTrue(elevate.run_elevated(["true"], ask_password=eof, interactive=True).declined)

    def test_sudo_wrong_password_retries_then_declines(self) -> None:
        calls: list[str] = []

        class Proc:
            returncode = 1
            stdout = ""
            stderr = "Sorry, try again."

        def fake_run(cmd: list[str], **kw: Any) -> Proc:
            calls.append(kw["input"])
            return Proc()

        with mock.patch.object(elevate, "is_admin", return_value=False), mock.patch.object(elevate, "mechanism", return_value="sudo"), mock.patch.object(elevate.subprocess, "run", fake_run):
            res = elevate.run_elevated(["true"], ask_password=lambda p: "pw", interactive=True)
        self.assertTrue(res.declined)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c == "pw\n" for c in calls), "password goes to stdin only")

    def test_sudo_success_and_failure(self) -> None:
        class Proc:
            def __init__(self, code: int) -> None:
                self.returncode, self.stdout, self.stderr = code, "done", ""

        with mock.patch.object(elevate, "is_admin", return_value=False), mock.patch.object(elevate, "mechanism", return_value="sudo"):
            with mock.patch.object(elevate.subprocess, "run", lambda *a, **k: Proc(0)):
                self.assertTrue(elevate.run_elevated(["x"], ask_password=lambda p: "pw", interactive=True).ok)
            with mock.patch.object(elevate.subprocess, "run", lambda *a, **k: Proc(2)):
                res = elevate.run_elevated(["x"], ask_password=lambda p: "pw", interactive=True)
                self.assertIs(res.outcome, Outcome.FAILED)
                self.assertEqual(res.exit_code, 2)

    def test_sudo_without_terminal_or_pkexec_is_unavailable(self) -> None:
        with mock.patch.object(elevate, "is_admin", return_value=False), mock.patch.object(elevate, "mechanism", return_value="sudo"), mock.patch.object(elevate.shutil, "which", return_value=None):
            res = elevate.run_elevated(["x"], interactive=False)
        self.assertIs(res.outcome, Outcome.UNAVAILABLE)

    def test_pkexec_dismissed_is_declined(self) -> None:
        class Proc:
            returncode, stdout, stderr = 126, "", ""

        with mock.patch.object(elevate, "is_admin", return_value=False), mock.patch.object(elevate, "mechanism", return_value="pkexec"), mock.patch.object(elevate.shutil, "which", return_value="/usr/bin/pkexec"), mock.patch.object(elevate.subprocess, "run", lambda *a, **k: Proc()):
            self.assertTrue(elevate.run_elevated(["x"], interactive=False).declined)


class CatalogueTest(unittest.TestCase):
    def test_components_per_os(self) -> None:
        self.assertEqual([c.key for c in components_for("linux")], ["libusb", "cups", "fonts"])
        self.assertEqual([c.key for c in components_for("macos")], ["libusb", "cups", "fonts"])
        self.assertEqual([c.key for c in components_for("windows")], ["fonts"])
        self.assertEqual(components_for("unknown"), [])
        self.assertFalse(any(c.packages for c in components_for("windows")), "nothing is installed on Windows")

    def test_install_commands(self) -> None:
        apt = Manager("apt", ("apt-get", "install", "-y"), True, ("apt-get", "update"))
        self.assertEqual(install_commands(apt, ["a", "b"]), [["apt-get", "update"], ["apt-get", "install", "-y", "a", "b"]])
        winget = Manager("winget", ("winget", "install", "--id"), True)
        self.assertEqual(install_commands(winget, ["X", "Y"]), [["winget", "install", "--id", "X"], ["winget", "install", "--id", "Y"]])

    def test_current_os_detectors_do_not_crash(self) -> None:
        family = "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux"
        for c in components_for(family):
            self.assertIsInstance(c.detect(), bool)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows font layout")
    def test_windows_ships_devanagari_font(self) -> None:
        self.assertTrue(pm.detect_fonts_windows(), "Nirmala UI (.ttf or .ttc) should be found on Windows 8.1+")


class StageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.present: dict[str, bool] = {"libusb": False, "cups": True, "fonts": False}
        comps = [
            Component("libusb", lambda: self.present["libusb"], ("libusb-1.0-0",), "syscomp.consequence.libusb"),
            Component("cups", lambda: self.present["cups"], ("cups",), "syscomp.consequence.cups"),
            Component("fonts", lambda: self.present["fonts"], ("fonts-noto-core",), "syscomp.consequence.fonts"),
        ]
        self._patches = [
            mock.patch.object(pm, "components_for", lambda fam: comps),
            mock.patch.object(pm, "manager_for", lambda fam: Manager("apt", ("apt-get", "install", "-y"), True, ("apt-get", "update"))),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _stage(self, elevated: list[Elevation]) -> tuple[SystemComponentsStage, list[list[str]]]:
        calls: list[list[str]] = []

        def fake_elevated(cmd: list[str], **kw: Any) -> Elevation:
            calls.append(list(cmd))
            res = elevated[min(len(calls) - 1, len(elevated) - 1)]
            if res.ok:  # simulate the install taking effect
                for k in self.present:
                    self.present[k] = True
            return res

        return SystemComponentsStage(os_family="linux", run_elevated=fake_elevated), calls

    def test_all_present_is_ok(self) -> None:
        self.present.update(libusb=True, fonts=True)
        ctx, _ = _ctx(self.tmp)
        stage, calls = self._stage([])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(calls, [])

    def test_owner_says_no_warns_with_consequences(self) -> None:
        ctx, out = _ctx(self.tmp, "no\n")
        stage, calls = self._stage([])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-004")
        self.assertIn("USB receipt printer", result.hint)
        self.assertIn("Hindi text", result.hint)
        self.assertEqual(calls, [])
        self.assertIn("apt-get install -y libusb-1.0-0 fonts-noto-core", out.getvalue())
        self.assertIn("administrator password", out.getvalue())

    def test_owner_says_yes_installs_with_one_elevation_per_command(self) -> None:
        ctx, _ = _ctx(self.tmp, "yes\n")
        stage, calls = self._stage([Elevation(Outcome.OK, 0)])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.OK, result.hint)
        self.assertEqual(calls, [["apt-get", "update"], ["apt-get", "install", "-y", "libusb-1.0-0", "fonts-noto-core"]])
        self.assertEqual(result.data["installed"], ["libusb", "fonts"])

    def test_uac_or_sudo_declined_is_warn(self) -> None:
        ctx, _ = _ctx(self.tmp, "yes\n")
        stage, _ = self._stage([Elevation(Outcome.DECLINED)])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-004")

    def test_elevation_unavailable_gives_manual_command(self) -> None:
        ctx, _ = _ctx(self.tmp, "yes\n")
        stage, _ = self._stage([Elevation(Outcome.UNAVAILABLE, detail="no sudo")])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertIn("apt-get update", result.hint)

    def test_install_failure_is_warn_e_sys_003(self) -> None:
        ctx, _ = _ctx(self.tmp, "yes\n")
        stage, _ = self._stage([Elevation(Outcome.FAILED, 100, detail="E: Unable to locate")])
        result = stage.run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-003")

    def test_no_package_manager_is_warn_with_manual_hint(self) -> None:
        with mock.patch.object(pm, "manager_for", lambda fam: None):
            ctx, _ = _ctx(self.tmp)
            result = SystemComponentsStage(os_family="linux").run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-003")
        self.assertIn("libusb-1.0-0", result.hint)

    def test_yes_flag_answers_the_question(self) -> None:
        ctx, _ = _ctx(self.tmp)
        assert ctx.wizard is not None
        ctx.wizard.assume_yes = True
        stage, calls = self._stage([Elevation(Outcome.OK, 0)])
        self.assertIs(stage.run(ctx).status, StageStatus.OK)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
