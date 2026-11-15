"""Unit 49 — AI providers over plain HTTP (docs/06 §2): Ollama, OpenAI-compatible (OpenAI + HF router), factory, circuit breaker."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from vaishnavi.ai.providers import factory
from vaishnavi.ai.providers.base import ChatMessage, LLMProvider, ToolSpec
from vaishnavi.ai.providers.fake_provider import FakeProvider
from vaishnavi.ai.providers.ollama_provider import OllamaProvider
from vaishnavi.ai.providers.openai_compat_provider import OpenAICompatProvider
from vaishnavi.core.errors import AIError
from vaishnavi.core.settings import AiSettings

TOOL = ToolSpec("get_stock", "Stock of an item", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})


class FakeServer(BaseHTTPRequestHandler):
    """Scripted Ollama + OpenAI endpoints; records every request body for assertions."""

    requests: ClassVar[list[tuple[str, dict[str, Any], dict[str, str]]]] = []
    mode: ClassVar[str] = "ok"  # ok | unauthorized | ratelimit | no_model

    def log_message(self, *_: Any) -> None:  # silence
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        FakeServer.requests.append((self.path, {}, dict(self.headers)))
        if self.path == "/api/tags":
            models = [] if FakeServer.mode == "no_model" else [{"name": "qwen2.5:3b-instruct"}, {"name": "nomic-embed-text:latest"}]
            self._json(200, {"models": models})
        elif self.path == "/v1/models":
            if FakeServer.mode == "unauthorized":
                self._json(401, {"error": {"message": "Incorrect API key"}})
            else:
                self._json(200, {"data": [{"id": "gpt-4o-mini"}]})
        else:
            self._json(404, {"error": "no"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        FakeServer.requests.append((self.path, payload, dict(self.headers)))
        if FakeServer.mode == "ratelimit":
            self._json(429, {"error": {"message": "slow down"}})
            return
        if FakeServer.mode == "unauthorized":
            self._json(401, {"error": {"message": "Incorrect API key"}})
            return
        if self.path == "/api/chat":
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for word, done in (("Camphor ", False), ("37 left.", False), ("", True)):
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": word}, "done": done}) + "\n").encode())
                return
            if payload.get("tools"):
                self._json(200, {"model": payload["model"], "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_stock", "arguments": {"query": "camphor"}}}]}, "done": True, "prompt_eval_count": 50, "eval_count": 12})
            else:
                self._json(200, {"model": payload["model"], "message": {"role": "assistant", "content": "Camphor 50g: 37 left."}, "done": True, "prompt_eval_count": 40, "eval_count": 8})
        elif self.path == "/api/embed":
            self._json(200, {"model": payload["model"], "embeddings": [[0.1, 0.2, 0.3] for _ in payload["input"]]})
        elif self.path == "/v1/chat/completions":
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in ("Camphor ", "37 left."):
                    self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": piece}, "finish_reason": None}]}) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            if payload.get("tools"):
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_stock", "arguments": json.dumps({"query": "camphor"})}}]}
                self._json(200, {"model": payload["model"], "choices": [{"message": msg, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 70, "completion_tokens": 9}})
            else:
                self._json(200, {"model": payload["model"], "choices": [{"message": {"role": "assistant", "content": "Camphor 50g: 37 left."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 60, "completion_tokens": 7}})
        elif self.path == "/v1/embeddings":
            self._json(200, {"data": [{"index": i, "embedding": [0.5, 0.5]} for i, _ in enumerate(payload["input"])], "usage": {"prompt_tokens": 12}})
        else:
            self._json(404, {"error": "no"})


class ServerBase(unittest.TestCase):
    server: ClassVar[ThreadingHTTPServer]
    base: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeServer.requests = []
        FakeServer.mode = "ok"


class OllamaProviderTest(ServerBase):
    def _p(self) -> OllamaProvider:
        return OllamaProvider(host=self.base, chat_model="qwen2.5:3b-instruct", embed_model="nomic-embed-text", keep_alive="30m", num_ctx=8192, timeout_s=5)

    def test_chat_sends_ollama_shape_and_parses_text_and_usage(self) -> None:
        p = self._p()
        self.assertIsInstance(p, LLMProvider)
        self.assertTrue(p.capabilities().local)
        r = p.chat([ChatMessage("system", "be brief"), ChatMessage("user", "camphor stock?")])
        self.assertEqual(r.text, "Camphor 50g: 37 left.")
        self.assertEqual((r.prompt_tokens, r.completion_tokens, r.model), (40, 8, "qwen2.5:3b-instruct"))
        self.assertGreaterEqual(r.latency_ms, 0)
        path, body, _ = FakeServer.requests[-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["messages"], [{"role": "system", "content": "be brief"}, {"role": "user", "content": "camphor stock?"}])
        self.assertEqual((body["stream"], body["keep_alive"], body["options"]["num_ctx"], body["options"]["temperature"]), (False, "30m", 8192, 0.1))

    def test_tool_calls_are_parsed_and_tool_results_sent_back(self) -> None:
        p = self._p()
        r = p.chat([ChatMessage("user", "camphor?")], tools=[TOOL])
        self.assertEqual(r.text, "")
        self.assertEqual([(c.name, c.arguments) for c in r.tool_calls], [("get_stock", {"query": "camphor"})])
        self.assertTrue(r.tool_calls[0].id, "a synthetic id is minted when Ollama gives none")
        self.assertEqual(r.finish_reason, "tool_calls")
        body = FakeServer.requests[-1][1]
        self.assertEqual(body["tools"][0], {"type": "function", "function": {"name": "get_stock", "description": "Stock of an item", "parameters": TOOL.parameters}})
        p.chat([ChatMessage("user", "camphor?"), ChatMessage("assistant", "", ), ChatMessage("tool", '{"qty": 37}', name="get_stock", tool_call_id=r.tool_calls[0].id)])
        sent = FakeServer.requests[-1][1]["messages"][-1]
        self.assertEqual(sent, {"role": "tool", "content": '{"qty": 37}'}, "Ollama's tool message carries only role + content")

    def test_stream_embed_and_health(self) -> None:
        p = self._p()
        chunks = list(p.stream([ChatMessage("user", "hi")]))
        self.assertEqual("".join(c.delta for c in chunks), "Camphor 37 left.")
        self.assertTrue(chunks[-1].done and not chunks[0].done)
        vectors = p.embed(["a", "b"])
        self.assertEqual(vectors, [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]])
        self.assertEqual(FakeServer.requests[-1][1], {"model": "nomic-embed-text", "input": ["a", "b"]})
        h = p.health()
        self.assertTrue(h.ok, h)
        self.assertEqual(h.provider, "ollama")
        FakeServer.mode = "no_model"
        h = p.health()
        self.assertFalse(h.ok)
        self.assertEqual(h.error_code, "E-AI-004")
        self.assertIn("qwen2.5:3b-instruct", h.detail)

    def test_unreachable_host_maps_to_e_ai_001(self) -> None:
        p = OllamaProvider(host="http://127.0.0.1:9", chat_model="x", embed_model="y", timeout_s=1)
        with self.assertRaises(AIError) as ctx:
            p.chat([ChatMessage("user", "hi")])
        self.assertEqual(ctx.exception.code, "E-AI-001")
        self.assertEqual(p.health().error_code, "E-AI-001")


class OpenAICompatProviderTest(ServerBase):
    def _p(self, name: str = "openai") -> OpenAICompatProvider:
        self.usage: list[tuple[str, str, int, int, int]] = []
        return OpenAICompatProvider(name=name, base_url=self.base + "/v1", api_key="sk-test-123", chat_model="gpt-4o-mini", embed_model="text-embedding-3-small", timeout_s=5, on_usage=lambda *a: self.usage.append(a))

    def test_chat_sends_bearer_and_openai_shape_and_accounts_usage(self) -> None:
        p = self._p()
        self.assertFalse(p.capabilities().local)
        r = p.chat([ChatMessage("user", "camphor stock?")], temperature=0.2, max_tokens=200)
        self.assertEqual(r.text, "Camphor 50g: 37 left.")
        self.assertEqual((r.prompt_tokens, r.completion_tokens), (60, 7))
        path, body, headers = FakeServer.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("Authorization"), "Bearer sk-test-123")
        self.assertEqual((body["model"], body["temperature"], body["max_tokens"], body["stream"]), ("gpt-4o-mini", 0.2, 200, False))
        self.assertEqual(self.usage, [("openai", "gpt-4o-mini", 60, 7, 0)])

    def test_tool_calls_parse_json_arguments_and_round_trip(self) -> None:
        p = self._p("huggingface")
        r = p.chat([ChatMessage("user", "camphor?")], tools=[TOOL])
        self.assertEqual([(c.id, c.name, c.arguments) for c in r.tool_calls], [("call_1", "get_stock", {"query": "camphor"})])
        self.assertEqual(r.finish_reason, "tool_calls")
        p.chat([ChatMessage("user", "camphor?"), ChatMessage("tool", '{"qty": 37}', name="get_stock", tool_call_id="call_1")])
        sent = FakeServer.requests[-1][1]["messages"][-1]
        self.assertEqual(sent, {"role": "tool", "content": '{"qty": 37}', "tool_call_id": "call_1", "name": "get_stock"})
        self.assertEqual(p.name, "huggingface")

    def test_stream_embed_health_and_error_mapping(self) -> None:
        p = self._p()
        self.assertEqual("".join(c.delta for c in p.stream([ChatMessage("user", "hi")])), "Camphor 37 left.")
        self.assertEqual(p.embed(["a", "b"]), [[0.5, 0.5], [0.5, 0.5]])
        self.assertEqual(self.usage[-1], ("openai", "text-embedding-3-small", 0, 0, 12))
        self.assertTrue(p.health().ok)
        FakeServer.mode = "unauthorized"
        h = p.health()
        self.assertEqual((h.ok, h.error_code), (False, "E-AI-012"))
        with self.assertRaises(AIError) as ctx:
            p.chat([ChatMessage("user", "hi")])
        self.assertEqual(ctx.exception.code, "E-AI-012")
        FakeServer.mode = "ratelimit"
        with self.assertRaises(AIError) as ctx:
            p.chat([ChatMessage("user", "hi")])
        self.assertEqual(ctx.exception.code, "E-AI-021")
        self.assertIn("sk-test-123", "sk-test-123")  # sanity: key present in the test
        self.assertNotIn("sk-test-123", str(ctx.exception), "the key never appears in an error message")
        offline = OpenAICompatProvider(name="openai", base_url="http://127.0.0.1:9/v1", api_key="k", chat_model="m", embed_model="e", timeout_s=1)
        with self.assertRaises(AIError) as ctx:
            offline.embed(["x"])
        self.assertEqual(ctx.exception.code, "E-AI-020")


class FactoryAndBreakerTest(unittest.TestCase):
    def test_factory_builds_the_configured_provider(self) -> None:
        s = AiSettings(provider="ollama")
        p = factory.build_provider(s, api_key=lambda name: None)
        self.assertEqual(p.name, "ollama")
        self.assertEqual(p.chat_model, "qwen2.5:3b-instruct")
        s = AiSettings(provider="openai")
        p = factory.build_provider(s, api_key=lambda name: "sk-x" if name == "openai" else None)
        self.assertEqual((p.name, p.chat_model), ("openai", "gpt-4o-mini"))
        s = AiSettings(provider="huggingface")
        p = factory.build_provider(s, api_key=lambda name: "hf_x")
        self.assertEqual((p.name, p.chat_model, p.embed_model), ("huggingface", "Qwen/Qwen2.5-7B-Instruct", "BAAI/bge-m3"))
        self.assertEqual(factory.build_provider(AiSettings(provider="none"), api_key=lambda n: None).name, "none")
        self.assertEqual(factory.build_provider(AiSettings(enabled=False, provider="ollama"), api_key=lambda n: None).name, "none", "disabled → null provider")
        missing = factory.build_provider(AiSettings(provider="openai"), api_key=lambda n: None)
        self.assertEqual(missing.name, "none", "cloud provider without a key degrades to the null provider")
        self.assertEqual(missing.health().error_code, "E-AI-011")

    def test_circuit_breaker_opens_after_failures_and_recovers(self) -> None:
        class Flaky(FakeProvider):
            def __init__(self) -> None:
                super().__init__(["ok"])
                self.fail = True

            def chat(self, messages, *, tools=(), temperature=0.1, max_tokens=None):  # type: ignore[no-untyped-def]
                if self.fail:
                    raise AIError("E-AI-001", detail="refused")
                return super().chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)

        clock = [1000.0]
        inner = Flaky()
        guarded = factory.CircuitBreaker(inner, failures=3, cooldown_s=60, now=lambda: clock[0])
        for _ in range(3):
            with self.assertRaises(AIError):
                guarded.chat([ChatMessage("user", "x")])
        self.assertTrue(guarded.is_open)
        inner.fail = False
        with self.assertRaises(AIError) as ctx:
            guarded.chat([ChatMessage("user", "x")])
        self.assertEqual(ctx.exception.code, "E-AI-001", "while open the last failure code is replayed without touching the provider")
        self.assertEqual(inner.calls, [], "no call reached the provider while open")
        h = guarded.health()
        self.assertFalse(h.ok)
        self.assertIn("circuit", h.detail)
        clock[0] += 61
        self.assertEqual(guarded.chat([ChatMessage("user", "x")]).text, "ok", "half-open probe succeeds and closes the breaker")
        self.assertFalse(guarded.is_open)
        self.assertEqual(guarded.name, "fake")
        self.assertTrue(guarded.health().ok)


if __name__ == "__main__":
    unittest.main()
