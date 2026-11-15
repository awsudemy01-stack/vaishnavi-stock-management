"""Users, roles, attendance (work sessions / app runs) and the recovery key.

Generated from ``db/schema.sql`` (unit 19) and kept in step by ``tests/core/test_db_models_match_schema.py``.
Column defaults and CHECK constraints live in the SQL; the ORM only maps names and types.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from vaishnavi.db.models.base import Base


class Role(Base):
    """``role`` table."""

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    permissions: Mapped[str] = mapped_column(Text)


class User(Base):
    """``user`` table."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"))
    password_hash: Mapped[str] = mapped_column(Text)
    owner_pin_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[int] = mapped_column()
    must_change_password: Mapped[int] = mapped_column()
    failed_logins: Mapped[int] = mapped_column()
    locked_until: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text)


class AppRun(Base):
    """``app_run`` table."""

    __tablename__ = "app_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[str] = mapped_column(Text)
    stopped_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    clean_stop: Mapped[int | None] = mapped_column(nullable=True)
    app_version: Mapped[str] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkSession(Base):
    """``work_session`` table."""

    __tablename__ = "work_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    app_run_id: Mapped[int | None] = mapped_column(ForeignKey("app_run.id"), nullable=True)
    login_at: Mapped[str] = mapped_column(Text)
    logout_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    client: Mapped[str | None] = mapped_column(Text, nullable=True)
    idle_seconds: Mapped[int] = mapped_column()
    lock_count: Mapped[int] = mapped_column()
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecoveryKey(Base):
    """``recovery_key`` table."""

    __tablename__ = "recovery_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_at: Mapped[str | None] = mapped_column(Text, nullable=True)
