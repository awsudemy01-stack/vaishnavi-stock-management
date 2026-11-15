"""LabelService — label models from the catalogue, print jobs (ESC/POS strip · A4 sheet), the print log (docs/11 §2; stdlib).

Who may print: the Owner prints labels for new / restocked items (``MANAGE_PRODUCTS``); staff may print a
single **replacement** label from F3 Find (``PRINT_LABEL``) — the only label action a cashier has.
Every job is logged per product (``label_print_log``) and audited (``LABEL_PRINTED`` / ``LABEL_REPRINTED``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Protocol

from vaishnavi.core.clock import iso_utc
from vaishnavi.core.money import format_inr
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.db.records import ProductRecord
from vaishnavi.services.audit import AuditService
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.services.labels")

TARGETS: Final = ("ESCPOS", "PDF", "TSPL")
REASONS: Final = ("NEW", "RESTOCK", "REPLACEMENT")


@dataclass(frozen=True)
class LabelModel:
    """Everything printed on one label (rendered by ``hardware/labels.py``)."""

    name: str
    name_hi: str | None
    sku: str
    product_id: int
    barcode: str
    price_paise: int
    mrp_paise: int | None = None

    @property
    def code_line(self) -> str:
        """``CAM-0050 · #1042``."""
        return f"{self.sku} · #{self.product_id}"

    @property
    def price_text(self) -> str:
        """``₹25.00``."""
        return format_inr(self.price_paise)


class LabelRenderer(Protocol):
    """Implemented by the ``vaishnavi.hardware.labels`` module (injected — services never import hardware)."""

    def render_escpos_batch(self, models: list[LabelModel], *, paper_mm: int = 80) -> bytes: ...  # pragma: no cover - protocol
    def render_html_sheet(self, models: list[LabelModel], *, layout: str = "65-up") -> str: ...  # pragma: no cover - protocol
    def render_pdf_sheet(self, models: list[LabelModel], *, layout: str = "65-up") -> bytes | None: ...  # pragma: no cover - protocol


class LabelStore(Protocol):
    """``label_print_log``."""

    def insert_print(self, session: Session, *, product_id: int, copies: int, target: str, reason: str, user_id: int, at: str) -> int: ...  # pragma: no cover - protocol
    def recent(self, session: Session, *, limit: int) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol


class ProductReader(Protocol):
    """The catalogue slice needed here."""

    def product_by_id(self, session: Session, product_id: int) -> ProductRecord | None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class PrintJob:
    """What goes to the printer / browser."""

    target: str
    copies: int
    products: int
    payload: bytes
    content_type: str
    filename: str


def model_for(product: ProductRecord) -> LabelModel:
    """Product → label (the internal code is derived from the id when the column is empty — a label can always be recreated)."""
    from vaishnavi.core.barcode import internal_ean13  # noqa: PLC0415

    return LabelModel(name=product.name, name_hi=product.name_hi, sku=product.sku, product_id=product.id, barcode=product.internal_barcode or internal_ean13(product.id), price_paise=product.price_paise, mrp_paise=product.mrp_paise)


class LabelService:
    """Labels."""

    def __init__(self, txn: TxnFactory, store: LabelStore, audit: AuditService, *, products: ProductReader, renderer: LabelRenderer) -> None:
        self._txn = txn
        self._store = store
        self._audit = audit
        self._products = products
        self._renderer = renderer

    def models_for(self, product_ids: list[int], *, copies: int = 1) -> list[LabelModel]:
        """``copies`` labels per product, in the given order; unknown / system products are skipped."""
        out: list[LabelModel] = []
        with self._txn(label="labels.models") as session:
            for pid in product_ids:
                product = self._products.product_by_id(session, pid)
                if product is None or product.is_system:
                    log.debug("LABEL_SKIPPED product=%s", pid)
                    continue
                out.extend([model_for(product)] * max(1, copies))
        return out

    def print_job(self, actor: Actor, *, product_ids: list[int], copies: int, target: str, reason: str, paper_mm: int = 80, layout: str = "65-up") -> PrintJob:
        """Render + log. Staff: one REPLACEMENT label at a time."""
        if target not in TARGETS or reason not in REASONS:
            raise ValueError("bad target or reason")
        if copies < 1:
            raise ValueError("copies must be >= 1")
        if reason == "REPLACEMENT" and copies == 1 and len(product_ids) == 1:
            require(actor, Permission.PRINT_LABEL)  # a cashier fixing a torn label from F3 Find
        else:
            require(actor, Permission.MANAGE_PRODUCTS)  # Owner-only (DENY for staff in the matrix)
        models = self.models_for(product_ids, copies=copies)
        if target == "ESCPOS":
            payload, ctype, name = self._renderer.render_escpos_batch(models, paper_mm=paper_mm), "application/octet-stream", "labels.escpos"
        elif target == "PDF":
            pdf = self._renderer.render_pdf_sheet(models, layout=layout)
            if pdf is not None:
                payload, ctype, name = pdf, "application/pdf", "labels.pdf"
            else:
                payload, ctype, name = self._renderer.render_html_sheet(models, layout=layout).encode("utf-8"), "text/html; charset=utf-8", "labels.html"
        else:
            raise ValueError("TSPL label printers arrive in a later phase")
        now = iso_utc()
        action = "LABEL_REPRINTED" if reason == "REPLACEMENT" else "LABEL_PRINTED"
        with self._txn(label="labels.log") as session:
            for pid in dict.fromkeys(product_ids):
                if any(m.product_id == pid for m in models):
                    self._store.insert_print(session, product_id=pid, copies=copies, target=target, reason=reason, user_id=actor.user_id, at=now)
                    self._audit.record(session, action, user_id=actor.user_id, entity="product", entity_id=pid, after={"copies": copies, "target": target, "reason": reason})
        log.info("%s products=%d copies=%d target=%s by=%s bytes=%d", action, len(set(product_ids)), copies, target, actor.user_id, len(payload))
        return PrintJob(target, copies, len(set(product_ids)), payload, ctype, name)

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The print log."""
        with self._txn(label="labels.recent") as session:
            return self._store.recent(session, limit=limit)
