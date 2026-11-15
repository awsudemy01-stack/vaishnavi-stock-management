"""NullProvider — the assistant is switched off (``ai.provider = none``); every call says so with ``E-AI-010``."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

from vaishnavi.ai.providers.base import Capabilities, ChatChunk, ChatMessage, ChatResponse, ProviderHealth, ToolSpec
from vaishnavi.core.errors import AIError

log = logging.getLogger("vaishnavi.ai.providers.null")


class NullProvider:
    """Stock questions still work through the fast path; anything needing a model raises ``E-AI-010``."""

    name = "none"
    chat_model = ""
    embed_model = ""

    def __init__(self, *, reason_code: str = "E-AI-010", detail: str = "assistant turned off", **params: str) -> None:
        # ``reason_code`` lets the factory say *why* there is no model (e.g. E-AI-011 key missing).
        self._code = reason_code
        self._detail = detail
        self._params = params

    def capabilities(self) -> Capabilities:
        """Nothing."""
        return Capabilities(streaming=False, tools=False, embeddings=False, local=True)

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:
        """Always refuses."""
        log.debug("NULL_PROVIDER chat refused messages=%d code=%s", len(messages), self._code)
        raise AIError(self._code, detail=self._detail, **self._params)

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:
        """Always refuses."""
        raise AIError(self._code, detail=self._detail, **self._params)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Always refuses."""
        raise AIError(self._code, detail=self._detail, **self._params)

    def health(self) -> ProviderHealth:
        """Off, by choice (or by a missing key)."""
        return ProviderHealth(False, self.name, self._detail, error_code=self._code)
