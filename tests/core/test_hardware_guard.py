"""Unit 39 — HardwareGuard: scanner mandatory / printer optional, time-boxed Owner overrides, hot-plug poll (docs/05 §10)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import BillingError, PrinterError, ScannerError
from vaishnavi.core.events import EventBus, HardwareStatusChanged
from vaishnavi.core.permissions import Actor, PinRequired, Role
from vaishnavi.core.settings import HardwareSettings, Settings
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteHardwareStore, SqliteTxn
from vaishnavi.platform.printing import PrinterStatus
from vaishnavi.services.audit import AuditService
from vaishnavi.services.hardware_guard import HardwareGuard

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)
STAFF_PIN = Actor(user_id=2, role=Role.STAFF, pin_verified=True)


class FakeProbes:
    def __init__(self) -> None:
        self.scanner_present: bool | None = True
        self.printer = PrinterStatus("none", "", False, "not set up")
        self.discovered: list[str] = []

    def scanner(self, hw: HardwareSettings) -> bool | None:
        return None if not hw.scanner_vid_pid else self.scanner_present

    def printer_status(self, hw: HardwareSettings) -> PrinterStatus:
        return self.printer

    def discover(self) -> list[str]:
        return list(self.discovered)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = root / "v.db"
        make_live_db(self.db)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.events: list[HardwareStatusChanged] = []
        self.bus.subscribe(HardwareStatusChanged, self.events.append)
        self.txn = SqliteTxn(self.db)
        self.audit = AuditService(SqliteAuditStore())
        self.settings = Settings()
        self.settings.hardware.scanner_vid_pid = "0C2E:0B61"
        self.probes = FakeProbes()
        self.guard = HardwareGuard(self.txn, SqliteHardwareStore(), self.audit, settings=lambda: self.settings, scanner_present=self.probes.scanner, printer_status=self.probes.printer_status, discover_printers=self.probes.discover, event_bus=self.bus)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class GuardTest(Base):
    def test_state_reflects_probes_and_publishes_changes_once(self) -> None:
        st = self.guard.refresh()
        self.assertEqual((st.scanner_paired, st.scanner_present, st.printer_configured, st.printer_ready), (True, True, False, False))
        self.assertEqual(self.events, [], "first probe is the baseline — no 'changed' event")
        self.probes.scanner_present = False
        self.guard.refresh()
        self.guard.refresh()
        self.assertEqual([(e.device, e.present) for e in self.events], [("scanner", False)], "one event per change, not per poll")
        self.probes.scanner_present = True
        self.guard.refresh()
        self.assertEqual([(e.device, e.present) for e in self.events][-1], ("scanner", True))

    def test_staff_need_a_present_scanner_and_may_not_type_codes(self) -> None:
        self.guard.refresh()
        self.guard.can_add_line(STAFF, via_scan=True)
        with self.assertRaises(BillingError) as cm:
            self.guard.can_add_line(STAFF, via_scan=False)
        self.assertEqual(cm.exception.code, "E-BILL-070", "typed input into the scan box is rejected for a cashier")
        self.guard.can_add_line(OWNER, via_scan=False)
        self.probes.scanner_present = False
        self.guard.refresh()
        with self.assertRaises(ScannerError) as cm:
            self.guard.can_add_line(STAFF, via_scan=True)
        self.assertEqual(cm.exception.code, "E-SCN-001")
        with self.assertRaises(ScannerError):
            self.guard.can_checkout(STAFF, None)
        self.guard.can_checkout(OWNER, None)
        self.settings.hardware.scanner_vid_pid = ""
        self.guard.refresh()
        with self.assertRaises(ScannerError):
            self.guard.can_add_line(STAFF, via_scan=True)
        self.assertEqual(self.guard.state().scanner_paired, False, "no scanner paired → staff blocked until the owner pairs one")
        self.settings.billing.require_scan = False
        self.guard.can_add_line(STAFF, via_scan=False)
        self.guard.can_checkout(STAFF, None)

    def test_printer_is_optional_unless_require_printer_and_enabling_it_needs_a_ready_printer(self) -> None:
        self.guard.refresh()
        self.guard.can_checkout(STAFF, None)
        self.settings.billing.require_printer = True
        with self.assertRaises(PrinterError) as cm:
            self.guard.can_checkout(STAFF, None)
        self.assertEqual(cm.exception.code, "E-PRN-002")
        with self.assertRaises(PrinterError):
            self.guard.can_checkout(OWNER, None)
        self.assertFalse(self.guard.may_enable_require_printer(), "refuse to switch strictness on without a ready printer")
        self.settings.hardware.printer_kind = "usb"
        self.settings.hardware.printer_address = "04B8:0202"
        self.probes.printer = PrinterStatus("usb", "04B8:0202", True, "ready")
        self.guard.refresh()
        self.assertTrue(self.guard.may_enable_require_printer())
        self.guard.can_checkout(STAFF, None)

    def test_time_boxed_overrides_are_owner_or_pin_audited_and_expire(self) -> None:
        self.probes.scanner_present = False
        self.guard.refresh()
        with self.assertRaises(PinRequired):
            self.guard.start_override(STAFF, "SCANNER", minutes=30, reason="scanner broke")
        rec = self.guard.start_override(STAFF_PIN, "SCANNER", minutes=30, reason="scanner broke", approver_id=1)
        self.assertEqual((rec.kind, rec.owner_user_id, rec.reason), ("SCANNER", 1, "scanner broke"))
        self.guard.can_add_line(STAFF, via_scan=False)
        self.guard.can_checkout(STAFF, None)
        self.assertEqual(self.guard.active_override("SCANNER").id, rec.id)  # type: ignore[union-attr]
        self.clock.advance(minutes=31)
        self.assertIsNone(self.guard.active_override("SCANNER"), "expired")
        with self.assertRaises(ScannerError):
            self.guard.can_add_line(STAFF, via_scan=True)
        self.probes.scanner_present = True
        self.guard.refresh()
        self.settings.billing.require_printer = True
        self.guard.start_override(OWNER, "PRINTER", minutes=60, reason="no paper")
        self.guard.can_checkout(STAFF, None)
        self.guard.end_override(OWNER, "PRINTER")
        with self.assertRaises(PrinterError):
            self.guard.can_checkout(STAFF, None)
        actions = [r[0] for r in self._q("SELECT action FROM audit_log WHERE action LIKE 'HARDWARE_OVERRIDE%' ORDER BY id")]
        self.assertEqual(actions, ["HARDWARE_OVERRIDE", "HARDWARE_OVERRIDE", "HARDWARE_OVERRIDE_ENDED"])
        with self.assertRaises(ValueError):
            self.guard.start_override(OWNER, "PRINTER", minutes=0, reason="x")
        with self.assertRaises(ValueError):
            self.guard.start_override(OWNER, "PRINTER", minutes=10, reason="")

    def test_new_printer_discovery_raises_one_toast_per_device(self) -> None:
        self.guard.refresh()
        self.probes.discovered = ["TM-T82 (USB)"]
        self.guard.refresh()
        self.guard.refresh()
        toasts = [e for e in self.events if e.device == "printer" and e.present]
        self.assertEqual([t.detail for t in toasts], ["TM-T82 (USB)"])
        self.probes.discovered = ["TM-T82 (USB)", "Office LaserJet"]
        self.guard.refresh()
        toasts = [e for e in self.events if e.device == "printer" and e.present]
        self.assertEqual([t.detail for t in toasts], ["TM-T82 (USB)", "Office LaserJet"])

    def test_poll_never_raises_and_state_is_cached_between_polls(self) -> None:
        def boom(hw: HardwareSettings) -> bool | None:
            raise OSError("usb enumeration failed")

        guard = HardwareGuard(self.txn, SqliteHardwareStore(), self.audit, settings=lambda: self.settings, scanner_present=boom, printer_status=self.probes.printer_status, discover_printers=self.probes.discover, event_bus=self.bus)
        st = guard.poll()
        self.assertIsNone(st.scanner_present, "unknown, not False — a probe failure must not lock the counter")
        guard.can_add_line(STAFF, via_scan=True)
        self.assertIs(guard.state(), st, "cached until the next poll")


if __name__ == "__main__":
    unittest.main()
