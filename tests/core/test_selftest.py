"""Selftest + first-run tools (docs/07 §2, docs/17 §5, docs/13 unit 30): ``python -m vaishnavi selftest``,
``main.py --selftest``, ``main.py --doctor``, ``main.py --where``. Written first (docs/13 rule 7).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaishnavi.commands import selftest as selftest_cmd

PROJECT = Path(__file__).resolve().parents[2]


def _env_for(root: Path) -> dict[str, str]:
    import os

    return {**os.environ, "PYTHONPATH": str(PROJECT / "src"), "VAISHNAVI_PROGRAM_DIR": str(root), "PYTHONIOENCODING": "utf-8"}


class SuiteRunnerTest(unittest.TestCase):
    def test_runs_the_shipped_tests_by_pattern_and_reports(self) -> None:
        result = selftest_cmd.run_suite(PROJECT, pattern="test_core_money*")
        self.assertGreater(result.ran, 0)
        self.assertTrue(result.ok, result.output[-500:])
        self.assertEqual(result.failures + result.errors, 0)
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertIn("Ran ", result.output)

    def test_missing_tests_folder_is_reported_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = selftest_cmd.run_suite(Path(tmp), pattern="test_*")
        self.assertFalse(result.ok)
        self.assertEqual(result.ran, 0)
        self.assertIn("tests", result.output.lower())

    def test_exit_code_combines_tests_and_doctor(self) -> None:
        ok_suite = selftest_cmd.SuiteResult(ran=10, failures=0, errors=0, skipped=1, duration_ms=5, output="", ok=True)
        bad_suite = selftest_cmd.SuiteResult(ran=10, failures=1, errors=0, skipped=0, duration_ms=5, output="", ok=False)
        self.assertEqual(selftest_cmd.exit_code_for(ok_suite, doctor_exit=0), 0)
        self.assertEqual(selftest_cmd.exit_code_for(ok_suite, doctor_exit=1), 1, "tests pass, the PC has notes")
        self.assertEqual(selftest_cmd.exit_code_for(ok_suite, doctor_exit=2), 2)
        self.assertEqual(selftest_cmd.exit_code_for(bad_suite, doctor_exit=0), 2, "a failing test is a failing selftest")
        self.assertEqual(selftest_cmd.exit_code_for(ok_suite, doctor_exit=None), 0, "--no-live skips the doctor")


class SelftestCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "locations.toml").write_text(f'[locations]\nprimary = "{(self.root / "p").as_posix()}"\nsecondary = "{(self.root / "s").as_posix()}"\nsame_device = true\n', encoding="utf-8")
        (self.root / "p").mkdir()
        (self.root / "s").mkdir()
        # The program folder of a real install carries the tests; point the command at this repo's copy.
        (self.root / "tests").symlink_to(PROJECT / "tests", target_is_directory=True) if hasattr(Path, "symlink_to") and _can_symlink(self.root) else _copy_tests(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cmd(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "vaishnavi", *args], input="{}\n", capture_output=True, text=True, encoding="utf-8", env=_env_for(self.root), timeout=600, check=False)

    def test_selftest_json_reports_tests_and_live_checks_and_writes_a_report_file(self) -> None:
        self._cmd("init-db")
        proc = self._cmd("selftest", "--json", "--pattern", "test_core_money*")
        self.assertIn(proc.returncode, (0, 1, 2), proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["tests"]["ok"], report["tests"]["output"][-800:])
        self.assertGreater(report["tests"]["ran"], 0)
        self.assertIn("exit_code", report["doctor"])
        self.assertEqual(report["exit_code"], proc.returncode)
        self.assertEqual(report["exit_code"], selftest_cmd.exit_code_for(selftest_cmd.SuiteResult(**{k: report["tests"][k] for k in ("ran", "failures", "errors", "skipped", "duration_ms", "output", "ok")}), doctor_exit=report["doctor"]["exit_code"]))
        reports = list((self.root / "p" / "logs" / "selftest").glob("*.json"))
        self.assertEqual(len(reports), 1, "the result is kept for support")
        self.assertNotIn("Traceback", proc.stderr)

    def test_no_live_flag_skips_the_doctor(self) -> None:
        proc = self._cmd("selftest", "--json", "--pattern", "test_core_money*", "--no-live")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout[-500:])
        self.assertIsNone(json.loads(proc.stdout)["doctor"])


def _can_symlink(root: Path) -> bool:
    probe = root / ".probe"
    try:
        probe.symlink_to(root / "config", target_is_directory=True)
        probe.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


def _copy_tests(root: Path) -> None:
    import shutil

    shutil.copytree(PROJECT / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__"))


class InstallerModesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.program_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        import logging

        logging.shutdown()
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.NullHandler())
        self._tmp.cleanup()

    def _ctx(self):  # type: ignore[no-untyped-def]
        from tests.core.test_bootstrap_ai_provider import _ctx as installer_ctx

        ctx, out = installer_ctx(self.program_dir, "")
        ctx.results["venv"] = type("R", (), {"data": {"python": sys.executable}, "status": None})()  # type: ignore[assignment]
        return ctx, out

    def test_selftest_mode_hands_over_streams_and_maps_exit_codes(self) -> None:
        from bootstrap import selftest as mode
        from bootstrap.handover import AppResult
        from bootstrap.stages import ExitCode

        ctx, out = self._ctx()
        for exit_code, expected in ((0, ExitCode.OK), (1, ExitCode.WARN), (2, ExitCode.FAIL)):
            with mock.patch.object(mode.handover, "run_app", lambda *a, **k: AppResult(exit_code, "Ran 500 tests\nOK\n", 5)), mock.patch.object(mode.handover, "app_available", lambda p: (True, "0.1.0")):
                self.assertIs(mode.run_selftest(ctx), expected)
        self.assertIn("Ran 500 tests", out.getvalue())

    def test_doctor_mode_hands_over_with_fix_and_lang(self) -> None:
        from bootstrap import selftest as mode
        from bootstrap.cli import Mode, Options
        from bootstrap.handover import AppResult

        ctx, out = self._ctx()
        ctx.options = Options(mode=Mode.DOCTOR, fix=True, lang="hi")
        seen: dict[str, list[str]] = {}

        def fake(python, args, **kw):  # type: ignore[no-untyped-def]
            seen["args"] = list(args)
            return AppResult(1, " ! Backup stale (E-BKP-001)\n", 5)

        with mock.patch.object(mode.handover, "run_app", fake), mock.patch.object(mode.handover, "app_available", lambda p: (True, "0.1.0")):
            code = mode.run_doctor_mode(ctx)
        self.assertEqual(int(code), 1)
        self.assertEqual(seen["args"][0], "doctor")
        self.assertIn("--fix", seen["args"])
        self.assertIn("hi", seen["args"])
        self.assertIn("Backup stale", out.getvalue())

    def test_where_mode_prints_the_recorded_locations_without_the_app(self) -> None:
        from bootstrap import selftest as mode
        from bootstrap.stages import ExitCode

        ctx, out = self._ctx()
        (self.program_dir / "config").mkdir(exist_ok=True)
        (self.program_dir / "config" / "locations.toml").write_text('[locations]\nprimary = "D:/VaishnaviStore"\nsecondary = "E:/VaishnaviStore-Backups"\nsame_device = false\n', encoding="utf-8")
        self.assertIs(mode.run_where(ctx), ExitCode.OK)
        text = out.getvalue()
        self.assertIn(str(Path("D:/VaishnaviStore")), text)
        self.assertIn(str(Path("E:/VaishnaviStore-Backups")), text)
        self.assertIn("logs", text.lower())
        (self.program_dir / "config" / "locations.toml").unlink()
        self.assertIs(mode.run_where(ctx), ExitCode.WARN, "no install yet → says so, exit 1")

    def test_stop_mode_terminates_the_heartbeat_pid_and_waits_for_the_port(self) -> None:
        """Live run 3: yesterday's instance kept serving old code; the owner had no way to stop it except kill."""
        from bootstrap import selftest as mode

        cfg = self.program_dir / "config"
        cfg.mkdir(exist_ok=True)
        primary = self.program_dir / "data"
        (primary / "state").mkdir(parents=True)
        cfg.joinpath("locations.toml").write_text(f'[locations]\nprimary = "{primary.as_posix()}"\nsecondary = "{(self.program_dir / "bk").as_posix()}"\nsame_device = true\n', encoding="utf-8")
        (primary / "state" / "heartbeat.json").write_text('{"at": "2026-11-16T05:00:00Z", "pid": 4242, "loops": {}}', encoding="utf-8")
        killed: list[int] = []
        busy = {"value": True}

        def term(pid: int) -> bool:
            killed.append(pid)
            busy["value"] = False  # the app exits on SIGTERM
            return True

        clock = [0.0]
        outcome, detail = mode.stop_app(cfg, in_use=lambda h, p: busy["value"], terminate_fn=term, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
        self.assertEqual(outcome, "stopped")
        self.assertEqual(killed, [4242])
        self.assertIn("4242", detail)
        # the deliberate-stop marker is left for the watchdog in the installer window, and consumed once
        self.assertTrue((primary / "state" / mode.STOP_MARKER_FILE).exists())
        self.assertEqual(mode.stop_requested(cfg), "main.py --stop")
        self.assertIsNone(mode.stop_requested(cfg), "consumed")
        self.assertFalse((primary / "state" / mode.STOP_MARKER_FILE).exists())
        self.assertEqual(mode.stop_app(cfg, in_use=lambda h, p: False, terminate_fn=term)[0], "not_running")
        # pid gone already → not_running, never an error
        self.assertEqual(mode.stop_app(cfg, in_use=lambda h, p: True, terminate_fn=lambda pid: False)[0], "not_running")
        if not sys.platform.startswith("win"):
            self.assertFalse(mode.terminate(2**22 + 12345, windows=False), "a non-existent pid is reported as gone, not raised")
        # stubborn process → timeout
        clock[0] = 0.0
        self.assertEqual(mode.stop_app(cfg, in_use=lambda h, p: True, terminate_fn=lambda pid: True, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0], timeout_s=3)[0], "timeout")
        # no heartbeat → no_pid
        (primary / "state" / "heartbeat.json").unlink()
        self.assertEqual(mode.stop_app(cfg, in_use=lambda h, p: True, terminate_fn=term)[0], "no_pid")

    def test_stop_mode_ends_the_recorded_watchdog_before_the_app(self) -> None:
        """Live run 5b: the main.py window still ran OLD watchdog code after a code update, ignored the marker and relaunched."""
        from bootstrap import selftest as mode

        cfg = self.program_dir / "config"
        cfg.mkdir(exist_ok=True)
        primary = self.program_dir / "data"
        (primary / "state").mkdir(parents=True)
        cfg.joinpath("locations.toml").write_text(f'[locations]\nprimary = "{primary.as_posix()}"\nsecondary = "{(self.program_dir / "bk").as_posix()}"\nsame_device = true\n', encoding="utf-8")
        (primary / "state" / "heartbeat.json").write_text('{"at": "2026-11-20T09:00:00Z", "pid": 4242, "loops": {}}', encoding="utf-8")
        self.assertTrue(mode.write_watchdog_record(cfg, pid=777))
        self.assertEqual(mode.watchdog_pid(cfg), 777)
        order: list[str] = []
        busy = {"value": True}

        def term(pid: int) -> bool:
            order.append(f"app:{pid}")
            busy["value"] = False
            return True

        def kill(pid: int) -> bool:
            order.append(f"watchdog:{pid}")
            return True

        wd_stop = lambda c: mode.stop_watchdog(c, command_of=lambda pid: "/usr/bin/python3 /Users/rohit/vaishnavi/main.py", kill_fn=kill)  # noqa: E731
        outcome, detail = mode.stop_app(cfg, in_use=lambda h, p: busy["value"], terminate_fn=term, sleep=lambda s: None, clock=lambda: 0.0, stop_watchdog_fn=wd_stop)
        self.assertEqual(outcome, "stopped")
        self.assertEqual(order, ["watchdog:777", "app:4242"], "watchdog first — otherwise it sees the app exit and relaunches")
        self.assertIn("watchdog pid 777", detail)
        self.assertIsNone(mode.watchdog_pid(cfg), "record cleared")
        # a reused / dead pid is never killed: the record must point at a main.py process
        mode.write_watchdog_record(cfg, pid=778)
        killed: list[int] = []
        self.assertEqual(mode.stop_watchdog(cfg, command_of=lambda pid: "Safari", kill_fn=lambda pid: killed.append(pid) or True)[0], "stale")
        self.assertEqual(killed, [])
        self.assertIsNone(mode.watchdog_pid(cfg), "stale record removed")
        mode.write_watchdog_record(cfg, pid=779)
        self.assertEqual(mode.stop_watchdog(cfg, command_of=lambda pid: "", kill_fn=lambda pid: True)[0], "stale", "process gone")
        # own pid (the --stop process itself) is never a target
        mode.write_watchdog_record(cfg)
        self.assertEqual(mode.stop_watchdog(cfg, command_of=lambda pid: "python main.py", kill_fn=lambda pid: True)[0], "none")
        mode.clear_watchdog_record(cfg)
        self.assertEqual(mode.stop_watchdog(cfg)[0], "none")

    @unittest.skipIf(sys.platform.startswith("win"), "SIGTERM delivery is POSIX only")
    def test_supervise_treats_sigterm_as_a_deliberate_stop(self) -> None:
        """``--stop`` ends the watchdog window with SIGTERM; the new watchdog logs it instead of dying mid-loop."""
        import os
        import signal

        from bootstrap.app_stages import Watchdog, supervise

        class Proc:
            pid = 1
            returncode = None

            def poll(self) -> None:
                return None

        def sleep(_s: float) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        wd = Watchdog(launch=lambda: Proc(), health=lambda url: {"ok": True}, sleep=sleep, clock=lambda: 0.0)
        wd.adopt(Proc())
        before = signal.getsignal(signal.SIGTERM)
        supervise(wd)
        self.assertTrue(wd.stopped_on_purpose)
        self.assertIs(signal.getsignal(signal.SIGTERM), before, "handler restored")


if __name__ == "__main__":
    unittest.main()
