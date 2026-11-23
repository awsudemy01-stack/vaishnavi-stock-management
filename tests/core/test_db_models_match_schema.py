"""Models ↔ schema.sql consistency (AST-based, stdlib only) and the stdlib parts of db.migrate."""

from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from vaishnavi.db.schema import apply_schema, load_statements

MODELS_DIR = Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "models"
INTERNAL_TABLES = {"_write_guard"}
TYPE_FOR = {"INTEGER": "int", "TEXT": "str", "NUMERIC": "Decimal", "BLOB": "bytes"}


def sql_tables() -> dict[str, dict[str, dict[str, object]]]:
    """table → column → {type, nullable, pk} parsed from schema.sql (same mini-parser as the generator)."""
    out: dict[str, dict[str, dict[str, object]]] = {}
    for stmt in load_statements():
        if not stmt.upper().startswith("CREATE TABLE"):
            continue
        m = re.match(r"CREATE TABLE (\w+) \((.*)\)\s*;?\s*$", stmt, re.S)
        assert m, stmt[:60]
        name, body = m.group(1), m.group(2)
        parts, depth, cur = [], 0, ""
        for ch in body:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        cols: dict[str, dict[str, object]] = {}
        table_pk: list[str] = []
        for raw in parts:
            line = " ".join(raw.split())
            if not line or line.upper().startswith(("UNIQUE", "CHECK")):
                continue
            if line.upper().startswith("PRIMARY KEY"):
                table_pk = [c.strip() for c in re.search(r"\((.*)\)", line).group(1).split(",")]  # type: ignore[union-attr]
                continue
            tokens = line.split()
            rest = " ".join(tokens[2:]).upper()
            cols[tokens[0]] = {"type": tokens[1].upper(), "pk": "PRIMARY KEY" in rest, "notnull": "NOT NULL" in rest}
        for c in table_pk:
            cols[c]["pk"] = True
        out[name] = cols
    return out


def model_tables() -> dict[str, dict[str, str]]:
    """table → column → annotation text, parsed from the model modules without importing SQLAlchemy."""
    out: dict[str, dict[str, str]] = {}
    for path in MODELS_DIR.glob("*.py"):
        if path.name in ("__init__.py", "base.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            table = None
            cols: dict[str, str] = {}
            for item in node.body:
                if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets):
                    table = ast.literal_eval(item.value)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    cols[item.target.id] = ast.unparse(item.annotation)
            assert table, f"{path.name}:{node.name} has no __tablename__"
            out[table] = cols
    return out


class ModelsMatchSchemaTest(unittest.TestCase):
    def test_every_sql_table_has_a_model_and_vice_versa(self) -> None:
        sql = set(sql_tables()) - INTERNAL_TABLES
        models = set(model_tables())
        self.assertEqual(sql - models, set(), "tables without a model")
        self.assertEqual(models - sql, set(), "models without a table")

    def test_columns_names_types_and_nullability_match(self) -> None:
        sql, models = sql_tables(), model_tables()
        for table, cols in models.items():
            with self.subTest(table=table):
                sql_cols = sql[table]
                self.assertEqual(set(cols), set(sql_cols), f"{table}: column set differs")
                for col, ann in cols.items():
                    expected_py = TYPE_FOR[str(sql_cols[col]["type"])]
                    optional = not (sql_cols[col]["notnull"] or sql_cols[col]["pk"])
                    want = f"Mapped[{expected_py} | None]" if optional else f"Mapped[{expected_py}]"
                    self.assertEqual(ann, want, f"{table}.{col}")

    def test_models_package_imports_every_module(self) -> None:
        init = (MODELS_DIR / "__init__.py").read_text(encoding="utf-8")
        for path in MODELS_DIR.glob("*.py"):
            if path.name not in ("__init__.py", "base.py"):
                self.assertIn(path.stem, init, f"models/__init__.py must import {path.stem}")


class MigrateStdlibPartsTest(unittest.TestCase):
    def test_copy_database_is_consistent_and_verify_reports_missing_tables(self) -> None:
        # These helpers are stdlib; the Alembic runner itself is exercised on the shop PC.
        src_text = (Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "migrate.py").read_text(encoding="utf-8")
        ns: dict[str, object] = {}
        # Execute only the two stdlib functions to avoid importing alembic/sqlalchemy here.
        for name in ("copy_database", "verify_schema_objects"):
            m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\Z)", src_text, re.S)
            assert m
            exec("import logging, sqlite3\nfrom pathlib import Path\nlog = logging.getLogger('t')\n" + m.group(0), ns)  # noqa: S102 (test of our own source)
        copy_database, verify_schema_objects = ns["copy_database"], ns["verify_schema_objects"]
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "data" / "vaishnavi.db"
            live.parent.mkdir()
            conn = sqlite3.connect(live)
            apply_schema(conn)
            conn.execute("INSERT INTO role(id, name) VALUES (1, 'OWNER')")
            conn.commit()
            conn.close()
            copy = Path(tmp) / "tmp" / "trial.db"
            copy_database(live, copy)  # type: ignore[operator]
            c2 = sqlite3.connect(copy)
            try:
                self.assertEqual(c2.execute("SELECT name FROM role").fetchone()[0], "OWNER")
                self.assertEqual(verify_schema_objects(copy), [])  # type: ignore[operator]
                c2.execute("DROP TABLE festival")
                c2.commit()
            finally:
                c2.close()
            self.assertEqual(verify_schema_objects(copy), ["festival"])  # type: ignore[operator]

    def test_initial_migration_file_executes_schema_statements(self) -> None:
        versions = Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "alembic" / "versions"
        text = (versions / "0001_initial.py").read_text(encoding="utf-8")
        self.assertIn('revision = "0001_initial"', text)
        self.assertIn("down_revision = None", text)
        self.assertIn("load_statements()", text)
        ast.parse(text)

    def test_head_revision_constant_matches_the_newest_migration_file(self) -> None:
        from vaishnavi.db.schema import HEAD_REVISION

        versions = Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "alembic" / "versions"
        revisions: dict[str, str | None] = {}
        for file in versions.glob("[0-9]*.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ("revision", "down_revision")}
            revisions[values["revision"]] = values["down_revision"]
        heads = set(revisions) - {d for d in revisions.values() if d}
        self.assertEqual(heads, {HEAD_REVISION}, "exactly one head, and schema.py must name it")

    def test_0002_steps_replayed_with_sqlite3_let_staff_receive_goods(self) -> None:
        """Alembic is not importable here, so replay the 0002 SQL on a 0001-shaped database and check the effect."""
        from vaishnavi.db.schema import statement_for

        text = (Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "alembic" / "versions" / "0002_staff_receive.py").read_text(encoding="utf-8")
        previous = re.search(r'_PREVIOUS = """(.*?)"""', text, re.S)
        assert previous
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "v1.db")
            try:
                apply_schema(conn)
                # Shape it like a 0001 database: old trigger, no opening_denominations column.
                conn.execute("DROP TRIGGER trg_stock_movement_validate")
                conn.execute(previous.group(1).strip())
                conn.executescript("CREATE TABLE shift_old AS SELECT id, user_id, opened_at, closed_at, opening_float_paise, expected_cash_paise, counted_cash_paise, denominations, variance_paise, closed_by_user_id, status, note FROM shift; DROP TABLE shift; ALTER TABLE shift_old RENAME TO shift;")
                conn.executescript("INSERT INTO role(id, name) VALUES (1, 'OWNER'), (2, 'STAFF'); INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (1, 'o', 'O', 1, 'x'), (2, 's', 'S', 2, 'x'); INSERT INTO product(id, sku, name, price_paise) VALUES (10, 'C', 'Camphor', 1000);")
                conn.commit()
                with self.assertRaises(sqlite3.IntegrityError) as cm:
                    conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id, created_at) VALUES (10, 'PURCHASE', 5, 5, 2, 'now')")
                self.assertIn("E-INV-010", str(cm.exception))
                conn.rollback()
                # The upgrade steps.
                conn.execute("DROP TRIGGER IF EXISTS trg_stock_movement_validate")
                conn.execute(statement_for("trg_stock_movement_validate"))
                columns = {row[1] for row in conn.execute("PRAGMA table_info(shift)").fetchall()}
                self.assertNotIn("opening_denominations", columns)
                conn.execute("ALTER TABLE shift ADD COLUMN opening_denominations TEXT")
                conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id, created_at) VALUES (10, 'PURCHASE', 5, 5, 2, 'now')")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, user_id, created_at) VALUES (10, 'ADJUSTMENT', -1, 4, 2, 'now')")
                conn.execute("INSERT INTO shift(user_id, opened_at, opening_float_paise, opening_denominations) VALUES (2, 'now', 50000, '{\"500\": 1}')")
                self.assertEqual(conn.execute("SELECT opening_denominations FROM shift").fetchone()[0], '{"500": 1}')
            finally:
                conn.close()

    def test_0003_steps_replayed_with_sqlite3_add_the_reconciliation_columns(self) -> None:
        """0003 only adds nullable columns; replay its ALTERs on a 0002-shaped database and read old rows back as NULL."""
        text = (Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "alembic" / "versions" / "0003_shift_reconcile.py").read_text(encoding="utf-8")
        self.assertIn('down_revision = "0002_staff_receive"', text)
        tree = ast.parse(text)
        columns = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "COLUMNS")
        self.assertEqual([c[0] for c in columns], ["declared_json", "approved_by_user_id"])
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "v2.db")
            try:
                apply_schema(conn)
                conn.execute("PRAGMA foreign_keys = OFF")  # the table swap below would otherwise break cash_event's FK
                conn.executescript("CREATE TABLE shift_old AS SELECT id, user_id, opened_at, closed_at, opening_float_paise, opening_denominations, expected_cash_paise, counted_cash_paise, denominations, variance_paise, closed_by_user_id, status, note FROM shift; DROP TABLE shift; ALTER TABLE shift_old RENAME TO shift;")
                conn.executescript("INSERT INTO role(id, name) VALUES (1, 'OWNER'), (2, 'STAFF'); INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (1, 'o', 'O', 1, 'x'), (2, 's', 'S', 2, 'x');")
                conn.execute("INSERT INTO shift(user_id, opened_at, opening_float_paise, status) VALUES (2, 'now', 0, 'CLOSED')")
                existing = {row[1] for row in conn.execute("PRAGMA table_info(shift)").fetchall()}
                for name, ddl in columns:
                    self.assertNotIn(name, existing)
                    conn.execute(f"ALTER TABLE shift ADD COLUMN {name} {ddl}")
                self.assertEqual(conn.execute("SELECT declared_json, approved_by_user_id FROM shift").fetchone(), (None, None), "old rows read back as NULL")
                conn.execute("UPDATE shift SET declared_json = ?, approved_by_user_id = 1 WHERE user_id = 2", ('{"UPI": {"declared": 100, "system": 100}}',))
                self.assertEqual(conn.execute("SELECT approved_by_user_id FROM shift").fetchone()[0], 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
