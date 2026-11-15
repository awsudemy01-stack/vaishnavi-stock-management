"""Login + attendance (docs/05 §6a, docs/09 §2, docs/13 unit 28): WorkSessionService and the login page view-model.

Written first (docs/13 rule 7). Real SQLite files; the clock is frozen; NiceGUI is not touched.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from vaishnavi.core.clock import FrozenClock, iso_utc, set_clock
from vaishnavi.core.errors import AuthError
from vaishnavi.core.events import EventBus, WorkSessionEvent
from vaishnavi.core.permissions import Actor, Role
from vaishnavi.core.security import hash_secret
from vaishnavi.db.sqlite_stores import SqliteAuditStore, SqliteNotificationStore, SqliteTxn, SqliteUserStore, SqliteWorkSessionStore
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.audit import AuditService
from vaishnavi.services.notifications import NotificationService
from vaishnavi.services.user import UserService
from vaishnavi.services.work_session import EndReason, WorkSessionService
from vaishnavi.ui import state as ui_state
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import login as login_page

from tests.core.test_db_guardian_backup import T0, make_live_db

OWNER_ACTOR = Actor(user_id=1, role=Role.OWNER)
STAFF_ACTOR = Actor(user_id=2, role=Role.STAFF)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.bus = EventBus()
        self.txn = SqliteTxn(self.paths.database)
        self.audit = AuditService(SqliteAuditStore())
        self.users = UserService(self.txn, SqliteUserStore(), self.audit)
        self.users.complete_owner_setup("owner-pass-1", "7391")
        conn = sqlite3.connect(self.paths.database)
        conn.execute("UPDATE user SET password_hash = ? WHERE id = 2", (hash_secret("246810"),))  # numeric staff password, no forced change
        conn.commit()
        conn.close()
        self.sessions = WorkSessionService(self.txn, SqliteWorkSessionStore(), self.audit, event_bus=self.bus, app_run_id=None)
        self.events: list[WorkSessionEvent] = []
        self.bus.subscribe(WorkSessionEvent, self.events.append)

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _query(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


# ============================================================================ sessions
class WorkSessionServiceTest(Base):
    def test_login_opens_a_session_with_server_time_and_end_work_closes_it_cleanly(self) -> None:
        started = self.sessions.start(user_id=2, client="kiosk 127.0.0.1")
        self.assertEqual(started.login_at, iso_utc(T0))
        self.assertIsNone(started.unconfirmed, "first ever login — nothing to confirm")
        self.assertEqual([e.kind for e in self.events], ["LOGIN"])
        self.clock.advance(hours=9, minutes=15)
        preview = self.sessions.end_work_preview(started.session_id)
        self.assertEqual(preview.duration_text, "9 h 15 m")
        self.assertEqual(preview.bills, 2, "bills in the session window (make_live_db sold 2 at T0)")
        self.assertEqual(preview.sales_paise, 4000)
        self.assertTrue(preview.open_shift, "Ramesh's shift #1 is still open")
        with self.assertRaises(AuthError) as cm:
            self.sessions.end(started.session_id, actor=STAFF_ACTOR, note="going home")
        self.assertEqual(cm.exception.code, "E-AUTH-020", "logout with an open shift needs the Owner PIN")
        self.sessions.end(started.session_id, actor=Actor(user_id=2, role=Role.STAFF, pin_verified=True), note="going home")
        row = self._query("SELECT logout_at, end_reason, confirmed_note FROM work_session WHERE id = ?", started.session_id)[0]
        self.assertEqual(row[0], iso_utc(self.clock.now()))
        self.assertEqual(row[1], "LOGOUT")
        self.assertEqual(row[2], "going home")
        self.assertEqual([e.kind for e in self.events], ["LOGIN", "LOGOUT"])
        actions = [r[0] for r in self._query("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("LOGIN", actions)
        self.assertIn("LOGOUT", actions)

    def test_second_login_forces_the_first_session_closed(self) -> None:
        first = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(minutes=20)
        self.sessions.heartbeat(first.session_id)
        self.clock.advance(minutes=1)
        second = self.sessions.start(user_id=2, client="tablet")
        self.assertEqual(second.forced_session_ids, [first.session_id])
        row = self._query("SELECT logout_at, end_reason FROM work_session WHERE id = ?", first.session_id)[0]
        self.assertEqual(row[1], "FORCED_BY_LOGIN")
        self.assertEqual(row[0], iso_utc(T0 + timedelta(minutes=20)), "exit time = last heartbeat, not the moment of the new login")
        self.assertEqual(len(self.sessions.who_is_logged_in()), 1)

    def test_window_close_and_heartbeat_loss_are_flagged_and_must_be_confirmed_at_next_login(self) -> None:
        s1 = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(hours=8)
        self.sessions.window_closed(s1.session_id)
        self.assertEqual(self._query("SELECT end_reason FROM work_session WHERE id = ?", s1.session_id)[0][0], "WINDOW_CLOSED")
        self.assertFalse(self.sessions.window_closed(s1.session_id), "idempotent — a second beacon changes nothing")
        pending = self.sessions.pending_confirmation(user_id=2)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.id, s1.session_id)  # type: ignore[union-attr]
        self.clock.advance(hours=14)
        s2 = self.sessions.start(user_id=2, client="kiosk")
        self.assertEqual(s2.unconfirmed.id, s1.session_id, "the banner data travels with the login result")  # type: ignore[union-attr]
        with self.assertRaises(AuthError) as cm:
            self.sessions.require_confirmed(user_id=2)
        self.assertEqual(cm.exception.code, "E-AUTH-021")
        self.sessions.confirm(s1.session_id, user_id=2, note="forgot — left at 6:45")
        self.assertIsNone(self.sessions.pending_confirmation(user_id=2))
        self.sessions.require_confirmed(user_id=2)
        self.assertIn("CONFIRMED", [e.kind for e in self.events])
        with self.assertRaises(AuthError):
            self.sessions.confirm(s1.session_id, user_id=1, note="not mine")

    def test_monitor_closes_silent_sessions_with_last_seen_and_notifies(self) -> None:
        notifications = NotificationService(self.txn, SqliteNotificationStore(), event_bus=self.bus)
        notifications.subscribe(self.bus)
        s = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(seconds=30)
        self.sessions.heartbeat(s.session_id)
        self.clock.advance(seconds=45)
        self.assertEqual(self.sessions.monitor(), 0, "45 s of silence is within the 60 s allowance")
        self.clock.advance(seconds=20)
        self.assertEqual(self.sessions.monitor(), 1)
        row = self._query("SELECT logout_at, end_reason FROM work_session WHERE id = ?", s.session_id)[0]
        self.assertEqual(row, (iso_utc(T0 + timedelta(seconds=30)), "HEARTBEAT_LOST"))
        self.assertFalse(self.sessions.heartbeat(s.session_id), "a late heartbeat tells the browser the session is gone")
        self.assertIn("SESSION_UNCLEAN", [n["kind"] for n in notifications.unread(user_id=1)])

    def test_idle_lock_timeout_ends_the_session_at_the_lock_time_and_counts_idle(self) -> None:
        s = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(minutes=10)
        self.sessions.record_lock(s.session_id)
        self.clock.advance(minutes=5)
        self.sessions.record_unlock(s.session_id, locked_seconds=300)
        self.assertEqual(self._query("SELECT lock_count, idle_seconds FROM work_session WHERE id = ?", s.session_id)[0], (1, 300))
        self.clock.advance(minutes=1)
        locked_at = self.clock.now()
        self.sessions.record_lock(s.session_id)
        self.clock.advance(minutes=31)
        self.sessions.idle_lock_timeout(s.session_id, locked_since=locked_at)
        row = self._query("SELECT logout_at, end_reason, lock_count FROM work_session WHERE id = ?", s.session_id)[0]
        self.assertEqual(row, (iso_utc(locked_at), "IDLE_LOCK_TIMEOUT", 2))

    def test_app_shutdown_and_recovery_close_everything_open(self) -> None:
        a = self.sessions.start(user_id=1, client="kiosk")
        b = self.sessions.start(user_id=2, client="tablet")
        self.clock.advance(minutes=3)
        self.assertEqual(self.sessions.close_all(EndReason.APP_SHUTDOWN), 2)
        reasons = {r[0] for r in self._query("SELECT end_reason FROM work_session WHERE id IN (?, ?)", a.session_id, b.session_id)}
        self.assertEqual(reasons, {"APP_SHUTDOWN"})
        c = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(hours=12)  # the PC lost power overnight; next morning the app starts again
        self.assertEqual(self.sessions.recover_unclean(), 1)
        row = self._query("SELECT logout_at, end_reason FROM work_session WHERE id = ?", c.session_id)[0]
        self.assertEqual(row, (c.login_at, "HEARTBEAT_LOST"))
        self.assertIsNotNone(self.sessions.pending_confirmation(user_id=2))

    def test_owner_can_force_end_and_annotate_staff_cannot(self) -> None:
        s = self.sessions.start(user_id=2, client="tablet left on")
        self.clock.advance(hours=2)
        self.sessions.force_end(OWNER_ACTOR, s.session_id, note="tablet left logged in")
        row = self._query("SELECT end_reason, owner_note FROM work_session WHERE id = ?", s.session_id)[0]
        self.assertEqual(row, ("FORCED_BY_OWNER", "tablet left logged in"))
        with self.assertRaises(Exception):
            self.sessions.annotate(STAFF_ACTOR, s.session_id, note="no")
        self.sessions.annotate(OWNER_ACTOR, s.session_id, note="spoke to Ramesh")
        self.assertEqual(self._query("SELECT owner_note FROM work_session WHERE id = ?", s.session_id)[0][0], "spoke to Ramesh")

    def test_history_rows_carry_derived_figures_and_the_unclean_streak(self) -> None:
        s1 = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(hours=8)
        self.sessions.window_closed(s1.session_id)
        self.clock.advance(hours=16)
        s2 = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(hours=1)
        self.sessions.monitor_at(self.clock.now() + timedelta(minutes=5))  # simulate silence
        rows = self.sessions.history(OWNER_ACTOR, user_id=2)
        self.assertEqual([r.id for r in rows], [s2.session_id, s1.session_id], "newest first")
        self.assertEqual(rows[1].duration_seconds, 8 * 3600)
        self.assertEqual(rows[1].bills, 2)
        self.assertTrue(rows[1].unclean)
        self.assertEqual(self.sessions.unclean_streak(user_id=2), 2)
        with self.assertRaises(Exception):
            self.sessions.history(STAFF_ACTOR, user_id=1)
        own = self.sessions.history(STAFF_ACTOR, user_id=2)
        self.assertEqual(len(own), 2, "staff may read their own history")


# ============================================================================ login page view-model
class LoginPageModelTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.users.create_user(OWNER_ACTOR, "sunita", "Sunita", "135790")
        self.texts = UiTexts.load("en")

    def test_tiles_keypad_rule_and_button_text(self) -> None:
        model = login_page.build_login_model(self.users, self.sessions, texts=self.texts, selected="ramesh")
        self.assertEqual([t.username for t in model.tiles], ["owner", "ramesh", "sunita", login_page.OTHER_TILE])
        self.assertTrue(model.tiles[0].is_owner)
        self.assertEqual(model.tiles[1].initials, "RA")
        self.assertTrue(model.show_keypad, "staff accounts use the number pad")
        self.assertEqual(model.button_text, "Sign in & start work → 21:30")
        owner_model = login_page.build_login_model(self.users, self.sessions, texts=self.texts, selected="owner")
        self.assertFalse(owner_model.show_keypad, "the Owner types a full password")
        other = login_page.build_login_model(self.users, self.sessions, texts=self.texts, selected=login_page.OTHER_TILE)
        self.assertTrue(other.show_username_box)
        self.assertFalse(other.show_keypad)
        self.assertIsNone(model.banner)

    def test_banner_appears_for_an_unclean_previous_exit(self) -> None:
        s = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(hours=9, minutes=12)
        self.sessions.window_closed(s.session_id)
        self.clock.advance(hours=12)
        model = login_page.build_login_model(self.users, self.sessions, texts=self.texts, selected="ramesh")
        self.assertIsNotNone(model.banner)
        self.assertIn("06:42", model.banner.title)  # type: ignore[union-attr]  (T0 21:30 IST + 9 h 12 m)
        self.assertIn("Ramesh", model.banner.title)  # type: ignore[union-attr]
        hi = login_page.build_login_model(self.users, self.sessions, texts=UiTexts.load("hi"), selected="ramesh")
        self.assertNotEqual(hi.banner.title, model.banner.title)  # type: ignore[union-attr]

    def test_sign_in_flow_updates_client_state_and_reports_what_comes_next(self) -> None:
        client = ui_state.ClientState.anonymous("en")
        bad = login_page.sign_in(self.users, self.sessions, client, username="ramesh", password="0000", client_label="kiosk")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error_code, "E-AUTH-001")
        self.assertFalse(client.is_authenticated)
        good = login_page.sign_in(self.users, self.sessions, client, username="ramesh", password="246810", client_label="kiosk")
        self.assertTrue(good.ok, good.error_code)
        self.assertTrue(client.is_authenticated)
        self.assertEqual(client.user.username, "ramesh")  # type: ignore[union-attr]
        self.assertEqual(client.work_session_id, good.session_id)
        self.assertEqual(client.shift_id, 1, "Ramesh's open shift is picked up")
        self.assertEqual(good.next_path, "/pos", "staff land on the counter")
        self.assertIsNone(client.pending_exit_confirmation)
        # an unclean exit → the confirmation step comes first
        self.sessions.window_closed(good.session_id)  # type: ignore[arg-type]
        client2 = ui_state.ClientState.anonymous("en")
        again = login_page.sign_in(self.users, self.sessions, client2, username="ramesh", password="246810", client_label="kiosk")
        self.assertEqual(again.next_path, "/confirm-exit")
        self.assertEqual(client2.pending_exit_confirmation["session_id"], good.session_id)  # type: ignore[index]
        owner_client = ui_state.ClientState.anonymous("hi")
        owner = login_page.sign_in(self.users, self.sessions, owner_client, username="owner", password="owner-pass-1", client_label="kiosk")
        self.assertEqual(owner.next_path, "/", "the Owner lands on the dashboard")

    def test_first_owner_login_before_setup_is_refused_with_e_auth_005(self) -> None:
        import shutil

        fresh = Path(self._tmp.name) / "fresh.db"
        shutil.copy(self.paths.database, fresh)
        conn = sqlite3.connect(fresh)
        conn.execute("UPDATE user SET password_hash = '!SETUP' WHERE id = 1")
        conn.commit()
        conn.close()
        users = UserService(SqliteTxn(fresh), SqliteUserStore(), AuditService(SqliteAuditStore()))
        sessions = WorkSessionService(SqliteTxn(fresh), SqliteWorkSessionStore(), AuditService(SqliteAuditStore()), event_bus=EventBus())
        client = ui_state.ClientState.anonymous("en")
        res = login_page.sign_in(users, sessions, client, username="owner", password="anything", client_label="kiosk")
        self.assertEqual(res.error_code, "E-AUTH-005")
        self.assertEqual(res.next_path, "/onboarding", "the first-run wizard is the only way forward")


# ============================================================================ browser ↔ server endpoints
class SessionApiTest(Base):
    def test_heartbeat_and_closing_beacon_handlers(self) -> None:
        from vaishnavi.api import session as api_session

        s = self.sessions.start(user_id=2, client="kiosk")
        self.clock.advance(seconds=15)
        self.assertEqual(api_session.heartbeat(self.sessions, {"session_id": s.session_id}), {"ok": True, "alive": True})
        self.assertEqual(self._query("SELECT last_seen_at FROM work_session WHERE id = ?", s.session_id)[0][0], iso_utc(self.clock.now()))
        self.assertEqual(api_session.heartbeat(self.sessions, {"session_id": "garbage"}), {"ok": False, "alive": False, "error": "bad session id"})
        self.assertEqual(api_session.closing(self.sessions, {"session_id": s.session_id}), {"ok": True, "closed": True})
        self.assertEqual(self._query("SELECT end_reason FROM work_session WHERE id = ?", s.session_id)[0][0], "WINDOW_CLOSED")
        self.assertEqual(api_session.heartbeat(self.sessions, {"session_id": s.session_id}), {"ok": True, "alive": False}, "the page learns the session is gone and returns to login")
        self.assertEqual(api_session.closing(self.sessions, {"session_id": s.session_id}), {"ok": True, "closed": False})

    def test_guard_sends_a_user_with_an_unconfirmed_exit_to_the_confirmation_page(self) -> None:
        from vaishnavi.ui import guards

        client = ui_state.ClientState.anonymous("en")
        client.sign_in(ui_state.ClientUser(2, "ramesh", "Ramesh", Role.STAFF), work_session_id=1)
        client.pending_exit_confirmation = {"session_id": 1, "left_at": "x"}
        self.assertIs(guards.decide("/pos", client), guards.Decision.CONFIRM_EXIT)
        self.assertIs(guards.decide("/confirm-exit", client), guards.Decision.ALLOW)
        self.assertEqual(guards.redirect_for(guards.Decision.CONFIRM_EXIT, "/pos"), "/confirm-exit")
        client.pending_exit_confirmation = None
        self.assertIs(guards.decide("/pos", client), guards.Decision.ALLOW)

    def test_unlock_reauthenticates_with_the_own_password_without_a_new_login(self) -> None:
        self.assertFalse(self.users.reauthenticate(2, "wrong-pass"))
        self.assertTrue(self.users.reauthenticate(2, "246810"))
        self.assertEqual(self._query("SELECT COUNT(*) FROM work_session")[0][0], 0, "unlocking never opens a session")
        self.assertEqual(self._query("SELECT COUNT(*) FROM audit_log WHERE action = 'LOGIN'")[0][0], 0)
        self.assertIn("UNLOCK_FAILED", [r[0] for r in self._query("SELECT action FROM audit_log")])
        for _ in range(5):
            self.users.reauthenticate(2, "wrong-pass")
        with self.assertRaises(AuthError) as cm:
            self.users.reauthenticate(2, "246810")
        self.assertEqual(cm.exception.code, "E-AUTH-003", "the same lockout as the login applies")


# ============================================================================ first Owner login (onboarding step 1)
class OwnerSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)  # the seeded Owner still carries the !SETUP sentinel
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        from vaishnavi.app_services import AppServices
        from vaishnavi.core.settings import Settings

        from tests.core.test_services_foundation import ReversibleBox

        self.services = AppServices.build(Settings(), self.paths, root / "config", box=ReversibleBox())

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_setup_form_validates_then_completes_setup_and_issues_the_recovery_key(self) -> None:
        from vaishnavi.ui.pages import onboarding

        self.assertTrue(self.services.users.owner_setup_pending())
        problems = onboarding.validate_owner_setup(password="short", password_again="short", pin="1234", pin_again="1234")
        self.assertIn("password", problems)
        self.assertIn("pin", problems)
        problems = onboarding.validate_owner_setup(password="owner-pass-1", password_again="owner-pass-2", pin="7391", pin_again="7391")
        self.assertEqual(list(problems), ["password_again"])
        self.assertEqual(onboarding.validate_owner_setup(password="owner-pass-1", password_again="owner-pass-1", pin="7391", pin_again="7391"), {})
        result = onboarding.complete_owner_setup(self.services, password="owner-pass-1", pin="7391", display_name="Rajesh Kumar")
        self.assertFalse(self.services.users.owner_setup_pending())
        self.assertEqual(len(result.recovery_words), 12)
        self.assertGreaterEqual(len(result.sheet_lines), 5)
        self.assertEqual(result.user.display_name, "Rajesh Kumar")
        self.assertTrue(self.services.recovery.needs_attention(), "until the Owner ticks 'I have written it down'")
        onboarding.acknowledge_recovery_key(self.services, result.user)
        self.assertFalse(self.services.recovery.needs_attention())
        login = self.services.users.login("owner", "owner-pass-1")
        self.assertEqual(login.actor.role.value, "OWNER")
        with self.assertRaises(AuthError):
            onboarding.complete_owner_setup(self.services, password="again-pass-1", pin="7391", display_name=None)


if __name__ == "__main__":
    unittest.main()
