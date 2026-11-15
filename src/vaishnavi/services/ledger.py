"""LedgerService — tamper evidence for the sales ledger (stdlib only; docs/05 §3–4, docs/14 §2a).

Three independent checks, each written to ``integrity_check`` and logged ``INTEGRITY_OK`` /
``INTEGRITY_FAIL``:

* **CHAIN** — every sale carries ``hash = sha256(canonical(sale incl. prev_hash))`` and ``prev_hash`` =
  hash of the previous sale *by id* (genesis ``0×64``). Recomputing every hash and checking every link
  finds an edited amount (hash differs) **and** a deleted row (next ``prev_hash`` does not match).
  → ``E-LEDGER-001`` with ``sale_id``, ``expected``, ``found``.
* **GAPS** — bill numbers are continuous per financial year: ``COUNT(*) == MAX(seq_no)``; the exact
  missing numbers are listed. → ``E-LEDGER-002`` with ``before`` / ``after``.
* **STOCK** — Σ ``stock_movement.qty_delta`` must equal the cached ``product_stock.qty`` for every
  product, and the day's completed sale lines must equal the day's ``SALE`` movements per product
  (a sale saved without its movement means checkout was bypassed). Open items (system product) are
  excluded. → ``E-LEDGER-003``.

The audit chain (``services.audit.verify_chain``) is run alongside so one call — hourly job, start-up,
restore wizard, doctor — gives the full integrity picture. The service talks to the database through
:class:`LedgerStore`; the stdlib ``db/ledger_store.py`` implements it on a raw ``sqlite3`` connection
so the doctor can verify a database file without the ORM, and tests run on the real schema here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc, today_ist
from vaishnavi.db.records import GENESIS_HASH, AuditEntry, SaleRecord
from vaishnavi.services.audit import verify_chain as verify_audit_chain
from vaishnavi.services.common import Session

log = logging.getLogger("vaishnavi.services.ledger")

CODE_CHAIN: Final = "E-LEDGER-001"
CODE_GAPS: Final = "E-LEDGER-002"
CODE_STOCK: Final = "E-LEDGER-003"


# ----------------------------------------------------------------------------- canonical form
def canonical_qty(value: str | Decimal | int | float) -> str:
    """Quantity text with no exponent and no trailing zeros (``"2.00"`` → ``"2"``, ``"0.50"`` → ``"0.5"``).

    SQLite's NUMERIC affinity may hand back ``2``, ``2.0`` or ``'2.00'`` for the same stored value; the
    hash must not depend on that.
    """
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a quantity: {value!r}") from exc
    if dec == dec.to_integral_value():
        return str(dec.quantize(Decimal(1)))
    text = format(dec.normalize(), "f")
    return text


def canonical_sale(sale: SaleRecord) -> str:
    """The exact JSON that is hashed (docs/05 §4): sorted keys, compact separators, UTF-8 kept.

    Database ``id`` values are deliberately absent so a restore that renumbers rows still verifies;
    ``items`` keep their stored order (by ``sale_item.id``) and ``payments`` likewise.
    """
    doc = {
        "invoice_no": sale.invoice_no,
        "client_txn_id": sale.client_txn_id,
        "sold_at": sale.sold_at,
        "user_id": sale.user_id,
        "shift_id": sale.shift_id,
        "status": sale.status,
        "original_sale_id": sale.original_sale_id,
        "items": [
            {
                "product_id": it.product_id,
                "qty": canonical_qty(it.qty),
                "unit": it.unit,
                "shelf_price_paise": it.shelf_price_paise,
                "offer_id": it.offer_id,
                "discount_paise": it.discount_paise,
                "unit_price_paise": it.unit_price_paise,
                "tax_rate_bp": it.tax_rate_bp,
                "entry_method": it.entry_method,
                "is_open_item": bool(it.is_open_item),
                "open_item_name": it.open_item_name,
            }
            for it in sale.items
        ],
        "payments": [{"mode": p.mode, "amount_paise": p.amount_paise} for p in sale.payments],
        "subtotal_paise": sale.subtotal_paise,
        "item_discount_paise": sale.item_discount_paise,
        "bill_discount_paise": sale.bill_discount_paise,
        "taxable_paise": sale.taxable_paise,
        "cgst_paise": sale.cgst_paise,
        "sgst_paise": sale.sgst_paise,
        "roundoff_paise": sale.roundoff_paise,
        "total_paise": sale.total_paise,
        "prev_hash": sale.prev_hash,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sale_hash(sale: SaleRecord) -> str:
    """``sha256(canonical_sale(sale))`` as hex."""
    return hashlib.sha256(canonical_sale(sale).encode("utf-8")).hexdigest()


def verify_sales_chain(sales: Iterable[SaleRecord], *, expected_prev: str = GENESIS_HASH) -> int | None:
    """Return the id of the first sale whose hash or link is wrong, or ``None`` when intact."""
    prev = expected_prev
    for sale in sales:
        if sale.prev_hash != prev or sale_hash(sale) != sale.hash:
            return sale.id
        prev = sale.hash
    return None


# ----------------------------------------------------------------------------- reports
@dataclass(frozen=True)
class ChainReport:
    """Result of the sales chain check."""

    ok: bool
    checked: int
    head: str
    first_bad_id: int | None = None
    expected: str = ""
    found: str = ""
    detail: str = ""
    code: str = ""


@dataclass(frozen=True)
class GapReport:
    """Result of the invoice continuity check."""

    ok: bool
    counts: dict[str, tuple[int, int]]
    """``{fin_year: (COUNT(*), MAX(seq_no))}``."""
    gaps: dict[str, list[tuple[int, int]]]
    """``{fin_year: [(last seen, next seen), …]}``."""
    missing: dict[str, list[int]]
    detail: str = ""
    code: str = ""


@dataclass(frozen=True)
class StockMismatch:
    """One reconciliation difference."""

    kind: str  # SUM_VS_CACHED | SALE_LINES_VS_MOVEMENTS
    product_id: int
    expected: str
    found: str


@dataclass(frozen=True)
class StockReport:
    """Result of the stock reconciliation."""

    ok: bool
    products_checked: int
    day: str
    mismatches: tuple[StockMismatch, ...] = ()
    detail: str = ""
    code: str = ""


@dataclass(frozen=True)
class AuditReport:
    """Result of the audit-log chain check."""

    ok: bool
    checked: int
    first_bad_id: int | None = None
    detail: str = ""
    code: str = ""


Report = ChainReport | GapReport | StockReport | AuditReport


@dataclass(frozen=True)
class IntegrityStatus:
    """Everything the hourly job / banner / doctor needs from one pass."""

    ok: bool
    at: str
    head: str
    reports: dict[str, Report] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        """Error codes of the failed checks, in check order."""
        return [r.code for r in self.reports.values() if not r.ok and r.code]

    def summary(self) -> str:
        """One line for logs and the status page."""
        parts = [f"{kind}={'ok' if r.ok else r.code}" for kind, r in self.reports.items()]
        return " ".join(parts) + f" head={self.head[:12]}"


# ----------------------------------------------------------------------------- store protocol
class LedgerStore(Protocol):
    """What the service needs from the database (ORM repo or raw sqlite3)."""

    def iter_sales(self, session: Session) -> Iterator[SaleRecord]:  # pragma: no cover - protocol
        """Every sale with its items and payments, ``ORDER BY id``."""
        ...

    def last_sale_hash(self, session: Session) -> str:  # pragma: no cover - protocol
        """Hash of the newest sale by id, or :data:`GENESIS_HASH`."""
        ...

    def invoice_sequences(self, session: Session) -> dict[str, list[int]]:  # pragma: no cover - protocol
        """``{fin_year: sorted seq_no list}`` (every status counts — a voided bill keeps its number)."""
        ...

    def stock_sums(self, session: Session) -> dict[int, tuple[str, str | None]]:  # pragma: no cover - protocol
        """``{product_id: (Σ qty_delta as text, cached qty as text or None)}`` for non-system products with movements or stock."""
        ...

    def day_sale_lines(self, session: Session, day: str) -> dict[int, str]:  # pragma: no cover - protocol
        """``{product_id: Σ qty}`` of COMPLETED sale lines sold on ``day`` (IST), open items excluded."""
        ...

    def day_sale_movements(self, session: Session, day: str) -> dict[int, str]:  # pragma: no cover - protocol
        """``{product_id: -Σ qty_delta}`` of SALE movements created on ``day`` (IST)."""
        ...

    def iter_audit(self, session: Session) -> Iterator[AuditEntry]:  # pragma: no cover - protocol
        """Every audit row ``ORDER BY id``."""
        ...

    def record_check(self, session: Session, kind: str, ok: bool, details: str, at: str) -> None:  # pragma: no cover - protocol
        """Append an ``integrity_check`` row."""
        ...


# ----------------------------------------------------------------------------- service
class LedgerService:
    """Hash chain, invoice continuity and stock reconciliation."""

    def __init__(self, store: LedgerStore) -> None:
        self._store = store

    # -- chain ------------------------------------------------------------------------------
    def chain_head(self, session: Session) -> str:
        """Hash of the newest sale (what the Z-report prints and the backup manifest records)."""
        return self._store.last_sale_hash(session)

    def hash_for_new_sale(self, session: Session, sale: SaleRecord) -> SaleRecord:
        """Fill ``prev_hash`` from the current head and compute ``hash`` — called by checkout inside its
        ``BEGIN IMMEDIATE`` transaction so two counters cannot both chain onto the same head."""
        linked = replace(sale, prev_hash=self.chain_head(session))
        return replace(linked, hash=sale_hash(linked))

    def verify_sales(self, session: Session) -> ChainReport:
        """Walk every sale by id; stop at the first broken hash or link."""
        prev = GENESIS_HASH
        checked = 0
        for sale in self._store.iter_sales(session):
            checked += 1
            expected = sale_hash(sale)
            if sale.prev_hash != prev:
                detail = f"prev_hash mismatch: stored={sale.prev_hash[:12]} expected={prev[:12]} (a sale before it was removed or reordered)"
                log.error("INTEGRITY_FAIL check=CHAIN sale_id=%s invoice=%s %s code=%s", sale.id, sale.invoice_no, detail, CODE_CHAIN)
                return ChainReport(False, checked, prev, sale.id, prev, sale.prev_hash, detail, CODE_CHAIN)
            if expected != sale.hash:
                detail = f"hash mismatch for {sale.invoice_no}: a field of the sale was changed after it was saved"
                log.error("INTEGRITY_FAIL check=CHAIN sale_id=%s invoice=%s expected=%s found=%s code=%s", sale.id, sale.invoice_no, expected, sale.hash, CODE_CHAIN)
                return ChainReport(False, checked, prev, sale.id, expected, sale.hash, detail, CODE_CHAIN)
            prev = sale.hash
        log.info("INTEGRITY_OK check=CHAIN sales=%d head=%s", checked, prev[:12])
        return ChainReport(True, checked, prev)

    # -- gaps -------------------------------------------------------------------------------
    def verify_gaps(self, session: Session) -> GapReport:
        """``COUNT(*) == MAX(seq_no)`` per financial year, with the missing numbers listed."""
        sequences = self._store.invoice_sequences(session)
        counts: dict[str, tuple[int, int]] = {}
        gaps: dict[str, list[tuple[int, int]]] = {}
        missing: dict[str, list[int]] = {}
        for fy, seqs in sorted(sequences.items()):
            ordered = sorted(set(seqs))
            counts[fy] = (len(seqs), ordered[-1] if ordered else 0)
            fy_gaps: list[tuple[int, int]] = []
            fy_missing: list[int] = []
            last = 0
            for seq in ordered:
                if seq != last + 1:
                    fy_gaps.append((last, seq))
                    fy_missing.extend(range(last + 1, seq))
                last = seq
            if fy_gaps:
                gaps[fy] = fy_gaps
                missing[fy] = fy_missing
        if gaps:
            detail = "; ".join(f"{fy}: missing {', '.join(str(n) for n in nums[:20])}{' …' if len(nums) > 20 else ''}" for fy, nums in missing.items())
            for fy, pairs in gaps.items():
                for before, after in pairs:
                    log.error("INTEGRITY_FAIL check=GAPS fy=%s before=%s after=%s code=%s", fy, before, after, CODE_GAPS)
            return GapReport(False, counts, gaps, missing, detail, CODE_GAPS)
        log.info("INTEGRITY_OK check=GAPS years=%s", ",".join(f"{fy}:{c[0]}" for fy, c in counts.items()) or "-")
        return GapReport(True, counts, {}, {})

    # -- stock ------------------------------------------------------------------------------
    def reconcile_stock(self, session: Session, *, day: str | None = None) -> StockReport:
        """Σ movements vs cached qty for every product; the day's sale lines vs SALE movements."""
        day = day or today_ist().isoformat()
        mismatches: list[StockMismatch] = []
        sums = self._store.stock_sums(session)
        for product_id, (moved, cached) in sorted(sums.items()):
            expected = canonical_qty(moved)
            found = canonical_qty(cached) if cached is not None else "none"
            if expected != found:
                mismatches.append(StockMismatch("SUM_VS_CACHED", product_id, expected, found))
        lines = self._store.day_sale_lines(session, day)
        movements = self._store.day_sale_movements(session, day)
        for product_id in sorted(set(lines) | set(movements)):
            sold = canonical_qty(lines.get(product_id, "0"))
            moved = canonical_qty(movements.get(product_id, "0"))
            if sold != moved:
                mismatches.append(StockMismatch("SALE_LINES_VS_MOVEMENTS", product_id, sold, moved))
        if mismatches:
            for m in mismatches:
                log.error("INTEGRITY_FAIL check=STOCK kind=%s product=%s expected=%s found=%s day=%s code=%s", m.kind, m.product_id, m.expected, m.found, day, CODE_STOCK)
            detail = "; ".join(f"{m.kind} product {m.product_id}: expected {m.expected}, found {m.found}" for m in mismatches[:10])
            return StockReport(False, len(sums), day, tuple(mismatches), detail, CODE_STOCK)
        log.info("INTEGRITY_OK check=STOCK products=%d day=%s", len(sums), day)
        return StockReport(True, len(sums), day)

    # -- audit ------------------------------------------------------------------------------
    def verify_audit(self, session: Session) -> AuditReport:
        """The audit-log chain (``services.audit.verify_chain``), reported like the others."""
        entries = list(self._store.iter_audit(session))
        broken = verify_audit_chain(entries)
        if broken is not None:
            detail = f"audit_log row {broken} does not match its chain (edited or a row before it deleted)"
            log.error("INTEGRITY_FAIL check=AUDIT row=%s code=%s", broken, CODE_CHAIN)
            return AuditReport(False, len(entries), broken, detail, CODE_CHAIN)
        log.info("INTEGRITY_OK check=AUDIT rows=%d", len(entries))
        return AuditReport(True, len(entries))

    # -- all --------------------------------------------------------------------------------
    def run_all(self, session: Session, *, day: str | None = None, record: bool = True) -> IntegrityStatus:
        """Every check, none skipped after a failure; ``integrity_check`` rows written for CHAIN/GAPS/STOCK."""
        at = iso_utc()
        reports: dict[str, Report] = {}
        reports["CHAIN"] = chain = self.verify_sales(session)
        reports["GAPS"] = gaps = self.verify_gaps(session)
        reports["STOCK"] = stock = self.reconcile_stock(session, day=day)
        reports["AUDIT"] = self.verify_audit(session)
        if record:
            self._store.record_check(session, "CHAIN", chain.ok, _details(chain), at)
            self._store.record_check(session, "GAPS", gaps.ok, _details(gaps), at)
            self._store.record_check(session, "STOCK", stock.ok, _details(stock), at)
        ok = all(r.ok for r in reports.values())
        status = IntegrityStatus(ok, at, chain.head, reports)
        log.log(logging.INFO if ok else logging.ERROR, "INTEGRITY_%s %s", "OK" if ok else "FAIL", status.summary())
        return status


def _details(report: Report) -> str:
    data: dict[str, Any] = {"ok": report.ok}
    if isinstance(report, ChainReport):
        data.update(checked=report.checked, head=report.head, first_bad_id=report.first_bad_id)
    elif isinstance(report, GapReport):
        data.update(counts=report.counts, missing=report.missing)
    elif isinstance(report, StockReport):
        data.update(products=report.products_checked, day=report.day, mismatches=[m.__dict__ for m in report.mismatches[:50]])
    if report.detail:
        data["detail"] = report.detail
    return json.dumps(data, ensure_ascii=False, default=str)


def summarise_codes(reports: Sequence[Report]) -> list[str]:
    """Distinct error codes from a set of reports (for the status chip)."""
    seen: list[str] = []
    for r in reports:
        if not r.ok and r.code and r.code not in seen:
            seen.append(r.code)
    return seen
