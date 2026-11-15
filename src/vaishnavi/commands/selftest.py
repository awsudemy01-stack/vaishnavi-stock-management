"""``python -m vaishnavi selftest [--json] [--pattern GLOB] [--no-live]`` — the acceptance gate on the shop PC (docs/07 §2).

Two halves: (1) the shipped **test suite** (``<program>/tests/core`` — stdlib-only, runs on any interpreter)
executed in-process with ``unittest``; (2) the **live doctor** against this very PC (hardware, backup drive,
AI provider…). Exit 0 = tests pass and the doctor is clean · 1 = tests pass, the doctor has notes ·
2 = a test failed or the doctor found a blocker. The JSON report is also written to ``logs/selftest/``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from vaishnavi import __version__
from vaishnavi.bootstrap.doctor import DoctorContext, default_checks, render_json, render_text, run_doctor
from vaishnavi.commands import Env, register
from vaishnavi.core.clock import iso_utc, now_ist
from vaishnavi.core.i18n import Texts
from vaishnavi.platform.paths import program_dir

log = logging.getLogger("vaishnavi.commands.selftest")


@dataclass(frozen=True)
class SuiteResult:
    """Outcome of the unit-test half."""

    ran: int
    failures: int
    errors: int
    skipped: int
    duration_ms: int
    output: str
    ok: bool


def run_suite(root: Path, *, pattern: str = "test_*.py", tests_dir: str = "tests/core") -> SuiteResult:
    """Discover and run the shipped tests under ``root/tests/core`` (top-level ``root`` so ``tests.core.*`` imports work)."""
    folder = root / tests_dir
    if not folder.is_dir():
        msg = f"tests folder not found: {folder} (this copy of the program ships without tests)"
        log.error("SELFTEST_NO_TESTS %s", msg)
        return SuiteResult(0, 0, 1, 0, 0, msg, False)
    if not pattern.endswith(".py"):
        pattern = pattern + ".py"
    stream = io.StringIO()
    started = time.perf_counter()
    inserted = str(root) not in sys.path
    if inserted:
        sys.path.insert(0, str(root))
    try:
        suite = unittest.defaultTestLoader.discover(str(folder), pattern=pattern, top_level_dir=str(root))
        result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    finally:
        if inserted:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
    duration = int((time.perf_counter() - started) * 1000)
    ok = result.wasSuccessful() and result.testsRun > 0
    log.info("SELFTEST_SUITE ran=%d failures=%d errors=%d skipped=%d ms=%d ok=%s", result.testsRun, len(result.failures), len(result.errors), len(result.skipped), duration, ok)
    return SuiteResult(result.testsRun, len(result.failures), len(result.errors), len(result.skipped), duration, stream.getvalue(), ok)


def exit_code_for(suite: SuiteResult, *, doctor_exit: int | None) -> int:
    """0 clean · 1 notes · 2 failed."""
    if not suite.ok:
        return 2
    if doctor_exit is None:
        return 0
    return 2 if doctor_exit >= 2 else int(doctor_exit)


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.add_argument("--pattern", default="test_*", help="test file pattern (default: all shipped tests)")
    p.add_argument("--no-live", action="store_true", help="skip the live doctor on this PC (tests only)")
    p.add_argument("--ascii", action="store_true", help="plain ASCII marks")


@register("selftest", "run the shipped test suite plus the live health check (exit 0 ok · 1 notes · 2 failed)", _args)
def run(ns: argparse.Namespace, env: Env) -> int:
    """See module docstring. Logging is console-only here: the tests re-configure the root logger themselves."""
    root = program_dir()
    sys.stdout.write("" if ns.json else f"Vaishnavi Store Manager {__version__} selftest — tests from {root / 'tests' / 'core'}\n")
    sys.stdout.flush()
    suite = run_suite(root, pattern=ns.pattern)
    doctor_report: dict[str, Any] | None = None
    doctor_text = ""
    if not ns.no_live:
        ctx = DoctorContext(settings=env.settings, paths=env.paths, config_dir=env.config_dir, lang=env.lang, fix=False, runtime={"program_dir": str(root)})
        report = run_doctor(default_checks(), ctx)
        doctor_report = json.loads(render_json(report))
        unicode_ok = not ns.ascii and (sys.stdout.encoding or "").lower().replace("-", "") in ("utf8", "utf16")
        doctor_text = render_text(report, Texts.load(env.lang), unicode=unicode_ok)
    code = exit_code_for(suite, doctor_exit=doctor_report["exit_code"] if doctor_report else None)
    payload = {"version": __version__, "at": iso_utc(), "tests": asdict(suite), "doctor": doctor_report, "exit_code": code}
    _write_report(env, payload)
    if ns.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    else:
        sys.stdout.write(suite.output.rstrip() + "\n")
        if doctor_text:
            sys.stdout.write("\n" + doctor_text + "\n")
        verdict = {0: "SELFTEST OK", 1: "SELFTEST OK (with notes from the health check)", 2: "SELFTEST FAILED"}[code]
        sys.stdout.write(f"\n{verdict} — tests {suite.ran} run, {suite.failures} failed, {suite.errors} errors, {suite.skipped} skipped in {suite.duration_ms} ms\n")
    sys.stdout.flush()
    return code


def _write_report(env: Env, payload: dict[str, Any]) -> None:
    """Keep the result for support (only when the data root exists — never create it, E-SYS-013)."""
    if not env.paths.primary.exists():
        return
    folder = env.paths.logs / "selftest"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        slim = dict(payload)
        slim["tests"] = {**payload["tests"], "output": payload["tests"]["output"][-20000:]}
        (folder / f"selftest-{now_ist().strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("SELFTEST_REPORT_NOT_WRITTEN %s", exc)
