"""Doctor check: the configured AI provider is usable this morning (stdlib).

* ``none``  → PASS "assistant off" (stock questions still work through the fast path).
* ``ollama`` → binary (``E-AI-002``) → service answering (``E-AI-001``) → both models present (``E-AI-004``);
  ``--fix`` starts the service and pulls the missing models, then warms the chat model up.
* ``openai`` / ``huggingface`` → internet to the provider host (``E-AI-020``) → API key stored encrypted
  (``E-AI-011``) → monthly budget below the alert percentage (``E-AI-030``). No paid test call here —
  the installer made one when the key was entered.

Always WARN, never FAIL: billing never depends on the AI (docs/02).
"""

from __future__ import annotations

import logging
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from vaishnavi.bootstrap.doctor import CheckResult, DoctorContext
from vaishnavi.bootstrap.ollama_manager import OllamaManager
from vaishnavi.core.clock import now_ist
from vaishnavi.core.settings import Settings

log = logging.getLogger("vaishnavi.bootstrap.checks.ai_provider")

SECRET_KEYS = {"openai": "ai.openai.api_key", "huggingface": "ai.huggingface.api_key"}


def default_manager(settings: Settings) -> OllamaManager:
    """Manager for the configured Ollama host."""
    return OllamaManager(settings.ai.ollama.host)


def internet_to(host_url: str, timeout: float = 3.0) -> bool:
    """TCP connect to the provider's host (port 443)."""
    parsed = urlparse(host_url)
    host = parsed.hostname or host_url
    port = parsed.port or 443
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        log.debug("internet probe %s failed: %s", host, exc)
        return False


def key_present_in_db(ctx: DoctorContext, key: str) -> bool:
    """Is there an encrypted row for the key? (presence only — the doctor never decrypts)."""
    db = ctx.paths.database
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT 1 FROM secure_setting WHERE key = ?", (key,)).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        log.debug("secure_setting probe failed: %s", exc)
        return False


def budget_used_pct_from_db(ctx: DoctorContext) -> int:
    """Share of the monthly token budget used so far (``ai_usage`` rows of the current IST month)."""
    budget = ctx.settings.ai.cloud.monthly_token_budget
    if budget <= 0:
        return 0
    db = ctx.paths.database
    if not db.exists():
        return 0
    month_prefix = now_ist().strftime("%Y-%m")
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COALESCE(SUM(prompt_tokens + completion_tokens + embed_tokens), 0) FROM ai_usage WHERE substr(day, 1, 7) = ?", (month_prefix,)).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        log.debug("ai_usage probe failed: %s", exc)
        return 0
    return int(min(100, (int(row[0]) if row else 0) * 100 / budget))


@dataclass
class AiProviderCheck:
    """Provider-aware readiness; ``fix`` only acts for Ollama (start + pull + warm-up)."""

    key: str = "ai_provider"
    group: str = "ai"
    manager_factory: Callable[[Settings], OllamaManager] = field(default=default_manager)
    internet_probe: Callable[[str], bool] = field(default=internet_to)
    key_present: Callable[[DoctorContext, str], bool] = field(default=key_present_in_db)
    budget_used_pct: Callable[[DoctorContext], int] = field(default=budget_used_pct_from_db)

    def run(self, ctx: DoctorContext) -> CheckResult:
        """Dispatch on the provider."""
        ai = ctx.settings.ai
        if not ai.enabled or ai.provider == "none":
            return CheckResult.ok(ctx.t("doctor.ai_provider.off"), provider="none")
        if ai.provider == "ollama":
            return self._ollama(ctx)
        return self._cloud(ctx)

    def _ollama(self, ctx: DoctorContext) -> CheckResult:
        ai = ctx.settings.ai
        mgr = self.manager_factory(ctx.settings)
        if not mgr.is_running():
            if not mgr.binary:
                return CheckResult.warn(ctx.t("doctor.ai_provider.ollama_missing"), error_code="E-AI-002", hint=_fix("E-AI-002", ctx), provider="ollama", running=False)
            return CheckResult.warn(ctx.t("doctor.ai_provider.ollama_down"), error_code="E-AI-001", hint=_fix("E-AI-001", ctx), provider="ollama", running=False, binary=mgr.binary)
        missing = mgr.missing_models([ai.ollama.chat_model, ai.ollama.embed_model])
        if missing:
            return CheckResult.warn(ctx.t("doctor.ai_provider.models_missing", names=", ".join(missing)), error_code="E-AI-004", hint=_fix("E-AI-004", ctx, model=missing[0]), provider="ollama", running=True, missing=missing)
        return CheckResult.ok(ctx.t("doctor.ai_provider.ollama_ok", chat=ai.ollama.chat_model, embed=ai.ollama.embed_model), provider="ollama", running=True, chat_model=ai.ollama.chat_model, embed_model=ai.ollama.embed_model)

    def _cloud(self, ctx: DoctorContext) -> CheckResult:
        ai = ctx.settings.ai
        provider = ai.provider
        cfg = getattr(ai, provider)
        label = {"openai": "OpenAI", "huggingface": "Hugging Face"}.get(provider, provider)
        if not self.internet_probe(cfg.base_url):
            return CheckResult.warn(ctx.t("doctor.ai_provider.no_internet", provider=label), error_code="E-AI-020", hint=_fix("E-AI-020", ctx, provider=label), provider=provider, internet=False)
        if not self.key_present(ctx, SECRET_KEYS.get(provider, f"ai.{provider}.api_key")):
            return CheckResult.warn(ctx.t("doctor.ai_provider.no_key", provider=label), error_code="E-AI-011", hint=_fix("E-AI-011", ctx, provider=label), provider=provider, key=False)
        pct = self.budget_used_pct(ctx)
        if pct >= ai.cloud.budget_alert_pct:
            return CheckResult.warn(ctx.t("doctor.ai_provider.budget", percent=pct), error_code="E-AI-030", hint=_fix("E-AI-030", ctx, percent=pct), provider=provider, budget_pct=pct)
        return CheckResult.ok(ctx.t("doctor.ai_provider.cloud_ok", provider=label, model=cfg.chat_model, percent=pct), provider=provider, budget_pct=pct, chat_model=cfg.chat_model)

    def fix(self, ctx: DoctorContext) -> CheckResult | None:
        """Ollama only: start the service, pull missing models, warm up."""
        ai = ctx.settings.ai
        if ai.provider != "ollama" or not ai.enabled:
            return None
        mgr = self.manager_factory(ctx.settings)
        if not mgr.is_running() and not mgr.start():
            return self.run(ctx)
        pulled = mgr.ensure_models([ai.ollama.chat_model, ai.ollama.embed_model], on_progress=lambda name, status, pct: log.info("OLLAMA_PULL model=%s status=%s pct=%d", name, status, pct))
        if pulled:
            log.info("DOCTOR_FIX ollama pulled=%s", pulled)
        ms = mgr.warm_up(ai.ollama.chat_model, keep_alive=ai.ollama.keep_alive)
        result = self.run(ctx)
        if result.status.value == "PASS" and ms is not None:
            return CheckResult.ok(ctx.t("doctor.ai_provider.ollama_warm", chat=ai.ollama.chat_model, ms=ms), **result.data, warm_up_ms=ms)
        return result


def _fix(code: str, ctx: DoctorContext, **params: object) -> str:
    from vaishnavi.core.errors import describe  # noqa: PLC0415

    return describe(code, ctx.lang, dict(params))["what_to_do"]


CHECKS = [AiProviderCheck()]
