"""Hash-chained audit log, integrity checks, assistant security events.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class AuditLog(Base):
    """``audit_log`` table."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[int | None] = mapped_column(nullable=True)
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_hash: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(Text, unique=True)


class IntegrityCheck(Base):
    """``integrity_check`` table."""

    __tablename__ = "integrity_check"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    ok: Mapped[int] = mapped_column()
    details: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChatSecurityEvent(Base):
    """``chat_security_event`` table."""

    __tablename__ = "chat_security_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    category: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    message_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    layer: Mapped[str] = mapped_column(Text)
    owner_alerted: Mapped[int] = mapped_column()
