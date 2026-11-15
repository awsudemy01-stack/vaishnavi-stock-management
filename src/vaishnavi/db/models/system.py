"""Settings (plain and encrypted), database identity, backups, notifications, setup steps, publish queue.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class Setting(Base):
    """``setting`` table."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)


class SecureSetting(Base):
    """``secure_setting`` table."""

    __tablename__ = "secure_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    masked_preview: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    updated_at: Mapped[str] = mapped_column(Text)


class DbIdentity(Base):
    """``db_identity`` table."""

    __tablename__ = "db_identity"

    instance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text)
    app_version: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(Text)
    last_open_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_clean_close_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_taken_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    restored_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    restored_from: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackupLog(Base):
    """``backup_log`` table."""

    __tablename__ = "backup_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column()
    verified: Mapped[int] = mapped_column()
    trigger: Mapped[str] = mapped_column(Text)
    backup_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    """``notification`` table."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    severity: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)
    read_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class SetupStep(Base):
    """``setup_step`` table."""

    __tablename__ = "setup_step"

    step: Mapped[str] = mapped_column(Text, primary_key=True)
    done_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)


class PublishQueue(Base):
    """``publish_queue`` table."""

    __tablename__ = "publish_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(Text)
    document: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column()
    next_retry_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)
