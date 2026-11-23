"""Cashier day book (owner's request, 21-Nov): every bill with its items per cashier per day, 90-day retention, Owner-approved early deletion."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.events import EventBus
from vaishnavi.core.permissions import PermissionDenied
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.scheduler import jobs as jobs_module
from vaishnavi.services.billing import PaymentInput
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import reports as reports_page

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")
DAY = "2026-11-14"  # T0 in shop time


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.c = AppServices.build(self.settings, self.paths, root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=root / "config", started_at=T0)
        self.state.extra["services"] = self.c
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})
        self.c.shifts.open(OWNER, opening_float_paise=0)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, actor=STAFF, *, qty: str = "2", mode: str = "CASH"):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(actor)
        self.c.billing.add_scan(actor, cart, CAMPHOR, qty=Decimal(qty))
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(actor, cart, [PaymentInput(mode, total, tendered_paise=total if mode == "CASH" else None)])


class DayBookTest(Base):
    def test_every_bill_and_credit_note_lands_in_the_day_book_with_its_items(self) -> None:
        r1 = self._sale(STAFF, qty="2")
        r2 = self._sale(OWNER, qty="3", mode="UPI")
        self.c.billing.void(OWNER, r2.sale_id, reason="test")
        rows = self.c.cashier_records.day_book(OWNER, day=DAY)
        self.assertEqual(len(rows), 3, "two bills + one credit note")
        by_invoice = {r.invoice_no: r for r in rows}
        staff_row = by_invoice[r1.invoice_no]
        self.assertEqual((staff_row.user_id, staff_row.cashier_name, staff_row.status, staff_row.business_day, staff_row.payment_modes), (2, "Ramesh", "COMPLETED", DAY, "CASH"))
        self.assertEqual((staff_row.line_count, str(staff_row.item_qty), staff_row.total_paise), (1, "2", 2000))
        self.assertEqual([(i.name, i.qty, i.rate_paise, i.amount_paise) for i in staff_row.items], [("Camphor 50g", "2", 1000, 2000)], "the items ride along as a snapshot")
        self.assertEqual(staff_row.sold_at, "2026-11-14T16:00:00Z", "the exact timestamp of the sale")
        owner_row = by_invoice[r2.invoice_no]
        self.assertEqual((owner_row.payment_modes, owner_row.status), ("UPI", "COMPLETED"))
        notes = [r for r in rows if r.status != "COMPLETED"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].total_paise, 3000, "the credit note is recorded too, so the day reconciles")
        # idempotent: replaying the event never duplicates a bill
        self.assertIsNone(self.c.cashier_records.record(r1.sale_id))
        self.assertEqual(self._q("SELECT COUNT(*) FROM cashier_day_record")[0][0], 3)

    def test_staff_see_only_their_own_records_and_the_summary_is_per_cashier(self) -> None:
        self._sale(STAFF, qty="2")
        self._sale(STAFF, qty="1")
        self._sale(OWNER, qty="5")
        mine = self.c.cashier_records.day_book(STAFF, day=DAY, user_id=1)
        self.assertEqual({r.user_id for r in mine}, {2}, "a cashier cannot read another cashier's book even by asking")
        everyone = self.c.cashier_records.day_book(OWNER, day=DAY)
        self.assertEqual(len(everyone), 3)
        only_owner = self.c.cashier_records.day_book(OWNER, day=DAY, user_id=1)
        self.assertEqual([r.total_paise for r in only_owner], [5000])
        summary = {s.user_id: s for s in self.c.cashier_records.day_summary(OWNER, day=DAY)}
        self.assertEqual((summary[2].bills, str(summary[2].item_qty), summary[2].sales_paise, summary[2].net_paise), (2, "3", 3000, 3000))
        self.assertEqual((summary[1].bills, summary[1].sales_paise), (1, 5000))
        self.assertEqual(self.c.cashier_records.day_book(OWNER, day="2026-11-13"), [], "another day is empty")
        with self.assertRaises(ValueError):
            self.c.cashier_records.day_book(OWNER, day="not-a-day")

    def test_retention_prunes_after_90_days_and_the_owner_may_purge_earlier_with_a_reason(self) -> None:
        self._sale(STAFF)
        self.assertEqual(self.c.cashier_records.retention_days(), 90)
        status = self.c.cashier_records.status()
        self.assertEqual((status.rows, status.oldest_day, status.newest_day, status.auto_delete_before), (1, DAY, DAY, "2026-08-16"))
        self.assertEqual(self.c.cashier_records.prune(), 0, "within retention nothing goes")
        self.clock.advance(days=90)
        self.assertEqual(self.c.cashier_records.prune(), 0, "exactly 90 days old is still kept")
        self.clock.advance(days=1)
        self.assertEqual(self.c.cashier_records.prune(), 1, "day 91: the nightly job removes it")
        self.assertEqual(self.c.cashier_records.status().rows, 0)
        self.assertEqual(self._q("SELECT COUNT(*) FROM sale")[0][0], 3, "the permanent sales ledger is untouched (2 fixture bills + 1)")
        self.assertEqual(self._q("SELECT COUNT(*) FROM audit_log WHERE action = 'CASHIER_RECORDS_PRUNED'")[0][0], 1)
        # early deletion: Owner only, reason mandatory, audited
        self.clock.set(T0)
        self._sale(STAFF)
        with self.assertRaises(PermissionDenied):
            self.c.cashier_records.purge(STAFF, before_day="2026-11-15", reason="staff cannot")
        with self.assertRaises(ValueError):
            self.c.cashier_records.purge(OWNER, before_day="2026-11-15", reason="")
        self.assertEqual(self.c.cashier_records.purge(OWNER, before_day=DAY, reason="before the day itself"), 0, "the cut-off is exclusive")
        self.assertEqual(self.c.cashier_records.purge(OWNER, before_day="2026-11-15", reason="month closed"), 1)
        audit = self._q("SELECT user_id, after FROM audit_log WHERE action = 'CASHIER_RECORDS_PURGED' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(audit[0], 1)
        self.assertIn('"reason":"month closed"', audit[1])
        self.assertIn('"rows_deleted":1', audit[1])
        self.assertEqual(self.c.cashier_records.status().rows, 0)

    def test_retention_setting_is_validated_and_the_nightly_job_exists(self) -> None:
        self.c.settings_service.set_many(OWNER, {"billing.day_record_retention_days": 30})
        self.assertEqual(self.c.cashier_records.retention_days(), 30)
        from vaishnavi.core.errors import ConfigError

        with self.assertRaises(ConfigError):
            self.c.settings_service.set_many(OWNER, {"billing.day_record_retention_days": 0})
        names = {j.name: j for j in jobs_module.build_jobs(self.settings)}
        self.assertIn("day_book_prune", names)
        self.assertEqual(names["day_book_prune"].action, "day_book_prune")
        self.assertEqual(names["day_book_prune"].daily_at, "21:50", "after the nightly backup (21:30) has the rows")


class DayBookPageTest(Base):
    def test_report_lists_bills_with_items_then_subtotals_per_cashier_and_a_total(self) -> None:
        self._sale(STAFF, qty="2")
        self._sale(OWNER, qty="1", mode="UPI")
        self.assertIn("day_book", [r.key for r in reports_page.build_index(self.c, EN, STAFF).reports], "staff can open their own day book")
        table = reports_page.build_table(self.c, EN, OWNER, "day_book", {"day": DAY})
        assert table is not None
        self.assertEqual(table.title, f"Cashier day book — {DAY}")
        bills = [r for r in table.rows if r["invoice"].startswith("VPS/")]
        self.assertEqual(len(bills), 2)
        staff_bill = next(r for r in bills if r["cashier"] == "Ramesh")
        self.assertEqual((staff_bill["day"], staff_bill["time"], staff_bill["total"], staff_bill["mode"], staff_bill["status"]), (DAY, "21:30", "₹20.00", "CASH", "COMPLETED"))
        self.assertEqual(staff_bill["items_detail"], "Camphor 50g ×2 pcs @ ₹10.00 = ₹20.00")
        subtotals = [r for r in table.rows if r["invoice"].startswith("SUBTOTAL")]
        self.assertEqual({r["cashier"]: r["total"] for r in subtotals}, {"Ramesh": "₹20.00", "Owner": "₹10.00"})
        self.assertEqual(table.rows[-1]["invoice"], "TOTAL")
        self.assertEqual(table.rows[-1]["total"], "₹30.00")
        # the day filter and the cashier filter travel with the page's query string
        other_day = reports_page.build_table(self.c, EN, OWNER, "day_book", {"day": "2026-11-13"})
        assert other_day is not None
        self.assertEqual(other_day.rows, [])
        staff_only = reports_page.build_table(self.c, EN, OWNER, "day_book", {"day": DAY, "cashier": "2"})
        assert staff_only is not None
        self.assertEqual([r["cashier"] for r in staff_only.rows if r["invoice"].startswith("VPS/")], ["Ramesh"])
        staff_view = reports_page.build_table(self.c, EN, STAFF, "day_book", {"day": DAY, "cashier": "1"})
        assert staff_view is not None
        self.assertEqual({r["cashier"] for r in staff_view.rows if r["invoice"].startswith("VPS/")}, {"Ramesh"}, "staff cannot widen the filter to another cashier")
        self.assertEqual(reports_page.day_book_window({"day": "garbage"}), (DAY, DAY, None), "a bad date falls back to today")
        csv = reports_page.export_csv(self.c, OWNER, "day_book", texts=EN, params={"day": DAY})
        self.assertTrue(csv.startswith("\ufeffVaishnavi Puja Store"), "the export carries the shop letterhead")
        self.assertIn(f"Cashier day book — {DAY}", csv)
        self.assertIn("invoice_no", csv)

    def test_purge_from_the_page_needs_the_owner_password(self) -> None:
        self._sale(STAFF)
        self.c.users.reset_password(OWNER, 1, "owner-pass-77")
        denied = reports_page.purge_day_book(self.c, STAFF, before_day="2026-11-15", reason="x", password="whatever")
        self.assertFalse(denied.ok)
        wrong = reports_page.purge_day_book(self.c, OWNER, before_day="2026-11-15", reason="closing", password="nope")
        self.assertFalse(wrong.ok)
        self.assertEqual(self.c.cashier_records.status().rows, 1, "nothing deleted without the approval")
        ok = reports_page.purge_day_book(self.c, OWNER, before_day="2026-11-15", reason="closing", password="owner-pass-77")
        self.assertTrue(ok.ok)
        self.assertEqual(ok.deleted, 1)
        self.assertEqual(self.c.cashier_records.status().rows, 0)
        self.assertFalse(reports_page.purge_day_book(self.c, OWNER, before_day="2026-11-15", reason="", password="owner-pass-77").ok, "a reason is required")


class SchemaReplayTest(unittest.TestCase):
    def test_0004_creates_the_day_book_table_from_schema_sql(self) -> None:
        """Alembic is not importable here; replay the 0004 statements on a database without the table."""
        from vaishnavi.db.schema import HEAD_REVISION, apply_schema, statement_for

        self.assertEqual(HEAD_REVISION, "0004_cashier_day_record")
        conn = sqlite3.connect(":memory:")
        apply_schema(conn)
        conn.execute("DROP INDEX ix_cashier_day_record_day")
        conn.execute("DROP TABLE cashier_day_record")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("cashier_day_record", tables)
        conn.execute(statement_for("cashier_day_record"))
        conn.execute(statement_for("ix_cashier_day_record_day"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("cashier_day_record", tables)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO cashier_day_record(sale_id, invoice_no, business_day, sold_at, user_id, cashier_name, shift_id, status, total_paise, items_json) VALUES (1, 'X', '2026-11-14', '2026-11-14T10:00:00Z', 1, 'Owner', 1, 'BOGUS', 0, '[]')")


if __name__ == "__main__":
    unittest.main()
