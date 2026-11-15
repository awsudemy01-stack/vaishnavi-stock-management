"""Offline bundle validation/stage, TLS helper, and wrapper scripts sanity (stdlib only)."""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bootstrap import offline as off
from bootstrap.cli import Mode, Options, registered_modes
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.offline import OfflineStage, target_dir_name, validate_bundle
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore

REPO = Path(__file__).resolve().parents[2]


def _ctx(tmp: Path, offline: Path | None) -> StageContext:
    paths = InstallPaths.from_program_dir(tmp / "program")
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    return StageContext(Options(mode=Mode.INSTALL_RUN, offline_dir=offline), paths, Messages.load("en"), Console(io.StringIO(), unicode=False), StateStore(paths.state_file), "t")


class BundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.py = f"{sys.version_info[0]}.{sys.version_info[1]}"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bundle(self, target: str | None = None, flat: bool = False, with_ollama: bool = False, models: int = 0) -> Path:
        b = self.tmp / "offline"
        wheels = b / "wheels" if flat else b / "wheels" / (target or target_dir_name(off.os_family(), off.arch_tag(), self.py))
        wheels.mkdir(parents=True)
        (wheels / "nicegui-2.0-py3-none-any.whl").write_bytes(b"x")
        if with_ollama:
            d = b / "ollama" / off.os_family()
            d.mkdir(parents=True)
            (d / "installer.bin").write_bytes(b"x")
        if models:
            (b / "models").mkdir()
            for i in range(models):
                (b / "models" / f"m{i}.tgz").write_bytes(b"x")
        (b / "bundle.json").write_text(json.dumps({"targets": [wheels.name]}), encoding="utf-8")
        return b

    def test_valid_bundle_for_this_machine(self) -> None:
        b = self._bundle(with_ollama=True, models=2)
        check = validate_bundle(b, off.os_family(), off.arch_tag(), self.py)
        self.assertTrue(check.ok)
        assert check.wheels_dir is not None
        self.assertTrue(check.wheels_dir.name.startswith(off.os_family()))
        self.assertIsNotNone(check.ollama_installer)
        self.assertEqual(len(check.model_files), 2)

    def test_flat_legacy_layout_accepted(self) -> None:
        b = self._bundle(flat=True)
        self.assertTrue(validate_bundle(b, off.os_family(), off.arch_tag(), self.py).ok)

    def test_wrong_target_rejected_with_built_for(self) -> None:
        b = self._bundle(target="linux-x86_64-py3.12")
        check = validate_bundle(b, "windows", "x86_64", "3.12")
        self.assertFalse(check.ok)
        self.assertEqual(check.problems, ["offline.err_no_wheels"])
        self.assertEqual(check.built_for, "linux-x86_64-py3.12")

    def test_missing_folder(self) -> None:
        self.assertEqual(validate_bundle(self.tmp / "nope", "linux", "x86_64", "3.12").problems, ["offline.err_missing"])

    def test_stage_applies_only_with_flag_and_fails_clearly(self) -> None:
        self.assertFalse(OfflineStage().applies(_ctx(self.tmp, None)))
        bad = self._bundle(target="plan9-mips-py2.7")
        result = OfflineStage().run(_ctx(self.tmp, bad))
        self.assertEqual((result.status, result.error_code), (StageStatus.FAIL, "E-SYS-015"))
        self.assertIn("plan9", result.message)
        self.assertIn("--make-offline-bundle", result.hint)
        good = self._bundle()
        result = OfflineStage().run(_ctx(self.tmp, good))
        self.assertIs(result.status, StageStatus.OK)
        self.assertTrue(result.data["wheels_dir"])

    def test_make_bundle_mode_registered_and_requires_lock(self) -> None:
        self.assertIn(Mode.MAKE_OFFLINE_BUNDLE, registered_modes())
        ctx = _ctx(self.tmp, None)
        ctx.options = Options(mode=Mode.MAKE_OFFLINE_BUNDLE, bundle_target="linux")
        self.assertEqual(off.run_make_bundle(ctx), 2)  # no requirements.lock in the temp program dir


class WrapperScriptsTest(unittest.TestCase):
    """The double-click wrappers exist, call main.py, and explain a missing Python."""

    def test_wrappers_exist_and_reference_main_py(self) -> None:
        for name in ("Start Vaishnavi.bat", "Start Vaishnavi.command", "start-vaishnavi.sh"):
            with self.subTest(name=name):
                text = (REPO / name).read_text(encoding="utf-8")
                self.assertIn("main.py", text)
                self.assertIn("E-SYS-001", text)
                self.assertIn("3.11", text)

    def test_bat_has_crlf_safe_content_and_utf8(self) -> None:
        text = (REPO / "Start Vaishnavi.bat").read_text(encoding="utf-8")
        self.assertIn("chcp 65001", text)
        self.assertIn("PYTHONIOENCODING=utf-8", text)
        self.assertNotRegex(text, r"[^\x00-\x7f]", "batch files must stay ASCII")

    def test_shell_wrappers_have_shebang_and_prefer_venv(self) -> None:
        for name in ("Start Vaishnavi.command", "start-vaishnavi.sh"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/bash"))
            self.assertIn('.venv/bin/python', text)
            self.assertTrue(re.search(r'"\$PY" main\.py "\$@"', text))


if __name__ == "__main__":
    unittest.main()
