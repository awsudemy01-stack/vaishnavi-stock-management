"""WhatsAppChannel — the official Meta Cloud API (text summary; documents need a hosted media URL, so the
summary names the file the folder/e-mail channel delivered). Browser automation is deliberately avoided."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from vaishnavi.core.errors import PublishError
from vaishnavi.publishing.channels import http_post

log = logging.getLogger("vaishnavi.publishing.channels.whatsapp")

API = "https://graph.facebook.com/v19.0"


class WhatsAppChannel:
    """One ``messages`` call with a text body."""

    name = "whatsapp"

    def __init__(self, *, token: str, phone_id: str, to: str, post: Callable[..., dict[str, Any]] | None = None) -> None:
        if not token or not phone_id or not to:
            raise PublishError("E-PUB-001", channel=self.name, detail="access token, phone number id and recipient are required")
        self._token, self._phone_id, self._to = token, phone_id, to
        self._post = post or http_post

    def send(self, path: Path, *, summary: Sequence[str], subject: str) -> None:
        """Text message; failures are E-PUB-003 and retried."""
        body = f"{subject}\n" + "\n".join(summary) + f"\n\nFile: {path.name}"
        try:
            self._post(f"{API}/{self._phone_id}/messages", headers={"Authorization": f"Bearer {self._token}"}, json_body={"messaging_product": "whatsapp", "to": self._to, "type": "text", "text": {"body": body[:4000]}})
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError("E-PUB-003", channel=self.name, detail=f"{type(exc).__name__}: {exc}") from exc
        log.info("PUBLISH_WHATSAPP to=%s file=%s", self._to, path.name)
