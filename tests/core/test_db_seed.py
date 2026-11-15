"""db/seed.py on a stdlib sqlite3 database: idempotent, complete, and consistent with the services."""

from __future__ import annotations

import json
import sqlite3
import unittest

from vaishnavi.db import seed
from vaishnavi.db.records import PASSWORD_SENTINEL
from vaishnavi.db.schema import apply_schema
from vaishnavi.services.secure_settings import SECURE_KEYS
from vaishnavi.services.settings import DATA_KEYS


def fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    return conn


class SeedTest(unittest.TestCase):
    def test_seeds_every_reference_row_once(self) -> None:
        conn = fresh()
        report = seed.apply_seed(conn, host="test", now="2026-11-14T04:00:00Z")
        self.assertTrue(report.owner_created)
        self.assertEqual(len(report.instance_id), 16)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM role").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM category").fetchone()[0], len(seed.CATEGORIES))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM setup_step WHERE done_at IS NULL").fetchone()[0], 11)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM counter").fetchone()[0], len(seed.COUNTERS))
        self.assertEqual(conn.execute("SELECT is_active, redteam_passed FROM prompt_version WHERE version = 1").fetchone(), (1, 1))
        sku, is_system, price = conn.execute("SELECT sku, is_system, price_paise FROM product").fetchone()
        self.assertEqual((sku, is_system, price), (seed.OPEN_ITEM_SKU, 1, 0))
        name_hi = conn.execute("SELECT name_hi FROM category WHERE name = 'Camphor & Ghee'").fetchone()[0]
        self.assertEqual(name_hi, "कपूर और घी", "Hindi survives the round trip")
        # second run: nothing new
        again = seed.apply_seed(conn, host="test")
        self.assertEqual(again.total, 0)
        self.assertFalse(again.owner_created)
        self.assertEqual(again.instance_id, report.instance_id)
        conn.close()

    def test_owner_is_created_with_sentinel_not_a_usable_password(self) -> None:
        conn = fresh()
        seed.apply_seed(conn, now="2026-11-14T04:00:00Z")
        row = conn.execute("SELECT username, password_hash, must_change_password, owner_pin_hash FROM user").fetchone()
        self.assertEqual(row, (seed.OWNER_USERNAME, PASSWORD_SENTINEL, 1, None))
        self.assertTrue(seed.is_owner_setup_pending(conn))
        conn.execute("UPDATE user SET password_hash = '$scrypt$ln=15,r=8,p=1$a$b'")
        self.assertFalse(seed.is_owner_setup_pending(conn))
        conn.close()

    def test_existing_owner_is_left_alone(self) -> None:
        conn = fresh()
        conn.execute("INSERT INTO role(id, name) VALUES (1, 'OWNER')")
        conn.execute("INSERT INTO user(username, display_name, role_id, password_hash) VALUES ('meera', 'Meera', 1, '$scrypt$x')")
        conn.commit()
        report = seed.apply_seed(conn)
        self.assertFalse(report.owner_created)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM user").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT name FROM role ORDER BY id").fetchall(), [("OWNER",), ("STAFF",)])
        conn.close()

    def test_setting_rows_are_valid_json_data_keys(self) -> None:
        conn = fresh()
        seed.apply_seed(conn)
        rows = conn.execute("SELECT key, value, type FROM setting ORDER BY key").fetchall()
        self.assertEqual([r[0] for r in rows], ["catalog.units", "tax.presets"])
        for key, value, type_ in rows:
            self.assertEqual(type_, "json")
            self.assertIn(key, DATA_KEYS, "every seeded data key must be accepted by SettingsService")
            parsed = json.loads(value)
            self.assertIsInstance(parsed, list)
        presets = json.loads(rows[1][1])
        self.assertEqual(presets[0]["rate_bp"], 0, "no tax is assumed by default")
        units = json.loads(rows[0][1])
        self.assertTrue({u["base_unit"] for u in units} <= {"pcs", "g", "ml", "cm"}, "units match the product.base_unit CHECK")
        conn.close()

    def test_system_product_cannot_move_stock(self) -> None:
        conn = fresh()
        seed.apply_seed(conn)
        pid = conn.execute("SELECT id FROM product WHERE sku = ?", (seed.OPEN_ITEM_SKU,)).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id) VALUES (?, 'ADJUSTMENT', 1, 1, 1)", (pid,))
        conn.close()

    def test_secure_key_registry_contains_installer_keys(self) -> None:
        self.assertIn("ai.openai.api_key", SECURE_KEYS)
        self.assertIn("ai.huggingface.api_key", SECURE_KEYS)
        self.assertIn("payments.upi_id", SECURE_KEYS)


if __name__ == "__main__":
    unittest.main()
