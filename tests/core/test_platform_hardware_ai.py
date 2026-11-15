"""Platform adapters, hardware/AI/system/code-integrity doctor checks, Ollama manager, provider base (docs/07 §5, docs/10 §7/§9).

Written before the code (docs/13 rule 7). OS commands are replaced by recorded outputs; Ollama by a local
stdlib HTTP server; USB/serial by fixture trees. Promises covered: paired scanner presence is a start-up
check (strict mode), printer is never a blocker, Ollama service + both models are verified (and started /
pulled with --fix), cloud providers need internet + key + budget, application files must match the
release manifest (E-SYS-040), system components reported in plain words.
"""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from vaishnavi.ai.providers.base import ChatMessage, ProviderHealth
from vaishnavi.ai.providers.fake_provider import FakeProvider
from vaishnavi.ai.providers.null_provider import NullProvider
from vaishnavi.bootstrap import ollama_manager as om
from vaishnavi.bootstrap.checks import ai_provider as c_ai
from vaishnavi.bootstrap.checks import code_integrity as c_code
from vaishnavi.bootstrap.checks import hardware as c_hw
from vaishnavi.bootstrap.checks import system_components as c_sys
from vaishnavi.bootstrap.doctor import Status
from vaishnavi.core.errors import AIError
from vaishnavi.core.settings import Settings
from vaishnavi.platform import browser, devices, fonts, printing, serial_ports

from tests.core.test_doctor import _ctx

LINUX_SYSFS = {
    "1-1": {"idVendor": "0c2e", "idProduct": "0b61", "product": "Honeywell Scanner", "manufacturer": "Honeywell"},
    "1-2": {"idVendor": "04b8", "idProduct": "0202", "product": "TM-T82", "manufacturer": "EPSON", "bDeviceClass": "07"},
}
WINDOWS_PNP_JSON = json.dumps(
    [
        {"InstanceId": "USB\\VID_0C2E&PID_0B61\\S123", "FriendlyName": "USB Input Device", "Status": "OK", "Class": "HIDClass"},
        {"InstanceId": "USB\\VID_04B8&PID_0202\\6&1", "FriendlyName": "EPSON TM-T82", "Status": "OK", "Class": "USB"},
        {"InstanceId": "PCI\\VEN_8086", "FriendlyName": "Intel thing", "Status": "OK", "Class": "System"},
    ]
)
MAC_PROFILER_JSON = json.dumps({"SPUSBDataType": [{"_items": [{"_name": "Honeywell Scanner", "vendor_id": "0x0c2e", "product_id": "0x0b61", "manufacturer": "Honeywell"}, {"_name": "Hub", "_items": [{"_name": "TM-T82", "vendor_id": "0x04b8  (Seiko Epson)", "product_id": "0x0202"}]}]}]})
LPSTAT = "printer TM-T82 is idle.  enabled since Fri 14 Nov 2026\nprinter Office_HP disabled since Thu 13 Nov 2026 -\n\treason: paper out\nsystem default destination: TM-T82\n"
GET_PRINTER_JSON = json.dumps([{"Name": "EPSON TM-T82 Receipt", "PrinterStatus": "Normal", "PortName": "USB001", "Default": True}, {"Name": "Microsoft Print to PDF", "PrinterStatus": "Normal", "PortName": "PORTPROMPT:", "Default": False}, {"Name": "HP Office", "PrinterStatus": "Offline", "PortName": "WSD-1", "Default": False}])


# ============================================================================ platform adapters
class DevicesTest(unittest.TestCase):
    def test_linux_sysfs_tree_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for node, attrs in LINUX_SYSFS.items():
                (root / node).mkdir()
                for k, v in attrs.items():
                    (root / node / k).write_text(v + "\n", encoding="utf-8")
            found = devices.parse_linux_sysfs(root)
        self.assertEqual({(d.vid, d.pid) for d in found}, {("0C2E", "0B61"), ("04B8", "0202")})
        self.assertEqual(next(d for d in found if d.vid == "04B8").name, "TM-T82")

    def test_windows_and_macos_outputs_are_parsed(self) -> None:
        win = devices.parse_windows_pnp(WINDOWS_PNP_JSON)
        self.assertEqual({(d.vid, d.pid) for d in win}, {("0C2E", "0B61"), ("04B8", "0202")}, "non-USB entries are ignored")
        mac = devices.parse_macos_profiler(MAC_PROFILER_JSON)
        self.assertEqual({(d.vid, d.pid) for d in mac}, {("0C2E", "0B61"), ("04B8", "0202")}, "nested hubs are walked; vendor text after the id is ignored")

    def test_find_by_vid_pid_is_case_and_separator_insensitive(self) -> None:
        listed = devices.parse_windows_pnp(WINDOWS_PNP_JSON)
        with mock.patch.object(devices, "list_usb_devices", return_value=listed):
            self.assertIsNotNone(devices.find_device("0c2e:0b61"))
            self.assertIsNotNone(devices.find_device("0C2E-0B61"))
            self.assertIsNone(devices.find_device("1234:5678"))
        self.assertEqual(devices.normalise_vid_pid("c2e:b61"), "0C2E:0B61")
        with self.assertRaises(ValueError):
            devices.normalise_vid_pid("nonsense")

    def test_enumeration_failure_is_empty_list_not_exception(self) -> None:
        with mock.patch.object(devices.subprocess, "run", side_effect=OSError("no powershell")), mock.patch.object(devices.sys, "platform", "win32"):
            self.assertEqual(devices.list_usb_devices(), [])


class PrintingTest(unittest.TestCase):
    def test_lpstat_and_get_printer_outputs_are_parsed(self) -> None:
        cups = printing.parse_lpstat(LPSTAT)
        self.assertEqual([(p.name, p.ready, p.is_default) for p in cups], [("TM-T82", True, True), ("Office_HP", False, False)])
        self.assertIn("paper out", cups[1].detail)
        win = printing.parse_get_printer(GET_PRINTER_JSON)
        self.assertEqual([(p.name, p.ready) for p in win], [("EPSON TM-T82 Receipt", True), ("Microsoft Print to PDF", True), ("HP Office", False)])
        self.assertTrue(win[0].is_default)

    def test_network_printer_readiness_is_a_tcp_connect(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(printing.network_printer_ready(f"127.0.0.1:{port}", timeout=1))
        finally:
            srv.close()
        self.assertFalse(printing.network_printer_ready(f"127.0.0.1:{port}", timeout=0.3))
        self.assertFalse(printing.network_printer_ready("not-an-address", timeout=0.3))

    def test_status_for_configured_printer_kinds(self) -> None:
        settings = Settings()
        self.assertEqual(printing.printer_status(settings.hardware).kind, "none")
        settings.hardware.printer_kind = "system"
        settings.hardware.printer_address = "EPSON TM-T82 Receipt"
        with mock.patch.object(printing, "list_system_printers", return_value=printing.parse_get_printer(GET_PRINTER_JSON)):
            self.assertTrue(printing.printer_status(settings.hardware).ready)
            settings.hardware.printer_address = "Gone Printer"
            status = printing.printer_status(settings.hardware)
        self.assertFalse(status.ready)
        self.assertIn("not found", status.detail)
        settings.hardware.printer_kind = "usb"
        settings.hardware.printer_address = "04B8:0202"
        with mock.patch.object(devices, "list_usb_devices", return_value=devices.parse_windows_pnp(WINDOWS_PNP_JSON)):
            self.assertTrue(printing.printer_status(settings.hardware).ready)


class SerialFontsBrowserTest(unittest.TestCase):
    def test_serial_ports_listed_per_os(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp)
            (dev / "ttyUSB0").write_text("", encoding="utf-8")
            (dev / "ttyACM1").write_text("", encoding="utf-8")
            (dev / "tty.usbserial-14").write_text("", encoding="utf-8")
            (dev / "sda").write_text("", encoding="utf-8")
            ports = serial_ports.scan_dev(dev)
        self.assertEqual(sorted(ports), sorted([str(dev / "ttyUSB0"), str(dev / "ttyACM1"), str(dev / "tty.usbserial-14")]))
        self.assertTrue(serial_ports.port_present("COM3", ["COM3", "COM9"]))
        self.assertFalse(serial_ports.port_present("COM4", ["COM3"]))

    def test_devanagari_font_lookup_uses_known_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fontdir = Path(tmp)
            self.assertIsNone(fonts.find_devanagari_font([fontdir]))
            (fontdir / "NotoSansDevanagari-Regular.ttf").write_bytes(b"x")
            found = fonts.find_devanagari_font([fontdir])
        self.assertIsNotNone(found)
        self.assertTrue(str(found).endswith("NotoSansDevanagari-Regular.ttf"))

    def test_browser_command_prefers_app_mode_and_falls_back(self) -> None:
        with mock.patch.object(browser.shutil, "which", side_effect=lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None):
            cmd = browser.kiosk_command("http://127.0.0.1:8080", kiosk=True)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd[0], "/usr/bin/google-chrome")
        self.assertIn("--app=http://127.0.0.1:8080", cmd)
        with mock.patch.object(browser.shutil, "which", return_value=None), mock.patch.object(browser, "_candidate_paths", return_value=[]):
            self.assertIsNone(browser.kiosk_command("http://127.0.0.1:8080", kiosk=True))
        opened: list[str] = []
        with mock.patch.object(browser.shutil, "which", return_value=None), mock.patch.object(browser, "_candidate_paths", return_value=[]), mock.patch.object(browser.webbrowser, "open", lambda url, **kw: opened.append(url) or True):
            self.assertTrue(browser.open_url("http://127.0.0.1:8080", kiosk=True))
        self.assertEqual(opened, ["http://127.0.0.1:8080"])


# ============================================================================ Ollama manager (fake server)
class FakeOllama(BaseHTTPRequestHandler):
    models: list[str] = ["qwen2.5:3b-instruct", "nomic-embed-text"]
    pulled: list[str] = []
    fail_generate = False

    def log_message(self, *args: object) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": m, "size": 1000} for m in self.models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/pull":
            name = payload.get("name") or payload.get("model")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in ({"status": "pulling manifest"}, {"status": "downloading", "completed": 50, "total": 100}, {"status": "downloading", "completed": 100, "total": 100}, {"status": "success"}):
                self.wfile.write((json.dumps(line) + "\n").encode())
            type(self).pulled.append(name)
            type(self).models.append(name)
        elif self.path == "/api/generate":
            if self.fail_generate:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"response": "", "done": True, "eval_duration": 1}).encode())
        elif self.path == "/api/embeddings":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"embedding": [0.1, 0.2]}).encode())
        else:
            self.send_response(404)
            self.end_headers()


class OllamaManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeOllama.models = ["qwen2.5:3b-instruct", "nomic-embed-text"]
        FakeOllama.pulled = []
        FakeOllama.fail_generate = False

    def test_is_running_and_models_present(self) -> None:
        mgr = om.OllamaManager(self.host)
        self.assertTrue(mgr.is_running())
        self.assertEqual(set(mgr.list_models()), {"qwen2.5:3b-instruct", "nomic-embed-text"})
        self.assertTrue(mgr.has_model("qwen2.5:3b-instruct"))
        self.assertTrue(mgr.has_model("qwen2.5:3b"), "a tag without ':latest' or variant still matches the installed name prefix")
        self.assertFalse(mgr.has_model("llama3"))
        self.assertFalse(om.OllamaManager("http://127.0.0.1:1").is_running(), "nothing listening → not running, no exception")

    def test_pull_streams_progress_and_warm_up(self) -> None:
        mgr = om.OllamaManager(self.host)
        progress: list[tuple[str, int]] = []
        ok = mgr.pull("llama3.2:1b", on_progress=lambda status, pct: progress.append((status, pct)))
        self.assertTrue(ok)
        self.assertEqual(FakeOllama.pulled, ["llama3.2:1b"])
        self.assertIn(("downloading", 100), progress)
        self.assertTrue(mgr.has_model("llama3.2:1b"))
        ms = mgr.warm_up("qwen2.5:3b-instruct")
        self.assertIsNotNone(ms)
        FakeOllama.fail_generate = True
        self.assertIsNone(mgr.warm_up("qwen2.5:3b-instruct"))

    def test_ensure_models_pulls_only_missing(self) -> None:
        mgr = om.OllamaManager(self.host)
        missing = mgr.missing_models(["qwen2.5:3b-instruct", "nomic-embed-text", "newmodel"])
        self.assertEqual(missing, ["newmodel"])
        self.assertEqual(mgr.ensure_models(["qwen2.5:3b-instruct", "newmodel"]), ["newmodel"])
        self.assertEqual(FakeOllama.pulled, ["newmodel"])

    def test_start_uses_the_binary_and_waits_for_the_port(self) -> None:
        mgr = om.OllamaManager("http://127.0.0.1:1", binary="/fake/ollama")
        started: list[list[str]] = []
        with mock.patch.object(om.subprocess, "Popen", side_effect=lambda cmd, **kw: started.append(cmd) or mock.Mock(pid=4242)):
            self.assertFalse(mgr.start(wait_seconds=0.3), "port never answers → False, but the command was issued")
        self.assertEqual(started[0][:2], ["/fake/ollama", "serve"])
        self.assertFalse(om.OllamaManager("http://127.0.0.1:1", binary=None).start(wait_seconds=0.1), "no binary → cannot start")


# ============================================================================ provider base
class ProvidersTest(unittest.TestCase):
    def test_null_provider_is_off_and_refuses_to_chat(self) -> None:
        p = NullProvider()
        self.assertEqual(p.name, "none")
        self.assertFalse(p.health().ok)
        with self.assertRaises(AIError) as ctx:
            p.chat([ChatMessage("user", "hi")])
        self.assertEqual(ctx.exception.code, "E-AI-010")
        with self.assertRaises(AIError):
            p.embed(["x"])

    def test_fake_provider_replays_scripted_answers_and_records_calls(self) -> None:
        p = FakeProvider(replies=["first", "second"], embedding_dim=4)
        r1 = p.chat([ChatMessage("system", "s"), ChatMessage("user", "q1")])
        r2 = p.chat([ChatMessage("user", "q2")])
        self.assertEqual((r1.text, r2.text), ("first", "second"))
        self.assertEqual(p.chat([ChatMessage("user", "q3")]).text, "second", "last reply repeats")
        self.assertEqual(len(p.calls), 3)
        self.assertEqual(p.calls[0][-1].content, "q1")
        vec = p.embed(["a", "b"])
        self.assertEqual((len(vec), len(vec[0])), (2, 4))
        self.assertEqual(p.embed(["a"])[0], vec[0], "deterministic")
        self.assertTrue(p.health().ok)
        self.assertIsInstance(p.health(), ProviderHealth)


# ============================================================================ doctor checks
class SystemAndCodeChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_system_components_reports_each_missing_component_in_plain_words(self) -> None:
        check = c_sys.SystemComponentsCheck(probes={"libusb": lambda: True, "print_system": lambda: True, "devanagari_font": lambda: True})
        self.assertIs(check.run(_ctx(self.tmp)).status, Status.PASS)
        check = c_sys.SystemComponentsCheck(probes={"libusb": lambda: False, "print_system": lambda: True, "devanagari_font": lambda: False})
        result = check.run(_ctx(self.tmp))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SYS-003"))
        self.assertIn("USB", result.message)
        self.assertIn("Hindi", result.message)
        self.assertEqual(sorted(result.data["missing"]), ["devanagari_font", "libusb"])

    def test_code_integrity_manifest_round_trip_and_tamper_detection(self) -> None:
        prog = self.tmp / "prog"
        (prog / "src" / "vaishnavi").mkdir(parents=True)
        (prog / "src" / "vaishnavi" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (prog / "bootstrap").mkdir()
        (prog / "bootstrap" / "cli.py").write_text("x = 1\n", encoding="utf-8")
        (prog / "main.py").write_bytes(b"pass\n")  # bytes: Windows text mode would write \r\n
        (prog / "src" / "vaishnavi" / "__pycache__").mkdir()
        (prog / "src" / "vaishnavi" / "__pycache__" / "a.pyc").write_bytes(b"\0")
        ctx = _ctx(self.tmp, program_dir=prog)
        check = c_code.CodeIntegrityCheck()
        result = check.run(ctx)
        self.assertIs(result.status, Status.WARN, "no manifest → development checkout, say so")
        manifest = c_code.write_manifest(prog)
        self.assertTrue(manifest.exists())
        entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
        self.assertIn("src/vaishnavi/a.py", entries)
        self.assertNotIn("src/vaishnavi/__pycache__/a.pyc", entries, "caches are not part of the release")
        self.assertEqual(entries["main.py"], hashlib.sha256(b"pass\n").hexdigest())
        self.assertIs(check.run(ctx).status, Status.PASS)
        (prog / "src" / "vaishnavi" / "a.py").write_text("print(2)\n", encoding="utf-8")
        result = check.run(ctx)
        self.assertEqual((result.status, result.error_code), (Status.FAIL, "E-SYS-040"))
        self.assertIn("a.py", result.detail)
        (prog / "src" / "vaishnavi" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (prog / "src" / "vaishnavi" / "evil.py").write_text("", encoding="utf-8")
        result = check.run(ctx)
        self.assertEqual((result.status, result.error_code), (Status.FAIL, "E-SYS-040"), "an added file is also a change")


class HardwareCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.listed = devices.parse_windows_pnp(WINDOWS_PNP_JSON)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, settings: Settings, *, present: list[devices.UsbDevice] | None = None, ports: list[str] | None = None) -> c_hw.CheckResult:
        with mock.patch.object(devices, "list_usb_devices", return_value=self.listed if present is None else present), mock.patch.object(serial_ports, "list_ports", return_value=ports or []), mock.patch.object(printing, "list_system_printers", return_value=printing.parse_get_printer(GET_PRINTER_JSON)):
            return c_hw.HardwareCheck().run(_ctx(self.tmp, settings=settings))

    def test_no_scanner_paired_yet_is_a_note_not_a_blocker(self) -> None:
        result = self._run(Settings())
        self.assertIs(result.status, Status.WARN)
        self.assertEqual(result.error_code, "E-SCN-001")
        self.assertFalse(result.data["scanner_present"])
        self.assertIn("printer", result.message.lower())

    def test_paired_scanner_present_passes_and_absent_warns_with_strict_mode_note(self) -> None:
        settings = Settings()
        settings.hardware.scanner_vid_pid = "0C2E:0B61"
        result = self._run(settings)
        self.assertIs(result.status, Status.PASS)
        self.assertTrue(result.data["scanner_present"])
        result = self._run(settings, present=[])
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SCN-001"))
        self.assertTrue(result.data["billing_paused_for_staff"], "strict mode: staff cannot sell until the scanner is back")

    def test_printer_is_never_a_blocker_and_states_what_happens(self) -> None:
        settings = Settings()
        settings.hardware.scanner_vid_pid = "0C2E:0B61"
        result = self._run(settings)
        self.assertEqual(result.data["printer"]["kind"], "none")
        settings.hardware.printer_kind = "network"
        settings.hardware.printer_address = "127.0.0.1:1"
        result = self._run(settings)
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-PRN-001"))
        self.assertFalse(result.data["printer"]["ready"])
        settings.hardware.printer_kind = "system"
        settings.hardware.printer_address = "EPSON TM-T82 Receipt"
        self.assertIs(self._run(settings).status, Status.PASS)

    def test_scale_port_missing_warns_only_when_configured(self) -> None:
        settings = Settings()
        settings.hardware.scanner_vid_pid = "0C2E:0B61"
        settings.hardware.scale_port = "COM7"
        result = self._run(settings, ports=["COM3"])
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-SCALE-001"))
        self.assertIs(self._run(settings, ports=["COM7"]).status, Status.PASS)


class AiProviderCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_provider_none_is_a_pass_that_says_the_assistant_is_off(self) -> None:
        settings = Settings()
        settings.ai.provider = "none"
        result = c_ai.AiProviderCheck().run(_ctx(self.tmp, settings=settings))
        self.assertIs(result.status, Status.PASS)
        self.assertIn("off", result.message.lower())

    def test_ollama_not_running_warns_and_fix_starts_it_then_pulls_missing_models(self) -> None:
        settings = Settings()
        mgr = mock.Mock(spec=om.OllamaManager)
        mgr.binary = "/usr/bin/ollama"
        mgr.is_running.return_value = False
        check = c_ai.AiProviderCheck(manager_factory=lambda s: mgr)
        result = check.run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-001"))
        mgr.start.return_value = True
        mgr.is_running.side_effect = [False, True, True, True, True]  # fix: not running → started → running afterwards
        mgr.missing_models.return_value = ["nomic-embed-text"]
        mgr.ensure_models.return_value = ["nomic-embed-text"]
        mgr.warm_up.return_value = 1900
        fixed = check.fix(_ctx(self.tmp, settings=settings, fix=True))
        assert fixed is not None
        mgr.start.assert_called_once()
        mgr.ensure_models.assert_called_once()
        mgr.is_running.side_effect = None
        mgr.is_running.return_value = True
        mgr.missing_models.return_value = []
        self.assertIs(check.run(_ctx(self.tmp, settings=settings)).status, Status.PASS)

    def test_ollama_missing_model_warns_e_ai_004_and_missing_binary_e_ai_002(self) -> None:
        settings = Settings()
        mgr = mock.Mock(spec=om.OllamaManager)
        mgr.binary = "/usr/bin/ollama"
        mgr.is_running.return_value = True
        mgr.missing_models.return_value = ["nomic-embed-text"]
        result = c_ai.AiProviderCheck(manager_factory=lambda s: mgr).run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-004"))
        self.assertIn("nomic-embed-text", result.message)
        mgr.binary = None
        mgr.is_running.return_value = False
        result = c_ai.AiProviderCheck(manager_factory=lambda s: mgr).run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-002"))

    def test_cloud_provider_needs_internet_key_and_budget(self) -> None:
        settings = Settings()
        settings.ai.provider = "openai"
        check = c_ai.AiProviderCheck(internet_probe=lambda host: False, key_present=lambda ctx, key: True, budget_used_pct=lambda ctx: 10)
        result = check.run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-020"))
        check = c_ai.AiProviderCheck(internet_probe=lambda host: True, key_present=lambda ctx, key: False, budget_used_pct=lambda ctx: 10)
        result = check.run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-011"))
        check = c_ai.AiProviderCheck(internet_probe=lambda host: True, key_present=lambda ctx, key: True, budget_used_pct=lambda ctx: 95)
        result = check.run(_ctx(self.tmp, settings=settings))
        self.assertEqual((result.status, result.error_code), (Status.WARN, "E-AI-030"))
        check = c_ai.AiProviderCheck(internet_probe=lambda host: True, key_present=lambda ctx, key: True, budget_used_pct=lambda ctx: 12)
        result = check.run(_ctx(self.tmp, settings=settings))
        self.assertIs(result.status, Status.PASS)
        self.assertIn("12", result.message)


if __name__ == "__main__":
    unittest.main()
