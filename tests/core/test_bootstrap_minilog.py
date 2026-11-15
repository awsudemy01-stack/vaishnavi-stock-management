"""Mini log console: routes, sandboxing, level filter, port fallback, stage behaviour (stdlib only)."""

from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.minilog import RUNTIME_KEY, LogConsoleStage, MiniLogServer, filter_lines, level_of, stop_log_console, tail
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore

SAMPLE = (
    "2026-11-14 08:00:00 DEBUG   run=1 bootstrap.x: first\n"
    "2026-11-14 08:00:01 INFO    run=1 bootstrap.x: STAGE_START key=a\n"
    "2026-11-14 08:00:02 WARNING run=1 bootstrap.x: careful password=***\n"
    "2026-11-14 08:00:03 ERROR   run=1 bootstrap.x: boom\n"
    "Traceback (most recent call last):\n"
    "  File x, line 1\n"
    "RuntimeError: bug\n"
    "2026-11-14 08:00:04 INFO    run=1 bootstrap.x: after\n"
)


def _get(url: str) -> tuple[int, str, str]:
    with urlopen(url, timeout=5) as resp:  # noqa: S310 (local test server)
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


class PureFunctionsTest(unittest.TestCase):
    def test_level_of(self) -> None:
        self.assertEqual(level_of("2026-11-14 08:00:00 DEBUG   run=1 x: y"), "DEBUG")
        self.assertEqual(level_of("Traceback (most recent call last):"), "")

    def test_filter_keeps_continuation_lines_with_their_parent(self) -> None:
        lines = SAMPLE.splitlines(keepends=True)
        warn_up = filter_lines(lines, "WARNING")
        self.assertEqual(len(warn_up), 5)  # WARNING, ERROR + 3 traceback lines
        self.assertTrue(warn_up[-1].startswith("RuntimeError"))
        self.assertEqual(filter_lines(lines, "DEBUG"), lines)
        self.assertEqual(filter_lines(lines, "ERROR")[0].split()[2], "ERROR")
        self.assertEqual(filter_lines(lines, "nonsense"), lines)

    def test_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.log"
            p.write_text(SAMPLE, encoding="utf-8")
            self.assertEqual(tail(p, 2)[-1], "2026-11-14 08:00:04 INFO    run=1 bootstrap.x: after\n")
            self.assertEqual(tail(Path(tmp) / "missing.log", 5), [])


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name) / "install"
        self.log_dir.mkdir()
        (self.log_dir / "2026-11-13_old.log").write_text("old line\n", encoding="utf-8")
        (self.log_dir / "2026-11-14_new.log").write_text(SAMPLE, encoding="utf-8")
        import os, time  # noqa: E401

        now = time.time()
        os.utime(self.log_dir / "2026-11-13_old.log", (now - 100, now - 100))
        os.utime(self.log_dir / "2026-11-14_new.log", (now, now))
        (self.log_dir / "state.json").write_text(json.dumps({"schema": 1, "runs": [], "stages": {}}), encoding="utf-8")
        (self.log_dir / "secret.txt").write_text("not served", encoding="utf-8")
        self.server = MiniLogServer.start(self.log_dir, host="127.0.0.1", port=0)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self._tmp.cleanup()

    def test_page_shows_newest_log_with_level_classes(self) -> None:
        status, ctype, body = _get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("2026-11-14_new.log", body)
        self.assertIn('class="ERROR"', body)
        self.assertIn("boom", body)
        self.assertIn('http-equiv="refresh"', body)
        self.assertNotIn("old line", body)

    def test_raw_and_level_filter_and_file_selection(self) -> None:
        _, ctype, body = _get(self.base + "/raw")
        self.assertIn("text/plain", ctype)
        self.assertEqual(body, SAMPLE)
        _, _, body = _get(self.base + "/raw?level=ERROR")
        self.assertTrue(body.startswith("2026-11-14 08:00:03 ERROR"))
        self.assertIn("RuntimeError", body)
        _, _, body = _get(self.base + "/raw?file=2026-11-13_old.log")
        self.assertEqual(body, "old line\n")

    def test_files_state_and_healthz(self) -> None:
        _, _, body = _get(self.base + "/files")
        self.assertEqual(json.loads(body)["files"], ["2026-11-14_new.log", "2026-11-13_old.log"])
        _, _, body = _get(self.base + "/state")
        self.assertEqual(json.loads(body)["schema"], 1)
        _, _, body = _get(self.base + "/healthz")
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_only_log_files_inside_the_folder_are_served(self) -> None:
        for bad in ("/raw?file=secret.txt", "/raw?file=..%2Fother.log", "/raw?file=%2Fetc%2Fpasswd", "/nowhere"):
            with self.subTest(url=bad), self.assertRaises(HTTPError) as cm:
                urlopen(self.base + bad, timeout=5)  # noqa: S310
            self.assertEqual(cm.exception.code, 404)
            cm.exception.close()

    def test_empty_folder_page_says_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MiniLogServer.start(Path(tmp), host="127.0.0.1", port=0)
            try:
                _, _, body = _get(f"http://127.0.0.1:{server.port}/")
                self.assertIn("waiting for the first log line", body)
            finally:
                server.stop()


class PortFallbackTest(unittest.TestCase):
    def test_falls_back_to_next_port_when_busy(self) -> None:
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server = MiniLogServer.start(Path(tmp), host="127.0.0.1", port=busy_port, attempts=3)
                try:
                    self.assertNotEqual(server.port, busy_port)
                    self.assertIn(server.port, range(busy_port + 1, busy_port + 3))
                finally:
                    server.stop()
        finally:
            blocker.close()


class StageTest(unittest.TestCase):
    def test_stage_starts_server_registers_runtime_and_stops_on_handover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = InstallPaths.from_program_dir(Path(tmp))
            paths.install_log_dir.mkdir(parents=True)
            out = io.StringIO()
            ctx = StageContext(Options(mode=Mode.INSTALL_RUN), paths, Messages.load("en"), Console(out, unicode=False), StateStore(paths.state_file), "t")
            stage = LogConsoleStage(port=0)
            result = stage.run(ctx)
            try:
                self.assertIs(result.status, StageStatus.OK)
                self.assertIn(RUNTIME_KEY, ctx.runtime)
                self.assertTrue(result.data["url"].startswith("http://"))
                self.assertIn("watch at http://", result.message)
            finally:
                stop_log_console(ctx)
            self.assertNotIn(RUNTIME_KEY, ctx.runtime)
            stop_log_console(ctx)  # idempotent


if __name__ == "__main__":
    unittest.main()
