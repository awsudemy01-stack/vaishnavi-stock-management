"""Unit 52 — report publishing (docs/05 §11): renderer, outbox with retry, channels, ReportPublisher, Settings → Publishing."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from vaishnavi.api.system import AppState
from vaishnavi.app_services import AppServices
from vaishnavi.core.clock import FrozenClock, set_clock
from vaishnavi.core.errors import PublishError
from vaishnavi.core.events import EventBus
from vaishnavi.core.settings import Settings
from vaishnavi.platform.paths import AppPaths
from vaishnavi.publishing import renderer
from vaishnavi.publishing.channels.folder import FolderChannel
from vaishnavi.publishing.channels.smtp import SmtpChannel
from vaishnavi.publishing.channels.telegram import TelegramChannel
from vaishnavi.publishing.channels.whatsapp import WhatsAppChannel
from vaishnavi.publishing.outbox import BACKOFF_MINUTES, Outbox
from vaishnavi.services.billing import PaymentInput
from vaishnavi.ui.i18n import UiTexts
from vaishnavi.ui.pages import settings_publishing

from tests.core.test_billing import CAMPHOR, OWNER, STAFF
from tests.core.test_db_guardian_backup import T0, make_live_db
from tests.core.test_services_foundation import ReversibleBox

EN = UiTexts.load("en")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = AppPaths(self.root / "p", self.root / "s")
        self.paths.ensure()
        make_live_db(self.paths.database)
        self.clock = FrozenClock(T0)
        self._prev = set_clock(self.clock)
        self.settings = Settings()
        self.c = AppServices.build(self.settings, self.paths, self.root / "config", box=ReversibleBox(), event_bus=EventBus())
        self.state = AppState(settings=self.settings, paths=self.paths, config_dir=self.root / "config", started_at=T0)
        self.state.extra["services"] = self.c
        self.c.settings_service.set_many(OWNER, {"billing.require_scan": False})
        self.c.shifts.open(OWNER, opening_float_paise=50000)
        self.sync = self.root / "OneDrive" / "Reports"
        self.c.settings_service.set_many(OWNER, {"publishing.enabled": True, "publishing.channels": ["folder"], "publishing.folder": str(self.sync)})

    def tearDown(self) -> None:
        set_clock(self._prev)
        self._tmp.cleanup()

    def _q(self, sql: str, *params: object) -> list[tuple]:
        conn = sqlite3.connect(self.paths.database)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _sale(self, qty: str = "2", mode: str = "CASH"):  # type: ignore[no-untyped-def]
        cart = self.c.billing.new_cart(STAFF)
        self.c.billing.add_scan(STAFF, cart, CAMPHOR, qty=Decimal(qty))
        total = self.c.billing.totals(cart).total_paise
        return self.c.billing.checkout(STAFF, cart, [PaymentInput(mode, total, tendered_paise=total if mode == "CASH" else None)])


class RendererTest(unittest.TestCase):
    def test_digest_html_contains_figures_sections_and_no_scripts(self) -> None:
        data = renderer.DigestData(shop_name="Vaishnavi Puja Store", day="14-Nov-2026", net="₹18,420.00", bills=142, avg_bill="₹129.72", items="310", by_mode={"CASH": "₹11,200.00", "UPI": "₹7,220.00"}, tax="₹620.00", discounts="₹310.00", voids=1, overrides=2, variance_lines=["Shift #3 (Ramesh): −₹40.00 short"], low_stock=[("Camphor 50g", "4", "20")], out_of_stock=["Wicks"], insights=["3 items will run out within 7 days"], chain_head="ab12cd34", sparkline=[100, 200, 150], generated_at="21:35")
        html = renderer.render_digest(data)
        for needle in ("Vaishnavi Puja Store", "₹18,420.00", "142", "CASH", "₹11,200.00", "Camphor 50g", "Wicks", "ab12cd34", "<svg", "3 items will run out"):
            self.assertIn(needle, html)
        self.assertNotIn("<script", html)
        self.assertTrue(html.lstrip().startswith("<!doctype html>"))
        lines = renderer.digest_summary(data)
        self.assertEqual(len(lines), 5)
        self.assertIn("₹18,420.00", lines[0])

    def test_write_document_prefers_pdf_but_falls_back_to_html(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = renderer.write_document("<!doctype html><html><body>x</body></html>", Path(d) / "digest-2026-11-14", pdf_engine=None)
            self.assertEqual(path.suffix, ".html")
            self.assertTrue(path.exists())
            fake_pdf = lambda html, out: out.write_bytes(b"%PDF-1.4 fake")  # noqa: E731
            path = renderer.write_document("<html></html>", Path(d) / "weekly", pdf_engine=fake_pdf)
            self.assertEqual(path.suffix, ".pdf")
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))

    def test_letterhead_puts_shop_owner_date_and_title_on_every_document(self) -> None:
        lh = renderer.Letterhead(shop_name="Vaishnavi Puja Store", address="Khandve Nagar, Wagholi, Pune", phone="+911234567890", owner="Radhe", generated_at="21-Nov-2026 18:45 IST", prepared_by="Owner")
        html = renderer.render_table("Sales by day", "21-Nov-2026 18:45 · 2 rows", ["day", "total"], [["2026-11-20", "₹1,000.00"], ["2026-11-21", "₹1,500.00"]], letterhead=lh)
        for needle in ('class="lh"', "Vaishnavi Puja Store", "Khandve Nagar, Wagholi, Pune", "Ph: +911234567890", "Owner: <b>Radhe</b>", "Date &amp; time: <b>21-Nov-2026 18:45 IST</b>", "Prepared by: <b>Owner</b>", "<h1>Sales by day</h1>", "2 rows", "Computer-generated report"):
            self.assertIn(needle, html)
        self.assertIn("<title>Vaishnavi Puja Store — Sales by day</title>", html)
        plain = renderer.render_table("Sales by day", "sub", ["day"], [["x"]])
        self.assertNotIn('class="lh"', plain, "without a letterhead the old plain card header is kept")
        self.assertEqual(lh.lines(), ["Vaishnavi Puja Store", "Khandve Nagar, Wagholi, Pune", "Ph: +911234567890", "Owner: Radhe · Generated: 21-Nov-2026 18:45 IST · Prepared by: Owner"], "CSV/XLSX title rows")
        digest = renderer.DigestData(shop_name="Vaishnavi Puja Store", day="14-Nov-2026", net="₹1.00", bills=1, avg_bill="₹1.00", items="1", by_mode={}, tax="₹0", discounts="₹0", voids=0, overrides=0, variance_lines=[], low_stock=[], out_of_stock=[], insights=[], chain_head="ab", sparkline=[1], generated_at="21:35", letterhead=lh)
        self.assertIn("Owner: <b>Radhe</b>", renderer.render_digest(digest))


class OutboxTest(Base):
    def test_enqueue_due_backoff_and_failed_after_max(self) -> None:
        box = Outbox(self.c.txn, self.c.publish_store, max_attempts=3)
        doc = self.paths.primary / "reports" / "digest.html"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("<html></html>", encoding="utf-8")
        qid = box.enqueue("folder", "digest", doc)
        due = box.due()
        self.assertEqual([d.id for d in due], [qid])
        box.mark_failed(qid, "disk full")
        self.assertEqual(box.due(), [], "first failure waits for the backoff")
        row = self._q("SELECT attempts, status, last_error, next_retry_at FROM publish_queue WHERE id = ?", qid)[0]
        self.assertEqual((row[0], row[1]), (1, "PENDING"))
        self.assertIn("disk full", row[2])
        self.assertTrue(row[3] > "2026-11-14T16:00")
        self.clock.advance(seconds=BACKOFF_MINUTES[0] * 60 + 1)
        self.assertEqual(len(box.due()), 1, "due again after the backoff")
        box.mark_failed(qid, "still")
        self.clock.advance(seconds=BACKOFF_MINUTES[1] * 60 + 1)
        box.mark_failed(qid, "again")
        self.assertEqual(self._q("SELECT status, attempts FROM publish_queue WHERE id = ?", qid)[0], ("FAILED", 3))
        self.assertEqual(box.due(), [])
        other = box.enqueue("email", "digest", doc)
        box.mark_sent(other)
        self.assertEqual(self._q("SELECT status FROM publish_queue WHERE id = ?", other)[0][0], "SENT")
        self.assertEqual(box.counts(), {"PENDING": 0, "SENT": 1, "FAILED": 1})


class ChannelsTest(unittest.TestCase):
    def test_folder_channel_copies_into_month_folder(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "digest-2026-11-14.html"
            src.write_text("<html>hi</html>", encoding="utf-8")
            ch = FolderChannel(Path(d) / "sync")
            ch.send(src, summary=["Today ₹40.00"], subject="Daily digest 14-Nov-2026")
            copied = list((Path(d) / "sync" / "2026-11").glob("digest-2026-11-14.html"))
            self.assertEqual(len(copied), 1)
            with self.assertRaises(PublishError) as ctx:
                FolderChannel(Path(d) / "missing-drive" / "x", must_exist=True).send(src, summary=[], subject="s")
            self.assertEqual(ctx.exception.code, "E-PUB-001")

    def test_smtp_channel_builds_a_multipart_mail_with_attachment(self) -> None:
        sent: list[Any] = []

        class FakeSMTP:
            def __init__(self, host: str, port: int, timeout: float = 0) -> None:
                sent.append(("connect", host, port))

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *a: Any) -> None:
                sent.append(("quit",))

            def starttls(self) -> None:
                sent.append(("starttls",))

            def login(self, user: str, password: str) -> None:
                sent.append(("login", user, password))

            def send_message(self, msg: Any) -> None:
                sent.append(("message", msg))

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "digest.html"
            src.write_text("<html>hi</html>", encoding="utf-8")
            ch = SmtpChannel(host="smtp.gmail.com", port=587, user="shop@gmail.com", password="app-pass", sender="shop@gmail.com", to="owner@example.com", smtp_factory=FakeSMTP)
            ch.send(src, summary=["Today ₹40.00", "2 bills"], subject="Daily digest")
        kinds = [s[0] for s in sent]
        self.assertEqual(kinds, ["connect", "starttls", "login", "message", "quit"])
        msg = sent[3][1]
        self.assertEqual(msg["To"], "owner@example.com")
        self.assertEqual(msg["Subject"], "Daily digest")
        self.assertTrue(msg.is_multipart())
        names = [p.get_filename() for p in msg.iter_attachments()]
        self.assertEqual(names, ["digest.html"])
        self.assertIn("Today ₹40.00", msg.get_body(preferencelist=("plain",)).get_content())

    def test_telegram_and_whatsapp_channels_post_summary_and_document(self) -> None:
        calls: list[tuple[str, Any]] = []

        def fake_post(url: str, *, data: Any = None, files: Any = None, headers: Any = None, json_body: Any = None) -> dict[str, Any]:
            calls.append((url, {"data": data, "files": files, "json": json_body, "headers": headers}))
            return {"ok": True}

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "digest.html"
            src.write_text("<html>hi</html>", encoding="utf-8")
            TelegramChannel(token="123:ABC", chat_id="42", post=fake_post).send(src, summary=["Today ₹40.00"], subject="Digest")
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0][0].endswith("/bot123:ABC/sendMessage"))
            self.assertEqual(calls[0][1]["data"]["chat_id"], "42")
            self.assertIn("Today ₹40.00", calls[0][1]["data"]["text"])
            self.assertTrue(calls[1][0].endswith("/sendDocument"))
            self.assertEqual(calls[1][1]["files"]["document"][0], "digest.html")
            calls.clear()
            WhatsAppChannel(token="EAAx", phone_id="555", to="919999999999", post=fake_post).send(src, summary=["Today ₹40.00"], subject="Digest")
            self.assertEqual(len(calls), 1)
            self.assertIn("/555/messages", calls[0][0])
            self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer EAAx")
            self.assertEqual(calls[0][1]["json"]["to"], "919999999999")
            self.assertIn("Today ₹40.00", calls[0][1]["json"]["text"]["body"])

            def failing(url: str, **kw: Any) -> dict[str, Any]:
                raise OSError("no internet")

            with self.assertRaises(PublishError) as ctx:
                TelegramChannel(token="t", chat_id="c", post=failing).send(src, summary=[], subject="s")
            self.assertEqual(ctx.exception.code, "E-PUB-003")


class PublisherTest(Base):
    def test_daily_digest_renders_queues_and_delivers_to_the_folder(self) -> None:
        self._sale()
        self._sale(mode="UPI")
        result = self.c.publisher.daily_digest()
        self.assertTrue(result.path.exists())
        self.assertEqual(result.path.suffix, ".html", "no PDF engine on the dev PC → HTML")
        html = result.path.read_text(encoding="utf-8")
        self.assertIn("₹80.00", html, "2 fixture bills + 2 here")
        self.assertIn("UPI", html)
        self.assertIn("Camphor 50g", html)
        self.assertEqual(result.queued, ["folder"])
        self.assertEqual(result.sent, ["folder"])
        self.assertTrue(list((self.sync / "2026-11").glob("digest-2026-11-14.html")))
        self.assertEqual(self._q("SELECT channel, document, status FROM publish_queue"), [("folder", "digest", "SENT")])
        self.assertEqual(self.c.publisher.chip(), ("green", "Publish: sent 21:30"))
        self.assertIn("₹80.00", result.summary[0])

    def test_failed_channel_is_queued_and_retried_by_flush(self) -> None:
        self.c.settings_service.set_many(OWNER, {"publishing.channels": ["folder", "email"], "publishing.smtp_host": "smtp.example.com", "publishing.email_to": "owner@example.com", "publishing.smtp_user": "shop@example.com"})
        self.c.secure_settings.set(OWNER, "publish.smtp_password", "app-pass")
        attempts: list[str] = []

        def smtp_factory(host: str, port: int, timeout: float = 0) -> Any:
            attempts.append(host)
            raise OSError("network unreachable")

        self.c.publisher.smtp_factory = smtp_factory
        result = self.c.publisher.daily_digest()
        self.assertEqual(sorted(result.queued), ["email", "folder"])
        self.assertEqual(result.sent, ["folder"])
        self.assertEqual(result.failed, ["email"])
        self.assertEqual(self._q("SELECT status, attempts FROM publish_queue WHERE channel = 'email'")[0], ("PENDING", 1))
        self.assertEqual(self.c.publisher.chip()[0], "amber")
        self.assertEqual(self.c.publisher.flush(), (0, 0), "not due yet")
        self.clock.advance(seconds=BACKOFF_MINUTES[0] * 60 + 5)
        self.c.publisher.smtp_factory = _OkSmtpFactory()
        self.assertEqual(self.c.publisher.flush(), (1, 0))
        self.assertEqual(self._q("SELECT status FROM publish_queue WHERE channel = 'email'")[0][0], "SENT")
        self.assertEqual(len(attempts), 1)

    def test_weekly_report_and_disabled_publishing(self) -> None:
        self._sale()
        weekly = self.c.publisher.weekly_report()
        html = weekly.path.read_text(encoding="utf-8")
        self.assertIn("Top sellers", html)
        self.assertIn("Reorder", html)
        self.assertIn("Camphor 50g", html)
        self.c.settings_service.set_many(OWNER, {"publishing.enabled": False})
        off = self.c.publisher.daily_digest()
        self.assertEqual((off.queued, off.sent), ([], []), "disabled → rendered to the reports folder only, nothing queued")
        self.assertTrue(off.path.exists())
        self.assertEqual(self.c.publisher.chip()[0], "grey")

    def test_every_document_carries_the_shop_letterhead_from_settings(self) -> None:
        """Owner's complaint: the sales report had no shop name, date, address, owner or phone — now every document does."""
        from vaishnavi.ui.pages import reports as reports_page

        self.c.settings_service.set_many(OWNER, {"shop.address_line1": "Khandve Nagar, Wagholi", "shop.address_line2": "Pune", "shop.phone": "+911234567890", "shop.owner_name": "Radhe"})
        lh = self.c.publisher.letterhead("Sales by day", "x", prepared_by="Owner")
        self.assertEqual((lh.shop_name, lh.address, lh.phone, lh.owner, lh.generated_at), ("Vaishnavi Puja Store", "Khandve Nagar, Wagholi, Pune", "+911234567890", "Radhe", "14-Nov-2026 21:30 IST"))
        self._sale()
        path = reports_page.export_document(self.c, EN, OWNER, "sales")
        assert path is not None
        html = path.read_text(encoding="utf-8")
        for needle in ("Vaishnavi Puja Store", "Khandve Nagar, Wagholi, Pune", "Ph: +911234567890", "Owner: <b>Radhe</b>", "14-Nov-2026 21:30 IST", "Prepared by: <b>Owner</b>", "<h1>Sales by month — Nov-2026</h1>"):
            self.assertIn(needle, html)
        digest = self.c.publisher.daily_digest().path.read_text(encoding="utf-8")
        self.assertIn("Owner: <b>Radhe</b>", digest)
        self.assertIn("Khandve Nagar, Wagholi, Pune", digest)
        csv = reports_page.export_csv(self.c, OWNER, "sales", texts=EN)
        self.assertEqual(csv.splitlines()[:3], ["\ufeffVaishnavi Puja Store", '"Khandve Nagar, Wagholi, Pune"', "Ph: +911234567890"])
        self.assertIn("Owner: Radhe · Generated: 14-Nov-2026 21:30 IST · Prepared by: Owner", csv)
        self.assertIn("\nday,bills,", csv)
        import io
        import zipfile

        xlsx = reports_page.export_xlsx(self.c, EN, OWNER, "sales")
        assert xlsx is not None
        with zipfile.ZipFile(io.BytesIO(xlsx)) as zf:
            sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertLess(sheet.index("Vaishnavi Puja Store"), sheet.index("Owner: Radhe"))
        self.assertLess(sheet.index("Owner: Radhe"), sheet.index(">Day<"), "letterhead rows come before the column header")

    def test_settings_tab_model_and_save(self) -> None:
        m = settings_publishing.build_publishing_model(self.state, EN)
        self.assertTrue(m.enabled)
        self.assertEqual(m.channels, ["folder"])
        self.assertEqual(m.folder, str(self.sync))
        self.assertEqual([c.key for c in m.channel_options], ["folder", "email", "telegram", "whatsapp"])
        self.assertFalse(m.secrets_set["publish.smtp_password"])
        out = settings_publishing.save_publishing(self.state, OWNER, enabled=True, channels=["folder", "telegram"], folder=str(self.sync), telegram_chat_id="42", telegram_token="123:ABC", digest_time="21:40")
        self.assertTrue(out.ok, out.message)
        eff = self.c.settings_service.effective().publishing
        self.assertEqual((eff.channels, eff.telegram_chat_id, eff.digest_time), (["folder", "telegram"], "42", "21:40"))
        self.assertEqual(self.c.secure_settings.reveal_for_system("publish.telegram_token"), "123:ABC")
        m = settings_publishing.build_publishing_model(self.state, EN)
        self.assertTrue(m.secrets_set["publish.telegram_token"])
        bad = settings_publishing.save_publishing(self.state, OWNER, enabled=True, channels=["email"], email_to="", smtp_host="")
        self.assertFalse(bad.ok)
        test = settings_publishing.test_channel(self.state, OWNER, "folder")
        self.assertTrue(test.ok, test.message)
        self.assertTrue(list((self.sync / "2026-11").glob("test-*.html")))


class _OkSmtpFactory:
    def __call__(self, host: str, port: int, timeout: float = 0) -> Any:
        outer = self

        class Ok:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def starttls(self) -> None:
                return None

            def login(self, u: str, p: str) -> None:
                outer.logged = (u, p)

            def send_message(self, msg: Any) -> None:
                outer.msg = msg

        return Ok()


if __name__ == "__main__":
    unittest.main()
