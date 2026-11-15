"""ReportService — the Owner's anti-theft and control reports (docs/05 §9; stdlib, pandas-free SQL → dataclasses).

Phase 4 ships the **control** reports: discount audit, manual lines (lookup / open items per cashier),
voids & refunds, PIN / hardware overrides, shrinkage, cashier performance (bills · average bill ·
discounts · voids · manual lines · drawer variance), tax summary by rate, the integrity picture and the
audit trail. Every report is Owner-only (``REPORT_AUDIT`` / ``REPORT_SALES``); figures come straight from
the ledger tables so they cannot disagree with the receipts. ``to_csv`` turns any list of report rows
into an Excel-friendly CSV (UTF-8 BOM, dataclass field names as the header). Sales / profit / forecast
reports arrive in Phase 5 on the same service.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from typing import Any, Protocol

from vaishnavi.core.clock import business_day_bounds, iso_utc, parse_iso_utc, to_ist, today_ist
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import AuditEntry
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.services.report")

OVERRIDE_ACTIONS = ("OVERRIDE", "HARDWARE_OVERRIDE", "HARDWARE_OVERRIDE_ENDED", "DISCOUNT_OVERRIDE", "SALE_VOIDED", "REFUND", "CASH_OUT", "REPRINT")
IST_OFFSET_MINUTES = 330
"""SQLite groups UTC timestamps into shop days by shifting +05:30."""


# ----------------------------------------------------------------------------- rows
@dataclass(frozen=True)
class DiscountRow:
    """Ad-hoc discounts per cashier."""

    user_id: int
    lines: int
    bills: int
    adhoc_paise: int
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OfferUsageRow:
    """Named offer usage."""

    offer_id: int
    name: str
    lines: int
    given_paise: int


@dataclass(frozen=True)
class ManualLinesRow:
    """Lookup / open-item lines per cashier."""

    user_id: int
    total_lines: int
    lookup_lines: int
    open_item_lines: int
    open_item_paise: int

    @property
    def manual_ratio(self) -> float:
        """Share of lines that did not come from a scan."""
        return (self.lookup_lines + self.open_item_lines) / self.total_lines if self.total_lines else 0.0


@dataclass(frozen=True)
class VoidRow:
    """A credit note."""

    credit_note: str
    original_invoice: str
    status: str
    at: str
    user_id: int
    approved_by: int | None
    total_paise: int
    reason: str


@dataclass(frozen=True)
class OverrideRow:
    """Any Owner intervention on a staff screen (or an Owner override of a rule)."""

    at: str
    action: str
    user_id: int | None
    actor_user_id: int | None
    entity: str | None
    entity_id: int | None
    detail: str


@dataclass(frozen=True)
class ShrinkageRow:
    """Stock that left without a sale."""

    product_id: int
    name: str
    damage_qty: Decimal
    adjustment_qty: Decimal
    stock_take_qty: Decimal
    value_paise: int


@dataclass(frozen=True)
class CashierPerformanceRow:
    """Per cashier."""

    user_id: int
    bills: int
    sales_paise: int
    avg_bill_paise: int
    discount_paise: int
    voids: int
    manual_lines: int
    shifts_closed: int
    variance_paise: int


@dataclass(frozen=True)
class TaxRow:
    """GSTR-1 friendly totals by rate."""

    rate_bp: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    bills: int


@dataclass(frozen=True)
class IntegrityCheckRow:
    """One check of the integrity picture."""

    check: str
    ok: bool
    detail: str
    code: str


@dataclass(frozen=True)
class IntegrityView:
    """Integrity status for the report page / banner."""

    ok: bool
    at: str
    chain_head: str
    checks: list[IntegrityCheckRow]
    codes: list[str]


@dataclass(frozen=True)
class DaySalesRow:
    """One shop day."""

    day: str
    bills: int
    items: Decimal
    subtotal_paise: int
    discount_paise: int
    tax_paise: int
    total_paise: int
    cash_paise: int
    upi_paise: int
    card_paise: int
    refunds_paise: int


@dataclass(frozen=True)
class SalesSummary:
    """Period totals."""

    bills: int
    items: Decimal
    total_paise: int
    refunds_paise: int
    net_paise: int
    avg_bill_paise: int
    discount_paise: int
    tax_paise: int
    by_mode: dict[str, int]


@dataclass(frozen=True)
class ItemDayRow:
    """One product on one day."""

    day: str
    qty: Decimal
    amount_paise: int
    bills: int


@dataclass(frozen=True)
class TopSellerRow:
    """Ranked product."""

    product_id: int
    name: str
    qty: Decimal
    amount_paise: int
    share_pct: int


@dataclass(frozen=True)
class BasketPairRow:
    """Two products that sell together."""

    product_a: int
    name_a: str
    product_b: int
    name_b: str
    baskets: int
    share_pct: int
    """Share of all completed bills in the window that held both."""


@dataclass(frozen=True)
class DeadStockRow:
    """In stock, not selling."""

    product_id: int
    name: str
    stock_qty: Decimal
    value_paise: int
    last_sold_at: str | None


@dataclass(frozen=True)
class LowStockRow:
    """At or below the minimum."""

    product_id: int
    name: str
    stock_qty: Decimal
    min_stock: Decimal
    reorder_qty: Decimal


@dataclass(frozen=True)
class PurchaseSupplierRow:
    """Purchases per supplier."""

    supplier: str
    purchases: int
    total_paise: int


@dataclass(frozen=True)
class ProfitRow:
    """Gross profit per product from cost snapshots on the sale lines."""

    product_id: int
    name: str
    qty: Decimal
    revenue_paise: int
    cost_paise: int
    profit_paise: int
    margin_pct: int


@dataclass(frozen=True)
class ProfitReport:
    """Rows + totals + how many lines had no cost snapshot."""

    rows: list[ProfitRow]
    revenue_paise: int
    cost_paise: int
    profit_paise: int
    unknown_cost_lines: int


@dataclass(frozen=True)
class StockAsOfRow:
    """End-of-day balance."""

    product_id: int
    name: str
    qty_close: Decimal
    value_cost_paise: int


# ----------------------------------------------------------------------------- store
class ReportStore(Protocol):
    """SQL behind the reports."""

    def discount_audit(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def offer_usage(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def manual_lines(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def voids(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def audit_entries(self, session: Session, *, since: str | None, until: str | None, actions: tuple[str, ...] | None, user_id: int | None, limit: int, overrides_only: bool) -> list[AuditEntry]: ...  # pragma: no cover - protocol
    def shrinkage(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def cashier_performance(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def tax_summary(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def sales_by_day(self, session: Session, *, since: str, until: str, offset_minutes: int) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def item_sales(self, session: Session, *, product_id: int, since: str, until: str, offset_minutes: int) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def product_totals(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def basket_pairs(self, session: Session, *, since: str, until: str, limit: int) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def completed_bills(self, session: Session, *, since: str, until: str) -> int: ...  # pragma: no cover - protocol
    def dead_stock(self, session: Session, *, since: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def low_stock(self, session: Session) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def purchases_by_supplier(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def purchases_by_day(self, session: Session, *, since: str, until: str) -> list[tuple[str, int, int]]: ...  # pragma: no cover - protocol
    def profit_lines(self, session: Session, *, since: str, until: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def sale_times(self, session: Session, *, since: str, until: str) -> list[str]: ...  # pragma: no cover - protocol
    def stock_as_of(self, session: Session, *, day: str) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol


# ----------------------------------------------------------------------------- service
class ReportService:
    """Owner reports."""

    def __init__(self, txn: TxnFactory, store: ReportStore, ledger: LedgerService, audit: AuditService) -> None:
        self._txn = txn
        self._store = store
        self._ledger = ledger
        self._audit = audit

    # -- discounts & manual lines -------------------------------------------------------------
    def discount_audit(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[DiscountRow]:
        """Ad-hoc discounts per cashier with the reasons they gave."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.discounts") as session:
            rows = self._store.discount_audit(session, since=a, until=b)
        out: dict[int, DiscountRow] = {}
        for r in rows:
            uid = int(r["user_id"])
            cur = out.get(uid, DiscountRow(uid, 0, 0, 0, {}))
            reasons = dict(cur.reasons)
            reasons[str(r["reason"] or "-")] = reasons.get(str(r["reason"] or "-"), 0) + int(r["lines"])
            out[uid] = DiscountRow(uid, cur.lines + int(r["lines"]), cur.bills + int(r["bills"]), cur.adhoc_paise + int(r["paise"]), reasons)
        return sorted(out.values(), key=lambda d: -d.adhoc_paise)

    def offer_usage(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[OfferUsageRow]:
        """"Diwali Offer: 312 lines, ₹2,340 given"."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.offers") as session:
            return [OfferUsageRow(int(r["offer_id"]), str(r["name"]), int(r["lines"]), int(r["paise"])) for r in self._store.offer_usage(session, since=a, until=b)]

    def manual_lines(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[ManualLinesRow]:
        """Lookup and open-item lines per cashier (a rising ratio is a theft signal)."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.manual") as session:
            rows = self._store.manual_lines(session, since=a, until=b)
        return [ManualLinesRow(int(r["user_id"]), int(r["total_lines"]), int(r["lookup_lines"]), int(r["open_item_lines"]), int(r["open_item_paise"])) for r in rows if int(r["lookup_lines"]) or int(r["open_item_lines"])]

    # -- voids & overrides --------------------------------------------------------------------
    def voids(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[VoidRow]:
        """Every credit note: who, approved by whom, why."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.voids") as session:
            rows = self._store.voids(session, since=a, until=b)
        return [VoidRow(str(r["invoice_no"]), str(r["original_invoice"]), str(r["status"]), str(r["sold_at"]), int(r["user_id"]), r["approved_by_user_id"], int(r["total_paise"]), str(r["note"] or "")) for r in rows]

    def overrides(self, actor: Actor, *, since: str | None = None, until: str | None = None, limit: int = 500) -> list[OverrideRow]:
        """PIN overrides, hardware overrides, over-cap discounts, voids, cash-outs, reprints — newest first."""
        require(actor, Permission.REPORT_AUDIT)
        with self._txn(label="report.overrides") as session:
            entries = self._store.audit_entries(session, since=since, until=until, actions=OVERRIDE_ACTIONS, user_id=None, limit=limit, overrides_only=True)
        return [OverrideRow(e.at, e.action, e.user_id, e.actor_user_id, e.entity, e.entity_id, (e.after or "")[:200]) for e in entries]

    # -- stock & cashiers ---------------------------------------------------------------------
    def shrinkage(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[ShrinkageRow]:
        """Damage, negative adjustments and stock-take losses, valued at cost (or shelf price)."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.shrinkage") as session:
            rows = self._store.shrinkage(session, since=a, until=b)
        return [ShrinkageRow(int(r["product_id"]), str(r["name"]), _dec(r["damage_qty"]), _dec(r["adjustment_qty"]), _dec(r["stock_take_qty"]), int(r["value_paise"])) for r in rows]

    def cashier_performance(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[CashierPerformanceRow]:
        """Bills · sales · average bill · discounts · voids · manual lines · drawer variance per cashier."""
        require(actor, Permission.REPORT_AUDIT)
        a, b = _window(since, until)
        with self._txn(label="report.cashiers") as session:
            rows = self._store.cashier_performance(session, since=a, until=b)
        out = []
        for r in rows:
            bills = int(r["bills"])
            out.append(CashierPerformanceRow(int(r["user_id"]), bills, int(r["sales_paise"]), int(r["sales_paise"]) // bills if bills else 0, int(r["discount_paise"]), int(r["voids"]), int(r["manual_lines"]), int(r["shifts_closed"]), int(r["variance_paise"])))
        return sorted(out, key=lambda p: -p.sales_paise)

    def tax_summary(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[TaxRow]:
        """Taxable / CGST / SGST per rate over completed bills (credit notes subtracted)."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.tax") as session:
            rows = self._store.tax_summary(session, since=a, until=b)
        return [TaxRow(int(r["tax_rate_bp"]), int(r["taxable_paise"]), int(r["cgst_paise"]), int(r["sgst_paise"]), int(r["bills"])) for r in rows]

    # -- sales ------------------------------------------------------------------------------------
    def sales_by_day(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[DaySalesRow]:
        """Per shop day: completed bills, items, totals by mode; credit notes as refunds on their day."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.sales_day") as session:
            rows = self._store.sales_by_day(session, since=a, until=b, offset_minutes=IST_OFFSET_MINUTES)
        return [DaySalesRow(str(r["day"]), int(r["bills"]), _dec(r["items"]), int(r["subtotal_paise"]), int(r["discount_paise"]), int(r["tax_paise"]), int(r["total_paise"]), int(r["cash_paise"]), int(r["upi_paise"]), int(r["card_paise"]), int(r["refunds_paise"])) for r in rows]

    def sales_summary(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> SalesSummary:
        """Period totals from the per-day rows."""
        days = self.sales_by_day(actor, since=since, until=until)
        bills = sum(d.bills for d in days)
        total = sum(d.total_paise for d in days)
        refunds = sum(d.refunds_paise for d in days)
        by_mode = {k: v for k, v in (("CASH", sum(d.cash_paise for d in days)), ("UPI", sum(d.upi_paise for d in days)), ("CARD", sum(d.card_paise for d in days))) if v}
        return SalesSummary(bills, sum((d.items for d in days), Decimal(0)), total, refunds, total - refunds, total // bills if bills else 0, sum(d.discount_paise for d in days), sum(d.tax_paise for d in days), by_mode)

    def item_sales(self, actor: Actor, product_id: int, *, since: str | None = None, until: str | None = None) -> list[ItemDayRow]:
        """How one item sells over time."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.item") as session:
            rows = self._store.item_sales(session, product_id=product_id, since=a, until=b, offset_minutes=IST_OFFSET_MINUTES)
        return [ItemDayRow(str(r["day"]), _dec(r["qty"]), int(r["amount_paise"]), int(r["bills"])) for r in rows]

    def top_sellers(self, actor: Actor, *, since: str | None = None, until: str | None = None, limit: int = 20, by: str = "qty") -> list[TopSellerRow]:
        """Ranked by quantity or rupees (share of the period's revenue)."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.top") as session:
            rows = self._store.product_totals(session, since=a, until=b)
        revenue = sum(int(r["amount_paise"]) for r in rows) or 1
        key = (lambda r: (-_dec(r["qty"]), -int(r["amount_paise"]))) if by == "qty" else (lambda r: (-int(r["amount_paise"]), -_dec(r["qty"])))
        return [TopSellerRow(int(r["product_id"]), str(r["name"]), _dec(r["qty"]), int(r["amount_paise"]), int(round(int(r["amount_paise"]) * 100 / revenue))) for r in sorted(rows, key=key)[:limit]]

    def basket_pairs(self, actor: Actor, *, since: str | None = None, until: str | None = None, limit: int = 30) -> list[BasketPairRow]:
        """Products bought together — what to shelve side by side or bundle for a festival offer."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.basket") as session:
            rows = self._store.basket_pairs(session, since=a, until=b, limit=limit)
            bills = self._store.completed_bills(session, since=a, until=b) or 1
        return [BasketPairRow(int(r["product_a"]), str(r["name_a"]), int(r["product_b"]), str(r["name_b"]), int(r["baskets"]), int(round(int(r["baskets"]) * 100 / bills))) for r in rows]

    def dead_stock(self, actor: Actor, *, days: int = 60) -> list[DeadStockRow]:
        """In stock, not sold for ``days`` — stop buying."""
        require(actor, Permission.REPORT_SALES)
        from datetime import timedelta  # noqa: PLC0415

        since = iso_utc(business_day_bounds(today_ist())[0] - timedelta(days=days))
        with self._txn(label="report.dead") as session:
            rows = self._store.dead_stock(session, since=since)
        return [DeadStockRow(int(r["product_id"]), str(r["name"]), _dec(r["stock_qty"]), int(r["value_paise"]), r["last_sold_at"]) for r in rows]

    def low_stock(self, actor: Actor) -> list[LowStockRow]:
        """At or below the minimum (both roles — it is what to reorder today)."""
        require(actor, Permission.VIEW_STOCK_LEVEL)
        with self._txn(label="report.low") as session:
            rows = self._store.low_stock(session)
        return [LowStockRow(int(r["product_id"]), str(r["name"]), _dec(r["stock_qty"]), _dec(r["min_stock"]), _dec(r["reorder_qty"])) for r in rows]

    # -- purchases & profit ---------------------------------------------------------------------
    def purchases(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[PurchaseSupplierRow]:
        """Per supplier."""
        require(actor, Permission.REPORT_PURCHASES)
        a, b = _window(since, until)
        with self._txn(label="report.purchases") as session:
            rows = self._store.purchases_by_supplier(session, since=a, until=b)
        return [PurchaseSupplierRow(str(r["supplier"] or "—"), int(r["purchases"]), int(r["total_paise"])) for r in rows]

    def purchases_by_day(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> list[tuple[str, int, int]]:
        """``(day, purchases, paise)``."""
        require(actor, Permission.REPORT_PURCHASES)
        a, b = _window(since, until)
        with self._txn(label="report.purchases_day") as session:
            return self._store.purchases_by_day(session, since=a, until=b)

    def profit(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> ProfitReport:
        """Gross profit = Σ(line total − cost snapshot × qty) over lines that carry a cost; credit notes subtract."""
        require(actor, Permission.REPORT_PROFIT)
        a, b = _window(since, until)
        with self._txn(label="report.profit") as session:
            lines = self._store.profit_lines(session, since=a, until=b)
        acc: dict[int, list[Any]] = {}
        unknown = 0
        for ln in lines:
            if ln["cost_paise_snapshot"] is None:
                unknown += 1
                continue
            sign = 1 if ln["status"] == "COMPLETED" else -1
            qty = _dec(ln["qty"]) * sign
            revenue = int(ln["line_total_paise"]) * sign
            cost = int((_dec(ln["qty"]) * Decimal(int(ln["cost_paise_snapshot"])) / _dec(ln["pricing_unit_qty_snapshot"] or 1)).to_integral_value()) * sign
            cur = acc.setdefault(int(ln["product_id"]), [str(ln["name_snapshot"]), Decimal(0), 0, 0])
            cur[1] += qty
            cur[2] += revenue
            cur[3] += cost
        rows = [ProfitRow(pid, name, qty, rev, cost, rev - cost, int(round((rev - cost) * 100 / rev)) if rev else 0) for pid, (name, qty, rev, cost) in acc.items()]
        rows.sort(key=lambda r: -r.profit_paise)
        revenue = sum(r.revenue_paise for r in rows)
        cost = sum(r.cost_paise for r in rows)
        return ProfitReport(rows, revenue, cost, revenue - cost, unknown)

    def hourly_heatmap(self, actor: Actor, *, since: str | None = None, until: str | None = None) -> dict[tuple[int, int], int]:
        """``{(weekday 0=Mon, hour): bills}`` in shop time — when the counter is busy."""
        require(actor, Permission.REPORT_SALES)
        a, b = _window(since, until)
        with self._txn(label="report.heat") as session:
            times = self._store.sale_times(session, since=a, until=b)
        out: dict[tuple[int, int], int] = {}
        for t in times:
            local = to_ist(parse_iso_utc(t))
            key = (local.weekday(), local.hour)
            out[key] = out.get(key, 0) + 1
        return out

    def stock_as_of(self, actor: Actor, day: str) -> list[StockAsOfRow]:
        """End-of-day balances written by the nightly ``stock_daily_balance`` job."""
        require(actor, Permission.REPORT_SALES)
        with self._txn(label="report.asof") as session:
            rows = self._store.stock_as_of(session, day=day)
        return [StockAsOfRow(int(r["product_id"]), str(r["name"]), _dec(r["qty_close"]), int(r["value_cost_paise"])) for r in rows]

    # -- integrity & audit --------------------------------------------------------------------
    def integrity(self, actor: Actor) -> IntegrityView:
        """Run the full integrity pass now (not recorded — the hourly job records)."""
        require(actor, Permission.REPORT_AUDIT)
        with self._txn(label="report.integrity") as session:
            status = self._ledger.run_all(session, record=False)
        checks = [IntegrityCheckRow(kind, r.ok, getattr(r, "detail", ""), getattr(r, "code", "")) for kind, r in status.reports.items()]
        return IntegrityView(status.ok, status.at, status.head, checks, status.codes)

    def audit_trail(self, actor: Actor, *, limit: int = 200, action: str | None = None, user_id: int | None = None, since: str | None = None, until: str | None = None) -> list[AuditEntry]:
        """Newest first."""
        require(actor, Permission.REPORT_AUDIT)
        with self._txn(label="report.audit") as session:
            return self._store.audit_entries(session, since=since, until=until, actions=(action,) if action else None, user_id=user_id, limit=limit, overrides_only=False)

    # -- export -------------------------------------------------------------------------------
    @staticmethod
    def to_csv(rows: Sequence[Any]) -> str:
        """Dataclass rows → CSV text with a UTF-8 BOM (Excel opens Hindi correctly)."""
        buf = io.StringIO()
        buf.write("\ufeff")
        if not rows:
            return buf.getvalue()
        first = rows[0]
        names = [f.name for f in fields(first)] if is_dataclass(first) else list(first.keys())
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(names)
        for row in rows:
            values = [getattr(row, n) for n in names] if is_dataclass(row) else [row[n] for n in names]
            writer.writerow([_cell(v) for v in values])
        return buf.getvalue()


# ----------------------------------------------------------------------------- helpers
def _window(since: str | None, until: str | None) -> tuple[str, str]:
    """Default = the last 90 shop days up to the end of today. Date-only values (``YYYY-MM-DD``) mean whole shop days."""
    from datetime import date, timedelta  # noqa: PLC0415

    def start_of(text: str) -> str:
        return iso_utc(business_day_bounds(date.fromisoformat(text))[0]) if len(text) == 10 else text

    def end_of(text: str) -> str:
        return iso_utc(business_day_bounds(date.fromisoformat(text))[1]) if len(text) == 10 else text

    start, end = business_day_bounds(today_ist())
    return (start_of(since) if since else iso_utc(start - timedelta(days=90))), (end_of(until) if until else iso_utc(end))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _cell(value: Any) -> Any:
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, Decimal):
        return str(value)
    return value
