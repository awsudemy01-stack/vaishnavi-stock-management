"""InventoryService — stock as a ledger of movements (ADR-004; stdlib).

Stock is never edited: the Owner records **movements** (OPENING · PURCHASE · ADJUSTMENT · DAMAGE ·
STOCK_TAKE; SALE/SALE_VOID/RETURN belong to billing) and the database trigger keeps ``product_stock``
equal to the running balance (``E-INV-013`` if ever not). Staff cannot move stock here at all
(``STOCK_MOVEMENTS`` is Owner-only — the trigger enforces it too, ``E-INV-010``). Negative stock is
refused (policy BLOCK, ``E-INV-003``). Every movement is audited and published as ``StockChanged`` with
``low_stock`` when the balance crosses the product's minimum.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, today_ist
from vaishnavi.core.errors import InventoryError
from vaishnavi.core.events import EventBus, StockChanged, bus
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.db.records import MovementRecord, ProductRecord, PurchaseRecord, StockCountLineRecord, StockCountRecord, SupplierRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.inventory")

OWNER_MOVEMENTS: Final = ("OPENING", "PURCHASE", "ADJUSTMENT", "DAMAGE", "STOCK_TAKE")


@dataclass(frozen=True)
class PurchaseLine:
    """One received line."""

    product_id: int
    qty: Decimal
    unit_cost_paise: int
    expiry_date: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    """Σ movements vs cached stock."""

    ok: bool
    products_checked: int
    mismatches: list[tuple[int, Decimal, Decimal]]
    """``(product_id, Σ movements, cached)``."""


@dataclass(frozen=True)
class StockTakeSummary:
    """Outcome of an approval."""

    count_id: int
    lines: int
    movements: int
    variance_value_paise: int


class InventoryStore(Protocol):
    """What the service needs."""

    def stock_of(self, session: Session, product_id: int) -> Decimal: ...  # pragma: no cover - protocol
    def insert_movement(self, session: Session, *, product_id: int, type: str, qty_delta: Decimal, qty_after: Decimal, unit_cost_paise: int | None, ref_type: str | None, ref_id: int | None, reason: str | None, user_id: int, created_at: str) -> int: ...  # noqa: A002  # pragma: no cover - protocol
    def movements(self, session: Session, *, product_id: int | None, types: tuple[str, ...] | None, since: str | None, until: str | None, limit: int) -> list[MovementRecord]: ...  # pragma: no cover - protocol
    def has_movements(self, session: Session, product_id: int) -> bool: ...  # pragma: no cover - protocol
    def movement_sums(self, session: Session) -> list[tuple[int, Decimal, Decimal]]: ...  # pragma: no cover - protocol
    def stock_levels(self, session: Session) -> list[tuple[int, Decimal, Decimal, int, int | None, str]]: ...  # pragma: no cover - protocol
    def upsert_daily_balance(self, session: Session, *, day: str, product_id: int, qty_close: Decimal, value_cost_paise: int) -> None: ...  # pragma: no cover - protocol
    def suppliers(self, session: Session) -> list[SupplierRecord]: ...  # pragma: no cover - protocol
    def supplier_by_name(self, session: Session, name: str) -> SupplierRecord | None: ...  # pragma: no cover - protocol
    def insert_supplier(self, session: Session, *, name: str, phone: str | None, gstin: str | None) -> SupplierRecord: ...  # pragma: no cover - protocol
    def insert_purchase(self, session: Session, *, supplier_id: int | None, invoice_no: str | None, purchase_date: str, total_paise: int, user_id: int, created_at: str) -> int: ...  # pragma: no cover - protocol
    def insert_purchase_item(self, session: Session, *, purchase_id: int, product_id: int, qty: Decimal, unit_cost_paise: int, expiry_date: str | None) -> int: ...  # pragma: no cover - protocol
    def purchases(self, session: Session, *, limit: int) -> list[PurchaseRecord]: ...  # pragma: no cover - protocol
    def insert_stock_count(self, session: Session, *, started_at: str, user_id: int, scope: str) -> int: ...  # pragma: no cover - protocol
    def stock_count(self, session: Session, count_id: int) -> StockCountRecord | None: ...  # pragma: no cover - protocol
    def update_stock_count(self, session: Session, count_id: int, **fields: Any) -> None: ...  # pragma: no cover - protocol
    def insert_count_line(self, session: Session, *, count_id: int, product_id: int, system_qty: Decimal) -> int: ...  # pragma: no cover - protocol
    def count_lines(self, session: Session, count_id: int) -> list[StockCountLineRecord]: ...  # pragma: no cover - protocol
    def set_counted(self, session: Session, *, count_id: int, product_id: int, counted_qty: Decimal, variance: Decimal, variance_value_paise: int) -> bool: ...  # pragma: no cover - protocol


class ProductLookup(Protocol):
    """The slice of the catalogue store the inventory needs."""

    def product_by_id(self, session: Session, product_id: int) -> ProductRecord | None: ...  # pragma: no cover - protocol
    def update_product(self, session: Session, product_id: int, *, expected_version: int | None, **fields: Any) -> bool: ...  # pragma: no cover - protocol
    def insert_price_history(self, session: Session, *, product_id: int, field: str, old: int | None, new: int | None, effective_from: str, changed_by: int | None, reason: str | None, source: str) -> int: ...  # pragma: no cover - protocol


class InventoryService:
    """Owner-side stock operations."""

    def __init__(self, txn: TxnFactory, store: InventoryStore, products: ProductLookup, audit: AuditService, *, event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._products = products
        self._audit = audit
        self._bus = event_bus

    # -- reads ------------------------------------------------------------------------------------
    def stock_of(self, product_id: int) -> Decimal:
        """Current quantity in base units."""
        with self._txn(label="inv.stock") as session:
            return self._store.stock_of(session, product_id)

    def movements(self, product_id: int | None = None, *, types: tuple[str, ...] | None = None, since: str | None = None, until: str | None = None, limit: int = 200) -> list[MovementRecord]:
        """Timeline, newest first."""
        with self._txn(label="inv.movements") as session:
            return self._store.movements(session, product_id=product_id, types=types, since=since, until=until, limit=limit)

    def low_stock_products(self) -> list[ProductRecord]:
        """Active products at or below their minimum (minimum > 0)."""
        out: list[ProductRecord] = []
        with self._txn(label="inv.low") as session:
            for pid, qty, min_stock, _price, _cost, _name in self._store.stock_levels(session):
                if min_stock > 0 and qty <= min_stock:
                    product = self._products.product_by_id(session, pid)
                    if product is not None:
                        out.append(product)
        return out

    def valuation_paise(self) -> int:
        """Stock value: cost when known, else the selling price."""
        total = 0
        with self._txn(label="inv.valuation") as session:
            for _pid, qty, _min, price, cost, _name in self._store.stock_levels(session):
                total += int(qty * (cost if cost is not None else price))
        return total

    def reconcile(self) -> ReconcileReport:
        """Σ movements must equal the cached figure for every product (the trigger makes this a tautology — unless the DB was edited by hand)."""
        with self._txn(label="inv.reconcile") as session:
            sums = self._store.movement_sums(session)
        mismatches = [(pid, total, cached) for pid, total, cached in sums if total != cached]
        if mismatches:
            log.error("STOCK_RECONCILE_MISMATCH count=%d first=%s code=E-INV-013", len(mismatches), mismatches[0])
        else:
            log.debug("STOCK_RECONCILE_OK products=%d", len(sums))
        return ReconcileReport(not mismatches, len(sums), mismatches)

    def suppliers(self) -> list[SupplierRecord]:
        """Alphabetical."""
        with self._txn(label="inv.suppliers") as session:
            return self._store.suppliers(session)

    def purchases(self, *, limit: int = 100) -> list[PurchaseRecord]:
        """Newest first."""
        with self._txn(label="inv.purchases") as session:
            return self._store.purchases(session, limit=limit)

    # -- movements --------------------------------------------------------------------------------
    def opening_stock(self, actor: Actor, product_id: int, qty: Decimal) -> MovementRecord | None:
        """The first count of a new product; allowed once (afterwards use adjust / stock-take). None when already set."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if qty <= 0:
            raise ValueError("opening stock must be > 0")
        with self._txn(label="inv.opening") as session:
            if self._store.has_movements(session, product_id):
                log.info("OPENING_STOCK_SKIPPED product=%s reason=already_moved", product_id)
                return None
            record, event = self._move(session, actor, product_id, "OPENING", qty, reason="opening stock", unit_cost=None, ref=(None, None), audit_action="STOCK_OPENING")
        self._bus.publish(event)
        return record

    def adjust(self, actor: Actor, product_id: int, qty_delta: Decimal, *, reason: str) -> MovementRecord:
        """Owner correction with a mandatory reason (either sign)."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if not reason or not reason.strip():
            raise ValueError("a reason is required for a stock adjustment")
        if qty_delta == 0:
            raise ValueError("adjustment must not be zero")
        with self._txn(label="inv.adjust") as session:
            record, event = self._move(session, actor, product_id, "ADJUSTMENT", qty_delta, reason=reason.strip(), unit_cost=None, ref=(None, None), audit_action="STOCK_ADJUSTED")
        self._bus.publish(event)
        return record

    def record_damage(self, actor: Actor, product_id: int, qty: Decimal, *, reason: str) -> MovementRecord:
        """Broken / spoiled items leave stock as DAMAGE (shrinkage report)."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if qty <= 0:
            raise ValueError("damage quantity must be > 0")
        if not reason or not reason.strip():
            raise ValueError("a reason is required")
        with self._txn(label="inv.damage") as session:
            record, event = self._move(session, actor, product_id, "DAMAGE", -qty, reason=reason.strip(), unit_cost=None, ref=(None, None), audit_action="STOCK_DAMAGE")
        self._bus.publish(event)
        return record

    def receive_goods(self, actor: Actor, product_id: int, qty: Decimal, *, note: str) -> MovementRecord:
        """A parcel arrived at the counter: add stock with a mandatory note — the one stock movement staff may make.

        Add only (the DB trigger refuses anything else from a non-Owner), so a dishonest count cannot hide a
        theft: stock can only go *up* here, and every receipt is audited (``STOCK_RECEIVED`` with who/when/what)
        for the Owner to review in Reports → Audit. No cost is recorded — the Owner books the supplier bill
        later with :meth:`record_purchase`, which also updates the purchase price.
        """
        require(actor, Permission.RECEIVE_STOCK)
        if qty <= 0:
            raise ValueError("received quantity must be > 0")
        if not note or len(note.strip()) < 3:
            raise ValueError("a note is required (who delivered / which bill)")
        with self._txn(label="inv.receive") as session:
            record, event = self._move(session, actor, product_id, "PURCHASE", qty, reason=f"received at counter: {note.strip()}", unit_cost=None, ref=(None, None), audit_action="STOCK_RECEIVED")
        self._bus.publish(event)
        log.info("STOCK_RECEIVED product=%s qty=%s by=%s role=%s", product_id, qty, actor.user_id, actor.role.value)
        return record

    # -- purchases --------------------------------------------------------------------------------
    def record_purchase(self, actor: Actor, *, supplier_name: str | None, invoice_no: str | None, lines: list[PurchaseLine], purchase_date: str | None = None, supplier_phone: str | None = None, supplier_gstin: str | None = None) -> PurchaseRecord:
        """Goods received: header + lines, PURCHASE movements, the product's cost follows the latest price (price history ``PURCHASE``)."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if not lines:
            raise ValueError("a purchase needs at least one line")
        for line in lines:
            if line.qty <= 0 or line.unit_cost_paise < 0:
                raise ValueError(f"bad purchase line for product {line.product_id}")
        now = iso_utc()
        events: list[StockChanged] = []
        with self._txn(label="inv.purchase") as session:
            supplier_id: int | None = None
            if supplier_name and supplier_name.strip():
                supplier = self._store.supplier_by_name(session, supplier_name.strip()) or self._store.insert_supplier(session, name=supplier_name.strip(), phone=supplier_phone, gstin=supplier_gstin)
                supplier_id = supplier.id
            total = sum(int(line.qty * line.unit_cost_paise) for line in lines)
            pid = self._store.insert_purchase(session, supplier_id=supplier_id, invoice_no=invoice_no, purchase_date=purchase_date or today_ist().isoformat(), total_paise=total, user_id=actor.user_id, created_at=now)
            labels = 0
            for line in lines:
                self._store.insert_purchase_item(session, purchase_id=pid, product_id=line.product_id, qty=line.qty, unit_cost_paise=line.unit_cost_paise, expiry_date=line.expiry_date)
                _, event = self._move(session, actor, line.product_id, "PURCHASE", line.qty, reason=f"purchase {invoice_no or pid}", unit_cost=line.unit_cost_paise, ref=("PURCHASE", pid), audit_action=None)
                events.append(event)
                product = self._products.product_by_id(session, line.product_id)
                if product is not None and product.cost_paise != line.unit_cost_paise:
                    self._products.update_product(session, line.product_id, expected_version=None, cost_paise=line.unit_cost_paise)
                    self._products.insert_price_history(session, product_id=line.product_id, field="COST", old=product.cost_paise, new=line.unit_cost_paise, effective_from=now, changed_by=actor.user_id, reason=f"purchase {invoice_no or pid}", source="PURCHASE")
                if product is not None and product.sale_mode == "PIECE":
                    labels += int(line.qty)
            self._audit.record(session, "PURCHASE_RECORDED", user_id=actor.user_id, entity="purchase", entity_id=pid, after={"supplier": supplier_name, "invoice_no": invoice_no, "lines": len(lines), "total_paise": total})
        for event in events:
            self._bus.publish(event)
        log.info("PURCHASE_RECORDED id=%s supplier=%s lines=%d total_paise=%d by=%s", pid, supplier_name, len(lines), total, actor.user_id)
        return PurchaseRecord(pid, supplier_id, invoice_no, purchase_date or today_ist().isoformat(), total, actor.user_id, now, label_count=labels)

    # -- stock-take -------------------------------------------------------------------------------
    def start_stock_take(self, actor: Actor, *, scope: str = "FULL", product_ids: list[int] | None = None, category_id: int | None = None) -> StockCountRecord:
        """Freeze the system quantities into a DRAFT count."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if scope not in ("FULL", "CATEGORY", "SELECTED"):
            raise ValueError("scope must be FULL, CATEGORY or SELECTED")
        now = iso_utc()
        with self._txn(label="inv.count_start") as session:
            count_id = self._store.insert_stock_count(session, started_at=now, user_id=actor.user_id, scope=scope)
            levels = self._store.stock_levels(session)
            chosen: list[tuple[int, Decimal]] = []
            for pid, qty, _min, _price, _cost, _name in levels:
                if scope == "SELECTED" and pid not in (product_ids or []):
                    continue
                if scope == "CATEGORY":
                    product = self._products.product_by_id(session, pid)
                    if product is None or product.category_id != category_id:
                        continue
                chosen.append((pid, qty))
            for pid, qty in chosen:
                self._store.insert_count_line(session, count_id=count_id, product_id=pid, system_qty=qty)
            self._audit.record(session, "STOCK_TAKE_STARTED", user_id=actor.user_id, entity="stock_count", entity_id=count_id, after={"scope": scope, "lines": len(chosen)})
            record = self._store.stock_count(session, count_id)
        assert record is not None
        log.info("STOCK_TAKE_STARTED id=%s scope=%s lines=%d", count_id, scope, len(chosen))
        return record

    def stock_take_lines(self, count_id: int) -> list[StockCountLineRecord]:
        """Lines with variances where counted."""
        with self._txn(label="inv.count_lines") as session:
            return self._store.count_lines(session, count_id)

    def stock_take(self, count_id: int) -> StockCountRecord | None:
        """Header."""
        with self._txn(label="inv.count") as session:
            return self._store.stock_count(session, count_id)

    def record_count(self, actor: Actor, count_id: int, product_id: int, counted_qty: Decimal) -> StockCountLineRecord:
        """Enter the physical count for one line."""
        require(actor, Permission.STOCK_MOVEMENTS)
        if counted_qty < 0:
            raise ValueError("counted quantity must be >= 0")
        with self._txn(label="inv.count_line") as session:
            count = self._require_draft(session, count_id)
            line = next((l for l in self._store.count_lines(session, count.id) if l.product_id == product_id), None)
            if line is None:
                raise InventoryError("E-INV-011", detail=f"product {product_id} not in count {count_id}")
            product = self._products.product_by_id(session, product_id)
            unit_value = (product.cost_paise if product and product.cost_paise is not None else (product.price_paise if product else 0))
            variance = counted_qty - line.system_qty
            self._store.set_counted(session, count_id=count_id, product_id=product_id, counted_qty=counted_qty, variance=variance, variance_value_paise=int(variance * unit_value))
            updated = next(l for l in self._store.count_lines(session, count.id) if l.product_id == product_id)
        return updated

    def approve_stock_take(self, actor: Actor, count_id: int) -> StockTakeSummary:
        """Apply the variances as STOCK_TAKE movements and close the count."""
        require(actor, Permission.STOCK_MOVEMENTS)
        events: list[StockChanged] = []
        with self._txn(label="inv.count_approve") as session:
            count = self._require_draft(session, count_id)
            lines = self._store.count_lines(session, count.id)
            moved = 0
            value = 0
            for line in lines:
                if line.counted_qty is None or line.variance is None or line.variance == 0:
                    continue
                _, event = self._move(session, actor, line.product_id, "STOCK_TAKE", line.variance, reason=f"stock-take #{count_id}", unit_cost=None, ref=("STOCK_COUNT", count_id), audit_action=None)
                events.append(event)
                moved += 1
                value += line.variance_value_paise or 0
            now = iso_utc()
            self._store.update_stock_count(session, count_id, status="APPROVED", approved_at=now, approved_by_user_id=actor.user_id)
            self._audit.record(session, "STOCK_TAKE_APPROVED", user_id=actor.user_id, entity="stock_count", entity_id=count_id, after={"lines": len(lines), "movements": moved, "variance_value_paise": value})
        for event in events:
            self._bus.publish(event)
        log.info("STOCK_TAKE_APPROVED id=%s movements=%d variance_value_paise=%d by=%s", count_id, moved, value, actor.user_id)
        return StockTakeSummary(count_id, len(lines), moved, value)

    def cancel_stock_take(self, actor: Actor, count_id: int) -> None:
        """Discard a draft."""
        require(actor, Permission.STOCK_MOVEMENTS)
        with self._txn(label="inv.count_cancel") as session:
            self._require_draft(session, count_id)
            self._store.update_stock_count(session, count_id, status="CANCELLED")
            self._audit.record(session, "STOCK_TAKE_CANCELLED", user_id=actor.user_id, entity="stock_count", entity_id=count_id)

    # -- jobs -------------------------------------------------------------------------------------
    def write_daily_balance(self, day: str | None = None) -> int:
        """End-of-day closing quantity per product (idempotent per day)."""
        day = day or today_ist().isoformat()
        with self._txn(label="inv.daily_balance") as session:
            levels = self._store.stock_levels(session)
            for pid, qty, _min, price, cost, _name in levels:
                self._store.upsert_daily_balance(session, day=day, product_id=pid, qty_close=qty, value_cost_paise=int(qty * (cost if cost is not None else price)))
        log.info("STOCK_DAILY_BALANCE day=%s products=%d", day, len(levels))
        return len(levels)

    # -- internals --------------------------------------------------------------------------------
    def _move(self, session: Session, actor: Actor, product_id: int, type_: str, qty_delta: Decimal, *, reason: str | None, unit_cost: int | None, ref: tuple[str | None, int | None], audit_action: str | None) -> tuple[MovementRecord, StockChanged]:
        product = self._products.product_by_id(session, product_id)
        if product is None or product.is_system:
            raise InventoryError("E-INV-011", detail=f"product {product_id} cannot move stock")
        before = self._store.stock_of(session, product_id)
        after = before + qty_delta
        if after < 0:
            log.warning("negative_stock policy=BLOCK product=%s have=%s want=%s → E-INV-003", product.sku, before, -qty_delta)
            raise InventoryError("E-INV-003", name=product.name, available=str(before), requested=str(-qty_delta))
        now = iso_utc()
        mid = self._store.insert_movement(session, product_id=product_id, type=type_, qty_delta=qty_delta, qty_after=after, unit_cost_paise=unit_cost, ref_type=ref[0], ref_id=ref[1], reason=reason, user_id=actor.user_id, created_at=now)
        if audit_action:
            self._audit.record(session, audit_action, user_id=actor.user_id, entity="product", entity_id=product_id, before={"qty": str(before)}, after={"qty": str(after), "delta": str(qty_delta), "reason": reason})
        low = product.min_stock > 0 and after <= product.min_stock and before > product.min_stock
        log.info("STOCK_MOVED id=%s product=%s type=%s delta=%s after=%s low=%s by=%s", mid, product.sku, type_, qty_delta, after, low, actor.user_id)
        record = MovementRecord(mid, product_id, type_, qty_delta, after, unit_cost, ref[0], ref[1], reason, actor.user_id, now, None)
        event = StockChanged(product_id=product_id, movement_type=type_, qty_delta=int(qty_delta), qty_after=int(after), low_stock=low, product_name=product.name, user_id=actor.user_id, by_staff=actor.role is not Role.OWNER)
        return record, event

    def _require_draft(self, session: Session, count_id: int) -> StockCountRecord:
        count = self._store.stock_count(session, count_id)
        if count is None:
            raise InventoryError("E-INV-011", detail=f"stock count {count_id} missing")
        if count.status != "DRAFT":
            raise InventoryError("E-INV-011", detail=f"stock count {count_id} is {count.status}")
        return count
