"""core.logging — files per family, formatters, correlation context, redaction, config (stdlib only)."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path

from vaishnavi.core import logging as vlog
from vaishnavi.core.logging import (
    ContextFilter,
    JsonFormatter,
    KeyValueFormatter,
    LoggingConfig,
    current_cid,
    get_logger,
    redact,
    set_user_context,
    setup_logging,
    with_correlation,
)

REPO = Path(__file__).resolve().parents[2]


class ContextTest(unittest.TestCase):
    def test_with_correlation_scopes_and_restores(self) -> None:
        self.assertEqual(current_cid(), "-")
        with with_correlation(user="ramesh", role="STAFF", shift=41) as cid:
            self.assertEqual(current_cid(), cid)
            self.assertEqual(len(cid), 26)
            with with_correlation("INNER") as inner:
                self.assertEqual(inner, "INNER")
            self.assertEqual(current_cid(), cid)
        self.assertEqual(current_cid(), "-")

    def test_context_is_per_thread(self) -> None:
        seen: dict[str, str] = {}

        def worker(name: str) -> None:
            with with_correlation(name):
                seen[name] = current_cid()

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen, {f"t{i}": f"t{i}" for i in range(4)})

    def test_filter_stamps_and_redacts(self) -> None:
        rec = logging.LogRecord("vaishnavi.x", logging.INFO, __file__, 1, "login ok password=%s", ("hunter2",), None)
        rec.pin = "1234"  # type: ignore[attr-defined]
        with with_correlation("C1", user="owner", role="OWNER"):
            self.assertTrue(ContextFilter().filter(rec))
        self.assertEqual(rec.cid, "C1")  # type: ignore[attr-defined]
        self.assertEqual(rec.user, "owner")  # type: ignore[attr-defined]
        self.assertEqual(rec.getMessage(), "login ok password=***")
        self.assertEqual(rec.pin, "***")  # type: ignore[attr-defined]

    def test_redact_covers_pin_and_recovery_key(self) -> None:
        self.assertEqual(redact("pin=1234 recovery_key=apple banana"), "pin=*** recovery_key=*** banana")
        self.assertEqual(redact("Authorization: Bearer abc.def"), "Authorization: ***")
        self.assertEqual(redact("nothing here"), "nothing here")


class FormatterTest(unittest.TestCase):
    def _record(self, **extra: object) -> logging.LogRecord:
        rec = logging.LogRecord("vaishnavi.services.billing", logging.INFO, __file__, 1, "SALE_COMMITTED", (), None)
        rec.created = 1_763_100_137.318  # 2025-11-14 06:02:17.318 UTC = 11:32:17 IST
        rec.msecs = 318.0
        for k, v in extra.items():
            setattr(rec, k, v)
        with with_correlation("01JCXQ7V", user="ramesh", role="STAFF", shift=41):
            ContextFilter().filter(rec)
        return rec

    def test_key_value_text(self) -> None:
        line = KeyValueFormatter().format(self._record(invoice="VPS/25-26/000123", total_paise=43000, note="two words"))
        self.assertIn(" IST | INFO  | vaishnavi.services.billing | cid=01JCXQ7V user=ramesh(STAFF) shift=41 | SALE_COMMITTED", line)
        self.assertIn("invoice=VPS/25-26/000123", line)
        self.assertIn("total_paise=43000", line)
        self.assertIn('note="two words"', line)
        self.assertTrue(line.startswith("2025-11-14 11:32:17.318 IST"), line[:40])

    def test_json_lines(self) -> None:
        doc = json.loads(JsonFormatter().format(self._record(invoice="X", items=3, when=Path("/x"))))
        self.assertEqual(doc["event"], "SALE_COMMITTED")
        self.assertEqual(doc["cid"], "01JCXQ7V")
        self.assertEqual(doc["role"], "STAFF")
        self.assertEqual(doc["items"], 3)
        self.assertEqual(doc["when"], str(Path("/x")), "non-JSON types become strings")
        self.assertTrue(doc["ts"].endswith("Z"))

    def test_exception_is_included(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.LogRecord("vaishnavi.x", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info())
        ContextFilter().filter(rec)
        self.assertIn("ValueError: boom", KeyValueFormatter().format(rec))
        self.assertIn("ValueError: boom", json.loads(JsonFormatter().format(rec))["exc"])


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name) / "logs"
        self.console = io.StringIO()
        self.handle = setup_logging(self.logs, config_path=REPO / "config" / "logging.toml", console_stream=self.console)

    def tearDown(self) -> None:
        self.handle.shutdown()
        logging.getLogger().addHandler(logging.NullHandler())
        self._tmp.cleanup()

    def _read(self, rel: str) -> str:
        self.handle.listener.stop()  # flush the queue
        self.handle.listener.start()
        return (self.logs / rel).read_text(encoding="utf-8")

    def test_files_per_family_and_routing(self) -> None:
        with with_correlation("CID1", user="owner", role="OWNER"):
            get_logger("services.billing").debug("DEBUG_ONLY", extra={"x": 1})
            get_logger("services.billing").warning("LOW_STOCK", extra={"product": "CAM-1"})
            get_logger("ai.chat").info("AI_CALL", extra={"tokens": 12})
            get_logger("hardware.printer").info("PRINTED")
            logging.getLogger("vaishnavi.audit").info("AUDIT_ROW", extra={"action": "LOGIN", "pin": "9999"})
        app = self._read("app/vaishnavi.log")
        self.assertIn("DEBUG_ONLY", app)
        self.assertIn("LOW_STOCK", app)
        self.assertIn("cid=CID1 user=owner(OWNER)", app)
        errors = self._read("errors/errors.log")
        self.assertIn("LOW_STOCK", errors)
        self.assertNotIn("DEBUG_ONLY", errors)
        ai = self._read("ai/ai.log")
        self.assertIn("AI_CALL", ai)
        self.assertNotIn("LOW_STOCK", ai)
        hw = self._read("hardware/hardware.log")
        self.assertIn("PRINTED", hw)
        audit_lines = [json.loads(l) for l in self._read("audit/audit.jsonl").splitlines() if l]
        self.assertEqual(audit_lines[-1]["event"], "AUDIT_ROW")
        self.assertEqual(audit_lines[-1]["pin"], "***")
        jsonl = [json.loads(l) for l in self._read("app/vaishnavi.jsonl").splitlines() if l]
        self.assertTrue(any(d["event"] == "LOW_STOCK" and d["product"] == "CAM-1" for d in jsonl))

    def test_console_shows_info_not_debug(self) -> None:
        get_logger("x").debug("hidden")
        get_logger("x").info("shown")
        self.handle.listener.stop()
        self.handle.listener.start()
        text = self.console.getvalue()
        self.assertIn("shown", text)
        self.assertNotIn("hidden", text)

    def test_logging_ready_event_and_levels_from_toml(self) -> None:
        self.assertIn("LOGGING_READY", self._read("app/vaishnavi.log"))
        self.assertEqual(logging.getLogger("apscheduler").level, logging.WARNING)
        self.assertEqual(self.handle.config.families["errors"].backup_days, 90)

    def test_set_user_context(self) -> None:
        set_user_context("sita", "STAFF", 7)
        get_logger("x").info("hello")
        self.assertIn("user=sita(STAFF) shift=7", self._read("app/vaishnavi.log"))
        set_user_context(None, None)


class ConfigTest(unittest.TestCase):
    def test_defaults_when_missing_or_broken(self) -> None:
        cfg = LoggingConfig.load(None)
        self.assertEqual(cfg.console_level, logging.INFO)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "logging.toml"
            p.write_text("[logging\nbroken", encoding="utf-8")
            self.assertEqual(LoggingConfig.load(p).console_level, logging.INFO)
            p.write_text('[logging]\nconsole_level = "DEBUG"\n[levels]\n"vaishnavi.ai" = "WARNING"\n[families]\nai = { days = 3 }\n', encoding="utf-8")
            cfg = LoggingConfig.load(p)
            self.assertEqual(cfg.console_level, logging.DEBUG)
            self.assertEqual(cfg.module_levels["vaishnavi.ai"], logging.WARNING)
            self.assertEqual(cfg.families["ai"].backup_days, 3)

    def test_repo_logging_toml_parses(self) -> None:
        cfg = LoggingConfig.load(REPO / "config" / "logging.toml")
        self.assertTrue(cfg.json_lines)
        self.assertIn("vaishnavi", cfg.module_levels)

    def test_get_logger_prefix(self) -> None:
        self.assertEqual(get_logger("services.x").name, "vaishnavi.services.x")
        self.assertEqual(get_logger("vaishnavi.ai").name, "vaishnavi.ai")
        self.assertIs(vlog.ROOT_LOGGER, "vaishnavi")


if __name__ == "__main__":
    unittest.main()
