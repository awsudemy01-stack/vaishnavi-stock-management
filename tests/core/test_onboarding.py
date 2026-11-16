"""Onboarding wizard + setup checklist (docs/09 §2a, docs/13 Phase 2 unit 35): OnboardingService (auto-detected step
status, skip, progress) and the wizard step submits. Written first (docs/13 rule 7).
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.settings import Settings
from vaishnavi.app_services import AppServices
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.catalog import ProductInput
from vaishnavi.services.onboarding import STEPS, OnboardingService
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import onboarding as wizard

from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

OWNER = Actor(user_id=1, role=Role.OWNER)
STAFF = Actor(user_id=2, role=Role.STAFF)
EN = UiTexts.load("en")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        settings = Settings()
        settings.locations.same_device = True
        self.services = AppServices.build(settings, self.paths, root / "config", box=ReversibleBox())
        self.services.users.complete_owner_setup("owner-pass-1", "7391")
        self.svc: OnboardingService = self.services.onboarding

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()


class OnboardingServiceTest(Base):
    def test_status_is_detected_from_data_not_just_ticks(self) -> None:
        self.assertEqual(len(STEPS), 11)
        status = self.svc.status()
        by = {s.key: s for s in status}
        self.assertFalse(by["shop_profile"].done, "default shop name but no address yet")
        self.assertTrue(by["products"].done, "the fixture already has a product")
        self.assertTrue(by["opening_stock"].done, "the fixture product has an OPENING movement")
        self.assertTrue(by["staff"].done, "Ramesh exists")
        self.assertFalse(by["scanner"].done)
        self.assertFalse(by["recovery_key"].done, "key not acknowledged")
        self.assertTrue(by["backup_drive"].done, "same-device backups were accepted knowingly at install")
        done, total = self.svc.progress()
        self.assertEqual(total, 11)
        self.assertEqual(done, sum(1 for s in status if s.done))
        self.assertEqual(self.svc.next_step(), "shop_profile")

    def test_skip_and_unskip_are_owner_only_and_audited(self) -> None:
        self.svc.skip(OWNER, "printer")
        self.assertTrue(next(s for s in self.svc.status() if s.key == "printer").skipped)
        self.svc.unskip(OWNER, "printer")
        self.assertFalse(next(s for s in self.svc.status() if s.key == "printer").skipped)
        with self.assertRaises(Exception):
            self.svc.skip(STAFF, "printer")
        with self.assertRaises(ValueError):
            self.svc.skip(OWNER, "not-a-step")
        self.svc.skip(OWNER, "printer")
        self.assertEqual(self.svc.next_step(), "shop_profile", "skipped steps are passed over")
        self.assertFalse(self.svc.is_complete())

    def test_checklist_card_text(self) -> None:
        card = self.svc.checklist(EN)
        self.assertRegex(card.title, r"Setup \d+/11")
        self.assertTrue(any(i.key == "scanner" and not i.done for i in card.items))
        self.assertEqual(card.next_path, "/onboarding?step=shop_profile")


class WizardStepsTest(Base):
    def test_shop_profile_tax_and_payments_steps(self) -> None:
        result = wizard.submit_shop_profile(self.services, OWNER, {"name": "Vaishnavi Puja Store", "name_hi": "वैष्णवी पूजा स्टोर", "address_line1": "12, Mandir Road", "address_line2": "Varanasi", "phone": "9800000001", "gstin": "09ABCDE1234F1Z5", "state_code": "09"})
        self.assertTrue(result.ok, result.message)
        eff = self.services.settings_service.effective()
        self.assertEqual((eff.shop.address_line1, eff.shop.gstin), ("12, Mandir Road", "09ABCDE1234F1Z5"))
        self.assertTrue(next(s for s in self.svc.status() if s.key == "shop_profile").done)
        bad = wizard.submit_shop_profile(self.services, OWNER, {"name": "", "address_line1": "x"})
        self.assertFalse(bad.ok)
        self.assertIn("name", bad.problems)
        tax = wizard.submit_tax(self.services, OWNER, {"registered": True, "default_rate": "5", "prices_include_tax": True})
        self.assertTrue(tax.ok, tax.message)
        self.assertEqual(self.services.settings_service.effective().tax.default_rate_bp, 500)
        pay = wizard.submit_payments(self.services, OWNER, {"upi_id": "vaishnavi@upi", "payee_name": "Rajesh Kumar", "bank_name": "SBI", "account_no": "123456789012", "ifsc": "SBIN0001234"})
        self.assertTrue(pay.ok, pay.message)
        self.assertEqual(self.services.secure_settings.preview("payments.account_no"), "•••• 9012")
        self.assertTrue(next(s for s in self.svc.status() if s.key == "payments").done)
        denied = wizard.submit_payments(self.services, STAFF, {"upi_id": "x@upi"})
        self.assertFalse(denied.ok)

    def test_tax_step_asks_for_gstin_instead_of_a_config_error(self) -> None:
        """Live run 2: flipping "GST registered" with no GSTIN produced the E-CFG-001 "app cannot start" banner."""
        missing = wizard.submit_tax(self.services, OWNER, {"registered": True, "default_rate": "5"})
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error_code, "E-CFG-003")
        self.assertIn("gstin", missing.problems)
        self.assertFalse(self.services.settings_service.effective().tax.registered, "nothing was stored")
        bad = wizard.submit_tax(self.services, OWNER, {"registered": True, "default_rate": "5", "gstin": "short"})
        self.assertIn("gstin", bad.problems)
        ok = wizard.submit_tax(self.services, OWNER, {"registered": True, "default_rate": "5", "gstin": "09abcde1234f1z5"})
        self.assertTrue(ok.ok, ok.message)
        eff = self.services.settings_service.effective()
        self.assertEqual((eff.tax.registered, eff.shop.gstin), (True, "09ABCDE1234F1Z5"))
        off = wizard.submit_tax(self.services, OWNER, {"registered": False, "default_rate": "0"})
        self.assertTrue(off.ok, off.message)
        self.assertEqual(self.services.settings_service.effective().shop.gstin, "09ABCDE1234F1Z5", "an empty GSTIN field never wipes the stored one")

    def test_staff_and_opening_stock_steps(self) -> None:
        staff = wizard.submit_staff(self.services, OWNER, {"username": "sunita", "display_name": "Sunita", "password": "135790"})
        self.assertTrue(staff.ok, staff.message)
        self.assertIn("sunita", [u for _, u, _, _ in self.services.users.login_tiles()])
        dup = wizard.submit_staff(self.services, OWNER, {"username": "sunita", "display_name": "S", "password": "135790"})
        self.assertFalse(dup.ok)
        self.assertEqual(dup.error_code, "E-AUTH-009")
        p = self.services.catalog.create(OWNER, ProductInput(name="Diya", category_id=None, price_paise=500))
        model = wizard.build_opening_stock(self.services, EN)
        self.assertIn(p.id, [r.product_id for r in model.rows])
        self.assertTrue(all(r.product_id != 10 for r in model.rows), "products that already moved are not asked again")
        res = wizard.submit_opening_stock(self.services, OWNER, {str(p.id): "24", "999999": "1"})
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.services.inventory.stock_of(p.id), Decimal("24"))

    def test_wizard_model_lists_steps_with_status_and_current(self) -> None:
        model = wizard.build_wizard(self.services, EN, current="tax")
        self.assertEqual([s.key for s in model.steps], list(STEPS))
        self.assertEqual(model.current.key, "tax")
        self.assertEqual(model.progress_text, f"{model.done_count}/11")
        self.assertTrue(model.current.title)
        self.assertEqual(model.next_key, "payments")
        self.assertEqual(model.prev_key, "shop_profile")
        last = wizard.build_wizard(self.services, EN, current="recovery_key")
        self.assertIsNone(last.next_key)


if __name__ == "__main__":
    unittest.main()
