"""CashierRecordService — the cashier day book (owner's request, 21-Nov): every bill with its items, per cashier, per day.

The permanent ``sale`` ledger is append-only and hash-chained, so it can never be trimmed. The Owner also
wants a plain *daily record* — "what did each cashier sell today, item by item, with the time" — that is kept
for a bounded time (``billing.day_record_retention_days``, 90 by default) and can be deleted earlier with the
Owner's approval. That is this table: a retention-bound copy written right after ``SaleCommitted`` (bills and
credit notes alike), listed per day / per cashier on Reports → Cashier day book, pruned by the nightly job and
purgeable by the Owner (audited ``CASHIER_RECORDS_PURGED`` with the day range and how many rows went).

Staff may read their own records only (``REPORT_OWN_SHIFT``); the Owner reads everyone's. Writing is
idempotent — one row per sale — so a replayed event never duplicates a bill.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from vaishnavi.core.clock import iso_utc, parse_iso_utc, to_ist, today_ist
from vaishnavi.core.events import EventBus, SaleCommitted, bus
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.settings import Settings
from vaishnavi.db.records import CashierDayRecordRow, DayRecordItem, SaleView
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.cashier_records")

DEFAULT_RETENTION_DAYS = 90
MAX_ROWS = 5000
"""Hard cap on one listing (90 days of a busy counter fits comfortably; CSV export reads the same list)."""


class CashierRecordStore(Protocol):
    """SQL behind the day book."""

    def insert(self, session: Session, *, sale_id: int, invoice_no: str, business_day: str, sold_at: str, user_id: int, cashier_name: str, shift_id: int, status: str, line_count: int, item_qty: Decimal, total_paise: int, payment_modes: str, items_json: str, created_at: str) -> int | None: ...  # pragma: no cover - protocol
    def list(self, session: Session, *, since_day: str, until_day: str, user_id: int | None, limit: int) -> list[CashierDayRecordRow]: ...  # pragma: no cover - protocol
    def delete_before(self, session: Session, *, day: str) -> int: ...  # pragma: no cover - protocol
    def stats(self, session: Session) -> tuple[int, str | None, str | None]: ...  # pragma: no cover - protocol


class SaleLookup(Protocol):
    """``BillingService.get_sale``."""

    def get_sale(self, sale_id: int) -> SaleView | None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class CashierDaySummary:
    """One cashier's day: bills, credit notes, items and money."""

    user_id: int
    cashier_name: str
    bills: int
    credit_notes: int
    item_qty: Decimal
    sales_paise: int
    refunds_paise: int
    first_sale_at: str | None
    last_sale_at: str | None

    @property
    def net_paise(self) -> int:
        """Bills minus credit notes."""
        return self.sales_paise - self.refunds_paise


@dataclass(frozen=True)
class RetentionStatus:
    """What the Owner sees above the day book: how much is kept and until when."""

    rows: int
    oldest_day: str | None
    newest_day: str | None
    retention_days: int
    auto_delete_before: str
    """Shop day before which the nightly job deletes rows (today − retention)."""


class CashierRecordService:
    """Write after every sale; read per day; prune on schedule; purge on the Owner's say-so."""

    def __init__(self, txn: TxnFactory, store: CashierRecordStore, sales: SaleLookup, audit: AuditService, *, settings: Callable[[], Settings], cashier_name: Callable[[int], str], event_bus: EventBus = bus) -> None:
        self._txn = txn
        self._store = store
        self._sales = sales
        self._audit = audit
        self._settings = settings
        self._cashier_name = cashier_name
        self._bus = event_bus

    # -- wiring -----------------------------------------------------------------------------------
    def subscribe(self, event_bus: EventBus | None = None) -> None:
        """Record every committed bill / credit note; a failure here never reaches the cashier (the sale is committed)."""

        def on_sale(event: SaleCommitted) -> None:
            try:
                self.record(event.sale_id)
            except Exception as exc:  # noqa: BLE001 - billing already committed; the day book is a copy
                log.error("CASHIER_RECORD_FAILED sale=%s error=%r", event.sale_id, exc, exc_info=True)

        (event_bus or self._bus).subscribe(SaleCommitted, on_sale)

    # -- write ------------------------------------------------------------------------------------
    def record(self, sale_id: int) -> CashierDayRecordRow | None:
        """Copy one sale into the day book (idempotent). None when the sale does not exist or was already recorded."""
        sale = self._sales.get_sale(sale_id)
        if sale is None:
            log.warning("CASHIER_RECORD_NO_SALE sale=%s", sale_id)
            return None
        items = [DayRecordItem(it.open_item_name or it.name_snapshot, _q(it.qty), it.unit, it.unit_price_paise, it.line_total_paise, it.entry_method, bool(it.is_open_item), it.discount_paise) for it in sale.items]
        items_json = json.dumps([asdict(i) for i in items], ensure_ascii=False)
        modes = "+".join(sorted({p.mode for p in sale.payments}))
        day = to_ist(parse_iso_utc(sale.sold_at)).date().isoformat()
        qty = sum((it.qty for it in sale.items), Decimal(0))
        name = self._cashier_name(sale.user_id)
        now = iso_utc()
        with self._txn(label="daybook.record") as session:
            rid = self._store.insert(session, sale_id=sale.id, invoice_no=sale.invoice_no, business_day=day, sold_at=sale.sold_at, user_id=sale.user_id, cashier_name=name, shift_id=sale.shift_id, status=sale.status, line_count=len(items), item_qty=qty, total_paise=sale.total_paise, payment_modes=modes, items_json=items_json, created_at=now)
        if rid is None:
            log.debug("CASHIER_RECORD_DUPLICATE sale=%s", sale_id)
            return None
        log.info("CASHIER_RECORDED id=%s sale=%s invoice=%s day=%s cashier=%s status=%s lines=%d total=%d", rid, sale.id, sale.invoice_no, day, sale.user_id, sale.status, len(items), sale.total_paise)
        return CashierDayRecordRow(rid, sale.id, sale.invoice_no, day, sale.sold_at, sale.user_id, name, sale.shift_id, sale.status, len(items), qty, sale.total_paise, modes, tuple(items), now)

    # -- read -------------------------------------------------------------------------------------
    def day_book(self, actor: Actor, *, day: str | None = None, since_day: str | None = None, until_day: str | None = None, user_id: int | None = None, limit: int = MAX_ROWS) -> list[CashierDayRecordRow]:
        """Records for one shop day (default today) or a day range, newest first. Staff see only their own."""
        require(actor, Permission.REPORT_OWN_SHIFT)
        if actor.role is not Role.OWNER:
            user_id = actor.user_id
        a = since_day or day or today_ist().isoformat()
        b = until_day or day or a
        date.fromisoformat(a), date.fromisoformat(b)  # noqa: B018 - validate the owner-typed days early
        if a > b:
            a, b = b, a
        with self._txn(label="daybook.list") as session:
            rows = self._store.list(session, since_day=a, until_day=b, user_id=user_id, limit=max(1, min(limit, MAX_ROWS)))
        log.debug("CASHIER_DAYBOOK_READ from=%s to=%s user=%s rows=%d by=%s", a, b, user_id, len(rows), actor.user_id)
        return rows

    def day_summary(self, actor: Actor, *, day: str | None = None, since_day: str | None = None, until_day: str | None = None) -> list[CashierDaySummary]:
        """Per-cashier totals over the same window as :meth:`day_book`."""
        rows = self.day_book(actor, day=day, since_day=since_day, until_day=until_day)
        acc: dict[int, dict[str, Any]] = {}
        for r in rows:
            cur = acc.setdefault(r.user_id, {"name": r.cashier_name, "bills": 0, "notes": 0, "qty": Decimal(0), "sales": 0, "refunds": 0, "first": r.sold_at, "last": r.sold_at})
            if r.status == "COMPLETED":
                cur["bills"] += 1
                cur["sales"] += r.total_paise
                cur["qty"] += r.item_qty
            else:
                cur["notes"] += 1
                cur["refunds"] += r.total_paise
            cur["first"] = min(cur["first"], r.sold_at)
            cur["last"] = max(cur["last"], r.sold_at)
        out = [CashierDaySummary(uid, v["name"], v["bills"], v["notes"], v["qty"], v["sales"], v["refunds"], v["first"], v["last"]) for uid, v in acc.items()]
        return sorted(out, key=lambda s: -s.net_paise)

    def retention_days(self) -> int:
        """Configured retention (``billing.day_record_retention_days``)."""
        try:
            return int(self._settings().billing.day_record_retention_days)
        except Exception:  # noqa: BLE001 - settings unreadable → documented default
            return DEFAULT_RETENTION_DAYS

    def status(self) -> RetentionStatus:
        """Row count, oldest/newest day and the automatic cut-off."""
        with self._txn(label="daybook.stats") as session:
            rows, oldest, newest = self._store.stats(session)
        return RetentionStatus(rows, oldest, newest, self.retention_days(), self._cutoff().isoformat())

    # -- delete -----------------------------------------------------------------------------------
    def prune(self) -> int:
        """Nightly job: delete records older than the retention window; returns how many went."""
        cutoff = self._cutoff().isoformat()
        with self._txn(label="daybook.prune") as session:
            gone = self._store.delete_before(session, day=cutoff)
            if gone:
                self._audit.record(session, "CASHIER_RECORDS_PRUNED", entity="cashier_day_record", after={"before_day": cutoff, "rows": gone, "retention_days": self.retention_days()})
        if gone:
            log.info("CASHIER_RECORDS_PRUNED before=%s rows=%d retention_days=%d", cutoff, gone, self.retention_days())
        return gone

    def purge(self, actor: Actor, *, before_day: str, reason: str) -> int:
        """Owner-approved early deletion of every record before ``before_day`` (exclusive); audited with the reason.

        The Owner is the admin here: staff cannot call this (``MANAGE_SYSTEM``), and the UI asks for the Owner PIN
        before invoking it so a logged-in Owner screen left open cannot wipe the book with one click.
        """
        require(actor, Permission.MANAGE_SYSTEM)
        if not reason or len(reason.strip()) < 3:
            raise ValueError("a reason is required (at least 3 characters)")
        day = date.fromisoformat(before_day)
        if day > today_ist() + timedelta(days=1):
            raise ValueError("cannot delete records of future days")
        with self._txn(label="daybook.purge") as session:
            rows_before, oldest, _newest = self._store.stats(session)
            gone = self._store.delete_before(session, day=day.isoformat())
            self._audit.record(session, "CASHIER_RECORDS_PURGED", user_id=actor.user_id, entity="cashier_day_record", before={"rows": rows_before, "oldest_day": oldest}, after={"before_day": day.isoformat(), "rows_deleted": gone, "reason": reason.strip()})
        log.warning("CASHIER_RECORDS_PURGED before=%s rows=%d by=%s reason=%s", day.isoformat(), gone, actor.user_id, reason.strip())
        return gone

    # -- internals --------------------------------------------------------------------------------
    def _cutoff(self) -> date:
        return today_ist() - timedelta(days=self.retention_days())


def _q(value: Decimal) -> str:
    text = format(Decimal(value).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
