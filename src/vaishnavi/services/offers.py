"""OfferService — named, pre-approved discounts the Owner defines and the POS applies automatically (docs/05 §8a).

An offer has a **type** (``PCT`` percent · ``AMOUNT`` rupees per line · ``BUY_X_GET_Y`` with X =
``min_qty`` and Y = ``value`` free pieces), a **scope** (one product · a category · a tag · the whole
bill), a validity window and a ``stackable`` flag. The cashier never types an offer: when an item lands
in the cart :meth:`best_for` picks the highest benefit among the applicable offers and the line shows
its name in the Discount column. Only the Owner may create, edit or deactivate (``MANAGE_OFFERS``);
every change is audited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, today_ist
from vaishnavi.core.money import apply_percent, multiply, to_paise
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import OfferRecord, ProductRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.offers")

OFFER_TYPES: Final = ("PCT", "AMOUNT", "BUY_X_GET_Y")
OFFER_SCOPES: Final = ("PRODUCT", "CATEGORY", "TAG", "BILL")


@dataclass(frozen=True)
class OfferInput:
    """Editor payload."""

    name: str
    type: str
    value: Decimal
    scope: str
    scope_id: int | None = None
    name_hi: str | None = None
    min_qty: Decimal = Decimal("0")
    valid_from: str | None = None
    valid_to: str | None = None
    stackable: bool = False


@dataclass(frozen=True)
class OfferMatch:
    """An offer that applies to a cart line, resolved into line-level discount figures."""

    offer: OfferRecord
    discount_bp: int
    """For PCT offers (1 % → 100)."""
    discount_paise: int
    """For AMOUNT and BUY_X_GET_Y offers — a fixed amount for the whole line."""
    stackable: bool

    def benefit_paise(self, gross_paise: int) -> int:
        """Rupee value of this offer on a line of ``gross_paise``."""
        return min(gross_paise, apply_percent(gross_paise, Decimal(self.discount_bp) / 100) + self.discount_paise)


class OfferStore(Protocol):
    """Rows the service needs."""

    def offers(self, session: Session, *, active_only: bool) -> list[OfferRecord]: ...  # pragma: no cover - protocol
    def offer(self, session: Session, offer_id: int) -> OfferRecord | None: ...  # pragma: no cover - protocol
    def insert_offer(self, session: Session, fields: dict[str, Any]) -> int: ...  # pragma: no cover - protocol
    def update_offer(self, session: Session, offer_id: int, **fields: Any) -> None: ...  # pragma: no cover - protocol
    def offer_usage(self, session: Session, offer_id: int) -> tuple[int, int]: ...  # pragma: no cover - protocol


class TagLookup(Protocol):
    """The catalogue slice offers need for TAG scope."""

    def tags_of(self, session: Session, product_id: int) -> list[str]: ...  # pragma: no cover - protocol
    def tag_id(self, session: Session, name: str) -> int | None: ...  # pragma: no cover - protocol


class OfferService:
    """Owner CRUD + POS matching."""

    def __init__(self, txn: TxnFactory, store: OfferStore, tags: TagLookup, audit: AuditService) -> None:
        self._txn = txn
        self._store = store
        self._tags = tags
        self._audit = audit

    # -- reads ------------------------------------------------------------------------------------
    def list_offers(self, *, active_only: bool = False) -> list[OfferRecord]:
        """Alphabetical."""
        with self._txn(label="offers.list") as session:
            return sorted(self._store.offers(session, active_only=active_only), key=lambda o: o.name.lower())

    def get(self, offer_id: int) -> OfferRecord | None:
        """One offer."""
        with self._txn(label="offers.get") as session:
            return self._store.offer(session, offer_id)

    def usage(self, offer_id: int) -> tuple[int, int]:
        """``(lines, paise given)`` — "Diwali Offer: 312 lines, ₹2,340 given"."""
        with self._txn(label="offers.usage") as session:
            return self._store.offer_usage(session, offer_id)

    def applicable(self, product: ProductRecord, qty: Decimal, *, on: date | None = None) -> list[OfferMatch]:
        """Every active, in-window offer whose scope covers the product and whose min qty is met."""
        day = (on or today_ist()).isoformat()
        with self._txn(label="offers.match") as session:
            offers = self._store.offers(session, active_only=True)
            tag_ids: set[int] = set()
            if any(o.scope == "TAG" for o in offers):
                for name in self._tags.tags_of(session, product.id):
                    tid = self._tags.tag_id(session, name)
                    if tid is not None:
                        tag_ids.add(tid)
        out: list[OfferMatch] = []
        for offer in offers:
            if not _in_window(offer, day) or offer.scope == "BILL":
                continue
            if offer.scope == "PRODUCT" and offer.scope_id != product.id:
                continue
            if offer.scope == "CATEGORY" and offer.scope_id != product.category_id:
                continue
            if offer.scope == "TAG" and offer.scope_id not in tag_ids:
                continue
            if qty < offer.min_qty:
                continue
            match = resolve(offer, product, qty)
            if match is not None:
                out.append(match)
        return out

    def best_for(self, product: ProductRecord, qty: Decimal, *, on: date | None = None) -> OfferMatch | None:
        """Highest benefit for this line, or None."""
        matches = self.applicable(product, qty, on=on)
        if not matches:
            return None
        gross = multiply(product.price_paise, qty / product.pricing_unit_qty)
        best = max(matches, key=lambda m: (m.benefit_paise(gross), -m.offer.id))
        log.debug("OFFER_MATCH product=%s qty=%s offer=%s benefit=%d of %d", product.sku, qty, best.offer.name, best.benefit_paise(gross), len(matches))
        return best

    def bill_offers(self, *, on: date | None = None) -> list[OfferRecord]:
        """Active whole-bill offers today (PCT / AMOUNT)."""
        day = (on or today_ist()).isoformat()
        with self._txn(label="offers.bill") as session:
            return [o for o in self._store.offers(session, active_only=True) if o.scope == "BILL" and _in_window(o, day)]

    # -- writes -----------------------------------------------------------------------------------
    def create(self, actor: Actor, data: OfferInput) -> OfferRecord:
        """Owner only."""
        require(actor, Permission.MANAGE_OFFERS)
        _validate(data)
        now = iso_utc()
        fields = {"name": data.name.strip(), "name_hi": data.name_hi, "type": data.type, "value": str(data.value), "scope": data.scope, "scope_id": data.scope_id, "min_qty": str(data.min_qty), "valid_from": data.valid_from, "valid_to": data.valid_to, "stackable": int(data.stackable), "is_active": 1, "created_by": actor.user_id, "created_at": now}
        with self._txn(label="offers.create") as session:
            oid = self._store.insert_offer(session, fields)
            self._audit.record(session, "OFFER_CREATED", user_id=actor.user_id, entity="offer", entity_id=oid, after={k: v for k, v in fields.items() if k not in ("created_at",)})
            record = self._store.offer(session, oid)
        assert record is not None
        log.info("OFFER_CREATED id=%s name=%r type=%s scope=%s by=%s", oid, record.name, record.type, record.scope, actor.user_id)
        return record

    def update(self, actor: Actor, offer_id: int, **changes: Any) -> OfferRecord:
        """Owner only; any column except id/created_*."""
        require(actor, Permission.MANAGE_OFFERS)
        allowed = {"name", "name_hi", "type", "value", "scope", "scope_id", "min_qty", "valid_from", "valid_to", "stackable", "is_active"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"cannot change {sorted(bad)}")
        with self._txn(label="offers.update") as session:
            before = self._store.offer(session, offer_id)
            if before is None:
                raise ValueError(f"offer {offer_id} not found")
            self._store.update_offer(session, offer_id, **changes)
            self._audit.record(session, "OFFER_UPDATED", user_id=actor.user_id, entity="offer", entity_id=offer_id, before={k: getattr(before, k) for k in changes}, after=changes)
            after = self._store.offer(session, offer_id)
        assert after is not None
        return after

    def deactivate(self, actor: Actor, offer_id: int) -> None:
        """Owner only; history is kept (sale lines reference the offer)."""
        require(actor, Permission.MANAGE_OFFERS)
        with self._txn(label="offers.deactivate") as session:
            self._store.update_offer(session, offer_id, is_active=0)
            self._audit.record(session, "OFFER_DEACTIVATED", user_id=actor.user_id, entity="offer", entity_id=offer_id)
        log.info("OFFER_DEACTIVATED id=%s by=%s", offer_id, actor.user_id)


# ----------------------------------------------------------------------------- pure helpers
def resolve(offer: OfferRecord, product: ProductRecord, qty: Decimal) -> OfferMatch | None:
    """Turn an offer into line discount figures for ``qty`` of ``product``."""
    if offer.type == "PCT":
        return OfferMatch(offer, int((offer.value * 100).to_integral_value()), 0, offer.stackable)
    if offer.type == "AMOUNT":
        return OfferMatch(offer, 0, to_paise(offer.value), offer.stackable)
    if offer.type == "BUY_X_GET_Y":
        x, y = offer.min_qty, offer.value
        if x <= 0 or y <= 0:
            return None
        groups = int(qty // (x + y))
        if groups == 0:
            return None
        free_units = Decimal(groups) * y
        return OfferMatch(offer, 0, multiply(product.price_paise, free_units / product.pricing_unit_qty), offer.stackable)
    return None


def _in_window(offer: OfferRecord, day: str) -> bool:
    if offer.valid_from and day < offer.valid_from:
        return False
    return not (offer.valid_to and day > offer.valid_to)


def _validate(data: OfferInput) -> None:
    if not data.name or not data.name.strip():
        raise ValueError("an offer needs a name")
    if data.type not in OFFER_TYPES:
        raise ValueError(f"offer type must be one of {OFFER_TYPES}")
    if data.scope not in OFFER_SCOPES:
        raise ValueError(f"offer scope must be one of {OFFER_SCOPES}")
    if data.scope != "BILL" and data.scope_id is None:
        raise ValueError("scope_id is required unless scope is BILL")
    if data.value < 0 or (data.type == "PCT" and data.value > 100):
        raise ValueError("offer value out of range")
    if data.type == "BUY_X_GET_Y" and (data.min_qty <= 0 or data.value <= 0):
        raise ValueError("buy-X-get-Y needs X (min_qty) and Y (value) above zero")
    if data.valid_from and data.valid_to and data.valid_from > data.valid_to:
        raise ValueError("valid_from is after valid_to")
