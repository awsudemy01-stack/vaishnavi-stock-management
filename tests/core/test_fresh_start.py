"""Fresh start (live run 4): a new database that keeps the shop's setup and drops every item, movement, bill and shift."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from vaishnavi.db.guardian import Fingerprint
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.fresh_start import KEEP_TABLES, fresh_start

from tests.core.test_db_guardian_backup import make_live_db


def _q(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class FreshStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # owner + ramesh, product 10 with stock 96, 2 sales, shift 1 open
        conn = sqlite3.connect(self.paths.database)
        try:
            conn.execute("UPDATE user SET password_hash = 'owner-hash', owner_pin_hash = 'pin-hash' WHERE id = 1")
            conn.execute("INSERT INTO setting(key, value, type, updated_at) VALUES ('shop.gstin', '09ABCDE1234F1Z5', 'str', 'now')")
            conn.execute("INSERT INTO category(name, sort_order) VALUES ('Owner Special', 999)")
            conn.commit()
        finally:
            conn.close()
        (self.paths.receipts / "2026" / "11").mkdir(parents=True)
        (self.paths.receipts / "2026" / "11" / "VPS-000001.html").write_text("<html>old bill</html>", encoding="utf-8")
        Fingerprint("old-instance", "abc", None, "2026-11-17T00:00:00Z", "0.1").save(self.paths.fingerprint)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_setup_is_kept_and_everything_else_is_cleared(self) -> None:
        old_instance = _q(self.paths.database, "SELECT instance_id FROM db_identity")[0][0]
        self.assertEqual(_q(self.paths.database, "SELECT COUNT(*) FROM sale")[0][0], 2)
        report = fresh_start(self.paths, stamp="2026-11-18_1200")
        # archive first
        archived = report.archive_dir / "vaishnavi.db"
        self.assertTrue(archived.exists())
        self.assertEqual(_q(archived, "SELECT COUNT(*) FROM sale")[0][0], 2, "the old bills live on in the archive")
        self.assertTrue((report.archive_dir / "receipts" / "2026" / "11" / "VPS-000001.html").exists())
        self.assertFalse((self.paths.receipts / "2026").exists(), "receipt archive starts empty too")
        # kept
        db = self.paths.database
        users = _q(db, "SELECT id, username, password_hash, owner_pin_hash FROM user ORDER BY id")
        self.assertEqual(users[0][1:], ("owner", "owner-hash", "pin-hash"), "owner password and PIN survive")
        self.assertEqual(users[1][1], "ramesh")
        self.assertEqual(_q(db, "SELECT value FROM setting WHERE key = 'shop.gstin'")[0][0], "09ABCDE1234F1Z5")
        names = {r[0] for r in _q(db, "SELECT name FROM category")}
        self.assertIn("Owner Special", names)
        self.assertIn("Oil & Wicks", names, "new default categories arrive with the fresh seed")
        self.assertEqual(set(report.kept), set(KEEP_TABLES))
        # cleared
        for table in ("sale", "sale_item", "payment", "stock_movement", "product_stock", "shift", "held_cart", "customer"):
            self.assertEqual(_q(db, f"SELECT COUNT(*) FROM {table}")[0][0], 0, table)  # noqa: S608
        self.assertEqual(_q(db, "SELECT COUNT(*) FROM product WHERE is_system = 0")[0][0], 0, "only the system open-item row remains")
        self.assertEqual(_q(db, "SELECT COUNT(*) FROM product WHERE is_system = 1")[0][0], 1)
        # identity + guardian
        new_instance = _q(db, "SELECT instance_id FROM db_identity")[0][0]
        self.assertNotEqual(new_instance, old_instance)
        self.assertEqual(report.new_instance_id, new_instance)
        self.assertFalse(self.paths.fingerprint.exists(), "the old fingerprint would make the guardian call the new file FOREIGN")
        self.assertEqual(_q(db, "SELECT version_num FROM alembic_version")[0][0], _q(archived, "SELECT version_num FROM alembic_version")[0][0])
        self.assertEqual(_q(db, "PRAGMA integrity_check")[0][0], "ok")
        self.assertEqual(json.loads(_q(db, "SELECT value FROM setting WHERE key = 'catalog.units'")[0][0])[0]["label"], "pc", "seeded reference settings present")

    def test_refuses_without_a_database_and_never_overwrites_an_archive(self) -> None:
        fresh_start(self.paths, stamp="same")
        with self.assertRaises(FileExistsError):
            fresh_start(self.paths, stamp="same")
        self.paths.database.unlink()
        with self.assertRaises(FileNotFoundError):
            fresh_start(self.paths)


if __name__ == "__main__":
    unittest.main()
