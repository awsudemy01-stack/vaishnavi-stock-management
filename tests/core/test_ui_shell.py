"""UI shell (docs/09 §1–2, docs/13 unit 27): theme, i18n, per-client state, route guards, status chips,
notification bell and the shell view-model. Written first (docs/13 rule 7).

Everything here is the framework-free half of the UI: pure view-models that NiceGUI merely renders.
NiceGUI itself is not installed on the development PC, so the render functions are not exercised.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from vaishnavi.api.system import AppState
from vaishnavi.core.clock import IST, UTC, FrozenClock, set_clock
from vaishnavi.core.permissions import Permission, Role
from vaishnavi.core.settings import Settings
from vaishnavi.db.sqlite_stores import SqliteNotificationStore, SqliteTxn
from vaishnavi.platform.paths import AppPaths
from vaishnavi.services.notifications import NotificationService
from vaishnavi.ui import guards, i18n, layout, state as ui_state, theme
from vaishnavi.ui.components import notification_bell, status_chip

from tests.core.test_db_guardian_backup import T0, make_live_db

I18N_DIR = Path(i18n.__file__).resolve().parent
OWNER = ui_state.ClientUser(user_id=1, username="owner", display_name="Rajesh Kumar", role=Role.OWNER)
STAFF = ui_state.ClientUser(user_id=2, username="ramesh", display_name="Ramesh", role=Role.STAFF)


# ============================================================================ theme
class ThemeTest(unittest.TestCase):
    def test_palette_matches_the_approved_mockup_and_css_carries_fonts(self) -> None:
        self.assertEqual(theme.PALETTE["saffron"], "#F57C00")
        self.assertEqual(theme.PALETTE["ok"], "#2E7D32")
        css = theme.css()
        self.assertIn("--saffron:#F57C00", css.replace(" ", ""))
        self.assertIn("Noto Sans Devanagari", css)
        self.assertIn(".chip", css)
        colors = theme.brand_colors()
        self.assertEqual(colors["primary"], "#F57C00")
        self.assertEqual(theme.level_color("green"), theme.PALETTE["ok"])
        self.assertEqual(theme.level_color("amber"), theme.PALETTE["warn"])
        self.assertEqual(theme.level_color("red"), theme.PALETTE["bad"])
        self.assertEqual(theme.level_color("unknown"), theme.PALETTE["muted"])

    def test_clock_text_is_shop_time_in_the_mockup_format(self) -> None:
        when = datetime(2026, 11, 14, 3, 58, tzinfo=UTC)  # 09:28 IST, a Saturday
        self.assertEqual(theme.format_clock(when), "Sat 14-Nov-2026 · 09:28")
        self.assertEqual(theme.format_clock(when, lang="hi"), "शनि 14-Nov-2026 · 09:28")


# ============================================================================ i18n
class I18nTest(unittest.TestCase):
    def test_hindi_overlays_english_and_missing_keys_never_raise(self) -> None:
        en, hi = i18n.UiTexts.load("en"), i18n.UiTexts.load("hi")
        self.assertEqual(en.t("nav.pos"), "Billing")
        self.assertNotEqual(hi.t("nav.pos"), "Billing", "Hindi has its own label")
        self.assertEqual(en.t("badge.session_since", time="09:30"), "since 09:30")
        self.assertEqual(hi.t("no.such.key"), "no.such.key")

    def test_language_normalisation(self) -> None:
        self.assertEqual(i18n.normalise_lang("hinglish"), "en", "Hinglish is a chat language; labels stay English")
        self.assertEqual(i18n.normalise_lang("hi"), "hi")
        self.assertEqual(i18n.normalise_lang("xx"), "en")
        self.assertEqual(i18n.normalise_lang(None), "en")

    def test_both_json_files_have_the_same_keys(self) -> None:
        en = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))
        hi = json.loads((I18N_DIR / "hi.json").read_text(encoding="utf-8"))
        self.assertEqual(set(i18n.flatten(en)), set(i18n.flatten(hi)), "every label needs its Hindi twin")
        self.assertGreater(len(i18n.flatten(en)), 40)

    def test_every_nav_item_and_chip_has_a_label_in_both_languages(self) -> None:
        for lang in ("en", "hi"):
            texts = i18n.UiTexts.load(lang)
            for item in guards.NAV_ITEMS:
                self.assertTrue(texts.has(item.label_key), f"{item.label_key} missing in {lang}")
            for key in status_chip.CHIP_ORDER:
                self.assertTrue(texts.has(f"chip.{key}"), f"chip.{key} missing in {lang}")


# ============================================================================ per-client state
class ClientStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)

    def tearDown(self) -> None:
        set_clock(self._prev)

    def test_sign_in_touch_idle_lock_and_sign_out(self) -> None:
        st = ui_state.ClientState.anonymous(lang="hi")
        self.assertFalse(st.is_authenticated)
        self.assertEqual(st.lang, "hi")
        st.sign_in(STAFF, work_session_id=7)
        self.assertTrue(st.is_authenticated)
        self.assertEqual(st.session_since, T0)
        self.assertEqual(st.actor().role, Role.STAFF)
        self.assertEqual(st.actor(pin_verified=True).pin_verified, True)
        self.assertEqual(st.initials(), "RA")
        self.clock.advance(minutes=9)
        self.assertFalse(st.should_lock(idle_minutes=10))
        self.clock.advance(minutes=2)
        self.assertTrue(st.should_lock(idle_minutes=10))
        st.touch()
        self.assertFalse(st.should_lock(idle_minutes=10), "activity resets the idle timer")
        st.lock()
        self.assertTrue(st.locked)
        self.clock.advance(minutes=31)
        self.assertTrue(st.lock_timed_out(max_locked_minutes=30), "a lock nobody opens ends the session")
        st.unlock()
        self.assertFalse(st.locked)
        st.sign_out()
        self.assertFalse(st.is_authenticated)
        self.assertIsNone(st.work_session_id)
        self.assertEqual(st.lang, "hi", "language survives sign-out")

    def test_round_trips_through_json_storage(self) -> None:
        st = ui_state.ClientState.anonymous(lang="en")
        st.sign_in(OWNER, work_session_id=3, shift_id=12)
        st.lock()
        st.pending_exit_confirmation = {"left_at": "2026-11-13T13:12:00Z", "kind": "WINDOW_CLOSED"}
        data = json.loads(json.dumps(st.to_dict()))  # NiceGUI's app.storage.user is JSON
        back = ui_state.ClientState.from_dict(data)
        self.assertEqual(back.user, OWNER)
        self.assertEqual(back.work_session_id, 3)
        self.assertEqual(back.shift_id, 12)
        self.assertTrue(back.locked)
        self.assertEqual(back.session_since, T0)
        self.assertEqual(back.pending_exit_confirmation["kind"], "WINDOW_CLOSED")
        self.assertEqual(ui_state.ClientState.from_dict({}).lang, "en", "empty storage → anonymous defaults")
        self.assertEqual(ui_state.ClientState.from_dict({"user": {"role": "ALIEN"}}).user, None, "garbage never crashes the shell")

    def test_initials_for_single_and_devanagari_names(self) -> None:
        self.assertEqual(ui_state.initials("Rajesh Kumar"), "RK")
        self.assertEqual(ui_state.initials("Sunita"), "SU")
        self.assertEqual(ui_state.initials("रमेश"), "र")
        self.assertEqual(ui_state.initials(""), "?")


# ============================================================================ guards
class GuardsTest(unittest.TestCase):
    def test_navigation_is_role_filtered_exactly_as_the_role_table(self) -> None:
        owner_keys = [i.key for i in guards.nav_for(Role.OWNER)]
        staff_keys = [i.key for i in guards.nav_for(Role.STAFF)]
        for key in ("dashboard", "pos", "products", "stock", "sales_history", "reports", "shifts", "settings"):
            self.assertIn(key, owner_keys)
        self.assertNotIn("settings", staff_keys, "settings is not rendered for staff")
        self.assertIn("pos", staff_keys)
        self.assertIn("products", staff_keys, "staff see Products as read-only Find item")
        self.assertEqual(owner_keys.index("dashboard"), 0)
        self.assertEqual(owner_keys.index("pos"), 1, "billing is the heart of the app — second item")

    def test_route_decisions(self) -> None:
        anon = ui_state.ClientState.anonymous()
        owner = ui_state.ClientState.anonymous()
        owner.sign_in(OWNER, work_session_id=1)
        staff = ui_state.ClientState.anonymous()
        staff.sign_in(STAFF, work_session_id=2)
        self.assertIs(guards.decide("/pos", anon), guards.Decision.LOGIN)
        self.assertIs(guards.decide("/login", anon), guards.Decision.ALLOW)
        self.assertIs(guards.decide("/pos", staff), guards.Decision.ALLOW)
        self.assertIs(guards.decide("/settings", staff), guards.Decision.FORBIDDEN)
        self.assertIs(guards.decide("/settings/backups", staff), guards.Decision.FORBIDDEN, "prefix match")
        self.assertIs(guards.decide("/system/logs", staff), guards.Decision.FORBIDDEN)
        self.assertIs(guards.decide("/settings/backups", owner), guards.Decision.ALLOW)
        self.assertIs(guards.decide("/login", owner), guards.Decision.REDIRECT_HOME, "a signed-in user does not see the login page")
        self.assertEqual(guards.redirect_for(guards.Decision.LOGIN, "/pos"), "/login?next=/pos")
        self.assertEqual(guards.redirect_for(guards.Decision.FORBIDDEN, "/settings"), "/?denied=/settings")
        self.assertEqual(guards.redirect_for(guards.Decision.REDIRECT_HOME, "/login"), "/")

    def test_locked_client_is_sent_to_the_lock_screen_and_forbidden_is_logged(self) -> None:
        staff = ui_state.ClientState.anonymous()
        staff.sign_in(STAFF, work_session_id=2)
        staff.lock()
        self.assertIs(guards.decide("/pos", staff), guards.Decision.LOCKED)
        self.assertIs(guards.decide("/lock", staff), guards.Decision.ALLOW)
        staff.unlock()
        with self.assertLogs("vaishnavi.ui.guards", level="WARNING") as logs:
            guards.decide("/settings", staff)
        self.assertTrue(any("E-AUTH-010" in line and "/settings" in line for line in logs.output))

    def test_required_permission_per_route(self) -> None:
        self.assertIs(guards.route_permission("/settings/users"), Permission.MANAGE_SYSTEM)
        self.assertIs(guards.route_permission("/pos"), Permission.SELL)
        self.assertIs(guards.route_permission("/reports/profit"), Permission.REPORT_PROFIT)
        self.assertIs(guards.route_permission("/reports"), Permission.REPORT_OWN_SHIFT)
        self.assertIsNone(guards.route_permission("/"), "dashboard only needs a login")


# ============================================================================ status chips
class StatusChipTest(unittest.TestCase):
    def test_chips_render_in_mockup_order_with_hints_and_unknowns_grey(self) -> None:
        chips = {"database": ("green", "DB ok"), "ledger": ("green", "Integrity ok"), "scanner": ("red", "Scanner: not connected"), "printer": ("amber", "Printer: not set up"), "ai_provider": ("green", "AI ready"), "backup": ("amber", "Backup: 2 queued")}
        views = status_chip.chip_views(chips, i18n.UiTexts.load("en"))
        self.assertEqual([v.key for v in views], list(status_chip.CHIP_ORDER))
        self.assertEqual([v.label for v in views][:6], ["DB", "Integrity", "Scanner", "Printer", "AI", "Backup"])
        scanner = next(v for v in views if v.key == "scanner")
        self.assertEqual(scanner.level, "red")
        self.assertIn("paused", scanner.hint.lower(), "red scanner explains that staff billing is paused")
        publish = next(v for v in views if v.key == "publish")
        self.assertEqual(publish.level, "grey")
        self.assertEqual(status_chip.worst_level(views), "red")
        hi = status_chip.chip_views(chips, i18n.UiTexts.load("hi"))
        self.assertNotEqual(hi[2].label, "Scanner")

    def test_chips_from_the_light_doctor_split_hardware_into_scanner_and_printer(self) -> None:
        from vaishnavi.bootstrap.doctor import CheckResult, Status

        hw = CheckResult(status=Status.WARN, message="x", key="hardware", data={"scanner_paired": True, "scanner_present": False, "billing_paused_for_staff": True, "printer": {"kind": "none", "ready": False, "address": "", "detail": ""}})
        chips = status_chip.chips_from_hardware(hw)
        self.assertEqual(chips["scanner"][0], "red")
        self.assertEqual(chips["printer"][0], "amber")
        hw_ok = CheckResult(status=Status.PASS, message="x", key="hardware", data={"scanner_paired": True, "scanner_present": True, "billing_paused_for_staff": False, "printer": {"kind": "usb", "ready": True, "address": "04b8:0202", "detail": ""}})
        chips = status_chip.chips_from_hardware(hw_ok)
        self.assertEqual(chips["scanner"][0], "green")
        self.assertEqual(chips["printer"][0], "green")
        hw_lenient = CheckResult(status=Status.WARN, message="x", key="hardware", data={"scanner_paired": False, "scanner_present": False, "billing_paused_for_staff": False, "printer": {"kind": "network", "ready": False, "address": "10.0.0.5", "detail": "offline"}})
        chips = status_chip.chips_from_hardware(hw_lenient)
        self.assertEqual(chips["scanner"][0], "amber", "no scanner but strict mode off → amber, not red")
        self.assertEqual(chips["printer"][0], "amber")


# ============================================================================ notification bell
class NotificationBellTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        import sqlite3

        conn = sqlite3.connect(self.paths.database)
        conn.execute("INSERT INTO user(id, username, display_name, role_id, password_hash) VALUES (3, 'sunita', 'Sunita', 2, 'x')")
        conn.commit()
        conn.close()
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.svc = NotificationService(SqliteTxn(self.paths.database), SqliteNotificationStore())
        self.svc.notify("WARN", "BACKUP_QUEUED", "Backup drive not found", deep_link="/settings/backups", source_ref="E-BKP-001")
        self.svc.notify("INFO", "LOW_STOCK", "Low stock: Camphor", deep_link="/stock?product=10", source_ref="product:10")
        self.svc.notify("WARN", "SCANNER_MISSING", "Barcode scanner not connected", deep_link="/settings/hardware", source_ref="E-SCN-001")
        self.svc.notify("INFO", "RECEIPTS_PENDING", "3 receipts waiting for the printer", deep_link="/pos", source_ref="print-queue")
        self.svc.notify("WARN", "SESSION_UNCLEAN", "You left yesterday without ending work", user_id=2, deep_link="/attendance", source_ref="session:9")
        self.svc.notify("WARN", "SESSION_UNCLEAN", "Sunita left without ending work", user_id=3, deep_link="/attendance", source_ref="session:10")

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_owner_sees_everything_staff_only_their_own_items(self) -> None:
        owner = notification_bell.bell_view(self.svc, user_id=1, role=Role.OWNER, texts=i18n.UiTexts.load("en"))
        self.assertEqual(owner.unread_count, 4, "all broadcasts; a banner addressed privately to a staff member is not duplicated into the owner's bell")
        staff = notification_bell.bell_view(self.svc, user_id=2, role=Role.STAFF, texts=i18n.UiTexts.load("en"))
        self.assertEqual({n.kind for n in staff.items}, {"SCANNER_MISSING", "RECEIPTS_PENDING", "SESSION_UNCLEAN"})
        self.assertEqual(staff.unread_count, 3)
        self.assertTrue(all(n.user_id in (None, 2) for n in staff.items))

    def test_items_carry_severity_colour_deep_link_and_relative_time(self) -> None:
        self.clock.advance(minutes=5)
        view = notification_bell.bell_view(self.svc, user_id=1, role=Role.OWNER, texts=i18n.UiTexts.load("en"))
        first = view.items[0]
        self.assertEqual(first.color, theme.severity_color(first.severity))
        self.assertTrue(first.deep_link.startswith("/"))
        self.assertEqual(first.when_text, "5 min ago")
        self.assertEqual(view.worst_severity, "WARN")
        self.assertEqual(notification_bell.relative_time(T0 - timedelta(hours=3), now=T0, texts=i18n.UiTexts.load("en")), "3 h ago")
        self.assertEqual(notification_bell.relative_time(T0 - timedelta(days=2), now=T0, texts=i18n.UiTexts.load("en")), "2 d ago")
        self.assertEqual(notification_bell.relative_time(T0, now=T0, texts=i18n.UiTexts.load("hi")), "अभी")


# ============================================================================ shell view-model
class ShellModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 11, 14, 3, 58, tzinfo=UTC))
        self._prev = set_clock(self.clock)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = AppPaths(root / "p", root / "s")
        self.app_state = AppState(settings=Settings(), paths=self.paths, config_dir=root / "config")
        self.app_state.chips.update({"database": ("green", "DB ok"), "ledger": ("green", "Integrity ok"), "scanner": ("green", "Scanner ok"), "printer": ("amber", "Printer: not set up"), "ai_provider": ("green", "AI ready"), "backup": ("green", "Backup ok")})

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def test_shell_for_a_signed_in_staff_member(self) -> None:
        client = ui_state.ClientState.anonymous(lang="en")
        client.sign_in(STAFF, work_session_id=5, shift_id=None)
        model = layout.build_shell(self.app_state, client)
        self.assertEqual(model.brand.title, "Vaishnavi Store Manager")
        self.assertIn("वैष्णवी पूजा स्टोर", model.brand.subtitle)
        self.assertEqual(model.clock_text, "Sat 14-Nov-2026 · 09:28")
        self.assertEqual([c.key for c in model.chips], list(status_chip.CHIP_ORDER))
        self.assertEqual([n.key for n in model.nav], [i.key for i in guards.nav_for(Role.STAFF)])
        self.assertEqual(model.badge.name, "Ramesh")
        self.assertEqual(model.badge.initials, "RA")
        self.assertEqual(model.badge.role_label, "Billing staff")
        self.assertEqual(model.badge.session_since_text, "since 09:28")
        self.assertIsNone(model.badge.shift_text)
        self.assertTrue(model.chat_bubble.visible)
        self.assertEqual(model.chat_bubble.title, "Sahayak · सहायक")
        self.assertEqual(model.lang, "en")
        self.assertEqual(model.lang_toggle, (("en", "EN", True), ("hi", "हिंदी", False)))
        self.assertIn(("Ctrl+Q", "shortcut.end_work"), [(s.keys, s.label_key) for s in model.shortcuts])

    def test_shell_for_anonymous_and_for_owner_in_hindi(self) -> None:
        anon = ui_state.ClientState.anonymous(lang="hi")
        model = layout.build_shell(self.app_state, anon)
        self.assertEqual(model.nav, [], "no navigation before sign-in")
        self.assertIsNone(model.badge)
        self.assertFalse(model.chat_bubble.visible, "the assistant needs a signed-in user")
        self.assertTrue(model.clock_text.startswith("शनि"))
        owner = ui_state.ClientState.anonymous(lang="hi")
        owner.sign_in(OWNER, work_session_id=1, shift_id=42)
        model = layout.build_shell(self.app_state, owner)
        self.assertEqual(model.badge.role_label, "मालिक")
        self.assertEqual(model.badge.shift_text, "शिफ़्ट #42")
        self.assertIn("settings", [n.key for n in model.nav])
        self.assertEqual(model.nav[1].label, "बिलिंग")

    def test_health_summary_for_the_login_pane_is_plain_language(self) -> None:
        self.app_state.chips["scanner"] = ("red", "Scanner: paired scanner not connected")
        self.app_state.extra["started_at_text"] = "09:02"
        summary = layout.health_summary(self.app_state, i18n.UiTexts.load("en"))
        self.assertEqual(summary.level, "red")
        self.assertIn("not ready", summary.title.lower())
        self.assertGreaterEqual(len(summary.lines), 5)
        self.assertTrue(any("Scanner" in line.text for line in summary.lines))
        self.assertTrue(summary.status_url.endswith("/system/status"))


class NiceGuiCompatibilityTest(unittest.TestCase):
    """Source-level guards for the NiceGUI >= 3 rules that bit the first live run (15-Nov-2026, NiceGUI 4.0.0)."""

    UI_DIR = Path(__file__).resolve().parents[2] / "src" / "vaishnavi" / "ui"

    def test_serve_creates_no_ui_element_in_global_scope(self) -> None:
        import ast

        source = (self.UI_DIR / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        serve = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "serve")
        calls = set()
        for node in ast.walk(serve):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "ui":
                calls.add(node.func.attr)
        self.assertEqual(calls, {"run"}, "a ui.* element (ui.timer, ui.label, ...) in serve() runs in global scope; NiceGUI >= 3 refuses to start with @ui.page routes — use app.timer / page functions")

    def test_html_calls_pass_sanitize_and_downloads_use_the_new_api(self) -> None:
        import re

        problems = []
        for path in self.UI_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"ui\.html\((.*)", text):
                if "sanitize=" not in m.group(1):
                    problems.append(f"{path.name}: ui.html(...) without sanitize=")
            if re.search(r"ui\.download\((?!\.)", text):
                problems.append(f"{path.name}: legacy ui.download(...) — use ui.download.content/file")
            if "e.content.read()" in text:
                problems.append(f"{path.name}: UploadEventArguments.content is gone — use await e.file.read()")
            for m in re.finditer(r"position=([a-z-]+)", text):
                if m.group(1) not in {"standard", "top", "right", "bottom", "left"}:
                    problems.append(f"{path.name}: q-dialog position={m.group(1)} is not a Quasar position — the dialog throws in the browser and never opens (live run 2)")
        self.assertEqual(problems, [])


class RouteRegistrationTest(unittest.TestCase):
    """Every page module registers against a stub NiceGUI; a page that fails to import would 404 in the shop (live run 2)."""

    def setUp(self) -> None:
        import sys
        import types

        self._saved = sys.modules.get("nicegui")
        self.routes: list[tuple[str, str]] = []

        class _Any:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def __getattr__(self, name: str) -> "_Any":
                return _Any()

            def __call__(self, *a: object, **k: object) -> "_Any":
                return _Any()

            def __enter__(self) -> "_Any":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        routes = self.routes

        def page(path: str, **kw: object):  # noqa: ANN202
            def deco(fn):  # noqa: ANN001, ANN202
                routes.append((path, fn.__name__))
                return fn

            return deco

        ui = _Any()
        ui.page = page
        stub = types.ModuleType("nicegui")
        stub.ui = ui  # type: ignore[attr-defined]
        stub.app = _Any()  # type: ignore[attr-defined]
        sys.modules["nicegui"] = stub
        self._any = _Any

    def tearDown(self) -> None:
        import sys

        if self._saved is None:
            sys.modules.pop("nicegui", None)
        else:
            sys.modules["nicegui"] = self._saved

    def test_all_shell_routes_are_registered(self) -> None:
        from unittest import mock

        from vaishnavi.app_services import AppServices
        from vaishnavi.core.events import EventBus
        from vaishnavi.ui import server

        from tests.core.test_services_foundation import ReversibleBox

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = AppPaths(root / "p", root / "s")
            paths.ensure()
            make_live_db(paths.database)
            settings = Settings()
            services = AppServices.build(settings, paths, root / "config", box=ReversibleBox(), event_bus=EventBus())
            state = AppState(settings=settings, paths=paths, config_dir=root / "config", started_at=T0)
            state.extra["services"] = services
            with mock.patch.object(server, "frame", lambda *a, **k: self._any()), self.assertNoLogs("vaishnavi.ui.server", level="ERROR"):
                server._register_frame_pages(state)
                server.register_pages(state)
        paths_seen = [p for p, _ in self.routes]
        self.assertEqual(server.missing_routes(paths_seen), [], f"registered: {sorted(paths_seen)}")
        self.assertIn("/shifts", paths_seen)
        self.assertIn("/sales/review", paths_seen)
        self.assertLess(paths_seen.index("/sales/review"), paths_seen.index("/sales/{sale_id}"), "the literal route must be registered before the parameterised one")

    def test_missing_routes_and_not_found_page(self) -> None:
        from vaishnavi.ui import server

        self.assertEqual(server.missing_routes(["/", "/pos"], required=("/", "/pos", "/shifts")), ["/shifts"])
        html = server.not_found_html("/shifts?open=<1>")
        self.assertIn("404", html)
        self.assertIn("&lt;1&gt;", html, "path is escaped")
        self.assertIn("href='/'", html)

    def test_match_report_asks_the_router_not_the_registry(self) -> None:
        """A route can be registered yet unreachable; the probe reports what a GET would really hit (live run 3)."""
        import enum
        import re

        from vaishnavi.ui import server

        class Match(enum.Enum):
            NONE = 0
            PARTIAL = 1
            FULL = 2

        class Route:
            def __init__(self, pattern: str, name: str, methods: set[str] = {"GET"}) -> None:  # noqa: B006
                self.path, self.endpoint, self._re, self._methods = pattern, type(name, (), {})(), re.compile(pattern.replace("{x}", "[^/]+") + "$"), methods
                self.endpoint.__qualname__ = name

            def matches(self, scope: dict) -> tuple[Match, dict]:  # type: ignore[type-arg]
                if not self._re.match(scope["path"]):
                    return Match.NONE, {}
                return (Match.FULL if scope["method"] in self._methods else Match.PARTIAL), {}

        class App:
            routes = [Route("/sales/{x}", "sale_page"), Route("/sales/review", "review_page"), Route("/pos", "pos_page", {"POST"}), Route("/", "home")]

        report = server.match_report(App(), ("/", "/sales/review", "/pos", "/shifts"))
        self.assertTrue(report["/"].endswith("home"))
        self.assertTrue(report["/sales/review"].endswith("sale_page"), "first FULL match wins — the parameterised route shadows the literal one")
        self.assertIsNone(report["/pos"], "method mismatch is only a PARTIAL match")
        self.assertIsNone(report["/shifts"])

    def test_uvicorn_access_logger_propagates_after_capture(self) -> None:
        import logging

        from vaishnavi.ui import server

        access = logging.getLogger("uvicorn.access")
        access.addHandler(logging.NullHandler())
        access.propagate = False
        server.capture_uvicorn_access()
        self.assertTrue(access.propagate)
        self.assertEqual(access.handlers, [])


if __name__ == "__main__":
    unittest.main()
