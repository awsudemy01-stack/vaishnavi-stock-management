"""Console glyph fallback, message translation and secret redaction (stdlib only)."""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path

from bootstrap.console import Console, stream_supports_unicode
from bootstrap.i18n import MESSAGES_DIR, Messages
from bootstrap.logsetup import RedactingFilter, new_run_id, prune_old_logs, redact, setup_install_logging


class _Cp1252Stream(io.StringIO):
    encoding = "cp1252"


class _Utf8Stream(io.StringIO):
    encoding = "utf-8"


class ConsoleTest(unittest.TestCase):
    def test_unicode_detection(self) -> None:
        self.assertFalse(stream_supports_unicode(_Cp1252Stream()))
        self.assertTrue(stream_supports_unicode(_Utf8Stream()))

    def test_ascii_fallback_on_legacy_console(self) -> None:
        stream = _Cp1252Stream()
        c = Console(stream)
        c.ok("fine")
        c.warn("hmm")
        c.fail("bad")
        c.skip("skipped")
        text = stream.getvalue()
        self.assertIn("[OK]  fine", text)
        self.assertIn("[! ]  hmm", text)
        self.assertIn("[X ]  bad", text)
        self.assertIn("[- ]  skipped", text)
        self.assertNotIn("✔", text)

    def test_unicode_glyphs_when_supported(self) -> None:
        stream = _Utf8Stream()
        Console(stream).ok("fine")
        self.assertIn("✔  fine", stream.getvalue())

    def test_header_hint_and_progress(self) -> None:
        stream = _Utf8Stream()
        c = Console(stream)
        c.header("Title", "Log: x.log")
        c.hint("do this")
        c.progress("Installing", 5, 10)
        c.progress_end()
        text = stream.getvalue()
        self.assertTrue(text.startswith("Title"))
        self.assertIn("Log: x.log", text)
        self.assertIn("      do this", text)
        self.assertIn(" 50 %", text)


class MessagesTest(unittest.TestCase):
    def test_english_and_hindi_load_with_fallback(self) -> None:
        en, hi = Messages.load("en"), Messages.load("hi")
        self.assertEqual(en.t("runner.header"), "Vaishnavi Store Manager Setup")
        self.assertIn("सेटअप", hi.t("runner.header"))
        self.assertEqual(hi.t("result.warn", count=2), hi.t("result.warn", count=2))
        self.assertIn("2", hi.t("result.warn", count=2))

    def test_missing_key_returns_key_and_unknown_lang_falls_back(self) -> None:
        m = Messages.load("en")
        self.assertEqual(m.t("nope.missing"), "nope.missing")
        self.assertFalse(m.has("nope.missing"))
        self.assertEqual(Messages.load("fr").lang, "en")

    def test_bad_placeholder_does_not_crash(self) -> None:
        m = Messages.load("en")
        self.assertIn("{path}", m.t("runner.log_hint"))  # param missing → raw template

    def test_hindi_and_english_have_identical_key_sets(self) -> None:
        en = set(Messages._read(MESSAGES_DIR / "en.toml"))
        hi = set(Messages._read(MESSAGES_DIR / "hi.toml"))
        self.assertEqual(hi - en, set(), "hi.toml has keys that en.toml lacks")
        self.assertEqual(en - hi, set(), "en.toml keys missing a Hindi translation")

    def test_message_files_contain_no_encoding_damage(self) -> None:
        # '?' runs and U+FFFD are the fingerprints of text mangled by a cp1252 console.
        for name in ("en.toml", "hi.toml"):
            text = (MESSAGES_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", text, name)
            self.assertNotRegex(text, r"\?\?", name)
            self.assertNotRegex(text, r"\s\?\s", f"{name}: lone '?' where a dash/arrow was expected")
        hi = Messages._read(MESSAGES_DIR / "hi.toml")
        devanagari = sum(1 for v in hi.values() if any("\u0900" <= ch <= "\u097f" for ch in v))
        self.assertGreater(devanagari, len(hi) * 0.8, "most Hindi values must actually contain Devanagari")


class RedactionTest(unittest.TestCase):
    def test_redact_masks_common_secret_shapes(self) -> None:
        samples = {
            "api_key=sk-abcdefghijklmnop": "api_key=***",
            "password: hunter2 rest": "password: *** rest",
            'token="hf_ABCDEFGHIJKLMNOP"': "token=***",
            "Authorization: Bearer abc.def.ghi": "Authorization: ***",
            "plain text without secrets": "plain text without secrets",
            "found hf_ZZZZZZZZZZZZ in output": "found *** in output",
        }
        for raw, expected in samples.items():
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw), expected)

    def test_filter_masks_rendered_args(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "key %s", ("password=abc",), None)
        self.assertTrue(RedactingFilter().filter(record))
        self.assertEqual(record.getMessage(), "key password=***")


class LogSetupTest(unittest.TestCase):
    def tearDown(self) -> None:
        logging.shutdown()
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.NullHandler())

    def test_creates_log_file_with_run_id_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "install"
            for i in range(35):
                (log_dir.mkdir(parents=True, exist_ok=True), (log_dir / f"old_{i:02d}.log").write_text("x"))
            run_id = new_run_id()
            path = setup_install_logging(log_dir, run_id)
            logging.getLogger("t").info("hello password=secret")
            logging.shutdown()
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"run={run_id}", text)
            self.assertIn("hello password=***", text)
            self.assertLessEqual(len(list(log_dir.glob("*.log"))), 30)

    def test_prune_keeps_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for i in range(5):
                (d / f"{i}.log").write_text("x")
            self.assertEqual(prune_old_logs(d, keep=3), 2)
            self.assertEqual(len(list(d.glob("*.log"))), 3)


if __name__ == "__main__":
    unittest.main()
