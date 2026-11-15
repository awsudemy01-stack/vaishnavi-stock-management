"""Locations stage, handover runner, autostart backend/stage (stdlib only; OS commands faked)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bootstrap import autostart as au, handover as ho, locations as loc
from bootstrap.autostart import AutostartStage, Backend
from bootstrap.cli import Mode, Options, registered_modes
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.locations import Locations, LocationsStage, is_nested, read_locations
from bootstrap.stages import InstallPaths, StageContext, StageResult, StageStatus, StateStore
from bootstrap.wizard import AnswerStore, Wizard, WizardQuit


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _ctx(tmp: Path, typed: str = "", *, yes: bool = False, venv: bool = True) -> tuple[StageContext, io.StringIO]:
    paths = InstallPaths.from_program_dir(tmp / "program")
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    console, messages = Console(out, unicode=False), Messages.load("en")
    wizard = Wizard(console, messages, AnswerStore(paths.config_dir / "install-answers.toml"), assume_yes=yes, stdin=_Tty(typed))
    ctx = StageContext(Options(mode=Mode.INSTALL_RUN, assume_yes=yes), paths, messages, console, StateStore(paths.state_file), "t", wizard=wizard)
    ctx.results["survey"] = StageResult.ok(survey={"os_family": "linux"})
    if venv:
        ctx.results["venv"] = StageResult.ok(python=sys.executable)
    return ctx, out


class LocationsHelpersTest(unittest.TestCase):
    def test_nested_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "data"
            self.assertTrue(is_nested(a, a / "backups"))
            self.assertTrue(is_nested(a / "backups", a))
            self.assertTrue(is_nested(a, a))
            self.assertFalse(is_nested(a, Path(tmp) / "other"))

    def test_device_and_free_space_probe_nonexistent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "not" / "yet" / "there"
            self.assertTrue(loc.device_id(deep))
            self.assertIsNotNone(loc.free_gib(deep))
            self.assertEqual(loc.device_id(deep), loc.device_id(Path(tmp)))

    def test_ensure_writable_creates_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new" / "folder"
            self.assertIsNone(loc.ensure_writable(target))
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_locations_toml_roundtrip_with_backslashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "locations.toml"
            Locations(Path(r"C:\Users\Shop\VaishnaviStore"), Path(r"E:\Backups"), True).write(path)
            back = read_locations(path)
            assert back is not None
            self.assertEqual(str(back.primary), r"C:\Users\Shop\VaishnaviStore")
            self.assertTrue(back.same_device)
            self.assertIsNone(read_locations(path.with_name("missing.toml")))


class LocationsStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_asks_both_folders_rejects_nested_then_accepts_same_device(self) -> None:
        primary = self.tmp / "data"
        typed = f"{primary}\n{primary / 'inside'}\n{self.tmp / 'backups'}\nyes\n"
        ctx, out = _ctx(self.tmp, typed)
        result = LocationsStage().run(ctx)
        self.assertIs(result.status, StageStatus.WARN, "same device → warn")
        self.assertEqual(result.error_code, "E-SYS-012")
        self.assertTrue(primary.is_dir() and (self.tmp / "backups").is_dir())
        self.assertIn("must not be inside", out.getvalue())
        cfg = read_locations(ctx.paths.config_dir / "locations.toml")
        assert cfg is not None
        self.assertEqual(cfg.primary, primary)
        answers = tomllib.loads((ctx.paths.config_dir / "install-answers.toml").read_text(encoding="utf-8"))["answers"]
        self.assertEqual(Path(answers["primary_location"]), primary)

    def test_refusing_same_device_reasks_secondary(self) -> None:
        with mock.patch.object(loc, "device_id", side_effect=lambda p: "B" if "other" in str(p) else "A"):
            typed = f"{self.tmp / 'data'}\n{self.tmp / 'same'}\nno\n{self.tmp / 'other'}\n"
            ctx, _ = _ctx(self.tmp, typed)
            result = LocationsStage().run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(Path(result.data["secondary"]), self.tmp / "other")
        self.assertFalse(result.data["same_device"])

    def test_yes_flag_uses_defaults_accepting_same_disk_with_a_warning(self) -> None:
        fake_home = self.tmp / "home"
        with mock.patch.object(loc.Path, "home", classmethod(lambda cls: fake_home)), mock.patch.object(loc, "suggest_secondary", lambda home, s: home / "VaishnaviStore-Backups"):
            ctx, _ = _ctx(self.tmp, "", yes=True)
            result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.WARN, "E-SYS-012"))
        self.assertTrue((fake_home / "VaishnaviStore").is_dir())
        self.assertTrue((fake_home / "VaishnaviStore-Backups").is_dir())

    def test_repeating_the_same_secondary_twice_is_accepted_instead_of_looping(self) -> None:
        typed = f"{self.tmp / 'data'}\n{self.tmp / 'bk'}\nno\n{self.tmp / 'bk'}\n"
        ctx, _ = _ctx(self.tmp, typed)
        result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.WARN, "E-SYS-012"))

    def test_existing_config_reused_or_flagged(self) -> None:
        ctx, _ = _ctx(self.tmp, "")
        primary, secondary = self.tmp / "p", self.tmp / "s"
        Locations(primary, secondary, False).write(ctx.paths.config_dir / "locations.toml")
        # Recorded locations are never re-created: a missing folder means the disk is gone.
        result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.FAIL, "E-SYS-013"))
        self.assertFalse(primary.exists(), "must not silently create an empty data folder on another disk")

        primary.mkdir()
        result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.WARN, "E-SYS-014"), "secondary unplugged → backups queue")
        self.assertFalse(secondary.exists())

        secondary.mkdir()
        with mock.patch.object(loc, "device_id", side_effect=lambda p: "disk-a" if p == primary else "disk-b"):
            result = LocationsStage().run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(Path(result.data["primary"]), primary)

        # Both folders exist but are on one disk now (recorded flag was stale) → same-device warning.
        result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.WARN, "E-SYS-012"))

        with mock.patch.object(loc, "verify_existing", side_effect=lambda p: "gone" if p == primary else None):
            result = LocationsStage().run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.FAIL, "E-SYS-013"))

    def test_path_validation_messages(self) -> None:
        typed = f"relative/path\n{self.tmp / 'data'}\n{self.tmp / 'bk'}\nyes\n"
        ctx, out = _ctx(self.tmp, typed)
        LocationsStage().run(ctx)
        self.assertIn("full path", out.getvalue())
        with mock.patch.object(loc, "free_gib", return_value=0.5):
            ctx, out = _ctx(self.tmp / "second", f"{self.tmp / 'd2'}\n")  # fresh program dir: no locations.toml yet
            with self.assertRaises(WizardQuit):  # rejected for space, then EOF → owner "quit"
                LocationsStage().run(ctx)
            self.assertIn("GB free", out.getvalue())


class HandoverTest(unittest.TestCase):
    def test_app_available_false_on_bare_interpreter(self) -> None:
        ok, reason = ho.app_available(Path(sys.executable))
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_run_app_streams_and_passes_secrets_via_stdin(self) -> None:
        # Stand-in for `python -m vaishnavi`: a module that echoes stdin JSON keys and exits 3.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "vaishnavi"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "__main__.py").write_text(
                "import json,sys\n"
                "data=json.loads(sys.stdin.readline())\n"
                "print('args', sys.argv[1:])\n"
                "print('secret-keys', sorted(data['secrets']))\n"
                "sys.exit(3)\n",
                encoding="utf-8",
            )
            lines: list[str] = []
            res = ho.run_app(Path(sys.executable), ["doctor", "--json"], secrets={"ai.openai.api_key": "sk-x"}, on_line=lines.append, cwd=Path(tmp), timeout=60)
        self.assertEqual(res.exit_code, 3)
        self.assertFalse(res.ok)
        self.assertIn("args ['doctor', '--json']", res.output)
        self.assertIn("secret-keys ['ai.openai.api_key']", res.output)
        self.assertNotIn("sk-x", res.output)
        self.assertEqual(len(lines), 2)

    def test_run_app_handles_missing_interpreter(self) -> None:
        res = ho.run_app(Path("/definitely/missing/python"), ["run"])
        self.assertEqual(res.exit_code, -1)


class AutostartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_windows_backend_builds_schtasks_command(self) -> None:
        calls: list[list[str]] = []
        with mock.patch.object(au, "_run", side_effect=lambda cmd, timeout=60: (calls.append(cmd), (0, "SUCCESS"))[1]):
            b = Backend(self.tmp, Path("C:/v/python.exe"), family="windows")
            ok, _ = b.register()
            self.assertTrue(ok)
            self.assertEqual(calls[-1][:6], ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN"])
            self.assertIn("main.py", calls[-1][calls[-1].index("/TR") + 1])
            b.unregister()
            self.assertEqual(calls[-1][:3], ["schtasks", "/Delete", "/TN"])

    def test_macos_backend_writes_plist(self) -> None:
        home = self.tmp / "home"
        calls: list[list[str]] = []
        fake_run = lambda cmd, timeout=60: (calls.append(cmd), (0, ""))[1]  # noqa: E731
        with mock.patch.object(au.Path, "home", classmethod(lambda cls: home)), mock.patch.object(au.shutil, "which", return_value="/bin/launchctl"), \
             mock.patch.object(au.os, "getuid", lambda: 501, create=True), mock.patch.object(au, "_run", fake_run):
            b = Backend(self.tmp / "prog", Path("/v/bin/python"), family="macos")
            ok, _ = b.register()
            self.assertTrue(ok)
            plist = home / "Library" / "LaunchAgents" / "com.vaishnavi.store.plist"
            self.assertTrue(plist.exists())
            import plistlib

            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data["ProgramArguments"][1], str(self.tmp / "prog" / "main.py"))
            self.assertTrue(data["RunAtLoad"])
            self.assertNotIn("KeepAlive", data, "exit 1 (warnings) must not trigger a relaunch loop")
            self.assertEqual(calls[-1][:2], ["launchctl", "bootstrap"])
            self.assertTrue(b.is_registered())

    def test_macos_without_launchctl_is_reported_as_not_registered(self) -> None:
        home = self.tmp / "home"
        with mock.patch.object(au.Path, "home", classmethod(lambda cls: home)), mock.patch.object(au.shutil, "which", return_value=None):
            ok, detail = Backend(self.tmp / "prog", Path("/v/bin/python"), family="macos").register()
        self.assertFalse(ok)
        self.assertIn("launchctl", detail)

    def test_linux_backend_writes_unit(self) -> None:
        cfg = self.tmp / "xdg"
        calls: list[list[str]] = []
        fake_run = lambda cmd, timeout=60: (calls.append(cmd), (0, ""))[1]  # noqa: E731
        prog = self.tmp / "prog with space"
        with mock.patch.dict(au.os.environ, {"XDG_CONFIG_HOME": str(cfg)}), mock.patch.object(au.shutil, "which", return_value="/bin/systemctl"), mock.patch.object(au, "_run", fake_run):
            b = Backend(prog, Path("/v/bin/python"), family="linux")
            ok, detail = b.register()
            self.assertTrue(ok, detail)
            unit = (cfg / "systemd" / "user" / "vaishnavi.service").read_text(encoding="utf-8")
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("SuccessExitStatus=1 3", unit)
            self.assertIn('"--no-browser"', unit)
            self.assertIn(Backend._systemd_quote(str(prog / "main.py")), unit, "paths with spaces are quoted for systemd")
            self.assertEqual(Backend._systemd_quote('/opt/my shop/100%/a"b'), '"/opt/my shop/100%%/a\\"b"')
            self.assertIn(["systemctl", "--user", "enable", "vaishnavi.service"], calls)
            self.assertTrue(b.is_registered())
            b.unregister()
            self.assertFalse(b.is_registered())

    def test_linux_enable_failure_is_not_reported_as_success(self) -> None:
        cfg = self.tmp / "xdg"

        def failing_run(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
            return (1, "Failed to connect to bus") if "enable" in cmd else (0, "")

        with mock.patch.dict(au.os.environ, {"XDG_CONFIG_HOME": str(cfg)}), mock.patch.object(au.shutil, "which", return_value="/bin/systemctl"), mock.patch.object(au, "_run", failing_run):
            ok, detail = Backend(self.tmp / "prog", Path("/v/bin/python"), family="linux").register()
        self.assertFalse(ok)
        self.assertIn("enable failed", detail)

    def test_stage_asks_registers_or_skips(self) -> None:
        class FakeBackend(Backend):
            registered = False
            fail = False

            def is_registered(self) -> bool:
                return FakeBackend.registered

            def register(self) -> tuple[bool, str]:
                if FakeBackend.fail:
                    return False, "boom"
                FakeBackend.registered = True
                return True, ""

        ctx, _ = _ctx(self.tmp, "no\n")
        self.assertEqual(AutostartStage(backend_factory=FakeBackend).run(ctx).data["registered"], False)
        ctx, _ = _ctx(self.tmp, "yes\n")
        result = AutostartStage(backend_factory=FakeBackend).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertTrue(result.data["registered"])
        self.assertEqual(AutostartStage(backend_factory=FakeBackend).run(ctx).message, "already set up")
        FakeBackend.registered, FakeBackend.fail = False, True
        ctx, _ = _ctx(self.tmp, "yes\n")
        result = AutostartStage(backend_factory=FakeBackend).run(ctx)
        self.assertEqual((result.status, result.error_code), (StageStatus.WARN, "E-SYS-005"))

    def test_not_applicable_without_venv(self) -> None:
        ctx, _ = _ctx(self.tmp, "", venv=False)
        self.assertFalse(AutostartStage().applies(ctx))

    def test_uninstall_mode_is_registered(self) -> None:
        self.assertIn(Mode.UNINSTALL_AUTOSTART, registered_modes())


if __name__ == "__main__":
    unittest.main()
