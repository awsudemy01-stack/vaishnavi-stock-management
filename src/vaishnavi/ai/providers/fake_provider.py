"""FakeProvider — scripted answers and deterministic embeddings for tests and ``selftest`` (no network)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Sequence

from vaishnavi.ai.providers.base import Capabilities, ChatChunk, ChatMessage, ChatResponse, ProviderHealth, ToolCall, ToolSpec

log = logging.getLogger("vaishnavi.ai.providers.fake")


class FakeProvider:
    """Replays ``replies`` in order (the last one repeats); records every call for assertions."""

    name = "fake"
    chat_model = "fake-chat"
    embed_model = "fake-embed"

    def __init__(self, replies: Sequence[str] = ("ok",), *, embedding_dim: int = 8, tool_calls: Sequence[Sequence[ToolCall]] = (), healthy: bool = True, embeddings: bool = True) -> None:
        self._replies = list(replies) or ["ok"]
        self._tool_calls = list(tool_calls)
        self._dim = embedding_dim
        self._healthy = healthy
        self._embeddings = embeddings
        self.calls: list[list[ChatMessage]] = []
        self.embedded: list[str] = []

    def capabilities(self) -> Capabilities:
        """Everything, locally (embeddings can be switched off to exercise the keyword fallback)."""
        return Capabilities(local=True, embeddings=self._embeddings)

    def _next(self) -> tuple[str, tuple[ToolCall, ...]]:
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        calls = tuple(self._tool_calls[index]) if index < len(self._tool_calls) else ()
        return self._replies[index], calls

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:
        """Scripted."""
        self.calls.append(list(messages))
        text, calls = self._next()
        log.debug("FAKE_PROVIDER chat n=%d reply=%r tools=%d", len(self.calls), text[:40], len(tools))
        return ChatResponse(text, calls, self.chat_model, prompt_tokens=sum(len(m.content) // 4 for m in messages), completion_tokens=len(text) // 4, latency_ms=1)

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:
        """Word by word."""
        text = self.chat(messages, temperature=temperature).text
        words = text.split(" ")
        for i, word in enumerate(words):
            yield ChatChunk(word + (" " if i < len(words) - 1 else ""), done=False)
        yield ChatChunk("", done=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Deterministic pseudo-vectors from a hash (same text → same vector)."""
        out: list[list[float]] = []
        for text in texts:
            self.embedded.append(text)
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([((digest[i % len(digest)] / 255.0) * 2 - 1) for i in range(self._dim)])
        return out

    def health(self) -> ProviderHealth:
        """As configured."""
        return ProviderHealth(self._healthy, self.name, "fake provider", latency_ms=1)
