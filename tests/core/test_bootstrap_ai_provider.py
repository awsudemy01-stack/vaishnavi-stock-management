"""AI provider stage, cloud key test classification, Ollama helpers, settings writer (stdlib only)."""

from __future__ import annotations

import io
import json
import tempfile
import tomllib
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bootstrap import ai_provider as ap, ollama_install as oll
from bootstrap.ai_provider import RUNTIME_SECRETS, AiProviderStage, TestOutcome, key_from_environment, key_looks_valid, parse_env_file, test_cloud_key, write_ai_settings
from bootstrap.cli import Mode, Options
from bootstrap.console import Console
from bootstrap.elevate import Elevation, Outcome
from bootstrap.i18n import Messages
from bootstrap.stages import InstallPaths, StageContext, StageResult, StageStatus, StateStore
from bootstrap.wizard import AnswerStore, Wizard


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@dataclass
class FakeOllama:
    """Stands in for the ollama_install module."""

    binary: Path | None = None
    installed: set[str] = field(default_factory=set)
    install_ok: bool = True
    start_ok: bool = True
    pull_ok: bool = True
    calls: list[str] = field(default_factory=list)
    DEFAULT_HOST = "http://127.0.0.1:11434"
    PullProgress = oll.PullProgress

    def models_for_tier(self, tier: str) -> tuple[str, str]:
        return oll.models_for_tier(tier)

    def find_binary(self) -> Path | None:
        return self.binary

    def install(self, os_family: str, **kw: Any) -> tuple[bool, str]:
        self.calls.append("install")
        if self.install_ok:
            self.binary = Path("/usr/local/bin/ollama")
            return True, "exit 0"
        return False, "declined"

    def start_service(self, binary: Path) -> bool:
        self.calls.append("start")
        return self.start_ok

    def has_model(self, name: str) -> bool:
        return name in self.installed

    def pull_model(self, name: str, on_progress: Any) -> tuple[bool, str]:
        self.calls.append(f"pull:{name}")
        on_progress(oll.PullProgress("pulling", 50, 100))
        on_progress(oll.PullProgress("pulling", 100, 100))
        if self.pull_ok:
            self.installed.add(name)
            return True, "success"
        return False, "connection reset"

    def version(self) -> str:
        return "0.5.0"


def _ctx(tmp: Path, typed: str, *, survey: dict[str, Any] | None = None, env_key: str | None = None) -> tuple[StageContext, io.StringIO]:
    paths = InstallPaths.from_program_dir(tmp)
    paths.install_log_dir.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    console, messages = Console(out, unicode=False), Messages.load("en")
    wizard = Wizard(console, messages, AnswerStore(tmp / "config" / "install-answers.toml"), stdin=_Tty(typed), getpass_fn=lambda p: env_key or "sk-typedkey0123456789")
    ctx = StageContext(Options(mode=Mode.INSTALL_RUN), paths, messages, console, StateStore(paths.state_file), "t", wizard=wizard)
    ctx.results["survey"] = StageResult.ok(**(survey or {"survey": {"os_family": "linux", "internet": True}, "ai_provider": "ollama", "ai_local_tier": "3b", "ai_local_possible": True}))
    return ctx, out


class HelpersTest(unittest.TestCase):
    def test_key_shapes(self) -> None:
        self.assertTrue(key_looks_valid("openai", "sk-abcdefghij0123456789"))
        self.assertTrue(key_looks_valid("huggingface", "hf_abcdefghij0123456789"))
        self.assertFalse(key_looks_valid("openai", "hf_abcdefghij0123456789"))
        self.assertFalse(key_looks_valid("openai", "my shop password"))

    def test_parse_env_file(self) -> None:
        text = "# c\nexport VAISHNAVI_OPENAI_API_KEY='sk-x'\nVAISHNAVI_HF_API_KEY=\"hf_y\"\nbroken line\n"
        self.assertEqual(parse_env_file(text), {"VAISHNAVI_OPENAI_API_KEY": "sk-x", "VAISHNAVI_HF_API_KEY": "hf_y"})

    def test_key_from_environment_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            self.assertEqual(key_from_environment("openai", cfg, {}), (None, ""))
            (cfg / "secrets.env").write_text("VAISHNAVI_OPENAI_API_KEY=sk-fromfile\n", encoding="utf-8")
            self.assertEqual(key_from_environment("openai", cfg, {}), ("sk-fromfile", "file"))
            self.assertEqual(key_from_environment("openai", cfg, {"VAISHNAVI_OPENAI_API_KEY": "sk-fromenv"}), ("sk-fromenv", "env"))

    def test_models_for_tier(self) -> None:
        self.assertEqual(oll.models_for_tier("7b"), ("qwen2.5:7b-instruct", "bge-m3"))
        self.assertEqual(oll.models_for_tier("weird"), ("qwen2.5:3b-instruct", "nomic-embed-text"))

    def test_ollama_install_commands_per_os(self) -> None:
        plan = oll.install_commands("linux")
        assert plan is not None
        self.assertTrue(plan[1], "Linux script needs elevation")
        self.assertIsNone(oll.install_commands("plan9"))


class CloudTestClassificationTest(unittest.TestCase):
    def _post_factory(self, responses: list[tuple[int, Any]]) -> Any:
        calls: list[str] = []

        def post(url: str, body: dict[str, Any], key: str, timeout: float) -> tuple[int, Any]:
            calls.append(url)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        post.calls = calls  # type: ignore[attr-defined]
        return post

    def test_success_openai(self) -> None:
        post = self._post_factory([(200, {"choices": [{"message": {"content": "OK"}}]}), (200, {"data": [{"embedding": [0.1]}]})])
        out = test_cloud_key("openai", "sk-x", "gpt-4o-mini", "text-embedding-3-small", post=post)
        self.assertTrue(out.ok)
        self.assertIn("/chat/completions", post.calls[0])
        self.assertIn("/embeddings", post.calls[1])

    def test_success_huggingface_uses_feature_extraction(self) -> None:
        post = self._post_factory([(200, {"choices": [{}]}), (200, [[0.1, 0.2]])])
        out = test_cloud_key("huggingface", "hf_x", "Qwen/Qwen2.5-7B-Instruct", "BAAI/bge-m3", post=post)
        self.assertTrue(out.ok)
        self.assertIn("feature-extraction", post.calls[1])

    def test_reasons(self) -> None:
        cases = {401: "auth", 403: "auth", 402: "billing", 0: "network", 404: "model", 500: "other"}
        for status, reason in cases.items():
            with self.subTest(status=status):
                payload = "connection refused" if status == 0 else {"error": {"message": "x"}}
                out = test_cloud_key("openai", "sk-x", "m", "e", post=self._post_factory([(status, payload)]))
                self.assertFalse(out.ok)
                self.assertEqual(out.reason, reason)
        quota = test_cloud_key("openai", "sk-x", "m", "e", post=self._post_factory([(429, {"error": {"type": "insufficient_quota"}})]))
        self.assertEqual(quota.reason, "billing")


class SettingsWriterTest(unittest.TestCase):
    def test_writes_and_replaces_ai_block_keeping_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            path.write_text('[shop]\nname = "Vaishnavi Puja Store"\n\n[ai]\nprovider = "none"\n\n[ai.ollama]\nhost = "x"\n\n[server]\nport = 8080\n', encoding="utf-8")
            write_ai_settings(path, "openai", ap.CLOUD_DEFAULTS["openai"])
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["shop"]["name"], "Vaishnavi Puja Store")
            self.assertEqual(data["server"]["port"], 8080)
            self.assertEqual(data["ai"]["provider"], "openai")
            self.assertTrue(data["ai"]["enabled"])
            self.assertNotIn("ollama", data["ai"])
            self.assertEqual(data["ai"]["openai"]["chat_model"], "gpt-4o-mini")
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?m)^\s*api_key\s*=")
            write_ai_settings(path, "none", {})
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(data["ai"]["enabled"])
            self.assertEqual(data["shop"]["name"], "Vaishnavi Puja Store")

    def test_creates_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "settings.toml"
            write_ai_settings(path, "ollama", {"chat_model": "qwen2.5:3b-instruct", "embed_model": "nomic-embed-text"})
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["ai"]["ollama"]["chat_model"], "qwen2.5:3b-instruct")


class StageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _settings(self) -> dict[str, Any]:
        return tomllib.loads((self.tmp / "config" / "settings.toml").read_text(encoding="utf-8"))

    def test_none_chosen(self) -> None:
        ctx, _ = _ctx(self.tmp, "4\n")
        result = AiProviderStage(ollama=FakeOllama()).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(result.data["provider"], "none")
        self.assertFalse(self._settings()["ai"]["enabled"])

    def test_ollama_install_start_pull_happy_path(self) -> None:
        ctx, out = _ctx(self.tmp, "\n")  # Enter = recommended (ollama)
        fake = FakeOllama()
        result = AiProviderStage(ollama=fake).run(ctx)
        self.assertIs(result.status, StageStatus.OK, result.detail)
        self.assertEqual(fake.calls, ["install", "start", "pull:qwen2.5:3b-instruct", "pull:nomic-embed-text"])
        self.assertEqual(result.data["pulled"], ["qwen2.5:3b-instruct", "nomic-embed-text"])
        self.assertIn("100 %", out.getvalue())
        self.assertEqual(self._settings()["ai"]["ollama"]["chat_model"], "qwen2.5:3b-instruct")

    def test_ollama_already_ready_skips_install_and_pull(self) -> None:
        ctx, _ = _ctx(self.tmp, "1\n")
        fake = FakeOllama(binary=Path("/usr/bin/ollama"), installed={"qwen2.5:3b-instruct", "nomic-embed-text"})
        result = AiProviderStage(ollama=fake).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(fake.calls, ["start"])
        self.assertEqual(result.data["pulled"], [])

    def test_ollama_install_declined_is_warn_e_sys_004(self) -> None:
        ctx, _ = _ctx(self.tmp, "1\n")
        result = AiProviderStage(ollama=FakeOllama(install_ok=False)).run(ctx)
        self.assertIs(result.status, StageStatus.WARN)
        self.assertEqual(result.error_code, "E-SYS-004")
        self.assertFalse(result.data["ready"])

    def test_ollama_start_and_pull_failures(self) -> None:
        ctx, _ = _ctx(self.tmp, "1\n")
        r = AiProviderStage(ollama=FakeOllama(binary=Path("/x"), start_ok=False)).run(ctx)
        self.assertEqual((r.status, r.error_code), (StageStatus.WARN, "E-AI-001"))
        ctx, _ = _ctx(self.tmp, "1\n")
        r = AiProviderStage(ollama=FakeOllama(binary=Path("/x"), pull_ok=False)).run(ctx)
        self.assertEqual((r.status, r.error_code), (StageStatus.WARN, "E-AI-004"))

    def test_openai_typed_key_tested_and_handed_over_not_written(self) -> None:
        ctx, out = _ctx(self.tmp, "2\n")
        tester_calls: list[tuple[str, str]] = []

        def tester(provider: str, key: str, chat: str, embed: str) -> TestOutcome:
            tester_calls.append((provider, key))
            return TestOutcome(True, 900, 300)

        result = AiProviderStage(tester=tester, ollama=FakeOllama()).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(tester_calls, [("openai", "sk-typedkey0123456789")])
        self.assertEqual(ctx.runtime[RUNTIME_SECRETS], {"ai.openai.api_key": "sk-typedkey0123456789"})
        self.assertEqual(result.data["key_source"], "typed")
        text = out.getvalue()
        self.assertIn("What will be sent to OpenAI", text)
        self.assertIn("Never sent", text)
        self.assertNotIn("typedkey", text)
        for f in self.tmp.rglob("*"):
            if f.is_file():
                self.assertNotIn("typedkey", f.read_text(encoding="utf-8", errors="replace"), f"key leaked into {f}")

    def test_key_from_secrets_env_is_imported_and_file_deleted_only_after_commit(self) -> None:
        from bootstrap.ai_provider import RUNTIME_DELETE_SECRETS_FILE, delete_secrets_file_if_pending

        (self.tmp / "config").mkdir()
        secrets_file = self.tmp / "config" / "secrets.env"
        secrets_file.write_text("VAISHNAVI_HF_API_KEY=hf_filekey0123456789\n", encoding="utf-8")
        ctx, out = _ctx(self.tmp, "3\nyes\n")  # provider 3 = huggingface; yes = delete file
        result = AiProviderStage(tester=lambda *a: TestOutcome(True, 1, 1), ollama=FakeOllama()).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(result.data["key_source"], "file")
        self.assertIn("ai.huggingface.api_key", ctx.runtime[RUNTIME_SECRETS])
        # The only copy of the key stays until the database stage has stored it encrypted.
        self.assertTrue(secrets_file.exists(), "must not be deleted while the key is only in memory")
        self.assertEqual(ctx.runtime[RUNTIME_DELETE_SECRETS_FILE], secrets_file)
        self.assertIn("will be removed", out.getvalue())
        self.assertTrue(delete_secrets_file_if_pending(ctx))
        self.assertFalse(secrets_file.exists())
        self.assertFalse(delete_secrets_file_if_pending(ctx), "second call is a no-op")

    def test_bad_key_then_retry_then_other_provider(self) -> None:
        # 2 = openai; test fails → choose "2 = other provider" → pick 4 = none
        ctx, out = _ctx(self.tmp, "2\n2\n4\n")
        result = AiProviderStage(tester=lambda *a: TestOutcome(False, 0, 0, "auth", "401"), ollama=FakeOllama()).run(ctx)
        self.assertIs(result.status, StageStatus.OK)
        self.assertEqual(result.data["provider"], "none")
        self.assertIn("not accepted", out.getvalue())
        self.assertNotIn(RUNTIME_SECRETS, ctx.runtime)

    def test_failed_test_then_continue_without_ai(self) -> None:
        ctx, _ = _ctx(self.tmp, "2\n3\n")
        result = AiProviderStage(tester=lambda *a: TestOutcome(False, 0, 0, "network", "x"), ollama=FakeOllama()).run(ctx)
        self.assertEqual(result.data["provider"], "none")

    def test_prefilled_flag_and_answers_file_skip_the_question(self) -> None:
        ctx, _ = _ctx(self.tmp, "")
        assert ctx.wizard is not None
        ctx.wizard.prefilled = {"ai_provider": "none"}
        self.assertEqual(AiProviderStage(ollama=FakeOllama()).run(ctx).data["provider"], "none")

    def test_offline_pc_without_ollama_recommends_none(self) -> None:
        survey = {"survey": {"os_family": "linux", "internet": False}, "ai_provider": "ollama", "ai_local_tier": "3b", "ai_local_possible": True}
        ctx, out = _ctx(self.tmp, "\n", survey=survey)
        result = AiProviderStage(ollama=FakeOllama()).run(ctx)
        self.assertEqual(result.data["provider"], "none", "Enter accepts the adjusted recommendation")


if __name__ == "__main__":
    unittest.main()
