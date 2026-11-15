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
        text = (Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "db" / "alembic" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
        self.assertIn('revision = "0001_initial"', text)
        self.assertIn("down_revision = None", text)
        self.assertIn("load_statements()", text)
        ast.parse(text)


if __name__ == "__main__":
    unittest.main()
