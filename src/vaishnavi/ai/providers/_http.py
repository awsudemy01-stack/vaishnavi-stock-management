"""Tiny stdlib HTTP client shared by the providers (no ``requests`` / ``httpx`` / vendor SDKs).

Why stdlib: the providers are the only network code in the AI layer; ``urllib`` keeps the whole tier
importable on the development PC and leaves nothing to install. Every failure is mapped to the
``E-AI-0xx`` codes of docs/08 here, once, so the two providers stay small. The API key is never put in
an exception message or a log line.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

from vaishnavi.core.errors import AIError

log = logging.getLogger("vaishnavi.ai.providers.http")

USER_AGENT = "VaishnaviStoreManager/1.0 (+stdlib urllib)"


def _request(url: str, *, method: str, payload: Mapping[str, Any] | None, headers: Mapping[str, str], timeout_s: float) -> urllib.request.Request:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json, text/event-stream, application/x-ndjson")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    return req


def _map_error(exc: BaseException, *, provider: str, local: bool, model: str = "") -> AIError:
    """urllib exception → AIError with the documented code; detail never carries the request body/key."""
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        body = ""
        try:
            body = exc.read(2000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - best effort
            body = ""
        snippet = body[:200].replace("\n", " ")
        if status in (401, 403):
            return AIError("E-AI-012", provider=provider, detail=f"http {status}: {snippet}", cause=exc)
        if status == 429:
            return AIError("E-AI-021", provider=provider, detail=f"http 429: {snippet}", cause=exc)
        if status == 404 and local:
            return AIError("E-AI-004", model=model or "?", detail=f"http 404: {snippet}", cause=exc)
        code = "E-AI-001" if local else "E-AI-020"
        return AIError(code, provider=provider, detail=f"http {status}: {snippet}", cause=exc)
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        reason = getattr(exc, "reason", exc)
        return AIError("E-AI-001" if local else "E-AI-020", provider=provider, detail=f"{type(exc).__name__}: {reason}", cause=exc)
    return AIError("E-AI-001" if local else "E-AI-020", provider=provider, detail=f"{type(exc).__name__}: {exc}", cause=exc)


def post_json(url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str] | None = None, timeout_s: float, provider: str, local: bool, model: str = "") -> tuple[dict[str, Any], int]:
    """POST JSON, return ``(parsed body, latency_ms)``."""
    started = time.monotonic()
    req = _request(url, method="POST", payload=payload, headers=headers or {}, timeout_s=timeout_s)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - http(s) URLs from settings only
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - mapped below
        err = _map_error(exc, provider=provider, local=local, model=model)
        log.warning("AI_HTTP_FAIL provider=%s url=%s code=%s detail=%s", provider, _safe_url(url), err.code, err.detail)
        raise err from exc
    latency = int((time.monotonic() - started) * 1000)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError as exc:
        raise AIError("E-AI-001" if local else "E-AI-020", provider=provider, detail="response was not JSON", cause=exc) from exc
    log.debug("AI_HTTP_OK provider=%s url=%s ms=%d bytes=%d", provider, _safe_url(url), latency, len(raw))
    return body, latency


def get_json(url: str, *, headers: Mapping[str, str] | None = None, timeout_s: float, provider: str, local: bool) -> tuple[dict[str, Any], int]:
    """GET JSON, return ``(parsed body, latency_ms)``."""
    started = time.monotonic()
    req = _request(url, method="GET", payload=None, headers=headers or {}, timeout_s=timeout_s)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        err = _map_error(exc, provider=provider, local=local)
        log.warning("AI_HTTP_FAIL provider=%s url=%s code=%s detail=%s", provider, _safe_url(url), err.code, err.detail)
        raise err from exc
    latency = int((time.monotonic() - started) * 1000)
    try:
        return (json.loads(raw.decode("utf-8")) if raw else {}), latency
    except ValueError as exc:
        raise AIError("E-AI-001" if local else "E-AI-020", provider=provider, detail="response was not JSON", cause=exc) from exc


def post_lines(url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str] | None = None, timeout_s: float, provider: str, local: bool, model: str = "") -> Iterator[str]:
    """POST and yield the response line by line (NDJSON for Ollama, SSE ``data:`` lines for OpenAI)."""
    req = _request(url, method="POST", payload=payload, headers=headers or {}, timeout_s=timeout_s)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        err = _map_error(exc, provider=provider, local=local, model=model)
        log.warning("AI_HTTP_FAIL provider=%s url=%s code=%s detail=%s", provider, _safe_url(url), err.code, err.detail)
        raise err from exc
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                yield line
    except (OSError, socket.timeout) as exc:
        raise AIError("E-AI-001" if local else "E-AI-020", provider=provider, detail=f"stream interrupted: {type(exc).__name__}", cause=exc) from exc
    finally:
        resp.close()


def _safe_url(url: str) -> str:
    return url.split("?", 1)[0]
