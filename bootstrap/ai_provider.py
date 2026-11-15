"""AI provider stage: ask which engine the assistant uses, get it ready, record the choice (stdlib only).

Flow (docs/06 §2, docs/17 §3.2, ADR-041/042):

1. Question with four numbered options; the recommended one comes from the hardware survey.
2. ``ollama``  → install if missing (may need admin rights), start the service, pull the chat and
   embedding models for the hardware tier with a progress bar.
3. ``openai`` / ``huggingface`` → show the data + cost notice, obtain the key (env var →
   ``config/secrets.env`` → masked paste), run a live test (chat + embedding), on failure offer
   *try again / another provider / continue without AI*.
4. ``none`` → nothing to install; the fast path answers stock questions.

Keys are never written to any file by this stage: they are handed to the application through
``ctx.runtime["pending_secrets"]`` and stored encrypted by the database stage (``secure_setting``).
``[ai]`` provider and model names are written into ``config/settings.toml``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bootstrap import elevate, ollama_install as oll
from bootstrap.stages import StageContext, StageResult
from bootstrap.wizard import SKIPPED, Kind, Option, Question

if TYPE_CHECKING:
    from bootstrap.cli import Options

log = logging.getLogger("bootstrap.ai_provider")

PROVIDERS = ("ollama", "openai", "huggingface", "none")
SECRETS_ENV_FILE = "secrets.env"
SETTINGS_FILE = "settings.toml"
RUNTIME_SECRETS = "pending_secrets"
RUNTIME_DELETE_SECRETS_FILE = "delete_secrets_file"
"""``ctx.runtime`` key: path of ``config/secrets.env`` the owner agreed to remove once the key is stored encrypted."""
TEST_TIMEOUT_S = 25

ENV_KEYS = {"openai": "VAISHNAVI_OPENAI_API_KEY", "huggingface": "VAISHNAVI_HF_API_KEY"}
SECRET_SETTING_KEYS = {"openai": "ai.openai.api_key", "huggingface": "ai.huggingface.api_key"}

CLOUD_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "chat_model": "gpt-4o-mini", "embed_model": "text-embedding-3-small"},
    "huggingface": {"base_url": "https://router.huggingface.co/v1", "chat_model": "Qwen/Qwen2.5-7B-Instruct", "embed_model": "BAAI/bge-m3"},
}
HF_EMBED_ENDPOINT = "https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction"

PROVIDER_QUESTION_OPTIONS = tuple(Option(p, f"ai.option.{p}", f"ai.note.{p}") for p in PROVIDERS)


# ----------------------------------------------------------------------------- key acquisition
_KEY_SHAPES = {"openai": re.compile(r"^sk-[A-Za-z0-9_\-]{10,}$"), "huggingface": re.compile(r"^hf_[A-Za-z0-9]{10,}$")}


def key_looks_valid(provider: str, key: str) -> bool:
    """Cheap shape check before a network test (prevents pasting a password by mistake)."""
    shape = _KEY_SHAPES.get(provider)
    return bool(shape and shape.match(key.strip()))


def parse_env_file(text: str) -> dict[str, str]:
    """``KEY=value`` lines (quotes and ``export`` tolerated)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:]
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def key_from_environment(provider: str, config_dir: Path, environ: dict[str, str] | None = None) -> tuple[str | None, str]:
    """(key, source) from the env var or ``config/secrets.env``; source is 'env' | 'file' | ''."""
    env = os.environ if environ is None else environ
    name = ENV_KEYS[provider]
    value = env.get(name, "").strip()
    if value:
        return value, "env"
    path = config_dir / SECRETS_ENV_FILE
    if path.exists():
        try:
            value = parse_env_file(path.read_text(encoding="utf-8")).get(name, "").strip()
        except OSError as exc:
            log.warning("secrets.env unreadable: %s", exc)
            value = ""
        if value:
            return value, "file"
    return None, ""


# ----------------------------------------------------------------------------- live test
@dataclass
class TestOutcome:
    """Result of the cloud key test."""

    ok: bool
    chat_ms: int = 0
    embed_ms: int = 0
    reason: str = ""  # auth | billing | network | model | other
    detail: str = ""


def _post_json(url: str, body: dict[str, Any], key: str, timeout: float) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "vaishnavi-store-manager/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https, fixed hosts)
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "null")
        except ValueError:
            payload = None
        return exc.code, payload
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def _classify(status: int, payload: Any) -> tuple[str, str]:
    text = json.dumps(payload)[:300] if not isinstance(payload, str) else payload[:300]
    if status == 0:
        return "network", text
    if status in (401, 403):
        return "auth", text
    if status == 402 or (status == 429 and "quota" in text.lower()) or "billing" in text.lower() or "insufficient_quota" in text.lower():
        return "billing", text
    if status == 404 or "model" in text.lower() and status in (400, 422):
        return "model", text
    return "other", f"HTTP {status}: {text}"


def test_cloud_key(provider: str, key: str, chat_model: str, embed_model: str, *, timeout: float = TEST_TIMEOUT_S, post: Callable[..., tuple[int, Any]] = _post_json) -> TestOutcome:
    """One tiny chat completion and one embedding; never raises."""
    base = CLOUD_DEFAULTS[provider]["base_url"]
    t0 = time.monotonic()
    status, payload = post(f"{base}/chat/completions", {"model": chat_model, "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 5, "temperature": 0}, key, timeout)
    chat_ms = int((time.monotonic() - t0) * 1000)
    if status != 200 or not isinstance(payload, dict) or not payload.get("choices"):
        reason, detail = _classify(status, payload)
        log.warning("AI_TEST_CHAT_FAILED provider=%s status=%s reason=%s", provider, status, reason)
        return TestOutcome(False, chat_ms, 0, reason, detail)

    t1 = time.monotonic()
    if provider == "huggingface":
        status, payload = post(HF_EMBED_ENDPOINT.format(model=embed_model), {"inputs": "camphor"}, key, timeout)
        embed_ok = status == 200 and isinstance(payload, list) and len(payload) > 0
    else:
        status, payload = post(f"{base}/embeddings", {"model": embed_model, "input": "camphor"}, key, timeout)
        embed_ok = status == 200 and isinstance(payload, dict) and bool(payload.get("data"))
    embed_ms = int((time.monotonic() - t1) * 1000)
    if not embed_ok:
        reason, detail = _classify(status, payload)
        log.warning("AI_TEST_EMBED_FAILED provider=%s status=%s reason=%s", provider, status, reason)
        return TestOutcome(False, chat_ms, embed_ms, reason, detail)
    log.info("AI_TEST_OK provider=%s chat_ms=%d embed_ms=%d", provider, chat_ms, embed_ms)
    return TestOutcome(True, chat_ms, embed_ms)


# ----------------------------------------------------------------------------- settings.toml
def write_ai_settings(path: Path, provider: str, models: dict[str, str]) -> None:
    """Create or update the ``[ai]`` block of ``config/settings.toml`` (other sections untouched)."""
    lines = [
        "[ai]",
        "enabled = " + ("false" if provider == "none" else "true"),
        f'provider = "{provider}"',
    ]
    if provider == "ollama":
        lines += ["", "[ai.ollama]", f'host = "{oll.DEFAULT_HOST}"', f'chat_model = "{models["chat_model"]}"', f'embed_model = "{models["embed_model"]}"']
    elif provider in CLOUD_DEFAULTS:
        lines += ["", f"[ai.{provider}]", f'base_url = "{models["base_url"]}"', f'chat_model = "{models["chat_model"]}"', f'embed_model = "{models["embed_model"]}"', f'# the API key is stored encrypted (secure_setting "{SECRET_SETTING_KEYS[provider]}") — never here']
    block = "\n".join(lines) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    # Remove any previous [ai*] sections, keep everything else.
    kept: list[str] = []
    skipping = False
    for line in existing.splitlines():
        if re.match(r"^\s*\[ai(\.[\w.]+)?\]\s*$", line):
            skipping = True
            continue
        if re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text((body + "\n\n" if body else "") + block, encoding="utf-8")
    os.replace(tmp, path)
    log.info("AI_SETTINGS_WRITTEN provider=%s path=%s", provider, path)


# ----------------------------------------------------------------------------- stage
@dataclass
class AiProviderStage:
    """Stage 7: choose and prepare the assistant's engine."""

    key: str = "ai_provider"
    title_key: str = "stage.ai_provider"
    tester: Callable[..., TestOutcome] = test_cloud_key
    ollama: Any = field(default_factory=lambda: oll)
    run_elevated: Callable[..., elevate.Elevation] = elevate.run_elevated
    getpass_fn: Callable[[str], str] | None = None

    def applies(self, ctx: StageContext) -> bool:
        """Always — even 'none' is recorded so the doctor knows."""
        return True

    def cache_key(self, ctx: StageContext) -> str | None:
        """Not cached: Ollama/service/model presence is re-verified every start (fast when ready)."""
        return None

    # -- entry ---------------------------------------------------------------------------------
    def run(self, ctx: StageContext) -> StageResult:
        """Ask (or reuse), then prepare the chosen provider."""
        assert ctx.wizard is not None
        survey_data = ctx.result_data("survey")
        recommended = str(survey_data.get("ai_provider", "ollama"))
        tier = str(survey_data.get("ai_local_tier", "3b"))
        local_possible = bool(survey_data.get("ai_local_possible", True))
        if recommended not in PROVIDERS:
            recommended = "ollama"
        if recommended == "ollama" and ctx.options.offline_dir is None and survey_data.get("survey", {}).get("internet") is False and not self.ollama.find_binary():
            recommended = "none"  # cannot download Ollama or models without internet

        ctx.console.info(ctx.t(f"ai.pc_summary.{'local_ok' if local_possible else 'weak'}", tier=tier))
        question = Question(key="ai_provider", kind=Kind.CHOICE, prompt_key="ai.ask", help_key="ai.help", options=PROVIDER_QUESTION_OPTIONS, recommended=recommended, default=recommended)
        provider = ctx.wizard.ask(question)
        log.info("AI_PROVIDER_SELECTED provider=%s recommended=%s tier=%s", provider, recommended, tier)

        while True:
            if provider == "none":
                write_ai_settings(ctx.paths.config_dir / SETTINGS_FILE, "none", {})
                return StageResult.ok(ctx.t("ai.none_chosen"), provider="none")
            if provider == "ollama":
                return self._prepare_ollama(ctx, tier)
            result, retry_provider = self._prepare_cloud(ctx, provider)
            if result is not None:
                return result
            assert retry_provider is not None
            provider = retry_provider

    # -- local --------------------------------------------------------------------------------
    def _prepare_ollama(self, ctx: StageContext, tier: str) -> StageResult:
        o = self.ollama
        os_family = str(ctx.result_data("survey").get("survey", {}).get("os_family", "unknown"))
        chat_model, embed_model = o.models_for_tier(tier)
        binary = o.find_binary()
        if binary is None:
            ctx.console.busy(ctx.t("ai.ollama.installing"))
            ok, detail = o.install(os_family, run_elevated=self.run_elevated, password_prompt=ctx.t("syscomp.password_prompt"))
            if not ok:
                code = "E-SYS-004" if detail in ("declined", "elevation unavailable") else "E-AI-002"
                return StageResult.warn(ctx.t("ai.ollama.install_failed"), error_code=code, detail=detail, hint=ctx.t("ai.ollama.install_hint"), provider="ollama", ready=False)
            binary = o.find_binary()
            if binary is None:
                return StageResult.warn(ctx.t("ai.ollama.install_failed"), error_code="E-AI-002", detail="binary not found after install", hint=ctx.t("ai.ollama.install_hint"), provider="ollama", ready=False)
            log.info("OLLAMA_INSTALLED binary=%s", binary)

        if not o.start_service(binary):
            return StageResult.warn(ctx.t("ai.ollama.start_failed"), error_code="E-AI-001", hint=ctx.t("ai.ollama.start_hint"), provider="ollama", ready=False)

        pulled: list[str] = []
        for model in (chat_model, embed_model):
            if o.has_model(model):
                continue
            label = ctx.t("ai.ollama.pulling", model=model)
            last_pct = -1

            def on_progress(p: oll.PullProgress, _label: str = label) -> None:
                nonlocal last_pct
                if p.total and p.percent != last_pct:
                    last_pct = p.percent
                    ctx.console.progress(_label, p.completed, p.total)

            ok, status = o.pull_model(model, on_progress)
            ctx.console.progress_end()
            if not ok:
                return StageResult.warn(ctx.t("ai.ollama.pull_failed", model=model), error_code="E-AI-004", detail=status, hint=ctx.t("ai.ollama.pull_hint"), provider="ollama", ready=False, chat_model=chat_model, embed_model=embed_model)
            pulled.append(model)

        write_ai_settings(ctx.paths.config_dir / SETTINGS_FILE, "ollama", {"chat_model": chat_model, "embed_model": embed_model})
        msg = ctx.t("ai.ollama.ready", chat=chat_model, embed=embed_model)
        return StageResult.ok(msg, provider="ollama", ready=True, chat_model=chat_model, embed_model=embed_model, pulled=pulled, version=o.version())

    # -- cloud --------------------------------------------------------------------------------
    def _prepare_cloud(self, ctx: StageContext, provider: str) -> tuple[StageResult | None, str | None]:
        """Returns (result, None) when done, or (None, next_provider) to loop."""
        assert ctx.wizard is not None
        defaults = CLOUD_DEFAULTS[provider]
        ctx.console.info(ctx.t(f"ai.notice.{provider}"))
        ctx.console.hint(ctx.t("ai.notice.never_sent"))

        key, source = key_from_environment(provider, ctx.paths.config_dir)
        if key:
            log.info("SECRET_IMPORTED provider=%s source=%s", provider, source)
            ctx.console.info(ctx.t("ai.key.imported", source=ctx.t(f"ai.key.source.{source}")))
        while True:
            if not key:
                q = Question(key=f"{provider}_api_key", kind=Kind.SECRET, prompt_key=f"ai.key.ask.{provider}", help_key=f"ai.key.where.{provider}", allow_back=False)
                if self.getpass_fn is not None:
                    ctx.wizard.getpass_fn = self.getpass_fn
                key = ctx.wizard.ask(q)
                source = "typed"
                if not key_looks_valid(provider, key):
                    ctx.console.warn(ctx.t(f"ai.key.bad_shape.{provider}"))
                    key = None
                    continue
            ctx.console.busy(ctx.t("ai.key.testing"))
            outcome = self.tester(provider, key, defaults["chat_model"], defaults["embed_model"])
            if outcome.ok:
                break
            ctx.console.warn(ctx.t(f"ai.key.failed.{outcome.reason}" if outcome.reason in ("auth", "billing", "network", "model") else "ai.key.failed.other"))
            log.warning("AI_KEY_TEST_FAILED provider=%s reason=%s detail=%s", provider, outcome.reason, outcome.detail[:200])
            choice = ctx.wizard.ask(Question(key="ai_key_retry", kind=Kind.CHOICE, prompt_key="ai.key.retry_ask", options=(Option("retry", "ai.key.retry.retry"), Option("other", "ai.key.retry.other"), Option("none", "ai.key.retry.none")), default="retry", recommended="retry", allow_back=False, persist=False))
            if choice == "retry":
                key = None
                continue
            if choice == "other":
                nxt = ctx.wizard.ask(Question(key="ai_provider", kind=Kind.CHOICE, prompt_key="ai.ask", options=PROVIDER_QUESTION_OPTIONS, recommended="ollama", default="ollama", allow_back=False), force=True)
                return None, nxt
            return None, "none"

        secrets: dict[str, str] = ctx.runtime.setdefault(RUNTIME_SECRETS, {})
        secrets[SECRET_SETTING_KEYS[provider]] = key
        if source == "file":
            self._offer_delete_secrets_file(ctx)
        write_ai_settings(ctx.paths.config_dir / SETTINGS_FILE, provider, defaults)
        msg = ctx.t("ai.cloud.ready", provider=ctx.t(f"ai.option.{provider}"), chat=defaults["chat_model"], ms=outcome.chat_ms)
        return StageResult.ok(msg, provider=provider, ready=True, chat_model=defaults["chat_model"], embed_model=defaults["embed_model"], key_source=source), None

    def _offer_delete_secrets_file(self, ctx: StageContext) -> None:
        """Ask for consent now; the file is removed by the database stage **after** the key is stored
        encrypted (``ctx.runtime[RUNTIME_DELETE_SECRETS_FILE]``). Deleting here would lose the only
        copy if setup is cancelled before the database exists."""
        assert ctx.wizard is not None
        path = ctx.paths.config_dir / SECRETS_ENV_FILE
        answer = ctx.wizard.ask(Question(key="delete_secrets_env", kind=Kind.YESNO, prompt_key="ai.key.delete_file_ask", default="yes", allow_back=False, persist=False))
        if answer in ("yes",):
            ctx.runtime[RUNTIME_DELETE_SECRETS_FILE] = path
            log.info("SECRETS_FILE_DELETE_PENDING path=%s", path)
            ctx.console.info(ctx.t("ai.key.file_pending"))
        elif answer != SKIPPED:
            ctx.console.warn(ctx.t("ai.key.file_kept", path=str(path)))


def delete_secrets_file_if_pending(ctx: StageContext) -> bool:
    """Called by the database stage once the secrets are committed; True when a file was removed."""
    path = ctx.runtime.pop(RUNTIME_DELETE_SECRETS_FILE, None)
    if path is None:
        return False
    try:
        Path(path).unlink()
        log.info("SECRETS_FILE_DELETED path=%s", path)
        ctx.console.ok(ctx.t("ai.key.file_deleted"))
        return True
    except OSError as exc:
        log.warning("could not delete %s: %s code=E-SEC-005", path, exc)
        ctx.console.warn(ctx.t("ai.key.file_not_deleted", path=str(path)))
        return False


def register(options: Options) -> AiProviderStage:
    """Factory used by ``build_default_pipeline``."""
    return AiProviderStage()
