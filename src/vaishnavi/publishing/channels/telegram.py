"""TelegramChannel — summary message + document to the Owner's chat through the Bot API (free)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from vaishnavi.core.errors import PublishError
from vaishnavi.publishing.channels import http_post

log = logging.getLogger("vaishnavi.publishing.channels.telegram")

API = "https://api.telegram.org"


class TelegramChannel:
    """``sendMessage`` then ``sendDocument``."""

    name = "telegram"

    def __init__(self, *, token: str, chat_id: str, post: Callable[..., dict[str, Any]] | None = None) -> None:
        if not token or not chat_id:
            raise PublishError("E-PUB-001", channel=self.name, detail="bot token and chat id are required")
        self._token, self._chat = token, chat_id
        self._post = post or http_post

    def send(self, path: Path, *, summary: Sequence[str], subject: str) -> None:
        """Two API calls; any failure is E-PUB-003 and retried by the outbox."""
        base = f"{API}/bot{self._token}"
        try:
            self._post(f"{base}/sendMessage", data={"chat_id": self._chat, "text": f"*{subject}*\n" + "\n".join(summary), "parse_mode": "Markdown"})
            self._post(f"{base}/sendDocument", data={"chat_id": self._chat, "caption": subject}, files={"document": (path.name, path.read_bytes(), "application/octet-stream")})
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001 - injected transports may raise anything
            raise PublishError("E-PUB-003", channel=self.name, detail=f"{type(exc).__name__}: {exc}") from exc
        log.info("PUBLISH_TELEGRAM chat=%s file=%s", self._chat, path.name)
