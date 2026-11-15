"""ProviderFactory + CircuitBreaker (docs/06 §2.3–2.4).

``build_provider(settings.ai, api_key)`` returns the configured provider; a cloud provider without a
stored key, or ``enabled = false``, degrades to :class:`NullProvider` with the right reason code so the
status chip and the doctor explain *why*. :class:`CircuitBreaker` wraps any provider: after ``failures``
consecutive errors it stays open for ``cooldown_s`` and replays the last error instantly, so a dead
Ollama or a down internet link never makes the chat widget wait 60 s per message.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence

from vaishnavi.ai.providers.base import Capabilities, ChatChunk, ChatMessage, ChatResponse, LLMProvider, ProviderHealth, ToolSpec
from vaishnavi.ai.providers.null_provider import NullProvider
from vaishnavi.ai.providers.ollama_provider import OllamaProvider
from vaishnavi.ai.providers.openai_compat_provider import OpenAICompatProvider, UsageSink
from vaishnavi.core.errors import AIError
from vaishnavi.core.settings import AiSettings

log = logging.getLogger("vaishnavi.ai.providers.factory")

KeyLookup = Callable[[str], str | None]
"""``api_key("openai")`` → the decrypted key or None (SecureSettingsService.reveal_for_system)."""


def build_provider(ai: AiSettings, *, api_key: KeyLookup, on_usage: UsageSink | None = None) -> LLMProvider:
    """The provider for the current settings (never raises; degraded → NullProvider)."""
    if not ai.enabled or ai.provider == "none":
        log.info("AI_PROVIDER_BUILT provider=none reason=%s", "disabled" if not ai.enabled else "configured")
        return NullProvider()
    if ai.provider == "ollama":
        o = ai.ollama
        log.info("AI_PROVIDER_BUILT provider=ollama host=%s chat=%s embed=%s", o.host, o.chat_model, o.embed_model)
        return OllamaProvider(host=o.host, chat_model=o.chat_model, embed_model=o.embed_model, keep_alive=o.keep_alive, num_ctx=o.num_ctx, timeout_s=ai.request_timeout_s)
    cloud = ai.openai if ai.provider == "openai" else ai.huggingface
    key = api_key(ai.provider)
    if not key:
        log.warning("AI_PROVIDER_BUILT provider=none reason=key_missing wanted=%s code=E-AI-011", ai.provider)
        return NullProvider(reason_code="E-AI-011", detail=f"API key for {ai.provider} not set", provider=ai.provider)
    log.info("AI_PROVIDER_BUILT provider=%s base_url=%s chat=%s embed=%s", ai.provider, cloud.base_url, cloud.chat_model, cloud.embed_model)
    return OpenAICompatProvider(name=ai.provider, base_url=cloud.base_url, api_key=key, chat_model=cloud.chat_model, embed_model=cloud.embed_model, timeout_s=ai.request_timeout_s, on_usage=on_usage)


class CircuitBreaker:
    """Wraps a provider; same interface, fails fast while open."""

    def __init__(self, inner: LLMProvider, *, failures: int = 3, cooldown_s: float = 60, now: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self._failures = failures
        self._cooldown = cooldown_s
        self._now = now
        self._lock = threading.Lock()
        self._count = 0
        self._opened_at: float | None = None
        self._last: AIError | None = None

    # -- passthrough identity --------------------------------------------------------------------
    @property
    def name(self) -> str:  # type: ignore[override]
        """Inner provider name."""
        return self._inner.name

    @property
    def chat_model(self) -> str:  # type: ignore[override]
        """Inner chat model."""
        return self._inner.chat_model

    @property
    def embed_model(self) -> str:  # type: ignore[override]
        """Inner embedding model."""
        return self._inner.embed_model

    @property
    def inner(self) -> LLMProvider:
        """The wrapped provider."""
        return self._inner

    @property
    def is_open(self) -> bool:
        """True while calls are being rejected."""
        with self._lock:
            return self._opened_at is not None and (self._now() - self._opened_at) < self._cooldown

    def capabilities(self) -> Capabilities:
        """Inner capabilities."""
        return self._inner.capabilities()

    # -- state machine ---------------------------------------------------------------------------
    def _before(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if (self._now() - self._opened_at) < self._cooldown:
                assert self._last is not None
                log.debug("AI_CIRCUIT_OPEN provider=%s replay=%s", self._inner.name, self._last.code)
                raise self._last
            log.info("AI_CIRCUIT_HALF_OPEN provider=%s", self._inner.name)

    def _success(self) -> None:
        with self._lock:
            if self._count or self._opened_at is not None:
                log.info("AI_CIRCUIT_CLOSED provider=%s", self._inner.name)
            self._count = 0
            self._opened_at = None
            self._last = None

    def _failure(self, exc: AIError) -> None:
        with self._lock:
            self._count += 1
            self._last = exc
            if self._count >= self._failures or self._opened_at is not None:
                self._opened_at = self._now()
                log.warning("AI_CIRCUIT_OPENED provider=%s failures=%d cooldown_s=%s code=%s", self._inner.name, self._count, self._cooldown, exc.code)

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:
        """Guarded chat."""
        self._before()
        try:
            out = self._inner.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
        except AIError as exc:
            self._failure(exc)
            raise
        self._success()
        return out

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:
        """Guarded stream (a failure mid-stream counts once)."""
        self._before()
        try:
            for chunk in self._inner.stream(messages, temperature=temperature):
                yield chunk
        except AIError as exc:
            self._failure(exc)
            raise
        self._success()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Guarded embeddings."""
        self._before()
        try:
            out = self._inner.embed(texts)
        except AIError as exc:
            self._failure(exc)
            raise
        self._success()
        return out

    def health(self) -> ProviderHealth:
        """Open breaker → not ok, without probing; otherwise the inner probe."""
        if self.is_open:
            assert self._last is not None
            return ProviderHealth(False, self._inner.name, f"circuit open after repeated failures ({self._last.code})", None, self._last.code)
        return self._inner.health()
