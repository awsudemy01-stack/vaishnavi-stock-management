"""Unit 36 — TaxEngine (docs/05 §2, §8a) and measured quantities (docs/05 §8d). Pure functions, hand-computed fixtures."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from vaishnavi.core.clock import financial_year
from vaishnavi.core.errors import BillingError
from vaishnavi.services.measure import (
    QuantitySpec,
    ScaleCodeFormat,
    decode_scale_barcode,
    format_qty,
    line_amount,
    qty_for_amount,
    round_to_step,
    validate_qty,
)
from vaishnavi.services.tax import BillInput, LineInput, TaxEngine


def D(text: str) -> Decimal:  # noqa: N802 - terse fixture helper
    return Decimal(text)


PIECE = QuantitySpec(sale_mode="PIECE", base_unit="pcs", pricing_unit_qty=D("1"), pricing_unit_label="pc", min_qty=D("1"), qty_step=D("1"), presets=(), allow_decimal=False)
GHEE = QuantitySpec(sale_mode="WEIGHT", base_unit="g", pricing_unit_qty=D("100"), pricing_unit_label="100 g", min_qty=D("50"), qty_step=D("10"), presets=(D("50"), D("100"), D("250"), D("500"), D("1000")), allow_decimal=True)
OIL = QuantitySpec(sale_mode="VOLUME", base_unit="ml", pricing_unit_qty=D("100"), pricing_unit_label="100 ml", min_qty=D("50"), qty_step=D("50"), presets=(D("100"), D("200"), D("500")), allow_decimal=True)


class FinancialYearTest(unittest.TestCase):
    def test_indian_fy_runs_april_to_march(self) -> None:
        self.assertEqual(financial_year(date(2026, 11, 14)), "26-27")
        self.assertEqual(financial_year(date(2027, 3, 31)), "26-27")
        self.assertEqual(financial_year(date(2027, 4, 1)), "27-28")
        self.assertEqual(financial_year(date(2030, 1, 5)), "29-30")


class LineTaxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = TaxEngine(enabled=True)

    def test_docs_example_ten_rupees_one_percent_offer_is_nine_ninety(self) -> None:
        line = self.engine.line(LineInput(shelf_price_paise=1000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True, offer_discount_bp=100))
        self.assertEqual((line.gross_paise, line.discount_paise, line.line_total_paise), (1000, 10, 990))
        self.assertEqual(line.unit_price_paise, 990, "effective unit price after discount")
        self.assertEqual((line.taxable_paise, line.cgst_paise, line.sgst_paise), (990, 0, 0), "nil-rated puja item")

    def test_inclusive_twelve_percent_splits_like_core_money_with_odd_paisa_to_cgst(self) -> None:
        line = self.engine.line(LineInput(shelf_price_paise=1000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=1200, price_includes_tax=True))
        self.assertEqual((line.taxable_paise, line.cgst_paise, line.sgst_paise, line.line_total_paise), (893, 54, 53, 1000))

    def test_exclusive_price_adds_tax_on_top(self) -> None:
        line = self.engine.line(LineInput(shelf_price_paise=893, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=1200, price_includes_tax=False))
        self.assertEqual((line.taxable_paise, line.cgst_paise + line.sgst_paise, line.line_total_paise), (893, 107, 1000))

    def test_weighed_items_price_per_pricing_unit(self) -> None:
        ghee = self.engine.line(LineInput(shelf_price_paise=6000, qty=D("100"), pricing_unit_qty=D("100"), tax_rate_bp=0, price_includes_tax=True))
        self.assertEqual(ghee.line_total_paise, 6000, "100 g of ghee at ₹60/100 g")
        oil = self.engine.line(LineInput(shelf_price_paise=4500, qty=D("200"), pricing_unit_qty=D("100"), tax_rate_bp=0, price_includes_tax=True))
        self.assertEqual(oil.line_total_paise, 9000, "200 ml of oil at ₹45/100 ml")
        odd = self.engine.line(LineInput(shelf_price_paise=6000, qty=D("125.5"), pricing_unit_qty=D("100"), tax_rate_bp=0, price_includes_tax=True))
        self.assertEqual(odd.gross_paise, 7530)

    def test_adhoc_and_offer_discounts_stack_only_when_allowed(self) -> None:
        both = self.engine.line(LineInput(shelf_price_paise=10000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True, offer_discount_bp=1000, adhoc_discount_bp=500, offer_stackable=True))
        self.assertEqual(both.discount_paise, 1500)
        best = self.engine.line(LineInput(shelf_price_paise=10000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True, offer_discount_bp=1000, adhoc_discount_bp=500, offer_stackable=False))
        self.assertEqual((best.discount_paise, best.applied), (1000, "OFFER"), "the higher benefit wins and is named")
        adhoc_wins = self.engine.line(LineInput(shelf_price_paise=10000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True, offer_discount_bp=100, adhoc_discount_paise=500, offer_stackable=False))
        self.assertEqual((adhoc_wins.discount_paise, adhoc_wins.applied), (500, "ADHOC"))
        capped = self.engine.line(LineInput(shelf_price_paise=1000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True, adhoc_discount_paise=5000))
        self.assertEqual(capped.discount_paise, 1000, "a discount can never exceed the gross")

    def test_tax_disabled_zeroes_every_rate(self) -> None:
        line = TaxEngine(enabled=False).line(LineInput(shelf_price_paise=1000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=1200, price_includes_tax=True))
        self.assertEqual((line.tax_rate_bp, line.taxable_paise, line.cgst_paise, line.sgst_paise), (0, 1000, 0, 0))


class BillTaxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = TaxEngine(enabled=True)

    def _lines(self) -> list[LineInput]:
        return [
            LineInput(shelf_price_paise=6000, qty=D("2"), pricing_unit_qty=D("1"), tax_rate_bp=500, price_includes_tax=True, offer_discount_bp=100, offer_name="Diwali Offer"),  # 120.00 → -1.20
            LineInput(shelf_price_paise=25000, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=500, price_includes_tax=True),  # 250.00
            LineInput(shelf_price_paise=4000, qty=D("150"), pricing_unit_qty=D("100"), tax_rate_bp=0, price_includes_tax=True),  # 60.00 nil-rated
            LineInput(shelf_price_paise=3500, qty=D("1"), pricing_unit_qty=D("1"), tax_rate_bp=0, price_includes_tax=True),  # 35.00 nil-rated
        ]

    def test_docs_receipt_example_totals(self) -> None:
        bill = self.engine.bill(BillInput(lines=self._lines(), bill_discount_paise=1000))
        self.assertEqual(bill.subtotal_paise, 46500, "Σ shelf price × qty")
        self.assertEqual(bill.item_discount_paise, 120)
        self.assertEqual(bill.bill_discount_paise, 1000)
        self.assertEqual(bill.total_savings_paise, 1120, "'You saved ₹11.20 today'")
        self.assertEqual(sum(ln.bill_share_paise for ln in bill.lines), 1000, "bill discount pro-rated without losing a paisa")
        self.assertEqual(bill.taxable_paise + bill.cgst_paise + bill.sgst_paise + bill.roundoff_paise, bill.total_paise)
        self.assertEqual(bill.total_paise % 100, 0, "rounded to the rupee")
        self.assertEqual(bill.total_paise, 45400, "465.00 − 1.20 − 10.00 = 453.80 → ₹454")
        self.assertEqual(bill.roundoff_paise, 20)
        groups = {g.rate_bp: g for g in bill.tax_groups}
        self.assertEqual(set(groups), {0, 500})
        five = groups[500]
        self.assertEqual(five.taxable_paise + five.cgst_paise + five.sgst_paise, sum(ln.line_total_paise for ln in bill.lines if ln.tax_rate_bp == 500))
        self.assertEqual(groups[0].cgst_paise, 0)

    def test_bill_discount_percent_and_zero_rounding_modes(self) -> None:
        pct = self.engine.bill(BillInput(lines=self._lines(), bill_discount_bp=1000))
        self.assertEqual(pct.bill_discount_paise, 4638, "10 % of (465.00 − 1.20) = 46.38")
        none = TaxEngine(enabled=True, rounding="NONE").bill(BillInput(lines=self._lines()))
        self.assertEqual(none.roundoff_paise, 0)
        self.assertEqual(none.total_paise, 46380)

    def test_empty_bill_is_rejected_and_discount_cannot_exceed_net(self) -> None:
        with self.assertRaises(BillingError):
            self.engine.bill(BillInput(lines=[]))
        bill = self.engine.bill(BillInput(lines=self._lines(), bill_discount_paise=999999))
        self.assertEqual(bill.total_paise, 0)
        self.assertEqual(bill.bill_discount_paise, 46380)


class MeasureTest(unittest.TestCase):
    def test_piece_items_need_whole_numbers(self) -> None:
        self.assertEqual(validate_qty(PIECE, D("2")), D("2"))
        with self.assertRaises(BillingError) as cm:
            validate_qty(PIECE, D("1.5"))
        self.assertEqual(cm.exception.code, "E-MEAS-003")
        with self.assertRaises(BillingError) as cm:
            validate_qty(PIECE, D("0"))
        self.assertEqual(cm.exception.code, "E-MEAS-001")

    def test_weight_min_and_step(self) -> None:
        self.assertEqual(validate_qty(GHEE, D("250")), D("250"))
        with self.assertRaises(BillingError) as cm:
            validate_qty(GHEE, D("30"))
        self.assertEqual(cm.exception.code, "E-MEAS-001", "below the 50 g minimum")
        with self.assertRaises(BillingError) as cm:
            validate_qty(GHEE, D("125"))
        self.assertEqual(cm.exception.code, "E-MEAS-002", "not a multiple of the 10 g step")
        self.assertEqual(round_to_step(GHEE, D("125")), D("130"), "half-up to the step")
        self.assertEqual(round_to_step(GHEE, D("124")), D("120"))

    def test_by_amount_rounds_to_step(self) -> None:
        self.assertEqual(qty_for_amount(GHEE, amount_paise=5000, price_paise=6000), D("80"), "₹50 of ghee at ₹60/100 g = 83.3 g → 80 g")
        self.assertEqual(qty_for_amount(OIL, amount_paise=10000, price_paise=4500), D("200"), "₹100 of oil at ₹45/100 ml = 222 ml → 200 ml (50 ml step)")
        self.assertEqual(qty_for_amount(GHEE, amount_paise=100, price_paise=6000), D("50"), "never below the minimum")

    def test_line_amount_and_formatting(self) -> None:
        self.assertEqual(line_amount(GHEE, price_paise=6000, qty=D("100")), 6000)
        self.assertEqual(line_amount(OIL, price_paise=4500, qty=D("200")), 9000)
        self.assertEqual(line_amount(PIECE, price_paise=999, qty=D("3")), 2997)
        self.assertEqual(format_qty(GHEE, D("1000")), "1 kg")
        self.assertEqual(format_qty(GHEE, D("250")), "250 g")
        self.assertEqual(format_qty(OIL, D("1500")), "1.5 L")
        self.assertEqual(format_qty(PIECE, D("2")), "2 pcs")
        self.assertEqual(format_qty(PIECE, D("1")), "1 pc")

    def test_scale_barcode_weight_and_price_formats(self) -> None:
        fmt = ScaleCodeFormat.parse("21PPPPPWWWWWC")
        hit = decode_scale_barcode("2100042012509", fmt)  # PLU 00042, weight 01250 g, check 9
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual((hit.plu, hit.weight_base_units, hit.amount_paise), (42, D("1250"), None))
        price_fmt = ScaleCodeFormat.parse("22PPPPPAAAAAC")
        hit2 = decode_scale_barcode("2200042060002", price_fmt)  # amount 06000 paise = ₹60
        assert hit2 is not None
        self.assertEqual((hit2.plu, hit2.amount_paise), (42, 6000))
        self.assertIsNone(decode_scale_barcode("8901234567890", fmt), "a manufacturer code is not a scale code")
        self.assertIsNone(decode_scale_barcode("2100042012506", fmt), "bad check digit")
        with self.assertRaises(ValueError):
            ScaleCodeFormat.parse("21PPPPPWWWWW")  # must be 13 chars with a check digit


if __name__ == "__main__":
    unittest.main()
