"""core.money — integer paise arithmetic, GST splits, formatting, allocation (stdlib only)."""

from __future__ import annotations

import doctest
import unittest
from decimal import Decimal

from vaishnavi.core import money
from vaishnavi.core.money import (
    MoneyError,
    TaxSplit,
    add_exclusive_tax,
    allocate,
    apply_percent,
    format_inr,
    from_paise,
    multiply,
    parse_inr,
    rate_to_bp,
    round_off,
    split_inclusive_tax,
    to_paise,
)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, ignore: object) -> unittest.TestSuite:
    """Also run the docstring examples — they are the module's executable specification."""
    tests.addTests(doctest.DocTestSuite(money))
    return tests


class ConversionTest(unittest.TestCase):
    def test_to_paise_half_up_and_types(self) -> None:
        self.assertEqual(to_paise("0.005"), 1)  # half-up, not banker's
        self.assertEqual(to_paise("0.015"), 2)
        self.assertEqual(to_paise("0.025"), 3)
        self.assertEqual(to_paise(Decimal("2.675")), 268)
        self.assertEqual(to_paise(7), 700)
        self.assertEqual(to_paise("-1.50"), -150)

    def test_to_paise_rejects_garbage(self) -> None:
        for bad in ("abc", "", "1.2.3", "NaN", "Infinity", True):
            with self.subTest(bad=bad), self.assertRaises(MoneyError):
                to_paise(bad)  # type: ignore[arg-type]

    def test_roundtrip(self) -> None:
        for p in (0, 1, 99, 100, 123456789, -5):
            self.assertEqual(to_paise(from_paise(p)), p)

    def test_parse_inr_forms(self) -> None:
        self.assertEqual(parse_inr("10"), 1000)
        self.assertEqual(parse_inr("Rs. 10"), 1000)
        self.assertEqual(parse_inr("INR 2,50,000.75"), 25000075)
        self.assertEqual(parse_inr(" ₹9.99 "), 999)
        for bad in ("", "₹", "-", "ten"):
            with self.subTest(bad=bad), self.assertRaises(MoneyError):
                parse_inr(bad)

    def test_format_indian_grouping(self) -> None:
        self.assertEqual(format_inr(0), "₹0.00")
        self.assertEqual(format_inr(999), "₹9.99")
        self.assertEqual(format_inr(100000), "₹1,000.00")
        self.assertEqual(format_inr(10000000), "₹1,00,000.00")
        self.assertEqual(format_inr(123456789012), "₹1,23,45,67,890.12")
        self.assertEqual(format_inr(-100050), "-₹1,000.50")
        self.assertEqual(format_inr(100050, symbol=False, grouping=False), "1000.50")


class ArithmeticTest(unittest.TestCase):
    def test_multiply_fractional_quantities_for_weighed_items(self) -> None:
        self.assertEqual(multiply(45000, "0.1"), 4500)  # 100 g of ghee at ₹450/kg
        self.assertEqual(multiply(18000, "0.2"), 3600)  # 200 ml of oil at ₹180/l
        self.assertEqual(multiply(333, 3), 999)
        self.assertEqual(multiply(1, "0.5"), 1)  # half-up
        self.assertEqual(multiply(999, 0), 0)
        with self.assertRaises(MoneyError):
            multiply(100, "lots")

    def test_no_float_drift(self) -> None:
        total = sum(multiply(999, 1) for _ in range(1000))
        self.assertEqual(total, 999000)

    def test_apply_percent(self) -> None:
        self.assertEqual(apply_percent(1000, 1), 10)  # owner's example: ₹10 shelf, 1 % → ₹0.10 off
        self.assertEqual(apply_percent(1000, "12.5"), 125)
        self.assertEqual(apply_percent(5, 10), 1)  # 0.5 paisa → 1 (half-up)
        self.assertEqual(apply_percent(0, 50), 0)


class TaxTest(unittest.TestCase):
    def test_rate_to_bp(self) -> None:
        self.assertEqual(rate_to_bp(12), 1200)
        self.assertEqual(rate_to_bp("0.25"), 25)
        self.assertEqual(rate_to_bp(Decimal("18")), 1800)
        with self.assertRaises(MoneyError):
            rate_to_bp(101)

    def test_inclusive_split_sums_exactly_for_all_rates(self) -> None:
        for rate in (0, 25, 300, 500, 1200, 1800, 2800):
            for gross in (1, 7, 99, 100, 1000, 12345, 999999):
                with self.subTest(rate=rate, gross=gross):
                    s = split_inclusive_tax(gross, rate)
                    self.assertEqual(s.net + s.tax, gross)
                    self.assertGreaterEqual(s.tax, 0)
                    self.assertEqual(s.rate_bp, rate)

    def test_known_values(self) -> None:
        self.assertEqual(split_inclusive_tax(11800, 1800), TaxSplit(10000, 1800, 11800, 1800))
        self.assertEqual(split_inclusive_tax(10500, 500), TaxSplit(10000, 500, 10500, 500))
        self.assertEqual(add_exclusive_tax(10000, 1800), TaxSplit(10000, 1800, 11800, 1800))
        self.assertEqual(add_exclusive_tax(10000, 0), TaxSplit(10000, 0, 10000, 0))

    def test_halves_cgst_sgst(self) -> None:
        self.assertEqual(TaxSplit(0, 100, 100, 1200).halves(), (50, 50))
        self.assertEqual(TaxSplit(0, 101, 101, 1200).halves(), (51, 50))
        self.assertEqual(TaxSplit(0, 0, 0, 0).halves(), (0, 0))
        self.assertEqual(TaxSplit(0, 0, 0, 1200).rate_percent, Decimal("12"))


class BillHelpersTest(unittest.TestCase):
    def test_round_off(self) -> None:
        self.assertEqual(round_off(0), (0, 0))
        self.assertEqual(round_off(49), (0, -49))
        self.assertEqual(round_off(50), (100, 50))
        self.assertEqual(round_off(-150), (-200, -50))  # refund totals round the same way (half-up away from zero)

    def test_allocate_exact_and_fair(self) -> None:
        self.assertEqual(sum(allocate(999, [1, 2, 3, 4])), 999)
        self.assertEqual(allocate(10, [1, 1, 1]), [4, 3, 3])
        self.assertEqual(allocate(0, [5, 5]), [0, 0])
        self.assertEqual(allocate(7, [0, 0]), [7, 0])
        self.assertEqual(allocate(100, [100]), [100])
        with self.assertRaises(MoneyError):
            allocate(10, [])
        with self.assertRaises(MoneyError):
            allocate(10, [1, -1])

    def test_allocate_is_deterministic_on_ties(self) -> None:
        self.assertEqual(allocate(5, [2, 2, 2, 2]), allocate(5, [2, 2, 2, 2]))
        self.assertEqual(sum(allocate(5, [2, 2, 2, 2])), 5)


if __name__ == "__main__":
    unittest.main()
