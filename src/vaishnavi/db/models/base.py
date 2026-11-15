"""Declarative base shared by every model.

The physical schema is created by ``db/schema.sql`` (Alembic ``0001_initial``), not by
``Base.metadata.create_all`` — the SQL carries the CHECK constraints and triggers the ORM cannot
express. The ORM maps names and types for queries; ``tests/core/test_db_models_match_schema.py``
keeps the two in step.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all mapped tables."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"
