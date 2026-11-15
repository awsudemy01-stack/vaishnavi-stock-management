"""Delivery channels behind one interface (docs/05 §11).

``Channel.send(path, summary, subject)`` delivers one document and raises ``PublishError`` on failure
(``E-PUB-001`` configuration / destination problem, ``E-PUB-003`` transport failure → retried). Channels
receive their credentials in the constructor from ``SecureSettingsService``; nothing here reads TOML.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from vaishnavi.core.errors import PublishError

log = logging.getLogger("vaishnavi.publishing.channels")


class Channel(Protocol):
    """One delivery route."""

    name: str

    def send(self, path: Path, *, summary: Sequence[str], subject: str) -> None:  # pragma: no cover - protocol
        """Deliver ``path`` with a short text summary; raise ``PublishError`` on failure."""
        ...


def http_post(url: str, *, data: Mapping[str, Any] | None = None, files: Mapping[str, tuple[str, bytes, str]] | None = None, headers: Mapping[str, str] | None = None, json_body: Mapping[str, Any] | None = None, timeout_s: float = 30) -> dict[str, Any]:
    """Stdlib POST: JSON body, form fields, or multipart with files. Returns the parsed JSON (or ``{}``)."""
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "VaishnaviStoreManager/1.0")
    if files:
        boundary = f"----vsm{uuid.uuid4().hex}"
        body = bytearray()
        for k, v in (data or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (filename, content, mime) in files.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + bytes(content) + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        payload: bytes = bytes(body)
    elif json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    else:
        payload = json.dumps(dict(data or {})).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method="POST")
    for k, v in hdrs.items():
        req.add_header(k, v)
    host = url.split("/")[2] if "//" in url else url
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - https URLs built from constants
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read(300).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise PublishError("E-PUB-003", channel=host, detail=f"http {exc.code}: {snippet}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PublishError("E-PUB-003", channel=host, detail=f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}
