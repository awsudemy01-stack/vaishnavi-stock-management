"""ShiftService — cash drawer accountability (docs/05 §6; stdlib).

A cashier **opens** a shift with the float in the drawer, sells, records cash in / out (cash-out needs
the Owner PIN), and **closes** by counting the drawer by denomination. The system knows what *should*
be there — float + cash sales (tendered minus change) − cash refunds + cash-in − cash-out — and records
the **variance**; a shortfall needs a note. The **Z report** carries the day's totals by payment mode,
discounts, manual lines, voids, the invoice range and the **sales-chain head** so a photo of the paper
is an off-system anchor against tampering. Repeated negative variance per cashier is the clearest theft
signal and is a report of its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

from vaishnavi.core.clock import format_ist, iso_utc, parse_iso_utc
from vaishnavi.core.errors import ShiftError
from vaishnavi.core.events import EventBus, ShiftClosed, bus
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.db.records import CashEventRecord, ShiftRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.ledger import LedgerService

log = logging.getLogger("vaishnavi.services.shift")

DENOMINATIONS: Final = (500, 200, 100, 50, 20, 10, 5, 2, 1)
"""Rupee notes and coins offered on the open/close count screens (₹2000 was withdrawn in 2023; a stray
note can still be typed as a total)."""


def count_denominations(counts: dict[int, int]) -> int:
    """``{500: 4, 200: 3}`` → paise."""
    total = 0
    for value, n in counts.items():
        if int(n) < 0 or int(value) <= 0:
            raise ValueError("denomination counts must be positive")
        total += int(value) * int(n) * 100
    return total


@dataclass(frozen=True)
class ShiftReport:
    """X (mid-shift) or Z (after close) figures."""

    shift_id: int
    cashier_id: int
    kind: str  # X | Z
    opened_at: str
    closed_at: str | None
    opening_float_paise: int
    bills: int
    items_sold: Decimal
    sales_total_paise: int
    by_mode: dict[str, int]
    item_discount_paise: int
    bill_discount_paise: int
    manual_lines: int
    open_item_lines: int
    voids: int
    refunds_paise: int
    cash_in_paise: int
    cash_out_paise: int
    expected_cash_paise: int
    counted_cash_paise: int | None
    variance_paise: int | None
    denominations: dict[int, int]
    invoice_range: tuple[str, str] | None
    chain_head: str
    note: str | None = None
    events: list[CashEventRecord] = field(default_factory=list)


@dataclass(frozen=True)
class VarianceSummary:
    """Per-cashier variance pattern."""

    user_id: int
    shifts: int
    negative_shifts: int
    total_variance_paise: int
    worst_paise: int


class ShiftStore(Protocol):
    """Rows the service needs."""

    def insert_shift(self, session: Session, *, user_id: int, opened_at: str, opening_float_paise: int, opening_denominations: str | None = None) -> int: ...  # pragma: no cover - protocol
    def shift(self, session: Session, shift_id: int) -> ShiftRecord | None: ...  # pragma: no cover - protocol
    def open_shift_for(self, session: Session, user_id: int) -> ShiftRecord | None: ...  # pragma: no cover - protocol
    def open_shifts(self, session: Session) -> list[ShiftRecord]: ...  # pragma: no cover - protocol
    def close_shift(self, session: Session, shift_id: int, **fields: Any) -> None: ...  # pragma: no cover - protocol
    def insert_cash_event(self, session: Session, *, shift_id: int, type: str, amount_paise: int, reason: str, user_id: int, approved_by_user_id: int | None, at: str) -> int: ...  # noqa: A002  # pragma: no cover - protocol
    def cash_events(self, session: Session, shift_id: int) -> list[CashEventRecord]: ...  # pragma: no cover - protocol
    def sales_figures(self, session: Session, shift_id: int) -> dict[str, Any]: ...  # pragma: no cover - protocol
    def closed_shifts(self, session: Session, *, user_id: int | None, limit: int) -> list[ShiftRecord]: ...  # pragma: no cover - protocol


class ShiftService:
    """Open · cash events · close · reports."""

    def __init__(self, txn: TxnFactory, store: ShiftStore, audit: AuditService, ledger: LedgerService, *, event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._ledger = ledger
        self._bus = event_bus

    # -- reads ------------------------------------------------------------------------------------
    def current(self, user_id: int) -> ShiftRecord | None:
        """The user's OPEN shift."""
        with self._txn(label="shift.current") as session:
            return self._store.open_shift_for(session, user_id)

    def get(self, shift_id: int) -> ShiftRecord | None:
        """One shift."""
        with self._txn(label="shift.get") as session:
            return self._store.shift(session, shift_id)

    def open_shifts(self) -> list[ShiftRecord]:
        """Everyone's open shifts (Owner view: who holds a drawer right now)."""
        with self._txn(label="shift.open_all") as session:
            return self._store.open_shifts(session)

    def expected_cash(self, shift_id: int) -> int:
        """Float + cash sales − cash refunds + cash in − cash out."""
        with self._txn(label="shift.expected") as session:
            return self._expected(session, shift_id)

    # -- lifecycle --------------------------------------------------------------------------------
    def open(self, actor: Actor, *, opening_float_paise: int | None = None, denominations: dict[int, int] | None = None) -> ShiftRecord:
        """Start a shift with the float counted into the drawer (one open shift per user, ``E-SHIFT-002``).

        Preferably note by note (``denominations``) so the opening count is as honest as the closing one;
        a plain total is still accepted (Owner topping up quickly).
        """
        require(actor, Permission.SHIFT_OWN)
        if denominations is None and opening_float_paise is None:
            raise ValueError("count the float (denominations or a total)")
        opening = count_denominations(denominations) if denominations is not None else int(opening_float_paise or 0)
        if opening < 0:
            raise ValueError("opening float cannot be negative")
        denoms_json = json.dumps({str(k): int(v) for k, v in denominations.items() if int(v)}) if denominations is not None else None
        with self._txn(label="shift.open") as session:
            existing = self._store.open_shift_for(session, actor.user_id)
            if existing is not None:
                raise ShiftError("E-SHIFT-002", user=str(actor.user_id), detail=f"shift {existing.id} already open")
            sid = self._store.insert_shift(session, user_id=actor.user_id, opened_at=iso_utc(), opening_float_paise=opening, opening_denominations=denoms_json)
            self._audit.record(session, "SHIFT_OPENED", user_id=actor.user_id, entity="shift", entity_id=sid, after={"opening_float_paise": opening, "denominations": denoms_json})
            record = self._store.shift(session, sid)
        assert record is not None
        log.info("SHIFT_OPENED id=%s user=%s float=%d counted_by_note=%s", sid, actor.user_id, opening, denominations is not None)
        return record

    def cash_in(self, actor: Actor, amount_paise: int, *, reason: str, shift_id: int | None = None) -> CashEventRecord:
        """Money added to the drawer (change float, owner top-up)."""
        require(actor, Permission.SHIFT_OWN)
        return self._cash_event(actor, "CASH_IN", amount_paise, reason, shift_id, approved_by=None)

    def cash_out(self, actor: Actor, amount_paise: int, *, reason: str, shift_id: int | None = None, approver_id: int | None = None) -> CashEventRecord:
        """Money taken out (expenses, owner pick-up) — Owner PIN for staff; never more than the drawer holds (``E-SHIFT-004``)."""
        require(actor, Permission.CASH_OUT)
        approver = actor.user_id if actor.role is Role.OWNER else approver_id
        return self._cash_event(actor, "CASH_OUT", amount_paise, reason, shift_id, approved_by=approver)

    def close(self, actor: Actor, shift_id: int, *, denominations: dict[int, int] | None = None, counted_cash_paise: int | None = None, note: str = "") -> ShiftRecord:
        """Count the drawer; variance = counted − expected; a shortfall needs a note; Owner may close any shift."""
        require(actor, Permission.SHIFT_OWN)
        if denominations is None and counted_cash_paise is None:
            raise ValueError("count the drawer (denominations or a total)")
        counted = count_denominations(denominations) if denominations is not None else int(counted_cash_paise or 0)
        if counted < 0:
            raise ValueError("counted cash cannot be negative")
        with self._txn(label="shift.close") as session:
            shift = self._store.shift(session, shift_id)
            if shift is None:
                raise ShiftError("E-SHIFT-001", detail=f"shift {shift_id} missing")
            if shift.status != "OPEN":
                raise ShiftError("E-SHIFT-003", detail=f"shift {shift_id} is {shift.status}")
            if shift.user_id != actor.user_id and actor.role is not Role.OWNER:
                raise ShiftError("E-SHIFT-001", detail="not your shift")
            expected = self._expected(session, shift_id)
            variance = counted - expected
            if variance < 0 and not note.strip():
                raise ValueError("a note is required when the drawer is short")
            now = iso_utc()
            self._store.close_shift(session, shift_id, closed_at=now, expected_cash_paise=expected, counted_cash_paise=counted, denominations=json.dumps({str(k): int(v) for k, v in (denominations or {}).items()}), variance_paise=variance, closed_by_user_id=actor.user_id, status="CLOSED", note=note.strip() or None)
            self._audit.record(session, "SHIFT_CLOSED", user_id=shift.user_id, actor_user_id=actor.user_id, entity="shift", entity_id=shift_id, after={"expected_paise": expected, "counted_paise": counted, "variance_paise": variance, "note": note.strip() or None})
            record = self._store.shift(session, shift_id)
        assert record is not None
        self._bus.publish(ShiftClosed(shift_id=shift_id, user_id=shift.user_id, variance_paise=variance))
        level = log.warning if variance < 0 else log.info
        level("SHIFT_CLOSED id=%s cashier=%s by=%s expected=%d counted=%d variance=%d", shift_id, shift.user_id, actor.user_id, expected, counted, variance)
        return record

    # -- reports ----------------------------------------------------------------------------------
    def x_report(self, shift_id: int) -> ShiftReport:
        """Mid-shift snapshot (no count)."""
        return self._report(shift_id, "X")

    def z_report(self, shift_id: int) -> ShiftReport:
        """End-of-shift report (after close)."""
        return self._report(shift_id, "Z")

    def variance_history(self, *, user_id: int | None = None, limit: int = 100) -> list[ShiftRecord]:
        """Closed shifts, newest first."""
        with self._txn(label="shift.history") as session:
            return self._store.closed_shifts(session, user_id=user_id, limit=limit)

    def cashier_variance_summary(self, *, limit: int = 1000) -> dict[int, VarianceSummary]:
        """Per cashier: shifts, how many were short, total and worst variance."""
        out: dict[int, VarianceSummary] = {}
        for s in self.variance_history(limit=limit):
            v = s.variance_paise or 0
            cur = out.get(s.user_id, VarianceSummary(s.user_id, 0, 0, 0, 0))
            out[s.user_id] = VarianceSummary(s.user_id, cur.shifts + 1, cur.negative_shifts + (1 if v < 0 else 0), cur.total_variance_paise + v, min(cur.worst_paise, v))
        return out

    @staticmethod
    def report_lines(r: ShiftReport, *, width: int = 48) -> list[str]:
        """Plain-text X/Z report for the thermal printer / screen."""
        def row(label: str, value: str) -> str:
            return f"{label[: width - len(value) - 1]:<{width - len(value) - 1}} {value}"

        lines = [f"{r.kind} REPORT".center(width), f"Shift #{r.shift_id}  Cashier #{r.cashier_id}".center(width), "-" * width]
        lines.append(row("Opened", format_ist(parse_iso_utc(r.opened_at))))
        if r.closed_at:
            lines.append(row("Closed", format_ist(parse_iso_utc(r.closed_at))))
        if r.invoice_range:
            lines.append(row("Bills", f"{r.invoice_range[0].rsplit('/', 1)[-1]}-{r.invoice_range[1].rsplit('/', 1)[-1]} ({r.bills})"))
        lines.append(row("Items sold", str(r.items_sold)))
        lines.append(row("Sales total", format_inr(r.sales_total_paise)))
        for mode, amount in sorted(r.by_mode.items()):
            lines.append(row(f"  {mode}", format_inr(amount)))
        if r.item_discount_paise or r.bill_discount_paise:
            lines.append(row("Discounts given", format_inr(r.item_discount_paise + r.bill_discount_paise)))
        if r.manual_lines or r.open_item_lines:
            lines.append(row("Lookup / open-item lines", f"{r.manual_lines} / {r.open_item_lines}"))
        if r.voids or r.refunds_paise:
            lines.append(row("Voids / refunds", f"{r.voids} / {format_inr(r.refunds_paise)}"))
        lines.append("-" * width)
        lines.append(row("Opening float", format_inr(r.opening_float_paise)))
        lines.append(row("Cash in", format_inr(r.cash_in_paise)))
        lines.append(row("Cash out", format_inr(r.cash_out_paise)))
        lines.append(row("Expected cash", format_inr(r.expected_cash_paise)))
        if r.counted_cash_paise is not None:
            lines.append(row("Counted cash", format_inr(r.counted_cash_paise)))
            sign = "+" if (r.variance_paise or 0) >= 0 else "-"
            lines.append(row("VARIANCE", f"{sign}{format_inr(abs(r.variance_paise or 0))}"))
            for value, n in sorted(r.denominations.items(), reverse=True):
                lines.append(row(f"  {value} x {n}", format_inr(value * n * 100)))
        if r.note:
            lines.append(f"Note: {r.note}"[:width])
        lines.append("-" * width)
        lines.append(f"Chain {r.chain_head[:16]}...{r.chain_head[-8:]}".center(width))
        return lines

    # -- internals --------------------------------------------------------------------------------
    def _cash_event(self, actor: Actor, type_: str, amount: int, reason: str, shift_id: int | None, *, approved_by: int | None) -> CashEventRecord:
        if amount <= 0:
            raise ValueError("amount must be > 0")
        if not reason or not reason.strip():
            raise ValueError("a reason is required")
        with self._txn(label="shift.cash") as session:
            shift = self._store.shift(session, shift_id) if shift_id is not None else self._store.open_shift_for(session, actor.user_id)
            if shift is None or shift.status != "OPEN":
                raise ShiftError("E-SHIFT-001", detail="no open shift")
            if type_ == "CASH_OUT":
                have = self._expected(session, shift.id)
                if amount > have:
                    raise ShiftError("E-SHIFT-004", amount=format_inr(amount), drawer=format_inr(have))
            eid = self._store.insert_cash_event(session, shift_id=shift.id, type=type_, amount_paise=amount, reason=reason.strip(), user_id=actor.user_id, approved_by_user_id=approved_by, at=iso_utc())
            self._audit.record(session, type_, user_id=actor.user_id, actor_user_id=approved_by, entity="shift", entity_id=shift.id, after={"amount_paise": amount, "reason": reason.strip()})
            events = self._store.cash_events(session, shift.id)
        record = next(e for e in events if e.id == eid)
        log.info("%s shift=%s amount=%d reason=%r by=%s approver=%s", type_, shift.id, amount, reason, actor.user_id, approved_by)
        return record

    def _expected(self, session: Session, shift_id: int) -> int:
        shift = self._store.shift(session, shift_id)
        if shift is None:
            raise ShiftError("E-SHIFT-001", detail=f"shift {shift_id} missing")
        fig = self._store.sales_figures(session, shift_id)
        events = self._store.cash_events(session, shift_id)
        cash_in = sum(e.amount_paise for e in events if e.type == "CASH_IN")
        cash_out = sum(e.amount_paise for e in events if e.type == "CASH_OUT")
        return shift.opening_float_paise + int(fig["cash_net_paise"]) + cash_in - cash_out

    def _report(self, shift_id: int, kind: str) -> ShiftReport:
        with self._txn(label="shift.report") as session:
            shift = self._store.shift(session, shift_id)
            if shift is None:
                raise ShiftError("E-SHIFT-001", detail=f"shift {shift_id} missing")
            fig = self._store.sales_figures(session, shift_id)
            events = self._store.cash_events(session, shift_id)
            head = self._ledger.chain_head(session)
            expected = shift.expected_cash_paise if shift.status == "CLOSED" and shift.expected_cash_paise is not None else self._expected(session, shift_id)
        denominations = {int(k): int(v) for k, v in json.loads(shift.denominations or "{}").items()}
        return ShiftReport(
            shift_id=shift.id, cashier_id=shift.user_id, kind=kind, opened_at=shift.opened_at, closed_at=shift.closed_at, opening_float_paise=shift.opening_float_paise,
            bills=int(fig["bills"]), items_sold=Decimal(str(fig["items"])), sales_total_paise=int(fig["total_paise"]), by_mode=dict(fig["by_mode"]),
            item_discount_paise=int(fig["item_discount_paise"]), bill_discount_paise=int(fig["bill_discount_paise"]), manual_lines=int(fig["manual_lines"]), open_item_lines=int(fig["open_item_lines"]),
            voids=int(fig["voids"]), refunds_paise=int(fig["refunds_paise"]), cash_in_paise=sum(e.amount_paise for e in events if e.type == "CASH_IN"), cash_out_paise=sum(e.amount_paise for e in events if e.type == "CASH_OUT"),
            expected_cash_paise=expected, counted_cash_paise=shift.counted_cash_paise, variance_paise=shift.variance_paise, denominations=denominations,
            invoice_range=(str(fig["first_invoice"]), str(fig["last_invoice"])) if fig.get("first_invoice") else None, chain_head=head, note=shift.note, events=events,
        )
