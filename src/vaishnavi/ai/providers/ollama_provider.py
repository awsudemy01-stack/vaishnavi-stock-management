"""OllamaProvider — the local engine over its HTTP API (docs/06 §2.3), stdlib only.

``/api/chat`` (non-stream and NDJSON stream, native tools), ``/api/embed``, ``/api/tags`` for health.
Install / start / pull live in ``bootstrap/ollama_manager.py``; this class assumes a running server.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from vaishnavi.ai.providers import _http
from vaishnavi.ai.providers.base import Capabilities, ChatChunk, ChatMessage, ChatResponse, ProviderHealth, ToolCall, ToolSpec

log = logging.getLogger("vaishnavi.ai.providers.ollama")


def _messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        # Ollama's tool message is just role + content; names/ids are OpenAI-isms.
        row: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            row["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
        out.append(row)
    return out


def _tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


class OllamaProvider:
    """Local provider."""

    name = "ollama"

    def __init__(self, *, host: str, chat_model: str, embed_model: str, keep_alive: str = "30m", num_ctx: int = 8192, timeout_s: float = 60) -> None:
        self._host = host.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self._keep_alive = keep_alive
        self._num_ctx = num_ctx
        self._timeout = timeout_s

    def capabilities(self) -> Capabilities:
        """Everything, locally."""
        return Capabilities(streaming=True, tools=True, embeddings=True, local=True)

    def _payload(self, messages: Sequence[ChatMessage], *, stream: bool, tools: Sequence[ToolSpec], temperature: float, max_tokens: int | None) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature, "num_ctx": self._num_ctx}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: dict[str, Any] = {"model": self.chat_model, "messages": _messages(messages), "stream": stream, "keep_alive": self._keep_alive, "options": options}
        if tools:
            payload["tools"] = _tools(tools)
        return payload

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:
        """One completion, with native tool calls when ``tools`` are given."""
        body, latency = _http.post_json(f"{self._host}/api/chat", self._payload(messages, stream=False, tools=tools, temperature=temperature, max_tokens=max_tokens), timeout_s=self._timeout, provider=self.name, local=True, model=self.chat_model)
        message = body.get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            calls.append(ToolCall(id=str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"), name=str(fn.get("name") or ""), arguments=dict(args or {})))
        text = str(message.get("content") or "")
        response = ChatResponse(text, tuple(calls), str(body.get("model") or self.chat_model), int(body.get("prompt_eval_count") or 0), int(body.get("eval_count") or 0), latency, "tool_calls" if calls else "stop")
        log.info("AI_CHAT provider=ollama model=%s ms=%d prompt_tokens=%d completion_tokens=%d tool_calls=%d", response.model, latency, response.prompt_tokens, response.completion_tokens, len(calls))
        return response

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:
        """NDJSON stream; the final line carries ``done: true``."""
        started = time.monotonic()
        total = 0
        for line in _http.post_lines(f"{self._host}/api/chat", self._payload(messages, stream=True, tools=(), temperature=temperature, max_tokens=None), timeout_s=self._timeout, provider=self.name, local=True, model=self.chat_model):
            try:
                event = json.loads(line)
            except ValueError:
                log.debug("AI_STREAM_SKIP non-json line=%r", line[:80])
                continue
            delta = str((event.get("message") or {}).get("content") or "")
            total += len(delta)
            if event.get("done"):
                log.info("AI_STREAM_DONE provider=ollama ms=%d chars=%d", int((time.monotonic() - started) * 1000), total)
                yield ChatChunk("", done=True)
                return
            if delta:
                yield ChatChunk(delta)
        yield ChatChunk("", done=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """``/api/embed`` with a batch input."""
        if not texts:
            return []
        body, latency = _http.post_json(f"{self._host}/api/embed", {"model": self.embed_model, "input": list(texts)}, timeout_s=self._timeout, provider=self.name, local=True, model=self.embed_model)
        vectors = [[float(x) for x in v] for v in body.get("embeddings") or []]
        log.debug("AI_EMBED provider=ollama model=%s n=%d dim=%d ms=%d", self.embed_model, len(vectors), len(vectors[0]) if vectors else 0, latency)
        return vectors

    def health(self) -> ProviderHealth:
        """Server up and both models present."""
        try:
            body, latency = _http.get_json(f"{self._host}/api/tags", timeout_s=min(self._timeout, 5), provider=self.name, local=True)
        except Exception as exc:  # noqa: BLE001 - AIError or anything odd: never raise from a probe
            code = getattr(exc, "code", "E-AI-001")
            return ProviderHealth(False, self.name, str(getattr(exc, "detail", exc))[:200], None, code)
        names = {str(m.get("name") or "") for m in body.get("models") or []}
        bare = {n.split(":", 1)[0] for n in names}
        missing = [m for m in (self.chat_model, self.embed_model) if m not in names and m.split(":", 1)[0] not in bare]
        if missing:
            return ProviderHealth(False, self.name, f"model missing: {', '.join(missing)}", latency, "E-AI-004")
        return ProviderHealth(True, self.name, f"{self.chat_model} ready", latency)
