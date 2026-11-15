"""PurchaseOrderService — the reorder plan grouped by last supplier, as a document the owner can forward.

Lines come from ``ForecastService.plan`` (quantities already rounded to packs); the supplier is the one
on the product's most recent ``PURCHASE`` movement (else *Unassigned*); the estimated cost uses the last
purchase unit cost when known. Rendering goes through the injected publisher (services never import
``publishing``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from vaishnavi.core.clock import format_ist, now_utc
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Permission, require

log = logging.getLogger("vaishnavi.services.purchase_order")


def _q(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(Decimal(value).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class PoLine:
    """One item to order."""

    product_id: int
    name: str
    sku: str
    qty: Decimal
    stock: Decimal
    days_of_cover: Decimal | None
    unit_cost_paise: int | None
    classification: str

    @property
    def qty_text(self) -> str:
        """Quantity as the owner reads it."""
        return _q(self.qty)

    @property
    def est_cost_paise(self) -> int | None:
        """Quantity × last unit cost (None when the cost is unknown)."""
        return int(self.qty * self.unit_cost_paise) if self.unit_cost_paise is not None else None


@dataclass(frozen=True)
class PoGroup:
    """Lines for one supplier."""

    supplier: str
    phone: str
    lines: list[PoLine] = field(default_factory=list)

    @property
    def est_cost_paise(self) -> int:
        """Known costs summed."""
        return sum(line.est_cost_paise or 0 for line in self.lines)


@dataclass(frozen=True)
class PurchaseOrder:
    """The whole order."""

    generated_at: str
    groups: list[PoGroup]

    @property
    def total_lines(self) -> int:
        """Items across suppliers."""
        return sum(len(g.lines) for g in self.groups)

    def rows(self) -> list[tuple[str, str, str, str, str, str, str]]:
        """``(supplier, item, code, stock, cover, order qty, est. cost)`` for tables and exports."""
        out = []
        for g in self.groups:
            for line in g.lines:
                out.append((g.supplier, line.name, line.sku, _q(line.stock), _q(line.days_of_cover), line.qty_text, format_inr(line.est_cost_paise) if line.est_cost_paise is not None else ""))
        return out


class PurchaseOrderService:
    """Reorder plan → purchase order."""

    def __init__(self, forecast: Any, catalog: Any, inventory: Any, *, publisher: Any) -> None:
        self._forecast = forecast
        self._catalog = catalog
        self._inventory = inventory
        self._publisher = publisher

    def _last_supplier(self) -> tuple[dict[int, tuple[str, str]], dict[int, int]]:
        """product → (supplier name, phone); product → last unit cost paise, from PURCHASE movements."""
        suppliers = {s.id: s for s in self._inventory.suppliers()}
        purchases = {p.id: p for p in self._inventory.purchases(limit=2000)}
        supplier_of: dict[int, tuple[str, str]] = {}
        cost_of: dict[int, int] = {}
        for m in self._inventory.movements(types=("PURCHASE",), limit=5000):
            if m.product_id in supplier_of:
                continue  # movements are newest first
            purchase = purchases.get(m.ref_id) if m.ref_type == "PURCHASE" and m.ref_id is not None else None
            sup = suppliers.get(purchase.supplier_id) if purchase and purchase.supplier_id is not None else None
            supplier_of[m.product_id] = (sup.name, sup.phone or "") if sup else ("Unassigned", "")
            if m.unit_cost_paise is not None:
                cost_of[m.product_id] = int(m.unit_cost_paise)
        return supplier_of, cost_of

    def build(self, actor: Actor) -> PurchaseOrder:
        """Group the reorder plan by supplier (Owner)."""
        require(actor, Permission.REPORT_FORECAST)
        plan = [r for r in self._forecast.plan(actor) if r.reorder_qty > 0]
        supplier_of, cost_of = self._last_supplier()
        groups: dict[str, PoGroup] = {}
        for r in plan:
            product = self._catalog.get(r.product_id)
            name, phone = supplier_of.get(r.product_id, ("Unassigned", ""))
            cost = cost_of.get(r.product_id, product.cost_paise if product and product.cost_paise else None)
            group = groups.setdefault(name, PoGroup(name, phone))
            group.lines.append(PoLine(r.product_id, r.name, product.sku if product else "", r.reorder_qty, r.stock, r.days_of_cover, cost, r.classification))
        ordered = sorted(groups.values(), key=lambda g: (g.supplier == "Unassigned", g.supplier.lower()))
        po = PurchaseOrder(format_ist(now_utc(), "%d-%b-%Y %H:%M"), ordered)
        log.info("PURCHASE_ORDER_BUILT suppliers=%d lines=%d", len(ordered), po.total_lines)
        return po

    def document(self, actor: Actor) -> Path:
        """Write the PO as PDF/HTML under the reports folder (Owner); returns the path."""
        po = self.build(actor)
        headers = ["Supplier", "Item", "Code", "Stock", "Days of cover", "Order qty", "Est. cost"]
        rows = [list(r) for r in po.rows()]
        total = sum(g.est_cost_paise for g in po.groups)
        rows.append(["", "", "", "", "", "TOTAL (known costs)", format_inr(total)])
        return self._publisher.render_document("Purchase order", f"{po.generated_at} · {po.total_lines} items · {len(po.groups)} suppliers", headers, rows, stem=f"purchase-order-{now_utc().strftime('%Y%m%d-%H%M%S')}")

    def send(self, actor: Actor) -> Any:
        """PO → publishing channels (the owner forwards it to suppliers from the phone)."""
        po = self.build(actor)
        headers = ["Supplier", "Item", "Code", "Stock", "Days of cover", "Order qty", "Est. cost"]
        return self._publisher.publish_table("Purchase order", f"{po.generated_at} · {po.total_lines} items", headers, [list(r) for r in po.rows()], stem=f"purchase-order-{now_utc().strftime('%Y%m%d-%H%M%S')}")
