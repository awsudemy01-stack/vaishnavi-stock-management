"""core.settings — TOML + env + DB overrides, coercion, validation, secret refusal (stdlib only)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vaishnavi.core.errors import ConfigError, SecretsError
from vaishnavi.core.settings import Settings

REPO = Path(__file__).resolve().parents[2]


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str, name: str = "settings.toml") -> None:
        (self.cfg / name).write_text(text, encoding="utf-8")

    def test_defaults_without_any_file(self) -> None:
        s = Settings.load(self.cfg, environ={})
        self.assertEqual(s.shop.name, "Vaishnavi Puja Store")
        self.assertTrue(s.billing.require_scan)
        self.assertFalse(s.billing.require_printer)
        self.assertEqual(s.ai.provider, "ollama")
        self.assertEqual(s.server.bind_host, "127.0.0.1")
        self.assertEqual(s.locations.primary_path.name, "VaishnaviStore")
        self.assertEqual(s.source_files, [])

    def test_repo_template_loads_and_validates(self) -> None:
        s = Settings.load(REPO / "config", environ={})
        self.assertEqual(s.receipt.invoice_prefix, "VPS")
        self.assertEqual(s.ai.ollama.chat_model, "qwen2.5:3b-instruct")
        self.assertEqual(s.backup.nightly_time, "21:30")
        self.assertTrue(s.source_files[0].endswith("settings.toml"))

    def test_toml_values_nested_and_unknown_keys_ignored(self) -> None:
        self._write('[shop]\nname = "Test Shop"\nfuture_key = 1\n[billing]\nmanual_add_policy = "BLOCK"\nadhoc_discount_cap_percent = 5\n[ai.ollama]\nchat_model = "qwen2.5:7b-instruct"\n[unknown_section]\nx = 1\n')
        s = Settings.load(self.cfg, environ={})
        self.assertEqual(s.shop.name, "Test Shop")
        self.assertEqual(s.billing.manual_add_policy, "BLOCK")
        self.assertEqual(s.billing.adhoc_discount_cap_percent, 5)
        self.assertEqual(s.ai.ollama.chat_model, "qwen2.5:7b-instruct")

    def test_locations_toml_merged(self) -> None:
        self._write('[locations]\nprimary = "D:\\\\Data"\nsecondary = "E:\\\\Backups"\nsame_device = false\n', "locations.toml")
        s = Settings.load(self.cfg, environ={})
        self.assertEqual(str(s.locations.primary_path), "D:\\Data")
        self.assertFalse(s.locations.same_device)

    def test_environment_overrides_file(self) -> None:
        self._write("[server]\nport = 8080\n")
        s = Settings.load(self.cfg, environ={"VAISHNAVI_SERVER__PORT": "9090", "VAISHNAVI_SERVER__LAN_ACCESS": "yes", "VAISHNAVI_AI__PROVIDER": "none", "VAISHNAVI_NOPE__X": "1", "OTHER": "x"})
        self.assertEqual(s.server.port, 9090)
        self.assertTrue(s.server.lan_access)
        self.assertEqual(s.server.bind_host, "0.0.0.0")
        self.assertEqual(s.ai.provider, "none")

    def test_secret_in_toml_is_refused(self) -> None:
        self._write('[ai.openai]\napi_key = "sk-abc"\n')
        with self.assertRaises(SecretsError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertEqual(cm.exception.code, "E-SEC-006")
        self.assertIn("ai.openai.api_key", str(cm.exception))
        self._write('[publishing]\npassword = ""\n')  # empty value is tolerated
        Settings.load(self.cfg, environ={})

    def test_invalid_toml_and_bad_types(self) -> None:
        self._write("[shop\nbroken")
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertEqual(cm.exception.code, "E-CFG-001")
        self._write('[server]\nport = "eighty"\n')
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertIn("server.port", str(cm.exception))
        self._write('[billing]\nmanual_add_policy = "MAYBE"\n')
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertIn("billing.manual_add_policy", str(cm.exception))
        self._write("[receipt]\npaper_width_mm = 76\n")
        with self.assertRaises(ConfigError):
            Settings.load(self.cfg, environ={})

    def test_cross_field_validation(self) -> None:
        self._write('[shop]\nname = "  "\n')
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertEqual(cm.exception.params["key"], "shop.name")
        self._write("[tax]\nregistered = true\n")
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertEqual(cm.exception.params["key"], "shop.gstin")
        self._write("[server]\nport = 8443\n")
        with self.assertRaises(ConfigError):
            Settings.load(self.cfg, environ={})
        self._write('[backup]\nnightly_time = "25:00"\n')
        with self.assertRaises(ConfigError):
            Settings.load(self.cfg, environ={})
        self._write("[billing]\nheartbeat_seconds = 1\n")
        with self.assertRaises(ConfigError):
            Settings.load(self.cfg, environ={})

    def test_user_message_names_the_key_in_hindi_too(self) -> None:
        self._write('[server]\nport = "x"\n')
        with self.assertRaises(ConfigError) as cm:
            Settings.load(self.cfg, environ={})
        self.assertIn("server.port", cm.exception.user_message("hi"))


class OverridesTest(unittest.TestCase):
    def test_with_overrides_is_a_validated_copy(self) -> None:
        base = Settings()
        new = base.with_overrides({"shop.name": "Renamed", "billing.adhoc_discount_cap_percent": "15", "billing.require_printer": True, "ai.cloud.budget_alert_pct": 90})
        self.assertEqual(new.shop.name, "Renamed")
        self.assertEqual(new.billing.adhoc_discount_cap_percent, 15)
        self.assertTrue(new.billing.require_printer)
        self.assertEqual(new.ai.cloud.budget_alert_pct, 90)
        self.assertEqual(base.shop.name, "Vaishnavi Puja Store", "original untouched")
        self.assertFalse(base.billing.require_printer)
        with self.assertRaises(ConfigError):
            base.with_overrides({"billing.adhoc_discount_cap_percent": 150})
        with self.assertRaises(ConfigError):
            base.with_overrides({"nope.key": 1})
        with self.assertRaises(ConfigError):
            base.with_overrides({"shop.nope": 1})

    def test_list_coercion_from_string(self) -> None:
        s = Settings().with_overrides({"receipt.footer_lines": "Thanks | Visit again"})
        self.assertEqual(s.receipt.footer_lines, ["Thanks", "Visit again"])

    def test_as_dict_has_every_section_and_no_secret_fields(self) -> None:
        d = Settings().as_dict()
        self.assertEqual(set(d), {"shop", "tax", "receipt", "billing", "server", "ai", "backup", "hardware", "locations", "forecast", "publishing"})
        flat = str(d).lower()
        self.assertNotIn("api_key", flat)
        self.assertNotIn("password", flat)


if __name__ == "__main__":
    unittest.main()
