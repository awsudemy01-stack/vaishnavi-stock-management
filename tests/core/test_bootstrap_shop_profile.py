"""Shop profile installer stage (stdlib only): asks shop name · owner · address · phone, writes ``[shop]`` into settings.toml."""

from __future__ import annotations

import io
import tempfile
import tomllib
import unittest
from pathlib import Path

from bootstrap import shop_profile as sp
from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.i18n import Messages
from bootstrap.stages import InstallPaths, StageContext, StageStatus, StateStore, build_default_pipeline
from bootstrap.wizard import AnswerStore, Wizard


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _ctx(tmp: Path, typed: str = "", *, yes: bool = False) -> tuple[StageContext, io.StringIO]:
    paths = InstallPaths.from_program_dir(tmp / "program")
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    console, messages = Console(out, unicode=False), Messages.load("en")
    wizard = Wizard(console, messages, AnswerStore(paths.config_dir / "install-answers.toml"), assume_yes=yes, stdin=_Tty(typed))
    ctx = StageContext(Options(mode=Mode.INSTALL_RUN, assume_yes=yes), paths, messages, console, StateStore(paths.state_file), "t", wizard=wizard)
    return ctx, out


OTHER_SECTIONS = '[tax]\nregistered = false\n\n[receipt]\npaper_width_mm = 80\nfooter_lines = ["Thank you"]\n'


class ShopProfileStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_asks_the_shop_details_validates_the_phone_and_writes_only_the_shop_block(self) -> None:
        ctx, out = _ctx(self.tmp, "Shri Ganesh Stores\nRadhe\nKhandve Nagar, Wagholi\nPune 412207\n12345\n+91 12345 67890\ns\n")
        settings = ctx.paths.config_dir / "settings.toml"
        settings.write_text("# header comment\n[shop]\nname = \"Old Name\"\nname_hi = \"पुराना\"\nlanguage = \"hi\"\n\n" + OTHER_SECTIONS, encoding="utf-8")
        result = sp.ShopProfileStage().run(ctx)
        self.assertIs(result.status, StageStatus.OK, result.detail)
        self.assertIn("Shri Ganesh Stores", result.message)
        self.assertIn("10 to 13 digits", out.getvalue(), "a 5-digit phone is refused and asked again")
        data = tomllib.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual((data["shop"]["name"], data["shop"]["owner_name"], data["shop"]["address_line1"], data["shop"]["address_line2"], data["shop"]["phone"], data["shop"]["email"]), ("Shri Ganesh Stores", "Radhe", "Khandve Nagar, Wagholi", "Pune 412207", "+911234567890", ""))
        self.assertEqual((data["shop"]["name_hi"], data["shop"]["language"]), ("पुराना", "hi"), "keys the stage does not ask about survive")
        self.assertEqual((data["tax"]["registered"], data["receipt"]["paper_width_mm"], data["receipt"]["footer_lines"]), (False, 80, ["Thank you"]), "other sections untouched")
        self.assertTrue(settings.read_text(encoding="utf-8").startswith("# header comment"), "comments above the block are kept")
        # the application reads exactly these values
        from vaishnavi.core.settings import Settings

        loaded = Settings.load(ctx.paths.config_dir)
        self.assertEqual((loaded.shop.name, loaded.shop.owner_name, loaded.shop.phone), ("Shri Ganesh Stores", "Radhe", "+911234567890"))

    def test_a_complete_block_is_reused_without_questions_and_yes_mode_fills_the_name_only(self) -> None:
        ctx, out = _ctx(self.tmp)
        settings = ctx.paths.config_dir / "settings.toml"
        sp.write_shop_settings(settings, {"name": "Vaishnavi Puja Store", "owner_name": "Radhe", "address_line1": "Wagholi", "phone": "9876543210"})
        result = sp.ShopProfileStage().run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertIn("from config/settings.toml", result.message)
        self.assertNotIn("Shop name", out.getvalue(), "nothing asked")
        # --yes on a fresh machine: default shop name, the rest skipped → WARN pointing at Settings → Shop & GST
        ctx2, _ = _ctx(self.tmp / "fresh", yes=True)
        result2 = sp.ShopProfileStage().run(ctx2)
        self.assertIs(result2.status, StageStatus.WARN)
        self.assertIn("Settings", result2.hint)
        data = sp.read_shop_settings(ctx2.paths.config_dir / "settings.toml")
        self.assertEqual((data["name"], data["owner_name"], data["phone"]), (sp.DEFAULT_SHOP_NAME, "", ""))
        self.assertFalse(sp.is_complete(data))

    def test_stage_sits_between_locations_and_autostart_in_the_pipeline(self) -> None:
        keys = [s.key for s in build_default_pipeline(Options(mode=Mode.INSTALL_RUN))]
        self.assertIn("shop_profile", keys)
        self.assertLess(keys.index("locations"), keys.index("shop_profile"))
        self.assertLess(keys.index("shop_profile"), keys.index("autostart"))
        self.assertLess(keys.index("shop_profile"), keys.index("database"))


if __name__ == "__main__":
    unittest.main()
