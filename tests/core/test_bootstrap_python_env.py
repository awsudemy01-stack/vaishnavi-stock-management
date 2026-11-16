"""Python check, venv and packages stages (stdlib only; pip is faked, venv creation is real)."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bootstrap import python_env as pe
from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.python_env import PackagesStage, PythonCheckStage, VenvStage
from bootstrap.stages import InstallPaths, StageContext, StageResult, StageStatus, StateStore


def _ctx(tmp: Path, *, venv_python: str | None = None, offline: Path | None = None, internet: bool | None = True) -> tuple[StageContext, io.StringIO]:
    paths = InstallPaths.from_program_dir(tmp)
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    ctx = StageContext(Options(mode=Mode.INSTALL_RUN, offline_dir=offline), paths, Messages.load("en"), Console(out, unicode=False), StateStore(paths.state_file), "t")
    if venv_python:
        ctx.results["venv"] = StageResult.ok(python=venv_python)
    ctx.results["survey"] = StageResult.ok(survey={"internet": internet})
    return ctx, out


class HelpersTest(unittest.TestCase):
    def test_pip_commands_with_lock_offline_and_without(self) -> None:
        py, prog, lock, wheels = Path("/v/python"), Path("/p"), Path("/p/requirements.lock"), Path("/o/wheels")
        cmds = pe.pip_command(py, prog, lock=lock, wheels=wheels)
        self.assertEqual(len(cmds), 2)
        self.assertIn("--require-hashes", cmds[0])
        self.assertIn("--no-index", cmds[0])
        self.assertIn(str(wheels), cmds[0])
        self.assertIn("--no-deps", cmds[1])
        self.assertIn("-e", cmds[1])
        online = pe.pip_command(py, prog, lock=None, wheels=None)
        self.assertEqual(len(online), 1)
        self.assertNotIn("--require-hashes", online[0])
        self.assertNotIn("--no-index", online[0])

    def test_count_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "requirements.lock"
            lock.write_text("# comment\nnicegui==2.1.0 \\\n    --hash=sha256:abc\nsqlalchemy==2.0.30 \\\n    --hash=sha256:def\n--index-url x\n", encoding="utf-8")
            self.assertEqual(pe.count_requirements(lock), 2)

    def test_venv_home_parsing_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "pyvenv.cfg").write_text("home = C:\\Python312\nversion = 3.12.1\n", encoding="utf-8")
            self.assertEqual(pe.venv_home(d), "C:\\Python312")
            self.assertIsNone(pe.venv_home(d / "nope"))
            f = d / "x.txt"
            f.write_text("a", encoding="utf-8")
            self.assertEqual(len(pe.file_digest(f)), 16)
            self.assertEqual(pe.file_digest(d / "missing"), "")

    def test_venv_python_layout(self) -> None:
        p = pe.venv_python(Path("/x/.venv"))
        self.assertIn("python", p.name)


class PythonCheckTest(unittest.TestCase):
    def test_current_interpreter_passes_or_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, _ = _ctx(Path(tmp))
            result = PythonCheckStage().run(ctx)
            self.assertIn(result.status, (StageStatus.OK, StageStatus.WARN))
            self.assertEqual(result.data["version"], f"{sys.version_info[0]}.{sys.version_info[1]}")

    def test_old_python_fails_with_e_sys_001(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pe, "version_tuple", return_value=(3, 9)):
            ctx, _ = _ctx(Path(tmp))
            result = PythonCheckStage().run(ctx)
            self.assertIs(result.status, StageStatus.FAIL)
            self.assertEqual(result.error_code, "E-SYS-001")
            self.assertIn("3.11", result.message)

    def test_future_python_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pe, "version_tuple", return_value=(3, 15)):
            ctx, _ = _ctx(Path(tmp))
            result = PythonCheckStage().run(ctx)
            self.assertIs(result.status, StageStatus.WARN)
            self.assertEqual(result.error_code, "E-SYS-011")


class VenvStageTest(unittest.TestCase):
    """Creates a real venv (stdlib, offline). Slow-ish (~5 s) but the most important path to prove."""

    def test_create_then_reuse_then_rebuild_on_foreign_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, out = _ctx(Path(tmp))
            first = VenvStage().run(ctx)
            self.assertIs(first.status, StageStatus.OK, first.detail)
            self.assertTrue(first.data["created"])
            python = Path(first.data["python"])
            self.assertTrue(python.exists())
            self.assertIn("Creating the private Python environment", out.getvalue())

            second = VenvStage().run(ctx)
            self.assertFalse(second.data["created"])
            self.assertEqual(second.message, "ready (.venv)")

            cfg = Path(tmp) / ".venv" / "pyvenv.cfg"
            cfg.write_text(cfg.read_text(encoding="utf-8").replace("home = ", "home = /somewhere/else/"), encoding="utf-8")
            third = VenvStage().run(ctx)
            self.assertTrue(third.data["created"], "a venv from another interpreter must be rebuilt")
            self.assertIn("rebuilding", out.getvalue())


class _FakePip:
    """Scripted replacement for run_streaming."""

    def __init__(self, scripts: list[tuple[list[str], int]]) -> None:
        self.scripts = scripts
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], *, on_line: Any, timeout: float = 0, env: Any = None) -> int:
        self.calls.append(cmd)
        lines, code = self.scripts[len(self.calls) - 1]
        for line in lines:
            on_line(line)
        return code


class PackagesStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, fake: _FakePip, *, missing: list[str] | None = None, lock: bool = True, offline: Path | None = None, internet: bool | None = True) -> tuple[StageResult, str]:
        if lock:
            (self.tmp / "requirements.lock").write_text("a==1 \\\n --hash=sha256:x\nb==2 \\\n --hash=sha256:y\n", encoding="utf-8")
        ctx, out = _ctx(self.tmp, venv_python=str(self.tmp / ".venv" / "bin" / "python"), offline=offline, internet=internet)
        with mock.patch.object(pe, "run_streaming", fake), mock.patch.object(pe, "verify_imports", return_value=missing or []):
            return PackagesStage().run(ctx), out.getvalue()

    def test_not_applicable_without_venv(self) -> None:
        ctx, _ = _ctx(self.tmp)
        self.assertFalse(PackagesStage().applies(ctx))

    def test_lock_install_ok_with_progress(self) -> None:
        fake = _FakePip([(["Collecting a==1", "Collecting b==2", "Installing collected packages: a, b", "Successfully installed a-1 b-2"], 0), (["Successfully installed vaishnavi-store-manager-0.1.0"], 0)])
        result, out = self._run(fake)
        self.assertIs(result.status, StageStatus.OK, result.detail)
        self.assertTrue(result.data["hash_verified"])
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("--require-hashes", fake.calls[0])
        self.assertIn("100 %", out)
        self.assertIn("3 packages installed", result.message)

    def test_without_lock_warns_e_sys_041(self) -> None:
        fake = _FakePip([(["Successfully installed a-1"], 0)])
        result, _ = self._run(fake, lock=False)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-041")
        self.assertFalse(result.data["hash_verified"])
        self.assertNotIn("--require-hashes", fake.calls[0])

    def test_pip_failure_fails_with_internet_aware_hint(self) -> None:
        result, _ = self._run(_FakePip([(["ERROR: boom"], 1)]), internet=False)
        self.assertIs(result.status, StageStatus.FAIL)
        self.assertEqual(result.error_code, "E-SYS-002")
        self.assertIn("--offline", result.hint)
        result, _ = self._run(_FakePip([(["ERROR: boom"], 1)]), internet=True)
        self.assertIn("internet connection", result.hint)
        # Survey said "online" but pip could not reach PyPI (proxy / DNS): still the network hint.
        result, _ = self._run(_FakePip([(["WARNING: Retrying ... NewConnectionError: getaddrinfo failed", "ERROR: no dist"], 1)]), internet=True)
        self.assertIn("proxy/firewall", result.hint)

    def test_verify_failure_fails(self) -> None:
        result, _ = self._run(_FakePip([([], 0), ([], 0)]), missing=["nicegui"])
        self.assertIs(result.status, StageStatus.FAIL)
        self.assertIn("nicegui", result.message)

    def test_offline_requires_wheels_folder_and_uses_no_index(self) -> None:
        bundle = self.tmp / "offline"
        bundle.mkdir()
        result, _ = self._run(_FakePip([]), offline=bundle)
        self.assertIs(result.status, StageStatus.FAIL)
        self.assertIn("wheels", result.message)
        (bundle / "wheels").mkdir()
        fake = _FakePip([([], 0), ([], 0)])
        result, _ = self._run(fake, offline=bundle)
        self.assertIs(result.status, StageStatus.OK)
        self.assertIn("--no-index", fake.calls[0])

    def test_inputs_key_tracks_lock_interpreter_and_venv_identity(self) -> None:
        ctx, _ = _ctx(self.tmp, venv_python=str(self.tmp / ".venv" / "bin" / "python"))
        self.assertIsNone(PackagesStage().cache_key(ctx), "never skipped by the runner — verified on every start")
        k1 = PackagesStage().inputs_key(ctx)
        (self.tmp / "requirements.lock").write_text("a==1\n", encoding="utf-8")
        k2 = PackagesStage().inputs_key(ctx)
        self.assertNotEqual(k1, k2)
        (self.tmp / ".venv").mkdir(parents=True)
        (self.tmp / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        k3 = PackagesStage().inputs_key(ctx)
        self.assertNotEqual(k2, k3, "a rebuilt venv (new pyvenv.cfg) changes the key")
        ctx.results["venv"] = StageResult.ok(python="/other/python")
        self.assertNotEqual(k3, PackagesStage().inputs_key(ctx))

    def test_second_start_verifies_imports_instead_of_reinstalling(self) -> None:
        (self.tmp / "requirements.lock").write_text("a==1 \\\n --hash=sha256:x\n", encoding="utf-8")
        ctx, _ = _ctx(self.tmp, venv_python=str(self.tmp / ".venv" / "bin" / "python"))
        fake = _FakePip([(["Successfully installed a-1"], 0), ([], 0)])
        with mock.patch.object(pe, "run_streaming", fake), mock.patch.object(pe, "verify_imports", return_value=[]) as verify:
            first = PackagesStage().run(ctx)
            ctx.state.record_stage("packages", first, None)
            second = PackagesStage().run(ctx)
        self.assertIs(second.status, StageStatus.OK)
        self.assertTrue(second.data["verified_only"])
        self.assertEqual(len(fake.calls), 2, "pip ran only for the first start")
        self.assertEqual(verify.call_count, 2, "imports are really checked on every start")
        # Third start (live run 3): the previous record already carries verified_only — must not crash with
        # "got multiple values for keyword argument", for the hash-verified and the unpinned variants alike.
        ctx.state.record_stage("packages", second, None)
        with mock.patch.object(pe, "run_streaming", fake), mock.patch.object(pe, "verify_imports", return_value=[]):
            third_cached = PackagesStage().run(ctx)
        self.assertIs(third_cached.status, StageStatus.OK)
        self.assertTrue(third_cached.data["verified_only"])
        self.assertEqual(len(fake.calls), 2, "still no pip")
        unpinned = StageResult.warn("x", error_code="E-SYS-041", python=str(self.tmp / ".venv" / "bin" / "python"), hash_verified=False, installed=[], inputs_key=PackagesStage().inputs_key(ctx), verified_only=True)
        ctx.state.record_stage("packages", unpinned, None)
        with mock.patch.object(pe, "run_streaming", fake), mock.patch.object(pe, "verify_imports", return_value=[]):
            unpinned_again = PackagesStage().run(ctx)
        self.assertEqual((unpinned_again.status, unpinned_again.error_code), (StageStatus.WARN, "E-SYS-041"))
        self.assertTrue(unpinned_again.data["verified_only"])
        # Rebuilt venv → full reinstall even though nothing else changed.
        ctx.results["venv"] = StageResult.ok(python=str(self.tmp / ".venv" / "bin" / "python"), created=True)
        fake2 = _FakePip([([], 0), ([], 0)])
        with mock.patch.object(pe, "run_streaming", fake2), mock.patch.object(pe, "verify_imports", return_value=[]):
            PackagesStage().run(ctx)
        self.assertEqual(len(fake2.calls), 2)
        # Broken venv (a package no longer imports) → reinstall as well.
        ctx.results["venv"] = StageResult.ok(python=str(self.tmp / ".venv" / "bin" / "python"), created=False)
        fake3 = _FakePip([([], 0), ([], 0)])
        with mock.patch.object(pe, "run_streaming", fake3), mock.patch.object(pe, "verify_imports", side_effect=[["sqlalchemy"], []]):
            third = PackagesStage().run(ctx)
        self.assertIs(third.status, StageStatus.OK)
        self.assertEqual(len(fake3.calls), 2)


class RunStreamingTest(unittest.TestCase):
    def test_streams_lines_and_returns_exit_code(self) -> None:
        lines: list[str] = []
        code = pe.run_streaming([sys.executable, "-c", "print('one'); print('two'); raise SystemExit(3)"], on_line=lines.append, timeout=30)
        self.assertEqual(code, 3)
        self.assertEqual(lines, ["one", "two"])

    def test_silent_hung_child_hits_the_deadline(self) -> None:
        import time

        started = time.perf_counter()
        code = pe.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], on_line=lambda line: None, timeout=1.5)
        self.assertEqual(code, -1)
        self.assertLess(time.perf_counter() - started, 15, "a child that prints nothing must not block past the timeout")

    def test_missing_executable_is_reported_not_raised(self) -> None:
        self.assertEqual(pe.run_streaming(["/definitely/missing/pip"], on_line=lambda line: None, timeout=5), -2)

    def test_verify_imports_really_imports(self) -> None:
        missing = pe.verify_imports(Path(sys.executable), ("json", "definitely_not_a_module_xyz"))
        self.assertEqual(missing, ["definitely_not_a_module_xyz"])


if __name__ == "__main__":
    unittest.main()
