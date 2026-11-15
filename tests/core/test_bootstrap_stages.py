"""Stage runner semantics and resumable state (stdlib only)."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.stages import (
    ExitCode,
    InstallPaths,
    StageContext,
    StageResult,
    StageRunner,
    StageStatus,
    StateStore,
    build_default_pipeline,
)


@dataclass
class FakeStage:
    """Configurable stage for tests."""

    key: str
    result: StageResult | BaseException = field(default_factory=StageResult.ok)
    applicable: bool = True
    cache: str | None = None
    title_key: str = "runner.header"
    calls: int = 0

    def applies(self, ctx: StageContext) -> bool:
        return self.applicable

    def cache_key(self, ctx: StageContext) -> str | None:
        return self.cache

    def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = InstallPaths.from_program_dir(Path(self._tmp.name))
        self.paths.install_log_dir.mkdir(parents=True)
        self.out = io.StringIO()
        self.ctx = self._ctx()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ctx(self) -> StageContext:
        return StageContext(
            options=Options(mode=Mode.INSTALL_RUN),
            paths=self.paths,
            messages=Messages.load("en"),
            console=Console(self.out, unicode=False),
            state=StateStore(self.paths.state_file),
            run_id="test",
        )


class StageRunnerTest(RunnerTestBase):
    def test_runs_in_order_and_collects_results(self) -> None:
        a, b = FakeStage("a", StageResult.ok("done a", value=1)), FakeStage("b", StageResult.ok())
        outcome = StageRunner(self.ctx).run([a, b])
        self.assertIs(outcome.exit_code, ExitCode.OK)
        self.assertEqual(list(outcome.results), ["a", "b"])
        self.assertEqual(self.ctx.result_data("a"), {"value": 1})
        self.assertGreaterEqual(outcome.results["a"].elapsed_ms, 0)
        self.assertIn("[OK]", self.out.getvalue())

    def test_warn_continues_and_maps_to_exit_1(self) -> None:
        stages = [FakeStage("a", StageResult.warn("careful", hint="do this", error_code="E-SYS-004")), FakeStage("b")]
        outcome = StageRunner(self.ctx).run(stages)
        self.assertIs(outcome.exit_code, ExitCode.WARN)
        self.assertEqual(stages[1].calls, 1)
        self.assertEqual(outcome.warnings, ["a"])
        self.assertIn("do this", self.out.getvalue())

    def test_fail_stops_the_pipeline_and_maps_to_exit_2(self) -> None:
        stages = [FakeStage("a", StageResult.fail("broken", error_code="E-SYS-002", hint="fix it")), FakeStage("b")]
        outcome = StageRunner(self.ctx).run(stages)
        self.assertIs(outcome.exit_code, ExitCode.FAIL)
        self.assertEqual(outcome.stopped_at, "a")
        self.assertEqual(outcome.error_code, "E-SYS-002")
        self.assertEqual(stages[1].calls, 0)
        self.assertNotIn("b", outcome.results)
        self.assertIn("E-SYS-002", self.out.getvalue())

    def test_not_applicable_stage_is_skipped_without_running(self) -> None:
        s = FakeStage("a", applicable=False)
        outcome = StageRunner(self.ctx).run([s])
        self.assertEqual(s.calls, 0)
        self.assertIs(outcome.results["a"].status, StageStatus.SKIP)
        self.assertIs(outcome.exit_code, ExitCode.OK)

    def test_cached_stage_is_skipped_on_second_run_but_rerun_when_cache_key_changes(self) -> None:
        s = FakeStage("venv", StageResult.ok(python="3.12"), cache="lock-hash-1")
        StageRunner(self.ctx).run([s])
        self.assertEqual(s.calls, 1)

        ctx2 = self._ctx()  # new run, state reloaded from disk
        StageRunner(ctx2).run([s])
        self.assertEqual(s.calls, 1, "second run must skip the cached stage")
        self.assertIs(ctx2.results["venv"].status, StageStatus.SKIP)
        self.assertEqual(ctx2.result_data("venv"), {"python": "3.12"}, "cached data is replayed")

        StageRunner(self._ctx()).run([s])
        self.assertEqual(s.calls, 1, "third run must still skip (a cache hit must not overwrite the OK record with SKIP)")

        s.cache = "lock-hash-2"
        StageRunner(self._ctx()).run([s])
        self.assertEqual(s.calls, 2, "changed cache key must re-run the stage")

    def test_cached_warning_keeps_warning_the_owner(self) -> None:
        s = FakeStage("loc", StageResult.warn("same disk", error_code="E-SYS-012", hint="move it"), cache="k")
        StageRunner(self.ctx).run([s])
        ctx2 = self._ctx()
        outcome = StageRunner(ctx2).run([s])
        self.assertEqual(s.calls, 1)
        self.assertIs(outcome.exit_code, ExitCode.WARN, "a still-true warning must not silently become OK on the next start")
        self.assertEqual(ctx2.results["loc"].error_code, "E-SYS-012")
        self.assertIn("E-SYS-012", self.out.getvalue(), "warning codes are shown, not only logged")

    def test_state_is_checkpointed_after_every_stage(self) -> None:
        a = FakeStage("a", StageResult.ok(value=1), cache="k")
        b = FakeStage("b", StageResult.fail("x", error_code="E-SYS-002"))
        StageRunner(self.ctx).run([a, b])
        fresh = StateStore(self.paths.state_file)
        self.assertTrue(fresh.is_done("a", "k"))

    def test_install_lock_is_exclusive_and_released(self) -> None:
        from bootstrap.stages import InstallLock

        first, second = InstallLock(self.paths.lock_file), InstallLock(self.paths.lock_file)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire(), "a second installer must be refused while the first runs")
        first.release()
        self.assertTrue(second.acquire(), "released lock can be taken again")
        second.release()
        second.release()  # idempotent

    def test_failed_stage_is_not_treated_as_cached(self) -> None:
        s = FakeStage("a", StageResult.fail("x", error_code="E-SYS-002"), cache="k")
        StageRunner(self.ctx).run([s])
        StageRunner(self._ctx()).run([s])
        self.assertEqual(s.calls, 2)

    def test_exception_in_stage_is_internal_error_with_log_traceback(self) -> None:
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger("bootstrap.stages").addHandler(handler)
        try:
            stages = [FakeStage("a", RuntimeError("bug")), FakeStage("b")]
            outcome = StageRunner(self.ctx).run(stages)
        finally:
            logging.getLogger("bootstrap.stages").removeHandler(handler)
        self.assertIs(outcome.exit_code, ExitCode.INTERNAL)
        self.assertEqual(outcome.error_code, "E-SYS-099")
        self.assertEqual(stages[1].calls, 0)
        self.assertIn("Traceback", log_stream.getvalue())
        self.assertIn("RuntimeError: bug", log_stream.getvalue())
        self.assertNotIn("Traceback", self.out.getvalue())

    def test_keyboard_interrupt_saves_state_and_propagates(self) -> None:
        stages = [FakeStage("a", StageResult.ok(saved=True)), FakeStage("b", KeyboardInterrupt())]
        with self.assertRaises(KeyboardInterrupt):
            StageRunner(self.ctx).run(stages)
        data = json.loads(self.paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["stages"]["a"]["status"], "OK")
        self.assertNotIn("b", data["stages"])

    def test_empty_pipeline_is_ok_and_says_so(self) -> None:
        outcome = StageRunner(self.ctx).run([])
        self.assertIs(outcome.exit_code, ExitCode.OK)
        self.assertIn("Nothing to do", self.out.getvalue())

    def test_default_pipeline_builds(self) -> None:
        self.assertIsInstance(build_default_pipeline(Options(mode=Mode.INSTALL_RUN)), list)


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "install" / "state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_and_atomic_write(self) -> None:
        store = StateStore(self.path)
        store.start_run("r1", ["--doctor"])
        store.record_stage("a", StageResult.ok("m", x=1).with_elapsed(5), "k")
        store.finish_run("r1", 1)
        store.save()
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

        again = StateStore(self.path)
        self.assertTrue(again.is_done("a", "k"))
        self.assertFalse(again.is_done("a", "other"))
        self.assertEqual(again.stage_data("a"), {"x": 1})
        rec = again.stage_record("a")
        assert rec is not None
        self.assertEqual(rec["elapsed_ms"], 5)

    def test_corrupt_or_foreign_state_starts_fresh(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(StateStore(self.path).stage_record("a"))
        self.path.write_text(json.dumps({"schema": 99, "stages": {"a": {"status": "OK"}}}), encoding="utf-8")
        self.assertIsNone(StateStore(self.path).stage_record("a"))

    def test_run_history_is_capped(self) -> None:
        store = StateStore(self.path)
        for i in range(30):
            store.start_run(f"r{i}", [])
        store.save()
        runs: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))["runs"]
        self.assertEqual(len(runs), 20)
        self.assertEqual(runs[-1]["run_id"], "r29")


if __name__ == "__main__":
    unittest.main()
