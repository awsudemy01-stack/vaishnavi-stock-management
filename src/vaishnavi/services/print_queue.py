"""PrintQueueService — print the receipt when a printer is ready, otherwise archive + queue (docs/05 §10).

The owner's rule: **printing never stops the shop.** After every ``SaleCommitted`` this service
archives the receipt (always), then tries the configured printer. Not configured / not ready / the
transport raised → the sale is flagged ``receipt_printed = 0`` and a ``print_pending`` row is added
(``E-PRN-001`` logged, never raised to the cashier). The POS chip shows *Printer: not connected —
N receipts queued*; *Print pending receipts* flushes the queue once paper is back. Reprints carry the
``DUPLICATE`` label, bump ``reprint_count``, are audited, and above ``billing.reprint_limit`` need the
Owner (or PIN) — a classic way to "print a bill" for a friend is thereby counted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.errors import AuthError
from vaishnavi.core.events import EventBus, SaleCommitted, bus
from vaishnavi.core.permissions import Actor, Permission, Role, require
from vaishnavi.core.settings import HardwareSettings, Settings
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory
from vaishnavi.services.receipt import ReceiptService

log = logging.getLogger("vaishnavi.services.print_queue")


class PrintQueueStore(Protocol):
    """``print_pending`` rows."""

    def enqueue(self, session: Session, *, sale_id: int, queued_at: str, attempts: int) -> int: ...  # pragma: no cover - protocol
    def pending(self, session: Session) -> list[tuple[int, int, str, int]]: ...  # pragma: no cover - protocol
    def pending_count(self, session: Session) -> int: ...  # pragma: no cover - protocol
    def mark_printed(self, session: Session, queue_id: int, *, at: str) -> None: ...  # pragma: no cover - protocol
    def bump_attempt(self, session: Session, queue_id: int) -> None: ...  # pragma: no cover - protocol


class Transport(Protocol):
    """Something that accepts raw ESC/POS bytes."""

    def send(self, data: bytes) -> None: ...  # pragma: no cover - protocol


class StatusLike(Protocol):
    """``platform.printing.PrinterStatus`` shape."""

    kind: str
    address: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class PrintOutcome:
    """What happened to one receipt."""

    sale_id: int
    printed: bool
    queued: bool
    archive: Any = None
    error: str | None = None


@dataclass(frozen=True)
class FlushReport:
    """Result of *Print pending receipts*."""

    printed: int
    failed: int
    remaining: int
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrinterChip:
    """The POS status chip."""

    configured: bool
    ready: bool
    pending: int
    kind: str
    address: str
    detail: str

    @property
    def tone(self) -> str:
        """``green`` ready · ``amber`` queued/not ready · ``grey`` none configured and nothing queued."""
        if self.ready and not self.pending:
            return "green"
        if self.pending or self.configured:
            return "amber"
        return "grey"


class PrintQueueService:
    """Auto-print on commit, queue, flush, reprint."""

    def __init__(
        self,
        txn: TxnFactory,
        store: PrintQueueStore,
        receipts: ReceiptService,
        audit: AuditService,
        *,
        settings: Callable[[], Settings],
        status: Callable[[HardwareSettings], StatusLike],
        transport_factory: Callable[[HardwareSettings], Transport | None],
        event_bus: EventBus = bus,
    ) -> None:
        self._txn = txn
        self._store = store
        self._receipts = receipts
        self._audit = audit
        self._settings = settings
        self._status = status
        self._transport_factory = transport_factory
        self._bus = event_bus

    # -- wiring -----------------------------------------------------------------------------------
    def subscribe(self, event_bus: EventBus) -> None:
        """Print/queue after every committed sale; a failure here must never reach the cashier."""

        def on_sale(event: SaleCommitted) -> None:
            try:
                self.after_sale(event.sale_id)
            except Exception as exc:  # noqa: BLE001 - billing already committed
                log.error("PRINT_AFTER_SALE_FAILED sale=%s error=%r", event.sale_id, exc, exc_info=True)

        event_bus.subscribe(SaleCommitted, on_sale)

    # -- status -----------------------------------------------------------------------------------
    def pending_count(self) -> int:
        """Bills that went out without paper."""
        with self._txn(label="print.count") as session:
            return self._store.pending_count(session)

    def pending(self) -> list[tuple[int, int, str, int]]:
        """``(queue_id, sale_id, queued_at, attempts)``."""
        with self._txn(label="print.pending") as session:
            return self._store.pending(session)

    def chip(self) -> PrinterChip:
        """Status for the POS chip (never raises)."""
        hw = self._settings().hardware
        try:
            st = self._status(hw)
        except Exception as exc:  # noqa: BLE001
            log.warning("printer status probe failed: %s", exc)
            return PrinterChip(hw.printer_kind != "none", False, self.pending_count(), hw.printer_kind, hw.printer_address, str(exc))
        return PrinterChip(hw.printer_kind != "none", bool(st.ready), self.pending_count(), hw.printer_kind, hw.printer_address, st.detail)

    # -- the main entry ---------------------------------------------------------------------------
    def after_sale(self, sale_id: int) -> PrintOutcome:
        """Archive always; print when ready; otherwise queue (``E-PRN-001``)."""
        sale = self._receipts.load(sale_id)
        if sale is None:
            raise ValueError(f"sale {sale_id} not found")
        model = self._receipts.build(sale)
        archive = self._receipts.archive(model)
        hw = self._settings().hardware
        attempted = False
        error: str | None = None
        if hw.printer_kind != "none":
            attempted = True
            error = self._try_print(hw, model, drawer_kick=any(p.mode == "CASH" for p in sale.payments))
            if error is None:
                self._receipts.mark_printed(sale_id, reprint=False)
                log.info("RECEIPT_PRINTED sale=%s invoice=%s printer=%s", sale_id, sale.invoice_no, hw.printer_address)
                return PrintOutcome(sale_id, True, False, archive)
        with self._txn(label="print.enqueue") as session:
            self._store.enqueue(session, sale_id=sale_id, queued_at=iso_utc(), attempts=1 if attempted else 0)
        log.warning("RECEIPT_QUEUED sale=%s invoice=%s code=E-PRN-001 reason=%s", sale_id, sale.invoice_no, error or ("no printer configured" if not attempted else "printer not ready"))
        return PrintOutcome(sale_id, False, True, archive, error)

    def print_pending(self, actor: Actor | None = None) -> FlushReport:
        """*Print pending receipts* — oldest first; stops at the first failure to keep order."""
        hw = self._settings().hardware
        rows = self.pending()
        if not rows:
            return FlushReport(0, 0, 0)
        if hw.printer_kind == "none":
            return FlushReport(0, 0, len(rows), ["no printer configured"])
        printed = failed = 0
        errors: list[str] = []
        for queue_id, sale_id, _queued_at, _attempts in rows:
            sale = self._receipts.load(sale_id)
            if sale is None:
                failed += 1
                errors.append(f"sale {sale_id} missing")
                continue
            model = self._receipts.build(sale, copy_label=None if not sale.receipt_printed else "DUPLICATE")
            error = self._try_print(hw, model, drawer_kick=False)
            with self._txn(label="print.flush") as session:
                if error is None:
                    self._store.mark_printed(session, queue_id, at=iso_utc())
                else:
                    self._store.bump_attempt(session, queue_id)
            if error is None:
                self._receipts.mark_printed(sale_id, reprint=False)
                printed += 1
            else:
                failed += 1
                errors.append(error)
                break
        remaining = self.pending_count()
        log.info("PRINT_QUEUE_FLUSHED printed=%d failed=%d remaining=%d by=%s", printed, failed, remaining, actor.user_id if actor else "job")
        return FlushReport(printed, failed, remaining, errors)

    # -- plain text jobs (X/Z reports) -----------------------------------------------------------
    def print_lines(self, actor: Actor | None, lines: list[str], *, cut: bool = True) -> bool:
        """Send plain text (X / Z report) to the receipt printer; False when none is ready."""
        hw = self._settings().hardware
        if hw.printer_kind == "none":
            return False
        try:
            st = self._status(hw)
        except Exception as exc:  # noqa: BLE001
            log.warning("printer status probe failed: %s", exc)
            return False
        if not st.ready:
            return False
        transport = self._transport_factory(hw)
        if transport is None:
            return False

        def ascii_line(text: str) -> bytes:
            return text.replace("\u20b9", "Rs").replace("\u2212", "-").replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", "-").encode("ascii", "replace")

        body = b"\x1b@" + b"".join(ascii_line(ln) + b"\n" for ln in lines) + b"\x1bd\x03" + (b"\x1dV\x41\x03" if cut else b"")
        try:
            transport.send(body)
        except Exception as exc:  # noqa: BLE001
            log.warning("PRINT_LINES_FAILED error=%s", exc)
            return False
        log.info("PRINT_LINES_OK lines=%d by=%s", len(lines), actor.user_id if actor else "-")
        return True

    def reprint(self, actor: Actor, sale_id: int) -> PrintOutcome:
        """Print a ``DUPLICATE``; counted and audited; above the limit → Owner (``E-AUTH-011``)."""
        require(actor, Permission.HOLD_RESUME_REPRINT)
        sale = self._receipts.load(sale_id)
        if sale is None:
            raise ValueError(f"sale {sale_id} not found")
        limit = self._settings().billing.reprint_limit
        if sale.reprint_count >= limit and actor.role is not Role.OWNER and not actor.pin_verified:
            log.warning("REPRINT_LIMIT sale=%s count=%d limit=%d user=%s", sale_id, sale.reprint_count, limit, actor.user_id)
            raise AuthError("E-AUTH-011", permission="REPRINT", detail=f"reprint #{sale.reprint_count + 1} above limit {limit}")
        model = self._receipts.build(sale, copy_label="DUPLICATE")
        archive = self._receipts.archive(model)
        hw = self._settings().hardware
        error = self._try_print(hw, model, drawer_kick=False) if hw.printer_kind != "none" else "no printer configured"
        self._receipts.mark_printed(sale_id, reprint=True)
        with self._txn(label="print.reprint_audit") as session:
            self._audit.record(session, "REPRINT", user_id=actor.user_id, entity="sale", entity_id=sale_id, after={"count": sale.reprint_count + 1, "printed": error is None, "pin": actor.pin_verified})
        log.info("RECEIPT_REPRINT sale=%s invoice=%s count=%d printed=%s by=%s", sale_id, sale.invoice_no, sale.reprint_count + 1, error is None, actor.user_id)
        return PrintOutcome(sale_id, error is None, False, archive, error)

    # -- internals --------------------------------------------------------------------------------
    def _try_print(self, hw: HardwareSettings, model: Any, *, drawer_kick: bool) -> str | None:
        """None on success, else a short reason."""
        try:
            st = self._status(hw)
        except Exception as exc:  # noqa: BLE001
            return f"status probe failed: {exc}"
        if not st.ready:
            return st.detail or "printer not ready"
        transport = self._transport_factory(hw)
        if transport is None:
            return f"no transport for {hw.printer_kind}"
        try:
            transport.send(self._receipts.escpos(model, paper_mm=self._settings().receipt.paper_width_mm, drawer_kick=drawer_kick))
        except Exception as exc:  # noqa: BLE001 - hardware
            log.warning("PRINT_FAILED printer=%s error=%s code=E-PRN-001", hw.printer_address, exc)
            return str(exc)
        return None
