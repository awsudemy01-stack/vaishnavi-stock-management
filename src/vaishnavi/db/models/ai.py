"""Chat sessions/messages, usage, RAG chunks and embeddings, prompt versions, festivals, forecast snapshots.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, LargeBinary, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class ChatSession(Base):
    """``chat_session`` table."""

    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)


class ChatMessage(Base):
    """``chat_message`` table."""

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_session.id"))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens: Mapped[int | None] = mapped_column(nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    guardrail_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)


class AiUsage(Base):
    """``ai_usage`` table."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column()
    completion_tokens: Mapped[int] = mapped_column()
    embed_tokens: Mapped[int] = mapped_column()
    requests: Mapped[int] = mapped_column()
    errors: Mapped[int] = mapped_column()


class DocChunk(Base):
    """``doc_chunk`` table."""

    __tablename__ = "doc_chunk"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)


class Embedding(Base):
    """``embedding`` table."""

    __tablename__ = "embedding"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column()
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    dim: Mapped[int] = mapped_column()


class PromptVersion(Base):
    """``prompt_version`` table."""

    __tablename__ = "prompt_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(unique=True)
    editable_instructions: Mapped[str] = mapped_column(Text)
    core_hash: Mapped[str] = mapped_column(Text)
    default_language: Mapped[str] = mapped_column(Text)
    extra_blocked_phrases: Mapped[str] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column()
    redteam_passed: Mapped[int] = mapped_column()
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Festival(Base):
    """``festival`` table."""

    __tablename__ = "festival"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[str] = mapped_column(Text)
    end_date: Mapped[str] = mapped_column(Text)
    category_uplift: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)


class ForecastSnapshot(Base):
    """``forecast_snapshot`` table."""

    __tablename__ = "forecast_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    computed_at: Mapped[str] = mapped_column(Text)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product.id"), nullable=True)
    horizon_days: Mapped[int] = mapped_column()
    point: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(Text)
    caveats: Mapped[str | None] = mapped_column(Text, nullable=True)
