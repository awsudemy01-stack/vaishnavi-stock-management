"""ImportExportService — products in and out as CSV (docs/09 §5, docs/13 Phase 2; stdlib ``csv``).

Preview first, apply second: every line is validated into ``ImportRow`` (plain-language errors per
column); ``apply()`` creates new products (auto code, internal barcode, opening stock) or updates existing
ones matched by ``sku`` / ``barcode`` (prices only — a re-import never touches stock). Price changes carry
``source = IMPORT`` into the price history. Export writes UTF-8 **with BOM** so Excel shows Hindi.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from vaishnavi.core import barcode as bc
from vaishnavi.core.money import parse_inr, rate_to_bp
from vaishnavi.core.permissions import Actor, Permission, require
from vaishnavi.services.catalog import CatalogService, ProductInput
from vaishnavi.services.inventory import InventoryService

log = logging.getLogger("vaishnavi.services.import_export")

COLUMNS: Final = ("sku", "name", "name_hi", "category", "price", "mrp", "cost", "tax_rate", "hsn", "unit", "sale_mode", "barcode", "opening_stock", "min_stock", "tags")
EXPORT_COLUMNS: Final = (*COLUMNS, "internal_barcode", "stock", "id")
SALE_MODES: Final = {"PIECE": ("pcs", "pc"), "WEIGHT": ("g", "g"), "VOLUME": ("ml", "ml"), "LENGTH": ("cm", "cm")}


@dataclass
class ImportRow:
    """One CSV line after validation."""

    line_no: int
    raw: dict[str, str]
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    existing_id: int | None = None

    @property
    def ok(self) -> bool:
        """Importable."""
        return not self.errors


@dataclass(frozen=True)
class ImportPreview:
    """What the owner sees before confirming."""

    rows: list[ImportRow]
    columns: list[str]

    @property
    def total(self) -> int:
        """Lines."""
        return len(self.rows)

    @property
    def ok_count(self) -> int:
        """Importable lines."""
        return sum(1 for r in self.rows if r.ok)

    @property
    def error_count(self) -> int:
        """Lines with problems."""
        return self.total - self.ok_count


@dataclass(frozen=True)
class ImportResult:
    """Outcome of ``apply``."""

    created: int
    updated: int
    skipped: int
    product_ids: list[int]


def _money(text: str, column: str, errors: list[str]) -> int | None:
    text = text.strip()
    if not text:
        return None
    try:
        return parse_inr(text)
    except Exception:  # noqa: BLE001 - any parse problem is a user error
        errors.append(f"{column}: not an amount ({text!r})")
        return None


def _decimal(text: str, column: str, errors: list[str]) -> Decimal | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        errors.append(f"{column}: not a number ({text!r})")
        return None
    if value < 0:
        errors.append(f"{column}: must not be negative")
        return None
    return value


class ImportExportService:
    """CSV in / out."""

    def __init__(self, catalog: CatalogService, inventory: InventoryService) -> None:
        self._catalog = catalog
        self._inventory = inventory

    # -- preview ----------------------------------------------------------------------------------
    def preview_csv(self, text: str) -> ImportPreview:
        """Parse + validate; nothing is written."""
        text = text.lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(text))
        columns = [c.strip().lower() for c in (reader.fieldnames or [])]
        categories = {c.name.lower(): c.id for c in self._catalog.categories()}
        rows: list[ImportRow] = []
        for idx, raw in enumerate(reader, start=2):
            clean = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            row = ImportRow(line_no=idx, raw=clean)
            self._validate(row, categories)
            rows.append(row)
        log.info("IMPORT_PREVIEW lines=%d ok=%d errors=%d", len(rows), sum(r.ok for r in rows), sum(not r.ok for r in rows))
        return ImportPreview(rows, columns)

    def _validate(self, row: ImportRow, categories: dict[str, int]) -> None:
        r, e, d = row.raw, row.errors, row.data
        name = r.get("name", "")
        if not name:
            e.append("name: required")
        d["name"] = name
        d["name_hi"] = r.get("name_hi") or None
        d["sku"] = (r.get("sku") or "").upper() or None
        cat = r.get("category", "")
        if cat:
            if cat.lower() in categories:
                d["category_id"] = categories[cat.lower()]
            else:
                e.append(f"category: unknown ({cat!r}) — create it first or leave the column empty")
        else:
            d["category_id"] = None
        if "price" in r:
            price = _money(r.get("price", ""), "price", e)
            if price is None and r.get("price", "") == "":
                e.append("price: required")
            d["price_paise"] = price
        d["mrp_paise"] = _money(r.get("mrp", ""), "mrp", e)
        d["cost_paise"] = _money(r.get("cost", ""), "cost", e)
        tax = r.get("tax_rate", "")
        if tax:
            try:
                d["tax_rate_bp"] = rate_to_bp(tax.rstrip("%"))
            except Exception:  # noqa: BLE001
                e.append(f"tax_rate: not a percentage ({tax!r})")
        d["hsn_code"] = r.get("hsn") or None
        mode = (r.get("sale_mode") or "PIECE").upper()
        if mode not in SALE_MODES:
            e.append(f"sale_mode: one of {', '.join(SALE_MODES)}")
            mode = "PIECE"
        d["sale_mode"] = mode
        d["unit"] = r.get("unit") or SALE_MODES[mode][1]
        code = r.get("barcode", "")
        if code:
            problem = bc.validate(code)
            if problem:
                e.append(f"barcode: {problem}")
            else:
                d["barcode"] = bc.normalise(code)
        d["opening_stock"] = _decimal(r.get("opening_stock", ""), "opening_stock", e)
        d["min_stock"] = _decimal(r.get("min_stock", ""), "min_stock", e) or Decimal("0")
        d["tags"] = [t.strip() for t in (r.get("tags") or "").replace(";", ",").split(",") if t.strip()]
        # match an existing product
        existing = self._catalog.by_sku(d["sku"]) if d["sku"] else None
        if existing is None and d.get("barcode"):
            hit = self._catalog.resolve_barcode(d["barcode"])
            existing = hit.product if hit else None
        row.existing_id = existing.id if existing else None

    # -- apply ------------------------------------------------------------------------------------
    def apply(self, actor: Actor, preview: ImportPreview) -> ImportResult:
        """Create / update every importable line (Owner only)."""
        require(actor, Permission.MANAGE_PRODUCTS)
        created = updated = skipped = 0
        ids: list[int] = []
        for row in preview.rows:
            if not row.ok:
                skipped += 1
                continue
            d = row.data
            if row.existing_id is not None:
                product = self._catalog.get(row.existing_id)
                if product is None:
                    skipped += 1
                    continue
                changes = {k: v for k, v in {"name": d["name"], "name_hi": d["name_hi"], "price_paise": d.get("price_paise"), "mrp_paise": d["mrp_paise"], "cost_paise": d["cost_paise"], "tax_rate_bp": d.get("tax_rate_bp"), "hsn_code": d["hsn_code"], "min_stock": d["min_stock"]}.items() if v is not None and getattr(product, k) != v}
                if "name_hi" in changes and not d["name_hi"]:
                    changes.pop("name_hi")
                if changes:
                    self._catalog.update(actor, product.id, row_version=product.row_version, changes=changes, reason=f"CSV import line {row.line_no}", source="IMPORT")
                if d.get("tags"):
                    self._catalog.set_tags(actor, product.id, d["tags"])
                updated += 1
                ids.append(product.id)
                continue
            if d.get("price_paise") is None:
                skipped += 1
                continue
            base_unit = SALE_MODES[d["sale_mode"]][0]
            product = self._catalog.create(
                actor,
                ProductInput(
                    name=d["name"], name_hi=d["name_hi"], sku=d["sku"], category_id=d["category_id"], price_paise=d["price_paise"], mrp_paise=d["mrp_paise"], cost_paise=d["cost_paise"],
                    tax_rate_bp=d.get("tax_rate_bp", 0), hsn_code=d["hsn_code"], unit=d["unit"], sale_mode=d["sale_mode"], base_unit=base_unit, pricing_unit_label=d["unit"],
                    min_stock=d["min_stock"], manufacturer_barcode=d.get("barcode"), tags=d["tags"], allow_decimal_qty=d["sale_mode"] != "PIECE",
                ),
                source="IMPORT",
                reason=f"CSV import line {row.line_no}",
            )
            if d.get("opening_stock"):
                self._inventory.opening_stock(actor, product.id, d["opening_stock"])
            created += 1
            ids.append(product.id)
        log.info("IMPORT_APPLIED created=%d updated=%d skipped=%d by=%s", created, updated, skipped, actor.user_id)
        return ImportResult(created, updated, skipped, ids)

    # -- export -----------------------------------------------------------------------------------
    def export_csv(self, *, include_archived: bool = False) -> str:
        """All products as CSV (UTF-8 BOM for Excel); re-importable."""
        page = self._catalog.list_products(include_archived=include_archived, limit=100000)
        categories = {c.id: c.name for c in self._catalog.categories()}
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(EXPORT_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for p in page.items:
            mfr = next((b.barcode for b in self._catalog.barcodes(p.id) if b.kind == "MANUFACTURER"), "")
            writer.writerow({
                "sku": p.sku, "name": p.name, "name_hi": p.name_hi or "", "category": categories.get(p.category_id or -1, ""), "price": f"{p.price_paise / 100:.2f}",
                "mrp": f"{p.mrp_paise / 100:.2f}" if p.mrp_paise is not None else "", "cost": f"{p.cost_paise / 100:.2f}" if p.cost_paise is not None else "",
                "tax_rate": f"{p.tax_rate_bp / 100:g}", "hsn": p.hsn_code or "", "unit": p.unit, "sale_mode": p.sale_mode, "barcode": mfr, "opening_stock": "",
                "min_stock": f"{p.min_stock:f}", "tags": ";".join(self._catalog.tags_of(p.id)), "internal_barcode": p.internal_barcode or "", "stock": f"{p.stock_qty:f}", "id": p.id,
            })
        log.info("EXPORT_CSV products=%d", len(page.items))
        return "\ufeff" + out.getvalue()
