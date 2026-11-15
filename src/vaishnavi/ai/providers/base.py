"""LLMProvider — the one interface every AI backend implements (ADR-041; stdlib only).

Guardrails, tools, the fast path and role rules sit *above* this interface and therefore behave
identically for Ollama, OpenAI, Hugging Face or no provider at all. A provider only knows how to
turn messages into text (optionally with tool calls) and texts into vectors, and how to say whether it
is healthy. Provider failures are raised as ``AIError`` with the ``E-AI-0xx`` codes of docs/08.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ChatMessage:
    """One turn: ``role`` is ``system`` | ``user`` | ``assistant`` | ``tool``.

    An assistant turn that requested tools carries them in ``tool_calls`` so the next request can echo
    the exchange in the provider's own shape (OpenAI insists on it; Ollama accepts it).
    """

    role: str
    content: str
    name: str = ""
    tool_call_id: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call (JSON-schema parameters)."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResponse:
    """Final answer of one ``chat`` call."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = "stop"


@dataclass(frozen=True)
class ChatChunk:
    """A streamed piece of the answer."""

    delta: str
    done: bool = False


@dataclass(frozen=True)
class ProviderHealth:
    """What the status chip shows."""

    ok: bool
    provider: str
    detail: str = ""
    latency_ms: int | None = None
    error_code: str = ""


@dataclass(frozen=True)
class Capabilities:
    """What this provider can do."""

    streaming: bool = True
    tools: bool = True
    embeddings: bool = True
    local: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """The contract."""

    name: str
    chat_model: str
    embed_model: str

    def capabilities(self) -> Capabilities:  # pragma: no cover - protocol
        """Feature flags."""
        ...

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:  # pragma: no cover - protocol
        """Complete a conversation."""
        ...

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:  # pragma: no cover - protocol
        """Stream the answer."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover - protocol
        """Vectors for texts (same order)."""
        ...

    def health(self) -> ProviderHealth:  # pragma: no cover - protocol
        """Cheap liveness probe."""
        ...
