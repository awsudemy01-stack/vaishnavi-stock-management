"""CatalogService — products, the identity triangle (ID · code · barcodes), categories, tags (docs/05 §"identity", ADR-023; stdlib).

* **Create** assigns the human code ``PREFIX-NNNN`` (prefix from the category name) and the permanent
  internal EAN-13 derived from the new id; every price-like field starts its ``price_history``.
* **Update** is optimistic (``row_version`` → ``E-CAT-010``) and appends a ``price_history`` row for each
  changed money/tax field with the reason and the user.
* **Barcodes**: several per product (INTERNAL · MANUFACTURER · PACK with ``pack_qty``); a code belongs to
  exactly one product (``E-CAT-004``); numeric codes must carry a correct check digit (``E-CAT-020``).
* **Search** for F3 Find: ``#ID`` / bare id / code / barcode / name EN+HI, with a typo-tolerant pass.
Only the Owner writes (``MANAGE_PRODUCTS``); everyone may read and scan.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

from vaishnavi.core import barcode as bc
from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import CatalogError
from vaishnavi.core.events import EventBus, ProductChanged, bus
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import BarcodeRecord, CategoryRecord, ProductRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.catalog")

PRICE_FIELDS: Final = {"price_paise": "PRICE", "mrp_paise": "MRP", "cost_paise": "COST", "tax_rate_bp": "TAX_RATE"}
PERMANENT_FIELDS: Final = frozenset({"id", "internal_barcode", "is_system", "row_version", "created_at", "updated_at"})
SKU_RE: Final = re.compile(r"^[A-Z0-9]{2,6}-\d{4,}$")
_ID_QUERY: Final = re.compile(r"^#?(\d{1,10})$")


# ----------------------------------------------------------------------------- inputs / results
@dataclass(frozen=True)
class ProductInput:
    """What the editor / importer supplies for a new product."""

    name: str
    price_paise: int
    category_id: int | None = None
    name_hi: str | None = None
    sku: str | None = None
    mrp_paise: int | None = None
    cost_paise: int | None = None
    tax_rate_bp: int = 0
    price_includes_tax: bool = True
    hsn_code: str | None = None
    unit: str = "pcs"
    sale_mode: str = "PIECE"
    base_unit: str = "pcs"
    pricing_unit_qty: Decimal = Decimal("1")
    pricing_unit_label: str = "pc"
    min_qty: Decimal = Decimal("1")
    qty_step: Decimal = Decimal("1")
    qty_presets: str = "[]"
    allow_decimal_qty: bool = False
    min_stock: Decimal = Decimal("0")
    reorder_qty: Decimal = Decimal("0")
    image_path: str | None = None
    quick_pick: bool = False
    manufacturer_barcode: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BarcodeHit:
    """Result of :meth:`CatalogService.resolve_barcode`."""

    product: ProductRecord
    barcode: BarcodeRecord
    pack_qty: Decimal


@dataclass(frozen=True)
class SearchHit:
    """One F3 Find result."""

    product: ProductRecord
    matched: str  # id | code | barcode | name | name_hi | fuzzy
    score: float


@dataclass(frozen=True)
class ProductPage:
    """Grid page."""

    items: list[ProductRecord]
    total: int
    offset: int
    limit: int


# ----------------------------------------------------------------------------- store protocol
class CatalogStore(Protocol):
    """What the service needs (implemented by ``SqliteCatalogStore`` and the ORM repo)."""

    def product_by_id(self, session: Session, product_id: int) -> ProductRecord | None: ...  # pragma: no cover - protocol
    def product_by_sku(self, session: Session, sku: str) -> ProductRecord | None: ...  # pragma: no cover - protocol
    def insert_product(self, session: Session, fields: dict[str, Any]) -> int: ...  # pragma: no cover - protocol
    def update_product(self, session: Session, product_id: int, *, expected_version: int | None, **fields: Any) -> bool: ...  # pragma: no cover - protocol
    def next_sku_sequence(self, session: Session, prefix: str) -> int: ...  # pragma: no cover - protocol
    def search_products(self, session: Session, *, text: str, limit: int) -> list[ProductRecord]: ...  # pragma: no cover - protocol
    def active_products(self, session: Session, *, limit: int) -> list[ProductRecord]: ...  # pragma: no cover - protocol
    def list_products(self, session: Session, *, category_id: int | None, query: str, include_archived: bool, limit: int, offset: int) -> tuple[list[ProductRecord], int]: ...  # pragma: no cover - protocol
    def quick_picks(self, session: Session) -> list[ProductRecord]: ...  # pragma: no cover - protocol
    def barcode_owner(self, session: Session, code: str) -> BarcodeRecord | None: ...  # pragma: no cover - protocol
    def barcodes_of(self, session: Session, product_id: int) -> list[BarcodeRecord]: ...  # pragma: no cover - protocol
    def insert_barcode(self, session: Session, *, product_id: int, barcode: str, kind: str, symbology: str, pack_qty: Decimal, is_primary: bool) -> int: ...  # pragma: no cover - protocol
    def delete_barcode(self, session: Session, *, product_id: int, barcode: str) -> bool: ...  # pragma: no cover - protocol
    def categories(self, session: Session) -> list[CategoryRecord]: ...  # pragma: no cover - protocol
    def insert_category(self, session: Session, *, name: str, name_hi: str | None, parent_id: int | None, icon: str | None) -> CategoryRecord: ...  # pragma: no cover - protocol
    def update_category(self, session: Session, category_id: int, **fields: Any) -> None: ...  # pragma: no cover - protocol
    def set_tags(self, session: Session, product_id: int, names: list[str]) -> None: ...  # pragma: no cover - protocol
    def tags_of(self, session: Session, product_id: int) -> list[str]: ...  # pragma: no cover - protocol
    def insert_price_history(self, session: Session, *, product_id: int, field: str, old: int | None, new: int | None, effective_from: str, changed_by: int | None, reason: str | None, source: str) -> int: ...  # pragma: no cover - protocol


def sku_prefix(category_name: str | None) -> str:
    """``Camphor & Ghee`` → ``CAM``; no category → ``GEN``."""
    if not category_name:
        return "GEN"
    letters = re.sub(r"[^A-Za-z]", "", category_name.split("&")[0].split("(")[0]).upper()
    return (letters[:3] or "GEN").ljust(3, "X")


# ----------------------------------------------------------------------------- service
class CatalogService:
    """Products and their identities."""

    def __init__(self, txn: TxnFactory, store: CatalogStore, audit: AuditService, *, event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._bus = event_bus

    # -- reads ------------------------------------------------------------------------------------
    def get(self, product_id: int) -> ProductRecord | None:
        """Any product by id (archived included)."""
        with self._txn(label="catalog.get") as session:
            return self._store.product_by_id(session, product_id)

    def by_sku(self, sku: str) -> ProductRecord | None:
        """By code."""
        with self._txn(label="catalog.by_sku") as session:
            return self._store.product_by_sku(session, sku)

    def barcodes(self, product_id: int) -> list[BarcodeRecord]:
        """All codes of a product, primary first."""
        with self._txn(label="catalog.barcodes") as session:
            return self._store.barcodes_of(session, product_id)

    def resolve_barcode(self, code: str) -> BarcodeHit | None:
        """Scanner input → product (+ pack quantity); None when unknown or the product is archived."""
        code = bc.normalise(code)
        if not code:
            return None
        with self._txn(label="catalog.scan") as session:
            owner = self._store.barcode_owner(session, code)
            if owner is None:
                pid = bc.product_id_from_internal(code)
                product = self._store.product_by_id(session, pid) if pid else None
                if product is None or not product.is_active or product.deleted_at or product.is_system:
                    log.debug("SCAN_UNKNOWN code=%s", code)
                    return None
                # Internal code recreated from the id (label printed before the row existed) — still ours.
                return BarcodeHit(product, BarcodeRecord(0, product.id, code, "INTERNAL", "EAN13", Decimal("1"), True, product.created_at), Decimal("1"))
            product = self._store.product_by_id(session, owner.product_id)
        if product is None or not product.is_active or product.deleted_at or product.is_system:
            log.debug("SCAN_INACTIVE code=%s product=%s", code, owner.product_id)
            return None
        return BarcodeHit(product, owner, owner.pack_qty)

    def search(self, text: str, *, limit: int = 20) -> list[SearchHit]:
        """F3 Find: id / code / barcode / name (EN + HI) / fuzzy, best first."""
        q = bc.normalise(text)
        if not q:
            return []
        hits: dict[int, SearchHit] = {}
        with self._txn(label="catalog.search") as session:
            m = _ID_QUERY.match(q)
            if m:
                product = self._store.product_by_id(session, int(m.group(1)))
                if product is not None and self._sellable(product):
                    hits[product.id] = SearchHit(product, "id", 1.0)
            owner = self._store.barcode_owner(session, q)
            if owner is not None:
                product = self._store.product_by_id(session, owner.product_id)
                if product is not None and self._sellable(product):
                    hits.setdefault(product.id, SearchHit(product, "barcode", 1.0))
            by_sku = self._store.product_by_sku(session, q)
            if by_sku is not None and self._sellable(by_sku):
                hits.setdefault(by_sku.id, SearchHit(by_sku, "code", 0.99))
            for product in self._store.search_products(session, text=q, limit=limit * 2):
                matched = "name_hi" if product.name_hi and q in product.name_hi else ("code" if q.lower() in product.sku.lower() else "name")
                score = 0.9 if product.name.lower().startswith(q.lower()) else 0.8
                hits.setdefault(product.id, SearchHit(product, matched, score))
            if len(hits) < limit and len(q) >= 4 and not q.isdigit():
                for product in self._store.active_products(session, limit=5000):
                    if product.id in hits:
                        continue
                    ratio = max(difflib.SequenceMatcher(None, q.lower(), product.name.lower()).ratio(), *(difflib.SequenceMatcher(None, q.lower(), w.lower()).ratio() for w in product.name.split()))
                    if ratio >= 0.72:
                        hits[product.id] = SearchHit(product, "fuzzy", round(ratio * 0.7, 3))
        ordered = sorted(hits.values(), key=lambda h: (-h.score, h.product.name))[:limit]
        log.debug("CATALOG_SEARCH q=%r hits=%d", q, len(ordered))
        return ordered

    def list_products(self, *, category_id: int | None = None, query: str = "", include_archived: bool = False, limit: int = 100, offset: int = 0) -> ProductPage:
        """Grid page."""
        with self._txn(label="catalog.list") as session:
            items, total = self._store.list_products(session, category_id=category_id, query=query.strip(), include_archived=include_archived, limit=limit, offset=offset)
        return ProductPage(items, total, offset, limit)

    def quick_picks(self) -> list[ProductRecord]:
        """Owner POS tiles."""
        with self._txn(label="catalog.quick") as session:
            return self._store.quick_picks(session)

    def categories(self) -> list[CategoryRecord]:
        """Display order."""
        with self._txn(label="catalog.categories") as session:
            return self._store.categories(session)

    def tags_of(self, product_id: int) -> list[str]:
        """Sorted lower-case tags."""
        with self._txn(label="catalog.tags") as session:
            return self._store.tags_of(session, product_id)

    # -- writes -----------------------------------------------------------------------------------
    def create(self, actor: Actor, data: ProductInput, *, source: str = "EDITOR", reason: str | None = None) -> ProductRecord:
        """New product with code, internal barcode, optional manufacturer barcode, tags and price-history seed rows."""
        require(actor, Permission.MANAGE_PRODUCTS)
        if not data.name.strip():
            raise ValueError("name is required")
        if data.price_paise < 0:
            raise ValueError("price must be >= 0")
        if data.manufacturer_barcode:
            self._check_code(data.manufacturer_barcode)
        with self._txn(label="catalog.create") as session:
            category_name = next((c.name for c in self._store.categories(session) if c.id == data.category_id), None) if data.category_id else None
            sku = data.sku.strip().upper() if data.sku else self._next_sku(session, sku_prefix(category_name))
            other = self._store.product_by_sku(session, sku)
            if other is not None:
                raise CatalogError("E-CAT-005", name=sku, other=other.name, state="archived" if (not other.is_active or other.deleted_at) else "active", detail="duplicate code")
            fields = {
                "sku": sku, "name": data.name.strip(), "name_hi": (data.name_hi or None), "category_id": data.category_id, "unit": data.unit, "sale_mode": data.sale_mode,
                "base_unit": data.base_unit, "pricing_unit_qty": data.pricing_unit_qty, "pricing_unit_label": data.pricing_unit_label, "min_qty": data.min_qty, "qty_step": data.qty_step,
                "qty_presets": data.qty_presets, "allow_decimal_qty": data.allow_decimal_qty, "price_paise": data.price_paise, "mrp_paise": data.mrp_paise, "cost_paise": data.cost_paise,
                "tax_rate_bp": data.tax_rate_bp, "price_includes_tax": data.price_includes_tax, "hsn_code": data.hsn_code, "min_stock": data.min_stock, "reorder_qty": data.reorder_qty,
                "image_path": data.image_path, "quick_pick": data.quick_pick,
            }
            pid = self._store.insert_product(session, fields)
            internal = bc.internal_ean13(pid)
            self._store.update_product(session, pid, expected_version=None, internal_barcode=internal)
            self._store.insert_barcode(session, product_id=pid, barcode=internal, kind="INTERNAL", symbology="EAN13", pack_qty=Decimal("1"), is_primary=True)
            if data.manufacturer_barcode:
                self._add_barcode_in(session, pid, bc.normalise(data.manufacturer_barcode), "MANUFACTURER", Decimal("1"))
            if data.tags:
                self._store.set_tags(session, pid, sorted({t.strip().lower() for t in data.tags if t.strip()}))
            now = iso_utc()
            for column, hist_field in PRICE_FIELDS.items():
                value = fields[column]
                if value is not None:
                    self._store.insert_price_history(session, product_id=pid, field=hist_field, old=None, new=int(value), effective_from=now, changed_by=actor.user_id, reason=reason, source=source)
            self._audit.record(session, "PRODUCT_CREATED", user_id=actor.user_id, entity="product", entity_id=pid, after={"sku": sku, "name": fields["name"], "price_paise": data.price_paise, "source": source})
            product = self._store.product_by_id(session, pid)
        assert product is not None
        log.info("PRODUCT_CREATED id=%s sku=%s barcode=%s by=%s source=%s", pid, sku, internal, actor.user_id, source)
        self._bus.publish(ProductChanged(product_id=pid, change="created"))
        return product

    def update(self, actor: Actor, product_id: int, *, row_version: int, changes: dict[str, Any], reason: str | None = None, source: str = "EDITOR") -> ProductRecord:
        """Optimistic edit; price-like changes go to ``price_history`` with the reason."""
        require(actor, Permission.MANAGE_PRODUCTS)
        bad = set(changes) & PERMANENT_FIELDS
        if bad:
            raise ValueError(f"permanent fields cannot change: {sorted(bad)}")
        with self._txn(label="catalog.update") as session:
            before = self._store.product_by_id(session, product_id)
            if before is None:
                raise CatalogError("E-CAT-010", detail=f"product {product_id} missing")
            if before.row_version != row_version:
                log.warning("PRODUCT_STALE_EDIT id=%s expected=%s actual=%s code=E-CAT-010", product_id, row_version, before.row_version)
                raise CatalogError("E-CAT-010", detail=f"row_version {row_version} ≠ {before.row_version}")
            effective = {k: v for k, v in changes.items() if getattr(before, k, object()) != v}
            if "sku" in effective:
                sku = str(effective["sku"]).strip().upper()
                other = self._store.product_by_sku(session, sku)
                if other is not None and other.id != product_id:
                    raise CatalogError("E-CAT-005", name=sku, other=other.name, state="archived" if (not other.is_active or other.deleted_at) else "active", detail="duplicate code")
                effective["sku"] = sku
            if not effective:
                return before
            if not self._store.update_product(session, product_id, expected_version=row_version, **effective):
                raise CatalogError("E-CAT-010", detail="concurrent update")
            now = iso_utc()
            price_changed = False
            for column, hist_field in PRICE_FIELDS.items():
                if column in effective:
                    price_changed = True
                    self._store.insert_price_history(session, product_id=product_id, field=hist_field, old=getattr(before, column), new=effective[column], effective_from=now, changed_by=actor.user_id, reason=reason, source=source)
            self._audit.record(session, "PRODUCT_UPDATED", user_id=actor.user_id, entity="product", entity_id=product_id, before={k: _plain(getattr(before, k)) for k in effective}, after={k: _plain(v) for k, v in effective.items()} | ({"reason": reason} if reason else {}))
            after = self._store.product_by_id(session, product_id)
        assert after is not None
        log.info("PRODUCT_UPDATED id=%s fields=%s price_changed=%s by=%s", product_id, ",".join(sorted(effective)), price_changed, actor.user_id)
        self._bus.publish(ProductChanged(product_id=product_id, change="price" if price_changed else "updated"))
        return after

    def archive(self, actor: Actor, product_id: int) -> None:
        """Soft delete: hidden from scan, search and grids; history and reports keep it."""
        require(actor, Permission.MANAGE_PRODUCTS)
        with self._txn(label="catalog.archive") as session:
            product = self._store.product_by_id(session, product_id)
            if product is None or product.is_system:
                raise CatalogError("E-CAT-010", detail="cannot archive")
            self._store.update_product(session, product_id, expected_version=None, is_active=False, deleted_at=iso_utc())
            self._audit.record(session, "PRODUCT_ARCHIVED", user_id=actor.user_id, entity="product", entity_id=product_id, after={"sku": product.sku})
        log.info("PRODUCT_ARCHIVED id=%s by=%s", product_id, actor.user_id)
        self._bus.publish(ProductChanged(product_id=product_id, change="archived"))

    def restore(self, actor: Actor, product_id: int) -> None:
        """Undo :meth:`archive`."""
        require(actor, Permission.MANAGE_PRODUCTS)
        with self._txn(label="catalog.restore") as session:
            self._store.update_product(session, product_id, expected_version=None, is_active=True, deleted_at=None)
            self._audit.record(session, "PRODUCT_RESTORED", user_id=actor.user_id, entity="product", entity_id=product_id)
        self._bus.publish(ProductChanged(product_id=product_id, change="updated"))

    def add_barcode(self, actor: Actor, product_id: int, code: str, *, kind: str = "MANUFACTURER", pack_qty: Decimal = Decimal("1")) -> BarcodeRecord:
        """Attach a scanned manufacturer / pack code."""
        require(actor, Permission.MANAGE_PRODUCTS)
        code = bc.normalise(code)
        self._check_code(code)
        if kind not in ("MANUFACTURER", "PACK"):
            raise ValueError("kind must be MANUFACTURER or PACK")
        with self._txn(label="catalog.add_barcode") as session:
            record = self._add_barcode_in(session, product_id, code, kind, pack_qty)
            self._audit.record(session, "BARCODE_ADDED", user_id=actor.user_id, entity="product", entity_id=product_id, after={"barcode": code, "kind": kind, "pack_qty": str(pack_qty)})
        self._bus.publish(ProductChanged(product_id=product_id, change="barcode"))
        return record

    def remove_barcode(self, actor: Actor, product_id: int, code: str) -> None:
        """Detach a manufacturer / pack code (the internal one is permanent)."""
        require(actor, Permission.MANAGE_PRODUCTS)
        code = bc.normalise(code)
        with self._txn(label="catalog.remove_barcode") as session:
            owner = self._store.barcode_owner(session, code)
            if owner is None or owner.product_id != product_id:
                raise CatalogError("E-CAT-020", code=code, detail="not attached to this product")
            if owner.kind == "INTERNAL":
                raise CatalogError("E-CAT-020", code=code, detail="internal barcode is permanent")
            self._store.delete_barcode(session, product_id=product_id, barcode=code)
            self._audit.record(session, "BARCODE_REMOVED", user_id=actor.user_id, entity="product", entity_id=product_id, after={"barcode": code})
        self._bus.publish(ProductChanged(product_id=product_id, change="barcode"))

    def set_tags(self, actor: Actor, product_id: int, tags: list[str]) -> None:
        """Replace tags."""
        require(actor, Permission.MANAGE_PRODUCTS)
        clean = sorted({t.strip().lower() for t in tags if t.strip()})
        with self._txn(label="catalog.tags") as session:
            self._store.set_tags(session, product_id, clean)

    def set_quick_pick(self, actor: Actor, product_id: int, on: bool) -> None:
        """Show / hide as a POS tile."""
        require(actor, Permission.MANAGE_PRODUCTS)
        with self._txn(label="catalog.quick") as session:
            self._store.update_product(session, product_id, expected_version=None, quick_pick=on)

    def create_category(self, actor: Actor, *, name: str, name_hi: str | None = None, parent_id: int | None = None, icon: str | None = None) -> CategoryRecord:
        """New category at the end of the order."""
        require(actor, Permission.MANAGE_PRODUCTS)
        if not name.strip():
            raise ValueError("name is required")
        with self._txn(label="catalog.category") as session:
            cat = self._store.insert_category(session, name=name.strip(), name_hi=(name_hi or None), parent_id=parent_id, icon=icon)
            self._audit.record(session, "CATEGORY_CREATED", user_id=actor.user_id, entity="category", entity_id=cat.id, after={"name": cat.name})
        return cat

    def update_category(self, actor: Actor, category_id: int, **fields: Any) -> None:
        """Rename / reorder."""
        require(actor, Permission.MANAGE_PRODUCTS)
        with self._txn(label="catalog.category_update") as session:
            self._store.update_category(session, category_id, **fields)
            self._audit.record(session, "CATEGORY_UPDATED", user_id=actor.user_id, entity="category", entity_id=category_id, after={k: _plain(v) for k, v in fields.items()})

    # -- internals --------------------------------------------------------------------------------
    @staticmethod
    def _sellable(product: ProductRecord) -> bool:
        return product.is_active and not product.deleted_at and not product.is_system

    def _next_sku(self, session: Session, prefix: str) -> str:
        return f"{prefix}-{self._store.next_sku_sequence(session, prefix):04d}"

    @staticmethod
    def _check_code(code: str) -> None:
        problem = bc.validate(code)
        if problem:
            log.info("BARCODE_REJECTED code=%s reason=%s code_id=E-CAT-020", code, problem)
            raise CatalogError("E-CAT-020", code=code, detail=problem)

    def _add_barcode_in(self, session: Session, product_id: int, code: str, kind: str, pack_qty: Decimal) -> BarcodeRecord:
        owner = self._store.barcode_owner(session, code)
        if owner is not None:
            other = self._store.product_by_id(session, owner.product_id)
            log.warning("BARCODE_DUPLICATE code=%s owner=%s code_id=E-CAT-004", code, owner.product_id)
            raise CatalogError("E-CAT-004", name=other.name if other else str(owner.product_id), detail=f"owned by product {owner.product_id}")
        bid = self._store.insert_barcode(session, product_id=product_id, barcode=code, kind=kind, symbology=bc.detect_symbology(code), pack_qty=pack_qty, is_primary=False)
        log.info("BARCODE_ADDED id=%s product=%s code=%s kind=%s pack=%s", bid, product_id, code, kind, pack_qty)
        return BarcodeRecord(bid, product_id, code, kind, bc.detect_symbology(code), pack_qty, False, iso_utc())


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value
