"""OpenItemReviewService — turn repeated "open items" into real products (docs/05 §8c; stdlib).

Every free-text line a cashier billed without a barcode lands here, grouped by name. The Owner either
**converts** a group into a catalogue product (ID / SKU / barcode generated, labels can be printed) or
**dismisses** it. Both are audited; the queue shrinking over time is a sign labels are catching up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import ProductRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.open_item_review")


@dataclass(frozen=True)
class OpenItemGroup:
    """Same-named open items awaiting review."""

    name: str
    count: int
    total_paise: int
    last_sold_at: str
    typical_price_paise: int
    sale_item_ids: list[int]
    details: list[str]


class ReviewStore(Protocol):
    """Rows the service needs."""

    def open_item_pending(self, session: Session) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def insert_open_item_review(self, session: Session, *, sale_item_id: int, status: str, converted_product_id: int | None, reviewed_by: int, reviewed_at: str) -> int: ...  # pragma: no cover - protocol


class OpenItemReviewService:
    """Queue · convert · dismiss."""

    def __init__(self, txn: TxnFactory, store: ReviewStore, catalog: CatalogService, audit: AuditService) -> None:
        self._txn = txn
        self._store = store
        self._catalog = catalog
        self._audit = audit

    def pending(self) -> list[OpenItemGroup]:
        """Grouped by (case-insensitive) name, most frequent first."""
        with self._txn(label="review.pending") as session:
            rows = self._store.open_item_pending(session)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row["name"]).strip().lower(), []).append(row)
        out: list[OpenItemGroup] = []
        for items in groups.values():
            prices = sorted(int(r["unit_price_paise"]) for r in items)
            out.append(OpenItemGroup(
                name=str(items[0]["name"]).strip(), count=len(items), total_paise=sum(int(r["line_total_paise"]) for r in items), last_sold_at=max(str(r["sold_at"]) for r in items),
                typical_price_paise=prices[len(prices) // 2], sale_item_ids=[int(r["sale_item_id"]) for r in items], details=sorted({str(r["detail"]) for r in items if r["detail"]}),
            ))
        out.sort(key=lambda g: (-g.count, g.name.lower()))
        log.debug("OPEN_ITEM_QUEUE groups=%d lines=%d", len(out), len(rows))
        return out

    def convert(self, actor: Actor, sale_item_ids: list[int], *, name: str, price_paise: int, category_id: int | None, **extra: Any) -> ProductRecord:
        """Create the product (ID / SKU / internal barcode) and mark the lines converted."""
        require(actor, Permission.MANAGE_PRODUCTS)
        product = self._catalog.create(actor, ProductInput(name=name.strip(), price_paise=price_paise, category_id=category_id, **extra), source="CONVERSION", reason="converted from open items")
        now = iso_utc()
        with self._txn(label="review.convert") as session:
            for sid in sale_item_ids:
                self._store.insert_open_item_review(session, sale_item_id=sid, status="CONVERTED", converted_product_id=product.id, reviewed_by=actor.user_id, reviewed_at=now)
            self._audit.record(session, "OPEN_ITEM_CONVERTED", user_id=actor.user_id, entity="product", entity_id=product.id, after={"name": product.name, "lines": len(sale_item_ids)})
        log.info("OPEN_ITEM_CONVERTED product=%s lines=%d by=%s", product.id, len(sale_item_ids), actor.user_id)
        return product

    def dismiss(self, actor: Actor, sale_item_ids: list[int]) -> None:
        """Not worth a product (one-off)."""
        require(actor, Permission.MANAGE_PRODUCTS)
        now = iso_utc()
        with self._txn(label="review.dismiss") as session:
            for sid in sale_item_ids:
                self._store.insert_open_item_review(session, sale_item_id=sid, status="DISMISSED", converted_product_id=None, reviewed_by=actor.user_id, reviewed_at=now)
            self._audit.record(session, "OPEN_ITEM_DISMISSED", user_id=actor.user_id, entity="sale_item", entity_id=sale_item_ids[0] if sale_item_ids else None, after={"lines": len(sale_item_ids)})
        log.info("OPEN_ITEM_DISMISSED lines=%d by=%s", len(sale_item_ids), actor.user_id)
