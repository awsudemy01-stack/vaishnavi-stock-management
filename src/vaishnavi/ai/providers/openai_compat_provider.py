"""OpenAICompatProvider — OpenAI and the Hugging Face router through the same ``/v1`` HTTP API (docs/06 §2.3).

Stdlib only (no ``openai`` SDK): ``/chat/completions`` (JSON and SSE stream, native tools),
``/embeddings``, ``/models`` for health. Token usage is reported through ``on_usage`` so the
``ai_usage`` table and the monthly budget (``E-AI-030``) live in the service above, not here.
The API key is held in memory, sent only as the ``Authorization`` header and never logged.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from vaishnavi.ai.providers import _http
from vaishnavi.ai.providers.base import Capabilities, ChatChunk, ChatMessage, ChatResponse, ProviderHealth, ToolCall, ToolSpec

log = logging.getLogger("vaishnavi.ai.providers.openai_compat")

UsageSink = Callable[[str, str, int, int, int], None]
"""``(provider, model, prompt_tokens, completion_tokens, embed_tokens)``."""


def _messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        row: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "tool":
            row["tool_call_id"] = m.tool_call_id
            if m.name:
                row["name"] = m.name
        if m.role == "assistant" and m.tool_calls:
            row["content"] = m.content or None
            row["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in m.tool_calls]
        out.append(row)
    return out


def _tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


class OpenAICompatProvider:
    """Cloud provider (``openai`` or ``huggingface``)."""

    def __init__(self, *, name: str, base_url: str, api_key: str, chat_model: str, embed_model: str, timeout_s: float = 60, on_usage: UsageSink | None = None) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._key = api_key
        self.chat_model = chat_model
        self.embed_model = embed_model
        self._timeout = timeout_s
        self._on_usage = on_usage

    def capabilities(self) -> Capabilities:
        """Remote, full-featured."""
        return Capabilities(streaming=True, tools=True, embeddings=bool(self.embed_model), local=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def _usage(self, model: str, prompt: int, completion: int, embed: int) -> None:
        if self._on_usage is not None:
            try:
                self._on_usage(self.name, model, prompt, completion, embed)
            except Exception as exc:  # noqa: BLE001 - accounting must never break an answer
                log.error("AI_USAGE_SINK_FAILED provider=%s error=%s", self.name, exc)

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = (), temperature: float = 0.1, max_tokens: int | None = None) -> ChatResponse:
        """One completion."""
        payload: dict[str, Any] = {"model": self.chat_model, "messages": _messages(messages), "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = _tools(tools)
            payload["tool_choice"] = "auto"
        body, latency = _http.post_json(f"{self._base}/chat/completions", payload, headers=self._headers(), timeout_s=self._timeout, provider=self.name, local=False, model=self.chat_model)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args_raw = fn.get("arguments")
            args: Any
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw or "{}")
                except ValueError:
                    args = {"_raw": args_raw}
            else:
                args = args_raw or {}
            calls.append(ToolCall(id=str(raw.get("id") or f"call_{len(calls) + 1}"), name=str(fn.get("name") or ""), arguments=dict(args)))
        usage = body.get("usage") or {}
        prompt_tokens, completion_tokens = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        self._usage(self.chat_model, prompt_tokens, completion_tokens, 0)
        finish = str(choice.get("finish_reason") or ("tool_calls" if calls else "stop"))
        response = ChatResponse(str(message.get("content") or ""), tuple(calls), str(body.get("model") or self.chat_model), prompt_tokens, completion_tokens, latency, finish)
        log.info("AI_CHAT provider=%s model=%s ms=%d prompt_tokens=%d completion_tokens=%d tool_calls=%d finish=%s", self.name, response.model, latency, prompt_tokens, completion_tokens, len(calls), finish)
        return response

    def stream(self, messages: Sequence[ChatMessage], *, temperature: float = 0.1) -> Iterator[ChatChunk]:
        """SSE stream of ``data:`` events until ``[DONE]``."""
        started = time.monotonic()
        payload: dict[str, Any] = {"model": self.chat_model, "messages": _messages(messages), "temperature": temperature, "stream": True}
        total = 0
        for line in _http.post_lines(f"{self._base}/chat/completions", payload, headers=self._headers(), timeout_s=self._timeout, provider=self.name, local=False, model=self.chat_model):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                log.debug("AI_STREAM_SKIP non-json event=%r", data[:80])
                continue
            choice = (event.get("choices") or [{}])[0]
            delta = str((choice.get("delta") or {}).get("content") or "")
            if delta:
                total += len(delta)
                yield ChatChunk(delta)
        log.info("AI_STREAM_DONE provider=%s ms=%d chars=%d", self.name, int((time.monotonic() - started) * 1000), total)
        yield ChatChunk("", done=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """``/embeddings`` batch; results re-ordered by ``index``."""
        if not texts:
            return []
        body, latency = _http.post_json(f"{self._base}/embeddings", {"model": self.embed_model, "input": list(texts)}, headers=self._headers(), timeout_s=self._timeout, provider=self.name, local=False, model=self.embed_model)
        rows = sorted(body.get("data") or [], key=lambda r: int(r.get("index") or 0))
        vectors = [[float(x) for x in r.get("embedding") or []] for r in rows]
        tokens = int((body.get("usage") or {}).get("prompt_tokens") or 0)
        self._usage(self.embed_model, 0, 0, tokens)
        log.debug("AI_EMBED provider=%s model=%s n=%d dim=%d ms=%d tokens=%d", self.name, self.embed_model, len(vectors), len(vectors[0]) if vectors else 0, latency, tokens)
        return vectors

    def health(self) -> ProviderHealth:
        """``GET /models`` — reachable and the key accepted."""
        try:
            _body, latency = _http.get_json(f"{self._base}/models", headers=self._headers(), timeout_s=min(self._timeout, 8), provider=self.name, local=False)
        except Exception as exc:  # noqa: BLE001 - never raise from a probe
            code = getattr(exc, "code", "E-AI-020")
            return ProviderHealth(False, self.name, str(getattr(exc, "detail", exc))[:200], None, code)
        return ProviderHealth(True, self.name, f"{self.chat_model} via {self._base}", latency)
