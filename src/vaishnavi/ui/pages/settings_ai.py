"""Settings → Assistant (docs/06 §2.4, §7 layer 4): provider, models, API key (masked, tested, encrypted),
data notice, usage, and the Owner-instructions editor with versions / activate / rollback.

On save: key → ``secure_setting`` (``ai.<provider>.api_key``), ``[ai]`` overrides through SettingsService,
``AI_PROVIDER_CHANGED`` audit row (old → new, never the key), the live provider is rebuilt and the help
index re-embedded in the background. View-models are pure and tested; NiceGUI rendering is thin.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from vaishnavi.ai.providers.base import ChatMessage, LLMProvider
from vaishnavi.api.system import AppState
from vaishnavi.core.errors import VaishnaviError
from vaishnavi.core.permissions import Actor, PermissionDenied
from vaishnavi.ui.i18n import UiTexts

log = logging.getLogger("vaishnavi.ui.pages.settings_ai")

PROVIDERS: Final = (("ollama", "Ollama (local, free, private)"), ("openai", "OpenAI API (cloud)"), ("huggingface", "Hugging Face API (cloud)"), ("none", "Off — fast path only"))
MODEL_CHOICES: Final = {
    "ollama": (("qwen2.5:3b-instruct", "qwen2.5:1.5b-instruct", "qwen2.5:7b-instruct", "llama3.2:3b"), ("nomic-embed-text", "bge-m3")),
    "openai": (("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"), ("text-embedding-3-small",)),
    "huggingface": (("Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "meta-llama/Llama-3.3-70B-Instruct"), ("BAAI/bge-m3", "sentence-transformers/all-MiniLM-L6-v2")),
    "none": ((), ()),
}


@dataclass(frozen=True)
class VersionRow:
    """Instructions version."""

    version: int
    active: bool
    passed: bool
    created_at: str
    note: str
    text: str


@dataclass(frozen=True)
class UsageRow:
    """Tokens per provider/model (30 days)."""

    provider: str
    model: str
    requests: int
    tokens: int
    errors: int


@dataclass(frozen=True)
class AiModel:
    """The tab."""

    enabled: bool
    provider: str
    providers: tuple[tuple[str, str], ...]
    chat_model: str
    embed_model: str
    chat_choices: tuple[str, ...]
    embed_choices: tuple[str, ...]
    key_required: bool
    key_preview: str
    health_ok: bool
    health_text: str
    default_language: str
    budget_tokens: int
    budget_used_pct: int
    usage: list[UsageRow]
    active_prompt_version: int
    versions: list[VersionRow]
    core_text: str
    data_notice: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """Action result."""

    ok: bool
    message: str = ""
    error_code: str | None = None


def _key_preview(services: Any, provider: str) -> str:
    if provider not in ("openai", "huggingface"):
        return ""
    key = services.secure_settings.reveal_for_system(f"ai.{provider}.api_key")
    return f"{key[:4]}…  ({len(key)} chars)" if key else ""


def build_ai_model(state: AppState, texts: UiTexts) -> AiModel:
    """Everything the tab shows (no secrets)."""
    c = state.extra["services"]
    s = c.settings_service.effective()
    ai = s.ai
    cloud = {"openai": ai.openai, "huggingface": ai.huggingface}.get(ai.provider)
    chat_model = ai.ollama.chat_model if ai.provider == "ollama" else (cloud.chat_model if cloud else "")
    embed_model = ai.ollama.embed_model if ai.provider == "ollama" else (cloud.embed_model if cloud else "")
    health = c.ai_provider.health()
    usage_rows = c.chat.usage(days=30)
    usage = [UsageRow(str(r["provider"]), str(r["model"]), int(r["requests"] or 0), int(r["prompt_tokens"] or 0) + int(r["completion_tokens"] or 0) + int(r["embed_tokens"] or 0), int(r["errors"] or 0)) for r in usage_rows]
    month_tokens = sum(u.tokens for u in usage if u.provider in ("openai", "huggingface"))
    pct = int(month_tokens * 100 / ai.cloud.monthly_token_budget) if ai.cloud.monthly_token_budget else 0
    versions = [VersionRow(v.version, bool(v.is_active), bool(v.redteam_passed), v.created_at, v.note or "", v.editable_instructions) for v in c.prompts.versions()]
    from vaishnavi.ai import prompts as prompt_assets  # noqa: PLC0415

    labels = {k: texts.t(f"ai.{k}") for k in ("title", "provider", "chat_model", "embed_model", "api_key", "test", "save", "saved", "usage", "budget", "instructions", "propose", "activate", "rollback", "core", "notice", "language", "versions", "health", "key_hint")}
    return AiModel(ai.enabled, ai.provider, PROVIDERS, chat_model, embed_model, *MODEL_CHOICES.get(ai.provider, ((), ())), ai.provider in ("openai", "huggingface"), _key_preview(c, ai.provider), bool(health.ok), health.detail or health.error_code, ai.default_language, ai.cloud.monthly_token_budget, pct, usage, c.prompts.active().version, versions, prompt_assets.core_text(), texts.t("ai.data_notice"), labels)


def test_provider(state: AppState, actor: Actor, *, provider: str, api_key: str, chat_model: str = "", embed_model: str = "", build: Callable[..., LLMProvider] | None = None) -> Outcome:
    """One tiny chat + one embed against the candidate configuration (nothing is saved)."""
    c = state.extra["services"]
    s = c.settings_service.effective()
    if provider == "none":
        return Outcome(True, "assistant off — fast path only")
    try:
        if build is not None:
            candidate = build(provider=provider, api_key=api_key, chat_model=chat_model, embed_model=embed_model)
        else:
            from dataclasses import replace  # noqa: PLC0415

            from vaishnavi.ai.providers.factory import build_provider  # noqa: PLC0415

            ai = replace(s.ai, provider=provider, enabled=True)  # type: ignore[arg-type]
            if provider == "ollama":
                ai = replace(ai, ollama=replace(ai.ollama, chat_model=chat_model or ai.ollama.chat_model, embed_model=embed_model or ai.ollama.embed_model))
            else:
                section = replace(getattr(ai, provider), chat_model=chat_model or getattr(ai, provider).chat_model, embed_model=embed_model or getattr(ai, provider).embed_model)
                ai = replace(ai, **{provider: section})
            key = api_key or c.secure_settings.reveal_for_system(f"ai.{provider}.api_key") or ""
            candidate = build_provider(ai, api_key=lambda _n: key or None)
        started = time.monotonic()
        health = candidate.health()
        if not health.ok:
            return Outcome(False, f"{health.error_code}: {health.detail}", health.error_code)
        reply = candidate.chat([ChatMessage("user", "Reply with the single word: pong")], temperature=0.0, max_tokens=5)
        dim = len(candidate.embed(["camphor"])[0]) if candidate.capabilities().embeddings else 0
        ms = int((time.monotonic() - started) * 1000)
    except VaishnaviError as exc:
        log.warning("AI_PROVIDER_TEST_FAILED provider=%s code=%s", provider, exc.code)
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    log.info("AI_PROVIDER_TEST_OK provider=%s model=%s ms=%d embed_dim=%d by=%s", provider, reply.model, ms, dim, actor.user_id)
    return Outcome(True, f"OK · {reply.model or provider} answered \"{reply.text.strip()[:30]}\" in {ms} ms · embeddings {dim}-d")


def save_provider(state: AppState, actor: Actor, *, provider: str, api_key: str = "", chat_model: str = "", embed_model: str = "", enabled: bool = True, default_language: str | None = None) -> Outcome:
    """Persist provider + models (+ key), audit, rebuild the live provider."""
    c = state.extra["services"]
    before = c.settings_service.effective().ai
    changes: dict[str, Any] = {"ai.provider": provider, "ai.enabled": enabled}
    if default_language:
        changes["ai.default_language"] = default_language
    if provider == "ollama":
        if chat_model:
            changes["ai.ollama.chat_model"] = chat_model
        if embed_model:
            changes["ai.ollama.embed_model"] = embed_model
    elif provider in ("openai", "huggingface"):
        if chat_model:
            changes[f"ai.{provider}.chat_model"] = chat_model
        if embed_model:
            changes[f"ai.{provider}.embed_model"] = embed_model
    try:
        if api_key.strip() and provider in ("openai", "huggingface"):
            c.secure_settings.set(actor, f"ai.{provider}.api_key", api_key.strip())
        elif provider in ("openai", "huggingface") and not c.secure_settings.is_set(f"ai.{provider}.api_key"):
            return Outcome(False, "paste the API key first", "E-AI-011")
        c.settings_service.set_many(actor, changes)
        with c.txn(label="ai.provider_changed") as session:
            c.audit.record(session, "AI_PROVIDER_CHANGED", user_id=actor.user_id, entity="setting", after={"provider": provider, "chat_model": chat_model or "-", "embed_model": embed_model or "-", "enabled": enabled, "key_updated": bool(api_key.strip())}, before={"provider": before.provider})
    except PermissionDenied as exc:
        return Outcome(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    c.rebuild_ai_provider()
    log.info("AI_PROVIDER_CHANGED from=%s to=%s by=%s", before.provider, provider, actor.user_id)
    return Outcome(True)


def propose_instructions(state: AppState, actor: Actor, text: str, *, note: str = "") -> Outcome:
    """New instructions version (quick-check decides if it can be activated)."""
    c = state.extra["services"]
    try:
        result = c.prompts.propose(actor, text, note=note)
    except PermissionDenied as exc:
        return Outcome(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    if result.problems:
        return Outcome(False, f"version {result.version} saved but cannot be activated: " + "; ".join(result.problems), "E-GRD-006")
    return Outcome(True, f"version {result.version} saved — activate it when ready")


def activate_instructions(state: AppState, actor: Actor, version: int) -> Outcome:
    """Activate (or roll back to) a version."""
    c = state.extra["services"]
    try:
        c.prompts.activate(actor, version)
    except PermissionDenied as exc:
        return Outcome(False, str(exc), exc.code)
    except VaishnaviError as exc:
        return Outcome(False, f"{exc.user_message()} {exc.what_to_do()}".strip(), exc.code)
    return Outcome(True, f"version {version} is active")


# ----------------------------------------------------------------------------- NiceGUI (thin, untested here)
def render_tab(state: AppState, client: Any, texts: UiTexts) -> None:
    """Settings tab entry point."""
    render(state, texts, client.actor())


def render(state: AppState, texts: UiTexts, actor: Actor) -> None:
    """Body of the tab."""
    from nicegui import ui  # noqa: PLC0415

    box = ui.column().classes("w-full gap-3")

    def toast(o: Outcome) -> None:
        ui.notify(o.message or texts.t("common.saved"), type="positive" if o.ok else "negative")

    def draw() -> None:
        m = build_ai_model(state, texts)
        box.clear()
        with box:
            ui.label(m.labels["title"]).classes("text-lg font-semibold")
            ui.label(f"{m.labels['health']}: {'OK' if m.health_ok else m.health_text}").classes("text-sm " + ("text-green-700" if m.health_ok else "text-orange-800"))
            form: dict[str, Any] = {"provider": m.provider, "chat": m.chat_model, "embed": m.embed_model, "key": "", "lang": m.default_language, "enabled": m.enabled}
            with ui.column().classes("vsm-card p-3 gap-2"):
                ui.select({p: label for p, label in m.providers}, value=m.provider, label=m.labels["provider"]).bind_value(form, "provider").classes("w-80")
                ui.switch("Enabled").bind_value(form, "enabled")
                ui.select({k: v for k, v in (("english", "English"), ("hindi", "हिंदी"), ("hinglish", "Hinglish"))}, value=m.default_language, label=m.labels["language"]).bind_value(form, "lang").classes("w-60")
                ui.input(m.labels["chat_model"]).bind_value(form, "chat").classes("w-96")
                ui.input(m.labels["embed_model"]).bind_value(form, "embed").classes("w-96")
                ui.input(m.labels["api_key"], password=True, password_toggle_button=True).bind_value(form, "key").classes("w-96").props("autocomplete=off")
                ui.label(f"{m.labels['key_hint']} {m.key_preview}").classes("text-xs text-gray-600")
                ui.label(m.data_notice).classes("text-xs text-gray-600").style("max-width: 640px; white-space: pre-wrap")
                with ui.row().classes("gap-2"):
                    ui.button(m.labels["test"], icon="network_check", on_click=lambda: toast(test_provider(state, actor, provider=str(form["provider"]), api_key=str(form["key"]), chat_model=str(form["chat"]), embed_model=str(form["embed"])))).props("outline")
                    ui.button(m.labels["save"], icon="save", on_click=lambda: (toast(save_provider(state, actor, provider=str(form["provider"]), api_key=str(form["key"]), chat_model=str(form["chat"]), embed_model=str(form["embed"]), enabled=bool(form["enabled"]), default_language=str(form["lang"]))), draw())).props("unelevated color=primary")
            with ui.column().classes("vsm-card p-3 gap-1"):
                ui.label(f"{m.labels['usage']} · {m.labels['budget']}: {m.budget_used_pct}% of {m.budget_tokens:,}").classes("font-semibold")
                for u in m.usage:
                    ui.label(f"{u.provider} / {u.model}: {u.requests} requests · {u.tokens:,} tokens · {u.errors} errors").classes("text-sm")
            with ui.column().classes("vsm-card p-3 gap-2"):
                ui.label(f"{m.labels['instructions']} · v{m.active_prompt_version}").classes("font-semibold")
                active = next((v for v in m.versions if v.active), None)
                edit: dict[str, Any] = {"text": active.text if active else "", "note": ""}
                ui.textarea(label=m.labels["instructions"]).bind_value(edit, "text").classes("w-full").props("rows=6")
                with ui.row().classes("items-end gap-2"):
                    ui.input("Note").bind_value(edit, "note").classes("w-60")
                    ui.button(m.labels["propose"], icon="add", on_click=lambda: (toast(propose_instructions(state, actor, str(edit["text"]), note=str(edit["note"]))), draw())).props("outline")
                for v in m.versions:
                    with ui.row().classes("items-center gap-2 text-sm"):
                        ui.icon("check_circle" if v.active else ("radio_button_unchecked" if v.passed else "block")).classes("text-green-700" if v.active else ("text-gray-500" if v.passed else "text-red-700"))
                        ui.label(f"v{v.version} · {v.created_at[:10]} · {v.note or '-'}")
                        if not v.active and v.passed:
                            ui.button(m.labels["rollback"] if v.version < m.active_prompt_version else m.labels["activate"], on_click=lambda _e, n=v.version: (toast(activate_instructions(state, actor, n)), draw())).props("flat dense")
                with ui.expansion(m.labels["core"]).classes("w-full"):
                    ui.label(m.core_text).classes("text-xs").style("white-space: pre-wrap")

    draw()
