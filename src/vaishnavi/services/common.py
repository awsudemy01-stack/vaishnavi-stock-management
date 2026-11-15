"""Shared contracts for services (stdlib only).

Services never import SQLAlchemy. They receive an opaque *session* and talk to the database through
narrow store protocols that the ORM repositories in ``vaishnavi.db.repositories`` satisfy structurally.
This keeps the business rules testable with in-memory fakes on a machine that has no venv, and keeps
SQL in exactly one layer (docs/03 §2).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

Session = Any
"""An open transaction handle (``sqlalchemy.orm.Session`` at runtime; anything in tests)."""


class TxnFactory(Protocol):
    """``UnitOfWork`` shape: ``with txn(label="…") as session:`` commits on success, rolls back on error."""

    def __call__(self, *, label: str = "") -> AbstractContextManager[Session]:  # pragma: no cover - protocol
        """Open a transaction."""
        ...
