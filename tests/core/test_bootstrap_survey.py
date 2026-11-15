"""Hardware survey: parsers, AI recommendation table, persistence, stage (stdlib only)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from bootstrap import survey as sv
from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore
from bootstrap.survey import Gpu, Survey, SurveyStage, classify_vendor, recommend_ai, summary_line


class ParsersTest(unittest.TestCase):
    def test_os_release(self) -> None:
        self.assertEqual(sv.parse_os_release('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n'), "Ubuntu 24.04.1 LTS")
        self.assertEqual(sv.parse_os_release("NAME=x"), "")

    def test_meminfo(self) -> None:
        self.assertEqual(sv.parse_meminfo("MemTotal:       16318812 kB\nMemFree: 1 kB"), 15.6)
        self.assertIsNone(sv.parse_meminfo("nothing"))

    def test_nvidia_smi(self) -> None:
        gpus = sv.parse_nvidia_smi("NVIDIA GeForce RTX 3060, 12288\nNVIDIA T400, 2048\n")
        self.assertEqual([(g.name, g.vram_gib, g.vendor) for g in gpus], [("NVIDIA GeForce RTX 3060", 12.0, "nvidia"), ("NVIDIA T400", 2.0, "nvidia")])

    def test_vendor_classification(self) -> None:
        self.assertEqual(classify_vendor("Intel(R) UHD Graphics 770"), "intel")
        self.assertEqual(classify_vendor("AMD Radeon RX 6600"), "amd")
        self.assertEqual(classify_vendor("GeForce RTX 4060"), "nvidia")
        self.assertEqual(classify_vendor("Mystery"), "unknown")


class RecommendationTest(unittest.TestCase):
    """The table from docs/06 §2.1–2.2."""

    def test_16gb_no_gpu_is_local_3b(self) -> None:
        r = recommend_ai(Survey(ram_gib=16.0, internet=True))
        self.assertEqual((r.provider, r.local_tier, r.local_possible), ("ollama", "3b", True))

    def test_8gb_no_gpu_with_internet_recommends_cloud_but_local_small_possible(self) -> None:
        r = recommend_ai(Survey(ram_gib=8.0, internet=True))
        self.assertEqual((r.provider, r.local_tier, r.local_possible), ("openai", "1.5b", True))

    def test_8gb_no_internet_falls_back_to_small_local(self) -> None:
        r = recommend_ai(Survey(ram_gib=8.0, internet=False))
        self.assertEqual((r.provider, r.local_tier), ("ollama", "1.5b"))

    def test_4gb_no_internet_is_none(self) -> None:
        r = recommend_ai(Survey(ram_gib=4.0, internet=False))
        self.assertEqual((r.provider, r.local_possible), ("none", False))

    def test_nvidia_6gb_with_16gb_is_7b(self) -> None:
        r = recommend_ai(Survey(ram_gib=16.0, gpus=[Gpu("RTX 3060", 12.0, "nvidia")], internet=True))
        self.assertEqual((r.provider, r.local_tier), ("ollama", "7b"))

    def test_apple_silicon_16gb_is_7b(self) -> None:
        r = recommend_ai(Survey(ram_gib=16.0, apple_silicon=True, internet=True))
        self.assertEqual(r.local_tier, "7b")

    def test_32gb_cpu_only_is_3b(self) -> None:
        self.assertEqual(recommend_ai(Survey(ram_gib=32.0)).local_tier, "3b")

    def test_small_nvidia_does_not_unlock_7b(self) -> None:
        r = recommend_ai(Survey(ram_gib=16.0, gpus=[Gpu("T400", 2.0, "nvidia")]))
        self.assertEqual(r.local_tier, "3b")

    def test_unknown_ram_tries_local(self) -> None:
        r = recommend_ai(Survey())
        self.assertEqual((r.provider, r.reason_key), ("ollama", "survey.ai.unknown_ram"))


class SurveyModelTest(unittest.TestCase):
    def test_roundtrip_and_tolerance(self) -> None:
        s = Survey(os_family="linux", ram_gib=15.6, gpus=[Gpu("x", 4.0, "nvidia")], printers=["EPSON"])
        data = json.loads(json.dumps(s.to_dict()))
        data["future_field"] = 1
        back = Survey.from_dict(data)
        self.assertEqual(back.gpus[0].vram_gib, 4.0)
        self.assertEqual(back.printers, ["EPSON"])
        self.assertEqual(back.nvidia_vram_gib, 4.0)

    def test_summary_line_english_and_hindi(self) -> None:
        s = Survey(os_name="Windows", os_version="11 (10.0.26100)", ram_gib=16.0)
        en = summary_line(s, recommend_ai(s), Messages.load("en").t)
        self.assertEqual(en, "Windows 11, 16 GB memory, no graphics card → local AI will work on this PC")
        hi = summary_line(s, recommend_ai(s), Messages.load("hi").t)
        self.assertIn("16 GB मेमोरी", hi)

    def test_write_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x" / "survey.json"
            sv.write_survey(Survey(hostname="pc"), path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["hostname"], "pc")


class CollectTest(unittest.TestCase):
    """Real probes on this machine — only the OS-neutral facts are asserted."""

    def test_collect_basic_facts(self) -> None:
        s = sv.collect(Path.cwd(), probe_devices=False, probe_internet=False)
        self.assertIn(s.os_family, ("windows", "macos", "linux"))
        self.assertTrue(s.os_name)
        self.assertTrue(s.python_version.startswith("3."))
        self.assertIsNotNone(s.cpu_count)
        self.assertIsNotNone(s.ram_gib, "RAM probe must work on the three supported OSes")
        self.assertGreater(s.ram_gib or 0, 0)
        self.assertIsNotNone(s.disk_free_program_gib)
        self.assertIsNone(s.internet)
        self.assertEqual(s.probe_errors, {})


class StageTest(unittest.TestCase):
    def test_stage_writes_survey_json_and_exposes_recommendation(self) -> None:
        real_collect = sv.collect
        sv.collect = lambda program_dir, **kw: Survey(os_name="Ubuntu", os_version="24.04", ram_gib=16.0, internet=True)  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = InstallPaths.from_program_dir(Path(tmp))
                paths.install_log_dir.mkdir(parents=True)
                out = io.StringIO()
                ctx = StageContext(Options(mode=Mode.INSTALL_RUN), paths, Messages.load("en"), Console(out, unicode=False), StateStore(paths.state_file), "t")
                result = SurveyStage().run(ctx)
                self.assertIs(result.status, StageStatus.OK)
                self.assertEqual(result.data["ai_provider"], "ollama")
                self.assertEqual(result.data["ai_local_tier"], "3b")
                self.assertTrue((paths.install_log_dir / "survey.json").exists())
                self.assertIn("Ubuntu 24.04, 16 GB memory", result.message)
        finally:
            sv.collect = real_collect  # type: ignore[assignment]

    def test_cache_key_changes_weekly(self) -> None:
        key = SurveyStage().cache_key(None)  # type: ignore[arg-type]
        self.assertIn("-W", key or "")


if __name__ == "__main__":
    unittest.main()
