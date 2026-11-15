"""core.errors — error hierarchy, codes, EN/HI message catalogue (stdlib only).

Also the project-wide guard that every ``E-…`` code mentioned anywhere (docs, installer, app) has a
plain-language text in both languages.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from vaishnavi.core import errors
from vaishnavi.core.errors import (
    AREAS,
    AuthError,
    BillingError,
    Catalogue,
    DatabaseError,
    Severity,
    VaishnaviError,
    catalogue,
    describe,
    error_for,
)

REPO = Path(__file__).resolve().parents[2]
CODE_RE = re.compile(r"E-[A-Z]+-\d{3}")


def _codes_in_repo() -> set[str]:
    files = [*REPO.glob("docs/*.md"), *REPO.glob("bootstrap/**/*.py"), *REPO.glob("bootstrap/messages/*.toml"), *REPO.glob("src/**/*.py")]
    found: set[str] = set()
    for p in files:
        found.update(CODE_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
    return found


class CatalogueTest(unittest.TestCase):
    def test_en_and_hi_define_identical_code_sets(self) -> None:
        en, hi = catalogue.codes("en"), catalogue.codes("hi")
        self.assertEqual(hi - en, set(), "hi.toml has codes en.toml lacks")
        self.assertEqual(en - hi, set(), "codes missing a Hindi text")
        self.assertGreater(len(en), 80)

    def test_every_code_used_anywhere_has_a_message(self) -> None:
        missing = _codes_in_repo() - catalogue.codes("en")
        self.assertEqual(missing, set(), "codes referenced in docs/code without an owner-facing text")

    def test_every_message_code_belongs_to_a_known_area(self) -> None:
        for code in catalogue.codes("en"):
            area = code.split("-")[1]
            self.assertIn(area, AREAS, f"{code}: area {area} has no error class")

    def test_hindi_texts_are_really_hindi(self) -> None:
        texts = [m.msg for m in (catalogue.get(c, "hi") for c in catalogue.codes("hi")) if m]
        devanagari = sum(1 for t in texts if any("\u0900" <= ch <= "\u097f" for ch in t))
        self.assertGreater(devanagari, len(texts) * 0.9)
        for lang in ("en", "hi"):
            raw = (REPO / "src" / "vaishnavi" / "core" / "messages" / f"{lang}.toml").read_text(encoding="utf-8")
            self.assertNotIn("\ufffd", raw)
            self.assertNotRegex(raw, r"\?\?", lang)

    def test_fallback_and_missing(self) -> None:
        self.assertIsNotNone(catalogue.get("E-SYS-099", "fr"))  # unknown lang → English
        self.assertIsNone(catalogue.get("E-ZZZ-999"))
        self.assertEqual(catalogue.render("E-ZZZ-999", "en", {}), ("E-ZZZ-999", ""))

    def test_damaged_file_does_not_crash(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "en.toml").write_text("[errors\nbroken", encoding="utf-8")
            c = Catalogue(Path(tmp))
            self.assertEqual(c.codes(), set())
            self.assertIsNone(c.get("E-SYS-001"))


class ErrorClassesTest(unittest.TestCase):
    def test_technical_and_user_forms(self) -> None:
        err = BillingError("E-BILL-050", short="₹12.00", detail="tendered=988 total=2188")
        self.assertEqual(str(err), "E-BILL-050 short='₹12.00' tendered=988 total=2188")
        self.assertEqual(err.user_message(), "Payment is less than the bill total by ₹12.00")
        self.assertIn("बिल", err.user_message("hi"))
        self.assertTrue(err.what_to_do())
        self.assertEqual(err.area, "BILL")
        self.assertEqual(err.severity, Severity.ERROR)

    def test_default_code_and_cause(self) -> None:
        try:
            raise DatabaseError(cause=OSError("disk"))
        except DatabaseError as err:
            self.assertEqual(err.code, "E-DB-003")
            self.assertIsInstance(err.__cause__, OSError)
            self.assertEqual(err.severity, Severity.FATAL)

    def test_code_must_match_area_and_shape(self) -> None:
        with self.assertRaises(ValueError):
            AuthError("E-BILL-050")
        with self.assertRaises(ValueError):
            AuthError("AUTH-10")

    def test_as_problem_hides_detail_unless_asked(self) -> None:
        err = AuthError("E-AUTH-011", permission="VOID_REFUND", detail="user=7")
        p = err.as_problem("en")
        self.assertEqual(p["code"], "E-AUTH-011")
        self.assertIn("VOID_REFUND", p["message"])
        self.assertNotIn("detail", p)
        self.assertEqual(err.as_problem(include_detail=True)["detail"], "user=7")

    def test_error_for_builds_the_right_class(self) -> None:
        self.assertIsInstance(error_for("E-DB-021"), DatabaseError)
        self.assertIsInstance(error_for("E-SYS-040"), errors.SystemError_)
        self.assertIsInstance(error_for("E-GRD-002"), errors.GuardrailError)
        with self.assertRaises(ValueError):
            error_for("E-NOPE-001")
        with self.assertRaises(ValueError):
            error_for("garbage")

    def test_bad_placeholder_never_raises(self) -> None:
        err = BillingError("E-BILL-050")  # missing {short}
        self.assertIn("{short}", err.user_message())

    def test_describe(self) -> None:
        d = describe("E-SCN-002", "hi", {"code": "123"})
        self.assertEqual(d["code"], "E-SCN-002")
        self.assertIn("123", d["message"])

    def test_placeholder_named_code_does_not_clash_with_the_error_code(self) -> None:
        err = errors.ScannerError("E-SCN-002", code="5449000000996")
        self.assertEqual(err.code, "E-SCN-002")
        self.assertIn("5449000000996", err.user_message())

    def test_every_area_class_default_code_is_in_catalogue(self) -> None:
        for cls in AREAS.values():
            self.assertIn(cls.default_code, catalogue.codes(), cls.__name__)
            self.assertTrue(issubclass(cls, VaishnaviError))


if __name__ == "__main__":
    unittest.main()
