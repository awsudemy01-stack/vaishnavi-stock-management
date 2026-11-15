"""core.ids — ULID, internal EAN-13, GS1 check digits, invoice numbers (stdlib only)."""

from __future__ import annotations

import doctest
import threading
import unittest
from datetime import date

from vaishnavi.core import ids
from vaishnavi.core.ids import (
    BarcodeError,
    financial_year,
    format_invoice,
    gs1_check_digit,
    internal_barcode,
    is_internal_barcode,
    is_ulid,
    is_valid_gs1,
    new_ulid,
    normalise_scan,
    parse_internal_barcode,
    parse_invoice,
    ulid_timestamp_ms,
)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, ignore: object) -> unittest.TestSuite:
    tests.addTests(doctest.DocTestSuite(ids))
    return tests


class UlidTest(unittest.TestCase):
    def test_shape_and_timestamp(self) -> None:
        u = new_ulid(1_700_000_000_000)
        self.assertTrue(is_ulid(u))
        self.assertEqual(len(u), 26)
        self.assertEqual(ulid_timestamp_ms(u), 1_700_000_000_000)
        self.assertFalse(is_ulid("not-a-ulid"))
        self.assertFalse(is_ulid("01ARZ3NDEKTSV4RRFFQ69G5FAI"))  # I is not in Crockford
        with self.assertRaises(ValueError):
            ulid_timestamp_ms("x")

    def test_monotonic_within_same_millisecond(self) -> None:
        a, b, c = (new_ulid(42) for _ in range(3))
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertEqual(a[:10], b[:10])

    def test_unique_under_threads(self) -> None:
        seen: set[str] = set()
        lock = threading.Lock()

        def work() -> None:
            local = [new_ulid() for _ in range(500)]
            with lock:
                seen.update(local)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seen), 4000)

    def test_sorts_by_time(self) -> None:
        self.assertLess(new_ulid(1000), new_ulid(2000))


class BarcodeTest(unittest.TestCase):
    def test_check_digit_known_codes(self) -> None:
        # Real-world codes: Coca-Cola can (EAN-13), a UPC-A, an EAN-8.
        self.assertTrue(is_valid_gs1("5449000000996"))
        self.assertTrue(is_valid_gs1("036000291452"))
        self.assertTrue(is_valid_gs1("96385074"))
        self.assertFalse(is_valid_gs1("5449000000997"))
        self.assertFalse(is_valid_gs1("544900000099"))
        self.assertFalse(is_valid_gs1("ABCDEFGHIJKLM"))
        with self.assertRaises(BarcodeError):
            gs1_check_digit("12a")

    def test_internal_roundtrip_for_many_ids(self) -> None:
        for pid in (1, 7, 123, 99999, 10**6, 9_999_999_999):
            code = internal_barcode(pid)
            self.assertEqual(len(code), 13)
            self.assertTrue(code.startswith("29"))
            self.assertTrue(is_valid_gs1(code))
            self.assertTrue(is_internal_barcode(code))
            self.assertEqual(parse_internal_barcode(code), pid)

    def test_internal_rejects_bad_ids_and_foreign_codes(self) -> None:
        for bad in (0, -1, 10**10, True):
            with self.subTest(bad=bad), self.assertRaises(BarcodeError):
                internal_barcode(bad)  # type: ignore[arg-type]
        self.assertFalse(is_internal_barcode("5449000000996"))  # retail code
        self.assertFalse(is_internal_barcode("2900000001234"))  # wrong check digit
        with self.assertRaises(BarcodeError):
            parse_internal_barcode("5449000000996")

    def test_internal_codes_are_unique(self) -> None:
        codes = {internal_barcode(i) for i in range(1, 5000)}
        self.assertEqual(len(codes), 4999)

    def test_normalise_scan(self) -> None:
        self.assertEqual(normalise_scan("  2900000001237\r\n"), "2900000001237")
        self.assertEqual(normalise_scan("~5449000000996"), "5449000000996")
        self.assertEqual(normalise_scan("036000291452"), "0036000291452", "UPC-A becomes EAN-13")
        self.assertEqual(normalise_scan("CAMPHOR-50"), "CAMPHOR-50")
        self.assertEqual(normalise_scan(""), "")


class InvoiceTest(unittest.TestCase):
    def test_financial_year_boundaries(self) -> None:
        self.assertEqual(financial_year(date(2025, 4, 1)), "25-26")
        self.assertEqual(financial_year(date(2026, 3, 31)), "25-26")
        self.assertEqual(financial_year(date(2026, 4, 1)), "26-27")
        self.assertEqual(financial_year(date(2099, 12, 31)), "99-00")

    def test_format_parse_roundtrip(self) -> None:
        text = format_invoice(1, "25-26")
        self.assertEqual(text, "VPS/25-26/000001")
        parsed = parse_invoice(text)
        self.assertEqual((parsed.prefix, parsed.fy, parsed.sequence), ("VPS", "25-26", 1))
        self.assertEqual(str(parsed), text)
        self.assertEqual(parse_invoice(" vps/25-26/1234567 ").sequence, 1234567)
        with self.assertRaises(ValueError):
            format_invoice(0, "25-26")
        for bad in ("VPS-25-26-1", "VPS/2025/000001", "", "VPS/25-26/12"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_invoice(bad)


if __name__ == "__main__":
    unittest.main()
