"""SQLAlchemy models, one module per aggregate. Importing this package registers every table on ``Base``."""

from __future__ import annotations

from vaishnavi.db.models import ai, audit, catalog, inventory, sales, shift, system, user  # noqa: F401
from vaishnavi.db.models.base import Base

__all__ = ["Base", "ai", "audit", "catalog", "inventory", "sales", "shift", "system", "user"]
