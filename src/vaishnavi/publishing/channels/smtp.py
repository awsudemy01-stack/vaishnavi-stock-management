"""SmtpChannel — e-mail with the document attached (stdlib ``smtplib`` + ``email``; Gmail app password or any SMTP)."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from vaishnavi.core.errors import PublishError

log = logging.getLogger("vaishnavi.publishing.channels.smtp")

SmtpFactory = Callable[..., Any]
"""``factory(host, port, timeout=...)`` → context-manager SMTP client (``smtplib.SMTP`` by default)."""


class SmtpChannel:
    """STARTTLS + login + one multipart message."""

    name = "email"

    def __init__(self, *, host: str, port: int, user: str, password: str, sender: str, to: str, smtp_factory: SmtpFactory | None = None, timeout_s: float = 30) -> None:
        if not host or not to:
            raise PublishError("E-PUB-001", channel=self.name, detail="smtp_host and email_to are required")
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender, self._to = sender or user or f"vaishnavi@{host}", to
        self._factory = smtp_factory or smtplib.SMTP
        self._timeout = timeout_s

    def send(self, path: Path, *, summary: Sequence[str], subject: str) -> None:
        """Build and send the mail; network/auth failures → E-PUB-003 (retried)."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, self._to, subject
        msg.set_content("\n".join(summary) + "\n\n— Vaishnavi Store Manager")
        mime, _ = mimetypes.guess_type(path.name)
        main, sub = (mime or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
        try:
            with self._factory(self._host, self._port, timeout=self._timeout) as client:
                client.starttls()
                if self._user:
                    client.login(self._user, self._password)
                client.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise PublishError("E-PUB-003", channel=self.name, detail=f"{type(exc).__name__}: {exc}") from exc
        log.info("PUBLISH_EMAIL to=%s subject=%s file=%s", self._to, subject, path.name)
