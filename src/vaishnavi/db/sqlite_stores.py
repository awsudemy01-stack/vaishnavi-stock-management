"""Stdlib ``sqlite3`` implementations of the service store protocols (no ORM).

Why they exist: the first-day setup (``init-db``), the lock-out escape hatch (``reset-owner-password``),
the integrity check (``verify-chain``) and ``backup`` must work even when the SQLAlchemy wheel is
broken — and they must be testable on a machine with nothing installed. The running application uses
the ORM repositories for the same protocols; both produce the same rows because the schema's
constraints and triggers do the enforcing.

``SqliteTxn`` is the ``TxnFactory``: ``BEGIN IMMEDIATE`` (single writer, gap-free counters), commit on
success, rollback on any exception, connection closed explicitly (Windows file locks).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from vaishnavi.core.barcode import parse_receipt_ean13
from vaishnavi.core.logging import current_cid
from vaishnavi.core.permissions import Role
from vaishnavi.db.records import GENESIS_HASH, AuditEntry, BarcodeRecord, CashEventRecord, CashierDayRecordRow, CategoryRecord, ChatMessageRecord, ChatSecurityEventRecord, CustomerRecord, DayRecordItem, ForecastSnapshotRecord, HeldCartRecord, MovementRecord, OfferRecord, OverrideRecord, PaymentView, PriceHistoryRecord, ProductRecord, PromptVersionRecord, PublishQueueRecord as QueueItem, PurchaseRecord, SaleItemView, SaleView, ShiftRecord, StockCountLineRecord, StockCountRecord, SupplierRecord, UserRecord, WorkSessionRecord

log = logging.getLogger("vaishnavi.db.sqlite_stores")


# ----------------------------------------------------------------------------- transaction factory
class SqliteTxn:
    """``with txn(label="…") as conn:`` on a database file."""

    def __init__(self, database: Path, *, timeout: float = 5.0) -> None:
        self.database = Path(database)
        self.timeout = timeout

    @contextmanager
    def __call__(self, *, label: str = "") -> Iterator[sqlite3.Connection]:
        """Writer transaction; the connection's row factory is left as tuples."""
        conn = sqlite3.connect(self.database, timeout=self.timeout, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            # a damaged/locked file must not leak the handle (Windows keeps the file busy otherwise)
            conn.close()
            raise
        log.debug("SQLITE_TXN_BEGIN cid=%s label=%s", current_cid(), label)
        try:
            yield conn
            conn.execute("COMMIT")
            log.debug("SQLITE_TXN_COMMIT label=%s", label)
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            log.warning("SQLITE_TXN_ROLLBACK label=%s error=%r", label, exc)
            raise
        finally:
            conn.close()


def _row_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# ----------------------------------------------------------------------------- users / recovery
_USER_SQL = "SELECT u.id, u.username, u.display_name, r.name, u.password_hash, u.owner_pin_hash, u.is_active, u.must_change_password, u.failed_logins, u.locked_until, u.last_login_at, u.created_at FROM user u JOIN role r ON r.id = u.role_id"
_USER_UPDATABLE = frozenset({"display_name", "password_hash", "owner_pin_hash", "is_active", "must_change_password", "failed_logins", "locked_until", "last_login_at"})


def _user(row: tuple[Any, ...]) -> UserRecord:
    return UserRecord(int(row[0]), str(row[1]), str(row[2]), Role(str(row[3])), str(row[4]), row[5], bool(row[6]), bool(row[7]), int(row[8]), row[9], row[10], str(row[11]))


class SqliteUserStore:
    """``services.user.UserStore``."""

    def by_username(self, conn: sqlite3.Connection, username: str) -> UserRecord | None:
        """Case-insensitive (column collation)."""
        row = conn.execute(_USER_SQL + " WHERE u.username = ? COLLATE NOCASE", (username,)).fetchone()
        return _user(row) if row else None

    def by_id(self, conn: sqlite3.Connection, user_id: int) -> UserRecord | None:
        """By id."""
        row = conn.execute(_USER_SQL + " WHERE u.id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def list_all(self, conn: sqlite3.Connection) -> list[UserRecord]:
        """Owners first."""
        return [_user(r) for r in conn.execute(_USER_SQL + " ORDER BY r.id, u.username").fetchall()]

    def active_owners(self, conn: sqlite3.Connection) -> list[UserRecord]:
        """Active OWNER accounts."""
        return [_user(r) for r in conn.execute(_USER_SQL + " WHERE r.name = 'OWNER' AND u.is_active = 1 ORDER BY u.id").fetchall()]

    def create(self, conn: sqlite3.Connection, *, username: str, display_name: str, role: Role, password_hash: str, must_change_password: bool, at: str) -> UserRecord:
        """Insert."""
        cur = conn.execute(
            "INSERT INTO user(username, display_name, role_id, password_hash, must_change_password, created_at) VALUES (?, ?, (SELECT id FROM role WHERE name = ?), ?, ?, ?)",
            (username, display_name, role.value, password_hash, 1 if must_change_password else 0, at),
        )
        user = self.by_id(conn, int(cur.lastrowid))
        assert user is not None
        return user

    def update(self, conn: sqlite3.Connection, user_id: int, **fields: Any) -> None:
        """Whitelisted columns only."""
        unknown = set(fields) - _USER_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not fields:
            return
        values = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()}
        assignments = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE user SET {assignments} WHERE id = ?", (*values.values(), user_id))  # noqa: S608 (whitelisted names)


class SqliteRecoveryStore:
    """``services.recovery.RecoveryStore``."""

    def current(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """Newest unused key."""
        rows = _row_dicts(conn, "SELECT id, key_hash, issued_at, acknowledged_at, used_at FROM recovery_key WHERE used_at IS NULL ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def insert(self, conn: sqlite3.Connection, key_hash: str, issued_at: str) -> int:
        """Insert → id."""
        return int(conn.execute("INSERT INTO recovery_key(key_hash, issued_at) VALUES (?, ?)", (key_hash, issued_at)).lastrowid)

    def update(self, conn: sqlite3.Connection, key_id: int, **fields: Any) -> None:
        """acknowledged_at / used_at."""
        unknown = set(fields) - {"acknowledged_at", "used_at"}
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE recovery_key SET {assignments} WHERE id = ?", (*fields.values(), key_id))  # noqa: S608


# ----------------------------------------------------------------------------- audit
class SqliteAuditStore:
    """``services.audit.AuditStore`` + iteration for verification."""

    def last_hash(self, conn: sqlite3.Connection) -> str:
        """Newest hash or genesis."""
        row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        return str(row[0]) if row else GENESIS_HASH

    def append(self, conn: sqlite3.Connection, entry: AuditEntry) -> AuditEntry:
        """Insert → entry with id."""
        cur = conn.execute(
            "INSERT INTO audit_log(at, user_id, actor_user_id, action, entity, entity_id, before, after, correlation_id, ip, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.at, entry.user_id, entry.actor_user_id, entry.action, entry.entity, entry.entity_id, entry.before, entry.after, entry.correlation_id, entry.ip, entry.prev_hash, entry.hash),
        )
        return replace(entry, id=int(cur.lastrowid))

    def iter_all(self, conn: sqlite3.Connection) -> Iterator[AuditEntry]:
        """ORDER BY id."""
        for r in conn.execute("SELECT id, at, user_id, actor_user_id, action, entity, entity_id, before, after, correlation_id, ip, prev_hash, hash FROM audit_log ORDER BY id").fetchall():
            yield AuditEntry(id=int(r[0]), at=r[1], user_id=r[2], actor_user_id=r[3], action=r[4], entity=r[5], entity_id=r[6], before=r[7], after=r[8], correlation_id=r[9], ip=r[10], prev_hash=r[11], hash=r[12])


# ----------------------------------------------------------------------------- settings
class SqliteSettingStore:
    """``services.settings.SettingStore`` + ``services.secure_settings.SecureSettingStore``."""

    def all(self, conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
        """{key: (value, type)}."""
        return {str(k): (str(v), str(t)) for k, v, t in conn.execute("SELECT key, value, type FROM setting").fetchall()}

    def upsert(self, conn: sqlite3.Connection, key: str, value: str, type_: str, *, updated_by: int | None, at: str) -> None:
        """Insert or replace."""
        conn.execute("INSERT INTO setting(key, value, type, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, type = excluded.type, updated_by = excluded.updated_by, updated_at = excluded.updated_at", (key, value, type_, updated_by, at))

    def delete(self, conn: sqlite3.Connection, key: str) -> bool:
        """Remove."""
        return conn.execute("DELETE FROM setting WHERE key = ?", (key,)).rowcount > 0

    def secure_get(self, conn: sqlite3.Connection, key: str) -> tuple[bytes, str] | None:
        """(ciphertext, preview)."""
        row = conn.execute("SELECT ciphertext, masked_preview FROM secure_setting WHERE key = ?", (key,)).fetchone()
        return (bytes(row[0]), str(row[1])) if row else None

    def secure_previews(self, conn: sqlite3.Connection) -> dict[str, str]:
        """{key: preview}."""
        return {str(k): str(p) for k, p in conn.execute("SELECT key, masked_preview FROM secure_setting").fetchall()}

    def secure_upsert(self, conn: sqlite3.Connection, key: str, ciphertext: bytes, preview: str, *, updated_by: int | None, at: str) -> None:
        """Insert or replace."""
        conn.execute("INSERT INTO secure_setting(key, ciphertext, masked_preview, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET ciphertext = excluded.ciphertext, masked_preview = excluded.masked_preview, updated_by = excluded.updated_by, updated_at = excluded.updated_at", (key, ciphertext, preview, updated_by, at))

    def secure_delete(self, conn: sqlite3.Connection, key: str) -> bool:
        """Remove."""
        return conn.execute("DELETE FROM secure_setting WHERE key = ?", (key,)).rowcount > 0


# ----------------------------------------------------------------------------- system / notifications
class SqliteSystemStore:
    """identity · setup steps · counters."""

    def identity(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """The db_identity row."""
        rows = _row_dicts(conn, "SELECT instance_id, created_at, app_version, schema_version, last_open_at, last_clean_close_at, backup_id, backup_taken_at, restored_at, restored_from FROM db_identity LIMIT 1")
        return rows[0] if rows else None

    def setup_steps(self, conn: sqlite3.Connection) -> dict[str, str | None]:
        """{step: done_at}."""
        return {str(s): d for s, d in conn.execute("SELECT step, done_at FROM setup_step").fetchall()}

    def mark_setup_step(self, conn: sqlite3.Connection, step: str, *, done_at: str | None, done_by: int | None) -> bool:
        """Set/clear; False when unknown."""
        return conn.execute("UPDATE setup_step SET done_at = ?, done_by = ? WHERE step = ?", (done_at, done_by, step)).rowcount > 0

    def next_counter(self, conn: sqlite3.Connection, name: str) -> int:
        """Increment and return (inside the caller's BEGIN IMMEDIATE)."""
        conn.execute("INSERT INTO counter(name, value) VALUES (?, 0) ON CONFLICT(name) DO NOTHING", (name,))
        conn.execute("UPDATE counter SET value = value + 1 WHERE name = ?", (name,))
        return int(conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()[0])

    def peek_counter(self, conn: sqlite3.Connection, name: str) -> int:
        """Current value."""
        row = conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()
        return int(row[0]) if row else 0


class SqliteNotificationStore:
    """``services.notifications.NotificationStore``."""

    def insert(self, conn: sqlite3.Connection, *, user_id: int | None, severity: str, kind: str, title: str, body: str | None, deep_link: str | None, created_at: str, source_ref: str | None) -> int:
        """Insert → id (``user_id`` None = every user)."""
        return int(conn.execute("INSERT INTO notification(user_id, severity, kind, title, body, deep_link, created_at, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (user_id, severity, kind, title, body, deep_link, created_at, source_ref)).lastrowid)

    def unread(self, conn: sqlite3.Connection, *, user_id: int) -> list[dict[str, Any]]:
        """Unread rows for the user (own + broadcast), newest first."""
        return _row_dicts(conn, "SELECT id, user_id, severity, kind, title, body, deep_link, created_at, read_at, source_ref FROM notification WHERE read_at IS NULL AND (user_id IS NULL OR user_id = ?) ORDER BY id DESC", (user_id,))

    def recent(self, conn: sqlite3.Connection, *, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Newest rows (read or not)."""
        return _row_dicts(conn, "SELECT id, user_id, severity, kind, title, body, deep_link, created_at, read_at, source_ref FROM notification WHERE user_id IS NULL OR user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def mark_read(self, conn: sqlite3.Connection, notification_id: int, *, at: str) -> bool:
        """Set read_at."""
        return conn.execute("UPDATE notification SET read_at = ? WHERE id = ? AND read_at IS NULL", (at, notification_id)).rowcount > 0

    def exists_unread(self, conn: sqlite3.Connection, *, kind: str, source_ref: str | None) -> bool:
        """Dedup helper: an unread row with the same kind + source already exists."""
        return conn.execute("SELECT 1 FROM notification WHERE read_at IS NULL AND kind = ? AND COALESCE(source_ref, '') = COALESCE(?, '') LIMIT 1", (kind, source_ref)).fetchone() is not None


# ----------------------------------------------------------------------------- work sessions (attendance)
_WS_COLS = "id, user_id, app_run_id, login_at, logout_at, end_reason, last_seen_at, confirmed_at, confirmed_note, owner_note, client, idle_seconds, lock_count, correlation_id"
_WS_UPDATABLE = frozenset({"logout_at", "end_reason", "last_seen_at", "confirmed_at", "confirmed_note", "owner_note", "idle_seconds", "lock_count"})
_UNCLEAN = ("WINDOW_CLOSED", "HEARTBEAT_LOST")


def _ws(row: tuple[Any, ...]) -> WorkSessionRecord:
    return WorkSessionRecord(int(row[0]), int(row[1]), row[2], str(row[3]), row[4], row[5], str(row[6]), row[7], row[8], row[9], row[10], int(row[11]), int(row[12]), row[13])


class SqliteWorkSessionStore:
    """``services.work_session.WorkSessionStore``."""

    def insert(self, conn: sqlite3.Connection, *, user_id: int, app_run_id: int | None, login_at: str, client: str | None) -> int:
        """Open a session → id."""
        return int(conn.execute("INSERT INTO work_session(user_id, app_run_id, login_at, last_seen_at, client, correlation_id) VALUES (?, ?, ?, ?, ?, ?)", (user_id, app_run_id, login_at, login_at, client, current_cid())).lastrowid)

    def by_id(self, conn: sqlite3.Connection, session_id: int) -> WorkSessionRecord | None:
        """One row."""
        row = conn.execute(f"SELECT {_WS_COLS} FROM work_session WHERE id = ?", (session_id,)).fetchone()  # noqa: S608 (constant column list)
        return _ws(row) if row else None

    def open_sessions(self, conn: sqlite3.Connection, *, user_id: int | None = None) -> list[WorkSessionRecord]:
        """Sessions without ``logout_at`` (for one user or all)."""
        if user_id is None:
            rows = conn.execute(f"SELECT {_WS_COLS} FROM work_session WHERE logout_at IS NULL ORDER BY login_at").fetchall()  # noqa: S608
        else:
            rows = conn.execute(f"SELECT {_WS_COLS} FROM work_session WHERE logout_at IS NULL AND user_id = ? ORDER BY login_at", (user_id,)).fetchall()  # noqa: S608
        return [_ws(r) for r in rows]

    def stale(self, conn: sqlite3.Connection, *, last_seen_before: str) -> list[WorkSessionRecord]:
        """Open sessions whose heartbeat is older than the cut-off."""
        rows = conn.execute(f"SELECT {_WS_COLS} FROM work_session WHERE logout_at IS NULL AND last_seen_at < ? ORDER BY id", (last_seen_before,)).fetchall()  # noqa: S608
        return [_ws(r) for r in rows]

    def last_unconfirmed(self, conn: sqlite3.Connection, *, user_id: int) -> WorkSessionRecord | None:
        """Newest unclean, unconfirmed ended session of the user."""
        row = conn.execute(f"SELECT {_WS_COLS} FROM work_session WHERE user_id = ? AND logout_at IS NOT NULL AND confirmed_at IS NULL AND end_reason IN (?, ?) ORDER BY id DESC LIMIT 1", (user_id, *_UNCLEAN)).fetchone()  # noqa: S608
        return _ws(row) if row else None

    def history(self, conn: sqlite3.Connection, *, user_id: int | None, since: str | None, until: str | None, limit: int) -> list[WorkSessionRecord]:
        """Newest first, optional user and login-time window."""
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if since:
            clauses.append("login_at >= ?")
            params.append(since)
        if until:
            clauses.append("login_at < ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT {_WS_COLS} FROM work_session {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()  # noqa: S608
        return [_ws(r) for r in rows]

    def recent_end_reasons(self, conn: sqlite3.Connection, *, user_id: int, limit: int) -> list[str | None]:
        """End reasons of the newest ended sessions (for the unclean streak)."""
        return [r[0] for r in conn.execute("SELECT end_reason FROM work_session WHERE user_id = ? AND logout_at IS NOT NULL ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()]

    def update(self, conn: sqlite3.Connection, session_id: int, **fields: Any) -> None:
        """Whitelisted columns only."""
        unknown = set(fields) - _WS_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE work_session SET {assignments} WHERE id = ?", (*fields.values(), session_id))  # noqa: S608 (whitelisted names)

    def sales_in_window(self, conn: sqlite3.Connection, *, user_id: int, since: str, until: str) -> tuple[int, int]:
        """``(bills, total_paise)`` of COMPLETED sales by the user between the two instants (derived figures)."""
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_paise), 0) FROM sale WHERE user_id = ? AND status = 'COMPLETED' AND sold_at >= ? AND sold_at <= ?", (user_id, since, until)).fetchone()
        return int(row[0]), int(row[1])

    def open_shift_id(self, conn: sqlite3.Connection, *, user_id: int) -> int | None:
        """The user's OPEN shift, if any."""
        row = conn.execute("SELECT id FROM shift WHERE user_id = ? AND status = 'OPEN' LIMIT 1", (user_id,)).fetchone()
        return int(row[0]) if row else None


# ----------------------------------------------------------------------------- catalogue
_PRODUCT_COLS = (
    "p.id, p.sku, p.internal_barcode, p.name, p.name_hi, p.category_id, p.parent_product_id, p.unit, p.sale_mode, p.base_unit, "
    "p.pricing_unit_qty, p.pricing_unit_label, p.min_qty, p.qty_step, p.qty_presets, p.allow_decimal_qty, p.price_paise, p.mrp_paise, "
    "p.cost_paise, p.tax_rate_bp, p.price_includes_tax, p.hsn_code, p.min_stock, p.reorder_qty, p.image_path, p.is_active, p.quick_pick, "
    "p.is_system, p.row_version, p.created_at, p.updated_at, p.deleted_at, COALESCE(s.qty, 0)"
)
_PRODUCT_FROM = "FROM product p LEFT JOIN product_stock s ON s.product_id = p.id"
PRODUCT_EDITABLE = frozenset({"name", "name_hi", "category_id", "parent_product_id", "unit", "sale_mode", "base_unit", "pricing_unit_qty", "pricing_unit_label", "min_qty", "qty_step", "qty_presets", "allow_decimal_qty", "price_paise", "mrp_paise", "cost_paise", "tax_rate_bp", "price_includes_tax", "hsn_code", "min_stock", "reorder_qty", "image_path", "is_active", "quick_pick", "deleted_at", "sku"})
_DEC_FIELDS = frozenset({"pricing_unit_qty", "min_qty", "qty_step", "min_stock", "reorder_qty"})


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _product(row: tuple[Any, ...]) -> ProductRecord:
    return ProductRecord(
        id=int(row[0]), sku=str(row[1]), internal_barcode=row[2], name=str(row[3]), name_hi=row[4], category_id=row[5], parent_product_id=row[6],
        unit=str(row[7]), sale_mode=str(row[8]), base_unit=str(row[9]), pricing_unit_qty=_dec(row[10]), pricing_unit_label=str(row[11]), min_qty=_dec(row[12]),
        qty_step=_dec(row[13]), qty_presets=str(row[14]), allow_decimal_qty=bool(row[15]), price_paise=int(row[16]), mrp_paise=row[17], cost_paise=row[18],
        tax_rate_bp=int(row[19]), price_includes_tax=bool(row[20]), hsn_code=row[21], min_stock=_dec(row[22]), reorder_qty=_dec(row[23]), image_path=row[24],
        is_active=bool(row[25]), quick_pick=bool(row[26]), is_system=bool(row[27]), row_version=int(row[28]), created_at=str(row[29]), updated_at=str(row[30]),
        deleted_at=row[31], stock_qty=_dec(row[32]),
    )


def _barcode(row: tuple[Any, ...]) -> BarcodeRecord:
    return BarcodeRecord(int(row[0]), int(row[1]), str(row[2]), str(row[3]), str(row[4]), _dec(row[5]), bool(row[6]), str(row[7]))


def _category(row: tuple[Any, ...]) -> CategoryRecord:
    return CategoryRecord(int(row[0]), str(row[1]), row[2], row[3], int(row[4]), row[5])


def _price_row(row: tuple[Any, ...]) -> PriceHistoryRecord:
    field = str(row[2])
    old, new = (row[5], row[6]) if field == "TAX_RATE" else (row[3], row[4])
    return PriceHistoryRecord(int(row[0]), int(row[1]), field, old, new, str(row[7]), row[8], row[9], str(row[10]), row[11])


def _to_sql(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


class SqliteCatalogStore:
    """``services.catalog.CatalogStore`` + ``services.price_history.PriceHistoryStore``."""

    # -- products ---------------------------------------------------------------------------------
    def product_by_id(self, conn: sqlite3.Connection, product_id: int) -> ProductRecord | None:
        """One product (also archived / system)."""
        row = conn.execute(f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE p.id = ?", (product_id,)).fetchone()  # noqa: S608
        return _product(row) if row else None

    def product_by_sku(self, conn: sqlite3.Connection, sku: str) -> ProductRecord | None:
        """By code (case-insensitive column)."""
        row = conn.execute(f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE p.sku = ? COLLATE NOCASE", (sku,)).fetchone()  # noqa: S608
        return _product(row) if row else None

    def insert_product(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
        """Insert → id (``internal_barcode`` is set afterwards, it needs the id)."""
        keys = list(fields)
        cur = conn.execute(f"INSERT INTO product({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})", tuple(_to_sql(k, fields[k]) for k in keys))  # noqa: S608 (column names from a whitelist)
        return int(cur.lastrowid)

    def update_product(self, conn: sqlite3.Connection, product_id: int, *, expected_version: int | None, **fields: Any) -> bool:
        """Update whitelisted columns; with ``expected_version`` it is an optimistic write (False = stale)."""
        unknown = set(fields) - PRODUCT_EDITABLE - {"internal_barcode"}
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not fields:
            return True
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params: list[Any] = [_to_sql(k, v) for k, v in fields.items()] + [product_id]
        where = "id = ?"
        if expected_version is not None:
            where += " AND row_version = ?"
            params.append(expected_version)
        cur = conn.execute(f"UPDATE product SET {assignments} WHERE {where}", params)  # noqa: S608 (whitelisted names)
        return cur.rowcount > 0

    def next_sku_sequence(self, conn: sqlite3.Connection, prefix: str) -> int:
        """1 + the highest numeric suffix among ``PREFIX-NNNN`` codes (archived rows included — codes are never reused)."""
        rows = conn.execute("SELECT sku FROM product WHERE sku LIKE ? COLLATE NOCASE", (f"{prefix}-%",)).fetchall()
        best = 0
        for (sku,) in rows:
            tail = str(sku).rsplit("-", 1)[-1]
            if tail.isdigit():
                best = max(best, int(tail))
        return best + 1

    def search_products(self, conn: sqlite3.Connection, *, text: str, limit: int) -> list[ProductRecord]:
        """Active, non-system products whose name / Hindi name / code contains the text (cheap SQL pass; ranking is the service's job)."""
        like = f"%{text}%"
        rows = conn.execute(
            f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE p.is_active = 1 AND p.deleted_at IS NULL AND p.is_system = 0 AND (p.name LIKE ? COLLATE NOCASE OR p.name_hi LIKE ? OR p.sku LIKE ? COLLATE NOCASE) ORDER BY p.name LIMIT ?",  # noqa: S608
            (like, like, like, limit),
        ).fetchall()
        return [_product(r) for r in rows]

    def active_products(self, conn: sqlite3.Connection, *, limit: int) -> list[ProductRecord]:
        """All active non-system products (for the fuzzy pass and exports)."""
        rows = conn.execute(f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE p.is_active = 1 AND p.deleted_at IS NULL AND p.is_system = 0 ORDER BY p.name LIMIT ?", (limit,)).fetchall()  # noqa: S608
        return [_product(r) for r in rows]

    def list_products(self, conn: sqlite3.Connection, *, category_id: int | None, query: str, include_archived: bool, limit: int, offset: int) -> tuple[list[ProductRecord], int]:
        """Grid page + total."""
        clauses, params = ["p.is_system = 0"], []
        if not include_archived:
            clauses.append("p.is_active = 1 AND p.deleted_at IS NULL")
        if category_id is not None:
            clauses.append("p.category_id = ?")
            params.append(category_id)
        if query:
            clauses.append("(p.name LIKE ? COLLATE NOCASE OR p.name_hi LIKE ? OR p.sku LIKE ? COLLATE NOCASE)")
            params.extend([f"%{query}%"] * 3)
        where = " AND ".join(clauses)
        total = int(conn.execute(f"SELECT COUNT(*) FROM product p WHERE {where}", params).fetchone()[0])  # noqa: S608
        rows = conn.execute(f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE {where} ORDER BY p.name LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()  # noqa: S608
        return [_product(r) for r in rows], total

    def quick_picks(self, conn: sqlite3.Connection) -> list[ProductRecord]:
        """Tiles for the Owner's POS."""
        rows = conn.execute(f"SELECT {_PRODUCT_COLS} {_PRODUCT_FROM} WHERE p.quick_pick = 1 AND p.is_active = 1 AND p.deleted_at IS NULL ORDER BY p.name").fetchall()  # noqa: S608
        return [_product(r) for r in rows]

    # -- barcodes ---------------------------------------------------------------------------------
    def barcode_owner(self, conn: sqlite3.Connection, code: str) -> BarcodeRecord | None:
        """Who owns this code (any product, active or not)."""
        row = conn.execute("SELECT id, product_id, barcode, kind, symbology, pack_qty, is_primary, created_at FROM product_barcode WHERE barcode = ?", (code,)).fetchone()
        return _barcode(row) if row else None

    def barcodes_of(self, conn: sqlite3.Connection, product_id: int) -> list[BarcodeRecord]:
        """Primary first."""
        rows = conn.execute("SELECT id, product_id, barcode, kind, symbology, pack_qty, is_primary, created_at FROM product_barcode WHERE product_id = ? ORDER BY is_primary DESC, id", (product_id,)).fetchall()
        return [_barcode(r) for r in rows]

    def insert_barcode(self, conn: sqlite3.Connection, *, product_id: int, barcode: str, kind: str, symbology: str, pack_qty: Decimal, is_primary: bool) -> int:
        """Insert → id."""
        return int(conn.execute("INSERT INTO product_barcode(product_id, barcode, kind, symbology, pack_qty, is_primary) VALUES (?, ?, ?, ?, ?, ?)", (product_id, barcode, kind, symbology, str(pack_qty), 1 if is_primary else 0)).lastrowid)

    def delete_barcode(self, conn: sqlite3.Connection, *, product_id: int, barcode: str) -> bool:
        """Remove a non-internal code."""
        return conn.execute("DELETE FROM product_barcode WHERE product_id = ? AND barcode = ? AND kind <> 'INTERNAL'", (product_id, barcode)).rowcount > 0

    # -- categories / tags ------------------------------------------------------------------------
    def categories(self, conn: sqlite3.Connection) -> list[CategoryRecord]:
        """In display order."""
        return [_category(r) for r in conn.execute("SELECT id, name, name_hi, parent_id, sort_order, icon FROM category ORDER BY sort_order, id").fetchall()]

    def insert_category(self, conn: sqlite3.Connection, *, name: str, name_hi: str | None, parent_id: int | None, icon: str | None) -> CategoryRecord:
        """Append at the end of the order."""
        order = int(conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM category").fetchone()[0])
        cur = conn.execute("INSERT INTO category(name, name_hi, parent_id, sort_order, icon) VALUES (?, ?, ?, ?, ?)", (name, name_hi, parent_id, order, icon))
        return CategoryRecord(int(cur.lastrowid), name, name_hi, parent_id, order, icon)

    def update_category(self, conn: sqlite3.Connection, category_id: int, **fields: Any) -> None:
        """name / name_hi / parent_id / sort_order / icon."""
        allowed = {"name", "name_hi", "parent_id", "sort_order", "icon"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if fields:
            conn.execute(f"UPDATE category SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), category_id))  # noqa: S608

    def set_tags(self, conn: sqlite3.Connection, product_id: int, names: list[str]) -> None:
        """Replace the product's tags (tags are created on demand, lower-cased)."""
        conn.execute("DELETE FROM product_tag WHERE product_id = ?", (product_id,))
        for name in names:
            conn.execute("INSERT OR IGNORE INTO tag(name) VALUES (?)", (name,))
            tag_id = int(conn.execute("SELECT id FROM tag WHERE name = ? COLLATE NOCASE", (name,)).fetchone()[0])
            conn.execute("INSERT OR IGNORE INTO product_tag(product_id, tag_id) VALUES (?, ?)", (product_id, tag_id))

    def tags_of(self, conn: sqlite3.Connection, product_id: int) -> list[str]:
        """Sorted tag names."""
        return [str(r[0]) for r in conn.execute("SELECT t.name FROM tag t JOIN product_tag pt ON pt.tag_id = t.id WHERE pt.product_id = ? ORDER BY t.name", (product_id,)).fetchall()]

    def tag_id(self, conn: sqlite3.Connection, name: str) -> int | None:
        """Tag id by name (offers with TAG scope)."""
        row = conn.execute("SELECT id FROM tag WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return int(row[0]) if row else None

    # -- price history ----------------------------------------------------------------------------
    def insert_price_history(self, conn: sqlite3.Connection, *, product_id: int, field: str, old: int | None, new: int | None, effective_from: str, changed_by: int | None, reason: str | None, source: str) -> int:
        """Append one row (money fields in paise, TAX_RATE in basis points)."""
        if field == "TAX_RATE":
            cols = (None, None, old, new)
        else:
            cols = (old, new, None, None)
        return int(conn.execute(
            "INSERT INTO price_history(product_id, field, old_value_paise, new_value_paise, old_bp, new_bp, effective_from, changed_by, reason, source, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, field, *cols, effective_from, changed_by, reason, source, current_cid()),
        ).lastrowid)

    def price_history(self, conn: sqlite3.Connection, *, product_id: int, field: str | None, since: str | None, until: str | None) -> list[PriceHistoryRecord]:
        """Newest first within the window."""
        clauses, params = ["product_id = ?"], [product_id]
        if field:
            clauses.append("field = ?")
            params.append(field)
        if since:
            clauses.append("effective_from >= ?")
            params.append(since)
        if until:
            clauses.append("effective_from <= ?")
            params.append(until)
        rows = conn.execute(f"SELECT id, product_id, field, old_value_paise, new_value_paise, old_bp, new_bp, effective_from, changed_by, reason, source, correlation_id FROM price_history WHERE {' AND '.join(clauses)} ORDER BY effective_from DESC, id DESC", params).fetchall()  # noqa: S608
        return [_price_row(r) for r in rows]

    def last_price_before(self, conn: sqlite3.Connection, *, product_id: int, field: str, before: str) -> int | None:
        """The value in force just before ``before`` (for the chart's starting point)."""
        row = conn.execute(
            "SELECT new_value_paise, new_bp FROM price_history WHERE product_id = ? AND field = ? AND effective_from < ? ORDER BY effective_from DESC, id DESC LIMIT 1",
            (product_id, field, before),
        ).fetchone()
        if row is None:
            return None
        return row[1] if field == "TAX_RATE" else row[0]


# ----------------------------------------------------------------------------- inventory
_MV_COLS = "id, product_id, type, qty_delta, qty_after, unit_cost_paise, ref_type, ref_id, reason, user_id, created_at, correlation_id"


def _movement(row: tuple[Any, ...]) -> MovementRecord:
    return MovementRecord(int(row[0]), int(row[1]), str(row[2]), _dec(row[3]), _dec(row[4]), row[5], row[6], row[7], row[8], int(row[9]), str(row[10]), row[11])


def _count(row: tuple[Any, ...]) -> StockCountRecord:
    return StockCountRecord(int(row[0]), str(row[1]), row[2], int(row[3]), row[4], str(row[5]), str(row[6]))


def _count_line(row: tuple[Any, ...]) -> StockCountLineRecord:
    return StockCountLineRecord(int(row[0]), int(row[1]), int(row[2]), _dec(row[3]), _dec(row[4]) if row[4] is not None else None, _dec(row[5]) if row[5] is not None else None, row[6])


class SqliteInventoryStore:
    """``services.inventory.InventoryStore``."""

    # -- stock & movements -----------------------------------------------------------------------
    def stock_of(self, conn: sqlite3.Connection, product_id: int) -> Decimal:
        """Cached quantity (0 when the product never moved)."""
        row = conn.execute("SELECT qty FROM product_stock WHERE product_id = ?", (product_id,)).fetchone()
        return _dec(row[0]) if row else Decimal("0")

    def insert_movement(self, conn: sqlite3.Connection, *, product_id: int, type: str, qty_delta: Decimal, qty_after: Decimal, unit_cost_paise: int | None, ref_type: str | None, ref_id: int | None, reason: str | None, user_id: int, created_at: str) -> int:  # noqa: A002 - column name
        """Append (the trigger validates ``qty_after`` and maintains ``product_stock``)."""
        return int(conn.execute(
            "INSERT INTO stock_movement(product_id, type, qty_delta, qty_after, unit_cost_paise, ref_type, ref_id, reason, user_id, created_at, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, type, str(qty_delta), str(qty_after), unit_cost_paise, ref_type, ref_id, reason, user_id, created_at, current_cid()),
        ).lastrowid)

    def movements(self, conn: sqlite3.Connection, *, product_id: int | None, types: tuple[str, ...] | None, since: str | None, until: str | None, limit: int) -> list[MovementRecord]:
        """Newest first."""
        clauses, params = [], []
        if product_id is not None:
            clauses.append("product_id = ?")
            params.append(product_id)
        if types:
            clauses.append(f"type IN ({', '.join('?' for _ in types)})")
            params.extend(types)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT {_MV_COLS} FROM stock_movement {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()  # noqa: S608
        return [_movement(r) for r in rows]

    def has_movements(self, conn: sqlite3.Connection, product_id: int) -> bool:
        """Any movement at all (opening stock is allowed once)."""
        return conn.execute("SELECT 1 FROM stock_movement WHERE product_id = ? LIMIT 1", (product_id,)).fetchone() is not None

    def movement_sums(self, conn: sqlite3.Connection) -> list[tuple[int, Decimal, Decimal]]:
        """``(product_id, Σ qty_delta, cached qty)`` for every product that moved."""
        rows = conn.execute("SELECT m.product_id, SUM(m.qty_delta), COALESCE(s.qty, 0) FROM stock_movement m LEFT JOIN product_stock s ON s.product_id = m.product_id GROUP BY m.product_id").fetchall()
        return [(int(r[0]), _dec(r[1]), _dec(r[2])) for r in rows]

    def stock_levels(self, conn: sqlite3.Connection) -> list[tuple[int, Decimal, Decimal, int, int | None, str]]:
        """``(product_id, qty, min_stock, price_paise, cost_paise, name)`` for active non-system products."""
        rows = conn.execute(
            "SELECT p.id, COALESCE(s.qty, 0), p.min_stock, p.price_paise, p.cost_paise, p.name FROM product p LEFT JOIN product_stock s ON s.product_id = p.id WHERE p.is_active = 1 AND p.deleted_at IS NULL AND p.is_system = 0 ORDER BY p.name"
        ).fetchall()
        return [(int(r[0]), _dec(r[1]), _dec(r[2]), int(r[3]), r[4], str(r[5])) for r in rows]

    def upsert_daily_balance(self, conn: sqlite3.Connection, *, day: str, product_id: int, qty_close: Decimal, value_cost_paise: int) -> None:
        """One row per product per day."""
        conn.execute("INSERT INTO stock_daily_balance(day, product_id, qty_close, value_cost_paise) VALUES (?, ?, ?, ?) ON CONFLICT(day, product_id) DO UPDATE SET qty_close = excluded.qty_close, value_cost_paise = excluded.value_cost_paise", (day, product_id, str(qty_close), value_cost_paise))

    # -- suppliers & purchases -------------------------------------------------------------------
    def suppliers(self, conn: sqlite3.Connection) -> list[SupplierRecord]:
        """Alphabetical."""
        return [SupplierRecord(int(r[0]), str(r[1]), r[2], r[3]) for r in conn.execute("SELECT id, name, phone, gstin FROM supplier ORDER BY name COLLATE NOCASE").fetchall()]

    def supplier_by_name(self, conn: sqlite3.Connection, name: str) -> SupplierRecord | None:
        """Case-insensitive."""
        row = conn.execute("SELECT id, name, phone, gstin FROM supplier WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return SupplierRecord(int(row[0]), str(row[1]), row[2], row[3]) if row else None

    def insert_supplier(self, conn: sqlite3.Connection, *, name: str, phone: str | None, gstin: str | None) -> SupplierRecord:
        """Insert."""
        cur = conn.execute("INSERT INTO supplier(name, phone, gstin) VALUES (?, ?, ?)", (name, phone, gstin))
        return SupplierRecord(int(cur.lastrowid), name, phone, gstin)

    def insert_purchase(self, conn: sqlite3.Connection, *, supplier_id: int | None, invoice_no: str | None, purchase_date: str, total_paise: int, user_id: int, created_at: str) -> int:
        """Header → id."""
        return int(conn.execute("INSERT INTO purchase(supplier_id, supplier_invoice_no, purchase_date, total_paise, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (supplier_id, invoice_no, purchase_date, total_paise, user_id, created_at)).lastrowid)

    def insert_purchase_item(self, conn: sqlite3.Connection, *, purchase_id: int, product_id: int, qty: Decimal, unit_cost_paise: int, expiry_date: str | None) -> int:
        """Line → id."""
        return int(conn.execute("INSERT INTO purchase_item(purchase_id, product_id, qty, unit_cost_paise, expiry_date) VALUES (?, ?, ?, ?, ?)", (purchase_id, product_id, str(qty), unit_cost_paise, expiry_date)).lastrowid)

    def purchases(self, conn: sqlite3.Connection, *, limit: int) -> list[PurchaseRecord]:
        """Newest first."""
        rows = conn.execute("SELECT id, supplier_id, supplier_invoice_no, purchase_date, total_paise, user_id, created_at FROM purchase ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [PurchaseRecord(int(r[0]), r[1], r[2], str(r[3]), int(r[4]), int(r[5]), str(r[6])) for r in rows]

    # -- stock-take -------------------------------------------------------------------------------
    def insert_stock_count(self, conn: sqlite3.Connection, *, started_at: str, user_id: int, scope: str) -> int:
        """Header → id."""
        return int(conn.execute("INSERT INTO stock_count(started_at, started_by_user_id, status, scope) VALUES (?, ?, 'DRAFT', ?)", (started_at, user_id, scope)).lastrowid)

    def stock_count(self, conn: sqlite3.Connection, count_id: int) -> StockCountRecord | None:
        """Header."""
        row = conn.execute("SELECT id, started_at, approved_at, started_by_user_id, approved_by_user_id, status, scope FROM stock_count WHERE id = ?", (count_id,)).fetchone()
        return _count(row) if row else None

    def update_stock_count(self, conn: sqlite3.Connection, count_id: int, **fields: Any) -> None:
        """status / approved_at / approved_by_user_id."""
        allowed = {"status", "approved_at", "approved_by_user_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        conn.execute(f"UPDATE stock_count SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), count_id))  # noqa: S608

    def insert_count_line(self, conn: sqlite3.Connection, *, count_id: int, product_id: int, system_qty: Decimal) -> int:
        """Line with the system quantity frozen at start."""
        return int(conn.execute("INSERT INTO stock_count_line(stock_count_id, product_id, system_qty) VALUES (?, ?, ?)", (count_id, product_id, str(system_qty))).lastrowid)

    def count_lines(self, conn: sqlite3.Connection, count_id: int) -> list[StockCountLineRecord]:
        """All lines."""
        rows = conn.execute("SELECT id, stock_count_id, product_id, system_qty, counted_qty, variance, variance_value_paise FROM stock_count_line WHERE stock_count_id = ? ORDER BY id", (count_id,)).fetchall()
        return [_count_line(r) for r in rows]

    def set_counted(self, conn: sqlite3.Connection, *, count_id: int, product_id: int, counted_qty: Decimal, variance: Decimal, variance_value_paise: int) -> bool:
        """Record the counted quantity on a line."""
        return conn.execute("UPDATE stock_count_line SET counted_qty = ?, variance = ?, variance_value_paise = ? WHERE stock_count_id = ? AND product_id = ?", (str(counted_qty), str(variance), variance_value_paise, count_id, product_id)).rowcount > 0


# ----------------------------------------------------------------------------- labels
class SqliteLabelStore:
    """``services.labels.LabelStore`` — the ``label_print_log`` table."""

    def insert_print(self, conn: sqlite3.Connection, *, product_id: int, copies: int, target: str, reason: str, user_id: int, at: str) -> int:
        """One row per product per job."""
        return int(conn.execute("INSERT INTO label_print_log(product_id, copies, target, reason, user_id, at) VALUES (?, ?, ?, ?, ?, ?)", (product_id, copies, target, reason, user_id, at)).lastrowid)

    def recent(self, conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
        """Newest first with the product name."""
        return _row_dicts(conn, "SELECT l.id, l.product_id, p.name, p.sku, l.copies, l.target, l.reason, l.user_id, l.at FROM label_print_log l JOIN product p ON p.id = l.product_id ORDER BY l.id DESC LIMIT ?", (limit,))


# ----------------------------------------------------------------------------- offers & billing
_OFFER_COLS = "id, name, name_hi, type, value, scope, scope_id, min_qty, valid_from, valid_to, stackable, is_active, created_by, created_at"
_SALE_VIEW_COLS = "id, invoice_no, client_txn_id, public_id, fin_year, seq_no, sold_at, user_id, shift_id, customer_id, status, original_sale_id, subtotal_paise, item_discount_paise, bill_discount_paise, bill_discount_reason, total_savings_paise, approved_by_user_id, taxable_paise, cgst_paise, sgst_paise, roundoff_paise, total_paise, reprint_count, receipt_printed, prev_hash, hash, note"
_SALE_ITEM_COLS = "id, sale_id, product_id, name_snapshot, hsn_snapshot, qty, unit, qty_source, pricing_unit_qty_snapshot, shelf_price_paise, offer_id, offer_name_snapshot, discount_bp, discount_paise, discount_reason, unit_price_paise, tax_rate_bp, taxable_paise, cgst_paise, sgst_paise, line_total_paise, cost_paise_snapshot, entry_method, entry_reason, scanned_barcode, is_open_item, open_item_name, open_item_detail"
_PAYMENT_COLS = "id, sale_id, mode, amount_paise, tendered_paise, change_paise, reference"
_SALE_INSERT_COLS = frozenset(_SALE_VIEW_COLS.replace(" ", "").split(",")) - {"id"}
_SALE_ITEM_INSERT_COLS = frozenset(_SALE_ITEM_COLS.replace(" ", "").split(",")) - {"id"}
_PAYMENT_INSERT_COLS = frozenset(_PAYMENT_COLS.replace(" ", "").split(",")) - {"id"}
_OFFER_INSERT_COLS = frozenset(_OFFER_COLS.replace(" ", "").split(",")) - {"id"}


def _offer(row: tuple[Any, ...]) -> OfferRecord:
    return OfferRecord(int(row[0]), str(row[1]), row[2], str(row[3]), _dec(row[4]), str(row[5]), row[6], _dec(row[7]), row[8], row[9], bool(row[10]), bool(row[11]), row[12], str(row[13]))


def _sale_item_view(row: tuple[Any, ...]) -> SaleItemView:
    return SaleItemView(
        id=int(row[0]), sale_id=int(row[1]), product_id=row[2], name_snapshot=str(row[3]), hsn_snapshot=row[4], qty=_dec(row[5]), unit=str(row[6]), qty_source=str(row[7]),
        pricing_unit_qty_snapshot=_dec(row[8]), shelf_price_paise=int(row[9]), offer_id=row[10], offer_name_snapshot=row[11], discount_bp=int(row[12]), discount_paise=int(row[13]),
        discount_reason=row[14], unit_price_paise=int(row[15]), tax_rate_bp=int(row[16]), taxable_paise=int(row[17]), cgst_paise=int(row[18]), sgst_paise=int(row[19]),
        line_total_paise=int(row[20]), cost_paise_snapshot=row[21], entry_method=str(row[22]), entry_reason=row[23], scanned_barcode=row[24], is_open_item=bool(row[25]),
        open_item_name=row[26], open_item_detail=row[27],
    )


def _payment_view(row: tuple[Any, ...]) -> PaymentView:
    return PaymentView(int(row[0]), int(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]), row[6])


def _insert(conn: sqlite3.Connection, table: str, fields: dict[str, Any], allowed: frozenset[str]) -> int:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"{table}: unknown columns {sorted(unknown)}")
    keys = list(fields)
    cur = conn.execute(f"INSERT INTO {table}({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})", tuple(_to_sql(k, fields[k]) for k in keys))  # noqa: S608 (whitelisted names)
    return int(cur.lastrowid)


class SqliteBillingStore:
    """``services.offers.OfferStore`` + ``services.billing.BillingStore``."""

    # -- offers -----------------------------------------------------------------------------------
    def offers(self, conn: sqlite3.Connection, *, active_only: bool) -> list[OfferRecord]:
        """All offers (optionally active only), by id."""
        where = "WHERE is_active = 1" if active_only else ""
        return [_offer(r) for r in conn.execute(f"SELECT {_OFFER_COLS} FROM offer {where} ORDER BY id").fetchall()]  # noqa: S608

    def offer(self, conn: sqlite3.Connection, offer_id: int) -> OfferRecord | None:
        """One offer."""
        row = conn.execute(f"SELECT {_OFFER_COLS} FROM offer WHERE id = ?", (offer_id,)).fetchone()  # noqa: S608
        return _offer(row) if row else None

    def insert_offer(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
        """Insert → id."""
        return _insert(conn, "offer", fields, _OFFER_INSERT_COLS)

    def update_offer(self, conn: sqlite3.Connection, offer_id: int, **fields: Any) -> None:
        """Whitelisted columns."""
        unknown = set(fields) - _OFFER_INSERT_COLS
        if unknown:
            raise ValueError(f"offer: not updatable {sorted(unknown)}")
        if fields:
            conn.execute(f"UPDATE offer SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*(_to_sql(k, v) for k, v in fields.items()), offer_id))  # noqa: S608

    def offer_usage(self, conn: sqlite3.Connection, offer_id: int) -> tuple[int, int]:
        """``(lines, paise given)`` over completed sales."""
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(si.discount_paise), 0) FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.offer_id = ? AND s.status = 'COMPLETED'", (offer_id,)).fetchone()
        return int(row[0]), int(row[1])

    # -- lookups ----------------------------------------------------------------------------------
    def open_shift_id(self, conn: sqlite3.Connection, user_id: int) -> int | None:
        """The user's OPEN shift."""
        row = conn.execute("SELECT id FROM shift WHERE user_id = ? AND status = 'OPEN'", (user_id,)).fetchone()
        return int(row[0]) if row else None

    def open_item_product_id(self, conn: sqlite3.Connection) -> int:
        """The seeded ``OPEN-ITEM`` system product."""
        row = conn.execute("SELECT id FROM product WHERE is_system = 1 AND sku = 'OPEN-ITEM'").fetchone()
        if row is None:
            raise LookupError("system product OPEN-ITEM missing — run init-db")
        return int(row[0])

    def next_invoice_seq(self, conn: sqlite3.Connection, fin_year: str) -> int:
        """``counter['invoice:<fy>']`` += 1 (created from MAX(seq_no) of that FY on first use)."""
        name = f"invoice:{fin_year}"
        row = conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()
        if row is None:
            start = conn.execute("SELECT COALESCE(MAX(seq_no), 0) FROM sale WHERE fin_year = ?", (fin_year,)).fetchone()[0]
            conn.execute("INSERT INTO counter(name, value) VALUES (?, ?)", (name, int(start)))
        conn.execute("UPDATE counter SET value = value + 1 WHERE name = ?", (name,))
        return int(conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()[0])

    def peek_invoice_seq(self, conn: sqlite3.Connection, fin_year: str) -> int:
        """Current counter value for the FY without changing it."""
        row = conn.execute("SELECT value FROM counter WHERE name = ?", (f"invoice:{fin_year}",)).fetchone()
        if row is not None:
            return int(row[0])
        return int(conn.execute("SELECT COALESCE(MAX(seq_no), 0) FROM sale WHERE fin_year = ?", (fin_year,)).fetchone()[0])

    # -- sales ------------------------------------------------------------------------------------
    def _view(self, conn: sqlite3.Connection, row: tuple[Any, ...]) -> SaleView:
        sale_id = int(row[0])
        items = tuple(_sale_item_view(r) for r in conn.execute(f"SELECT {_SALE_ITEM_COLS} FROM sale_item WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall())  # noqa: S608
        payments = tuple(_payment_view(r) for r in conn.execute(f"SELECT {_PAYMENT_COLS} FROM payment WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall())  # noqa: S608
        return SaleView(
            id=sale_id, invoice_no=str(row[1]), client_txn_id=str(row[2]), public_id=str(row[3]), fin_year=str(row[4]), seq_no=int(row[5]), sold_at=str(row[6]), user_id=int(row[7]),
            shift_id=int(row[8]), customer_id=row[9], status=str(row[10]), original_sale_id=row[11], subtotal_paise=int(row[12]), item_discount_paise=int(row[13]),
            bill_discount_paise=int(row[14]), bill_discount_reason=row[15], total_savings_paise=int(row[16]), approved_by_user_id=row[17], taxable_paise=int(row[18]),
            cgst_paise=int(row[19]), sgst_paise=int(row[20]), roundoff_paise=int(row[21]), total_paise=int(row[22]), reprint_count=int(row[23]), receipt_printed=bool(row[24]),
            prev_hash=str(row[25]), hash=str(row[26]), note=row[27], items=items, payments=payments,
        )

    def sale(self, conn: sqlite3.Connection, sale_id: int) -> SaleView | None:
        """Full sale by id."""
        row = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE id = ?", (sale_id,)).fetchone()  # noqa: S608
        return self._view(conn, row) if row else None

    def sale_by_client_txn(self, conn: sqlite3.Connection, client_txn_id: str) -> SaleView | None:
        """Replay lookup."""
        row = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE client_txn_id = ?", (client_txn_id,)).fetchone()  # noqa: S608
        return self._view(conn, row) if row else None

    def find_sale(self, conn: sqlite3.Connection, text: str) -> SaleView | None:
        """By invoice number (case-insensitive), public id, or the receipt's EAN-13."""
        parsed = parse_receipt_ean13(text)
        if parsed is not None:
            row = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE fin_year = ? AND seq_no = ?", parsed).fetchone()  # noqa: S608
            return self._view(conn, row) if row else None
        row = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE invoice_no = ? COLLATE NOCASE OR public_id = ?", (text, text)).fetchone()  # noqa: S608
        return self._view(conn, row) if row else None

    def name_hi(self, conn: sqlite3.Connection, product_id: int) -> str | None:
        """Hindi name for the receipt (not snapshotted on the line)."""
        row = conn.execute("SELECT name_hi FROM product WHERE id = ?", (product_id,)).fetchone()
        return row[0] if row else None

    def recent_sales(self, conn: sqlite3.Connection, *, user_id: int | None, limit: int) -> list[SaleView]:
        """Newest first."""
        if user_id is None:
            rows = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale ORDER BY id DESC LIMIT ?", (limit,)).fetchall()  # noqa: S608
        else:
            rows = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()  # noqa: S608
        return [self._view(conn, r) for r in rows]

    def related_sales(self, conn: sqlite3.Connection, original_sale_id: int) -> list[SaleView]:
        """Credit notes issued against a bill, oldest first."""
        rows = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE original_sale_id = ? ORDER BY id", (original_sale_id,)).fetchall()  # noqa: S608
        return [self._view(conn, r) for r in rows]

    def search_sales(self, conn: sqlite3.Connection, *, text: str, user_id: int | None, limit: int) -> list[SaleView]:
        """Invoice fragment or public id, newest first."""
        like = f"%{text}%"
        if user_id is None:
            rows = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE invoice_no LIKE ? OR public_id = ? ORDER BY id DESC LIMIT ?", (like, text, limit)).fetchall()  # noqa: S608
        else:
            rows = conn.execute(f"SELECT {_SALE_VIEW_COLS} FROM sale WHERE (invoice_no LIKE ? OR public_id = ?) AND user_id = ? ORDER BY id DESC LIMIT ?", (like, text, user_id, limit)).fetchall()  # noqa: S608
        return [self._view(conn, r) for r in rows]

    # -- open-item review -------------------------------------------------------------------------
    def open_item_pending(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Open-item lines on completed bills that nobody has reviewed yet."""
        return _row_dicts(conn, "SELECT si.id AS sale_item_id, si.open_item_name AS name, si.open_item_detail AS detail, si.qty, si.line_total_paise, si.unit_price_paise, s.sold_at, s.invoice_no, s.user_id FROM sale_item si JOIN sale s ON s.id = si.sale_id LEFT JOIN open_item_review r ON r.sale_item_id = si.id WHERE si.is_open_item = 1 AND s.status = 'COMPLETED' AND r.id IS NULL ORDER BY si.id")

    def insert_open_item_review(self, conn: sqlite3.Connection, *, sale_item_id: int, status: str, converted_product_id: int | None, reviewed_by: int, reviewed_at: str) -> int:
        """Mark one line reviewed."""
        return int(conn.execute("INSERT INTO open_item_review(sale_item_id, status, converted_product_id, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?)", (sale_item_id, status, converted_product_id, reviewed_by, reviewed_at)).lastrowid)

    def day_summary(self, conn: sqlite3.Connection, *, user_id: int | None, start: str, end: str) -> tuple[int, int, int]:
        """``(bills, total paise, item lines)`` for completed sales in ``[start, end)``."""
        params: list[Any] = [start, end]
        where = "s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ?"
        if user_id is not None:
            where += " AND s.user_id = ?"
            params.append(user_id)
        row = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(s.total_paise), 0), (SELECT COUNT(*) FROM sale_item si JOIN sale s2 ON s2.id = si.sale_id WHERE {where.replace('s.', 's2.')}) FROM sale s WHERE {where}", (*params, *params)).fetchone()  # noqa: S608
        return int(row[0]), int(row[1]), int(row[2])

    def insert_sale(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
        """Header row → id."""
        return _insert(conn, "sale", fields, _SALE_INSERT_COLS)

    def insert_sale_item(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
        """Line row → id."""
        return _insert(conn, "sale_item", fields, _SALE_ITEM_INSERT_COLS)

    def insert_payment(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
        """Payment row → id."""
        return _insert(conn, "payment", fields, _PAYMENT_INSERT_COLS)

    def mark_printed(self, conn: sqlite3.Connection, sale_id: int, *, reprint: bool) -> None:
        """``receipt_printed = 1`` (+ ``reprint_count`` when it is a reprint) — the only sale columns the trigger lets change."""
        if reprint:
            conn.execute("UPDATE sale SET receipt_printed = 1, reprint_count = reprint_count + 1 WHERE id = ?", (sale_id,))
        else:
            conn.execute("UPDATE sale SET receipt_printed = 1 WHERE id = ?", (sale_id,))

    # -- held carts -------------------------------------------------------------------------------
    def insert_held(self, conn: sqlite3.Connection, *, user_id: int, label: str | None, payload: str, line_count: int, created_at: str) -> int:
        """Header + one payload row (the schema's ``held_cart_line`` carries the JSON)."""
        held_id = int(conn.execute("INSERT INTO held_cart(user_id, label, created_at) VALUES (?, ?, ?)", (user_id, label, created_at)).lastrowid)
        conn.execute("INSERT INTO held_cart_line(held_cart_id, product_id, qty, discount_paise, payload) VALUES (?, NULL, ?, 0, ?)", (held_id, max(1, line_count), payload))
        return held_id

    def held_carts(self, conn: sqlite3.Connection, *, user_id: int | None) -> list[HeldCartRecord]:
        """Oldest first."""
        sql = "SELECT h.id, h.user_id, h.label, h.created_at, l.qty, l.payload FROM held_cart h JOIN held_cart_line l ON l.held_cart_id = h.id"
        params: tuple[Any, ...] = ()
        if user_id is not None:
            sql += " WHERE h.user_id = ?"
            params = (user_id,)
        return [HeldCartRecord(int(r[0]), int(r[1]), r[2], str(r[3]), int(_dec(r[4])), str(r[5])) for r in conn.execute(sql + " ORDER BY h.id", params).fetchall()]

    def held_cart(self, conn: sqlite3.Connection, held_id: int) -> HeldCartRecord | None:
        """One held cart."""
        row = conn.execute("SELECT h.id, h.user_id, h.label, h.created_at, l.qty, l.payload FROM held_cart h JOIN held_cart_line l ON l.held_cart_id = h.id WHERE h.id = ?", (held_id,)).fetchone()
        return HeldCartRecord(int(row[0]), int(row[1]), row[2], str(row[3]), int(_dec(row[4])), str(row[5])) if row else None

    def delete_held(self, conn: sqlite3.Connection, held_id: int) -> None:
        """Remove header + line."""
        conn.execute("DELETE FROM held_cart_line WHERE held_cart_id = ?", (held_id,))
        conn.execute("DELETE FROM held_cart WHERE id = ?", (held_id,))

    # -- customers (phone book for WhatsApp bills) ------------------------------------------------
    def upsert_customer(self, conn: sqlite3.Connection, *, phone: str, name: str | None) -> CustomerRecord:
        """One row per phone; a non-empty name refreshes the stored one."""
        row = conn.execute("SELECT id, name, phone, note FROM customer WHERE phone = ?", (phone,)).fetchone()
        if row is None:
            cid = int(conn.execute("INSERT INTO customer(name, phone) VALUES (?, ?)", (name or None, phone)).lastrowid)
            return CustomerRecord(cid, name or None, phone, None)
        if name and name != row[1]:
            conn.execute("UPDATE customer SET name = ? WHERE id = ?", (name, row[0]))
            return CustomerRecord(int(row[0]), name, str(row[2]), row[3])
        return CustomerRecord(int(row[0]), row[1], str(row[2]), row[3])

    def customer(self, conn: sqlite3.Connection, customer_id: int) -> CustomerRecord | None:
        """By id."""
        row = conn.execute("SELECT id, name, phone, note FROM customer WHERE id = ?", (customer_id,)).fetchone()
        return CustomerRecord(int(row[0]), row[1], row[2], row[3]) if row else None


# ----------------------------------------------------------------------------- print queue
class SqlitePrintQueueStore:
    """``services.print_queue.PrintQueueStore`` - the ``print_pending`` table."""

    def enqueue(self, conn: sqlite3.Connection, *, sale_id: int, queued_at: str, attempts: int) -> int:
        """One row per unprinted sale (a second enqueue for the same sale is ignored)."""
        row = conn.execute("SELECT id FROM print_pending WHERE sale_id = ? AND printed_at IS NULL", (sale_id,)).fetchone()
        if row:
            return int(row[0])
        return int(conn.execute("INSERT INTO print_pending(sale_id, queued_at, attempts) VALUES (?, ?, ?)", (sale_id, queued_at, attempts)).lastrowid)

    def pending(self, conn: sqlite3.Connection) -> list[tuple[int, int, str, int]]:
        """``(id, sale_id, queued_at, attempts)`` oldest first."""
        return [(int(r[0]), int(r[1]), str(r[2]), int(r[3])) for r in conn.execute("SELECT id, sale_id, queued_at, attempts FROM print_pending WHERE printed_at IS NULL ORDER BY id").fetchall()]

    def pending_count(self, conn: sqlite3.Connection) -> int:
        """How many bills went out without paper."""
        return int(conn.execute("SELECT COUNT(*) FROM print_pending WHERE printed_at IS NULL").fetchone()[0])

    def mark_printed(self, conn: sqlite3.Connection, queue_id: int, *, at: str) -> None:
        """Done."""
        conn.execute("UPDATE print_pending SET printed_at = ?, attempts = attempts + 1 WHERE id = ?", (at, queue_id))

    def bump_attempt(self, conn: sqlite3.Connection, queue_id: int) -> None:
        """Failed again."""
        conn.execute("UPDATE print_pending SET attempts = attempts + 1 WHERE id = ?", (queue_id,))


# ----------------------------------------------------------------------------- hardware overrides
class SqliteHardwareStore:
    """``services.hardware_guard.OverrideStore`` - the ``hardware_override`` table."""

    def insert_override(self, conn: sqlite3.Connection, *, kind: str, owner_user_id: int, started_at: str, expires_at: str, reason: str | None) -> int:
        """One time-boxed row."""
        return int(conn.execute("INSERT INTO hardware_override(kind, owner_user_id, started_at, expires_at, reason) VALUES (?, ?, ?, ?, ?)", (kind, owner_user_id, started_at, expires_at, reason)).lastrowid)

    def active_override(self, conn: sqlite3.Connection, *, kind: str, now: str) -> OverrideRecord | None:
        """Newest unexpired row for the kind."""
        row = conn.execute("SELECT id, kind, owner_user_id, started_at, expires_at, reason FROM hardware_override WHERE kind = ? AND expires_at > ? ORDER BY id DESC LIMIT 1", (kind, now)).fetchone()
        return OverrideRecord(int(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4]), row[5]) if row else None

    def expire_override(self, conn: sqlite3.Connection, override_id: int, *, at: str) -> None:
        """End early."""
        conn.execute("UPDATE hardware_override SET expires_at = ? WHERE id = ?", (at, override_id))

    def overrides(self, conn: sqlite3.Connection, *, since: str | None, limit: int) -> list[OverrideRecord]:
        """History for the overrides report."""
        if since:
            rows = conn.execute("SELECT id, kind, owner_user_id, started_at, expires_at, reason FROM hardware_override WHERE started_at >= ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        else:
            rows = conn.execute("SELECT id, kind, owner_user_id, started_at, expires_at, reason FROM hardware_override ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [OverrideRecord(int(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]), r[5]) for r in rows]


# ----------------------------------------------------------------------------- shifts & cash
_SHIFT_COLS = "id, user_id, opened_at, closed_at, opening_float_paise, expected_cash_paise, counted_cash_paise, denominations, variance_paise, closed_by_user_id, status, note, opening_denominations, declared_json, approved_by_user_id"
_SHIFT_CLOSE_COLS = frozenset({"closed_at", "expected_cash_paise", "counted_cash_paise", "denominations", "variance_paise", "closed_by_user_id", "status", "note", "declared_json", "approved_by_user_id"})


def _shift(row: tuple[Any, ...]) -> ShiftRecord:
    return ShiftRecord(int(row[0]), int(row[1]), str(row[2]), row[3], int(row[4]), row[5], row[6], row[7], row[8], row[9], str(row[10]), row[11], row[12], row[13], row[14])


def _cash_event(row: tuple[Any, ...]) -> CashEventRecord:
    return CashEventRecord(int(row[0]), int(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]), row[6], str(row[7]))


class SqliteShiftStore:
    """``services.shift.ShiftStore``."""

    def insert_shift(self, conn: sqlite3.Connection, *, user_id: int, opened_at: str, opening_float_paise: int, opening_denominations: str | None = None) -> int:
        """OPEN row (the partial unique index refuses a second open shift per user)."""
        return int(conn.execute("INSERT INTO shift(user_id, opened_at, opening_float_paise, opening_denominations, status) VALUES (?, ?, ?, ?, 'OPEN')", (user_id, opened_at, opening_float_paise, opening_denominations)).lastrowid)

    def shift(self, conn: sqlite3.Connection, shift_id: int) -> ShiftRecord | None:
        """One shift."""
        row = conn.execute(f"SELECT {_SHIFT_COLS} FROM shift WHERE id = ?", (shift_id,)).fetchone()  # noqa: S608
        return _shift(row) if row else None

    def open_shift_for(self, conn: sqlite3.Connection, user_id: int) -> ShiftRecord | None:
        """The user's OPEN shift."""
        row = conn.execute(f"SELECT {_SHIFT_COLS} FROM shift WHERE user_id = ? AND status = 'OPEN'", (user_id,)).fetchone()  # noqa: S608
        return _shift(row) if row else None

    def open_shifts(self, conn: sqlite3.Connection) -> list[ShiftRecord]:
        """All OPEN shifts, oldest first."""
        return [_shift(r) for r in conn.execute(f"SELECT {_SHIFT_COLS} FROM shift WHERE status = 'OPEN' ORDER BY id").fetchall()]  # noqa: S608

    def close_shift(self, conn: sqlite3.Connection, shift_id: int, **fields: Any) -> None:
        """Set the closing columns."""
        unknown = set(fields) - _SHIFT_CLOSE_COLS
        if unknown:
            raise ValueError(f"shift: not updatable {sorted(unknown)}")
        conn.execute(f"UPDATE shift SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), shift_id))  # noqa: S608

    def insert_cash_event(self, conn: sqlite3.Connection, *, shift_id: int, type: str, amount_paise: int, reason: str, user_id: int, approved_by_user_id: int | None, at: str) -> int:  # noqa: A002
        """One cash movement."""
        return int(conn.execute("INSERT INTO cash_event(shift_id, type, amount_paise, reason, user_id, approved_by_user_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)", (shift_id, type, amount_paise, reason, user_id, approved_by_user_id, at)).lastrowid)

    def cash_events(self, conn: sqlite3.Connection, shift_id: int) -> list[CashEventRecord]:
        """Oldest first."""
        return [_cash_event(r) for r in conn.execute("SELECT id, shift_id, type, amount_paise, reason, user_id, approved_by_user_id, at FROM cash_event WHERE shift_id = ? ORDER BY id", (shift_id,)).fetchall()]

    def sales_figures(self, conn: sqlite3.Connection, shift_id: int) -> dict[str, Any]:
        """Everything the X/Z report needs from the sales tables for one shift."""
        head = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_paise), 0), COALESCE(SUM(item_discount_paise), 0), COALESCE(SUM(bill_discount_paise), 0), MIN(invoice_no), MAX(invoice_no) FROM sale WHERE shift_id = ? AND status = 'COMPLETED'", (shift_id,)).fetchone()
        by_mode: dict[str, int] = {}
        for mode, amount in conn.execute("SELECT p.mode, COALESCE(SUM(p.amount_paise), 0) FROM payment p JOIN sale s ON s.id = p.sale_id WHERE s.shift_id = ? AND s.status = 'COMPLETED' GROUP BY p.mode", (shift_id,)).fetchall():
            by_mode[str(mode)] = int(amount)
        cash_net = int(conn.execute("SELECT COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN p.amount_paise ELSE -p.amount_paise END), 0) FROM payment p JOIN sale s ON s.id = p.sale_id WHERE s.shift_id = ? AND p.mode = 'CASH'", (shift_id,)).fetchone()[0])
        items = conn.execute("SELECT COALESCE(SUM(si.qty), 0), SUM(CASE WHEN si.entry_method = 'LOOKUP' THEN 1 ELSE 0 END), SUM(CASE WHEN si.is_open_item = 1 THEN 1 ELSE 0 END) FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE s.shift_id = ? AND s.status = 'COMPLETED'", (shift_id,)).fetchone()
        voids = conn.execute("SELECT COUNT(*), COALESCE(SUM(ABS(total_paise)), 0) FROM sale WHERE shift_id = ? AND status IN ('VOIDED', 'REFUND')", (shift_id,)).fetchone()
        return {
            "bills": int(head[0]), "total_paise": int(head[1]), "item_discount_paise": int(head[2]), "bill_discount_paise": int(head[3]),
            "first_invoice": head[4], "last_invoice": head[5], "by_mode": by_mode, "cash_net_paise": cash_net,
            "items": items[0] or 0, "manual_lines": int(items[1] or 0), "open_item_lines": int(items[2] or 0),
            "voids": int(voids[0]), "refunds_paise": int(voids[1]),
        }

    def closed_shifts(self, conn: sqlite3.Connection, *, user_id: int | None, limit: int) -> list[ShiftRecord]:
        """Newest first."""
        if user_id is None:
            rows = conn.execute(f"SELECT {_SHIFT_COLS} FROM shift WHERE status = 'CLOSED' ORDER BY closed_at DESC, id DESC LIMIT ?", (limit,)).fetchall()  # noqa: S608
        else:
            rows = conn.execute(f"SELECT {_SHIFT_COLS} FROM shift WHERE status = 'CLOSED' AND user_id = ? ORDER BY closed_at DESC, id DESC LIMIT ?", (user_id, limit)).fetchall()  # noqa: S608
        return [_shift(r) for r in rows]


# ----------------------------------------------------------------------------- reports
class SqliteReportStore:
    """``services.report.ReportStore`` - control reports straight from the ledger tables."""

    def discount_audit(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Ad-hoc discount lines per cashier and reason (offer discounts excluded)."""
        return _row_dicts(conn, "SELECT s.user_id, si.discount_reason AS reason, COUNT(*) AS lines, COUNT(DISTINCT s.id) AS bills, COALESCE(SUM(si.discount_paise), 0) AS paise FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ? AND si.discount_paise > 0 AND si.offer_id IS NULL GROUP BY s.user_id, si.discount_reason", (since, until))

    def offer_usage(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Lines and rupees per named offer."""
        return _row_dicts(conn, "SELECT o.id AS offer_id, o.name, COUNT(*) AS lines, COALESCE(SUM(si.discount_paise), 0) AS paise FROM sale_item si JOIN sale s ON s.id = si.sale_id JOIN offer o ON o.id = si.offer_id WHERE s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ? GROUP BY o.id, o.name ORDER BY paise DESC", (since, until))

    def manual_lines(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Per cashier: total lines, lookup lines, open-item lines and their value."""
        return _row_dicts(conn, "SELECT s.user_id, COUNT(*) AS total_lines, SUM(CASE WHEN si.entry_method = 'LOOKUP' THEN 1 ELSE 0 END) AS lookup_lines, SUM(CASE WHEN si.is_open_item = 1 THEN 1 ELSE 0 END) AS open_item_lines, COALESCE(SUM(CASE WHEN si.is_open_item = 1 THEN si.line_total_paise ELSE 0 END), 0) AS open_item_paise FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ? GROUP BY s.user_id", (since, until))

    def voids(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Credit notes with their original bill, newest first."""
        return _row_dicts(conn, "SELECT v.invoice_no, o.invoice_no AS original_invoice, v.status, v.sold_at, v.user_id, v.approved_by_user_id, v.total_paise, v.note FROM sale v JOIN sale o ON o.id = v.original_sale_id WHERE v.status IN ('VOIDED', 'REFUND') AND v.sold_at >= ? AND v.sold_at < ? ORDER BY v.id DESC", (since, until))

    def audit_entries(self, conn: sqlite3.Connection, *, since: str | None, until: str | None, actions: tuple[str, ...] | None, user_id: int | None, limit: int, overrides_only: bool) -> list[AuditEntry]:
        """Filtered audit rows, newest first."""
        where = ["1 = 1"]
        params: list[Any] = []
        if since:
            where.append("at >= ?")
            params.append(since)
        if until:
            where.append("at < ?")
            params.append(until)
        if actions:
            where.append(f"action IN ({', '.join('?' for _ in actions)})")
            params.extend(actions)
        if user_id is not None:
            where.append("(user_id = ? OR actor_user_id = ?)")
            params.extend([user_id, user_id])
        if overrides_only:
            where.append("(actor_user_id IS NOT NULL OR action IN ('OVERRIDE', 'HARDWARE_OVERRIDE', 'HARDWARE_OVERRIDE_ENDED', 'DISCOUNT_OVERRIDE'))")
        params.append(limit)
        rows = conn.execute(f"SELECT id, at, user_id, actor_user_id, action, entity, entity_id, before, after, correlation_id, ip, prev_hash, hash FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params).fetchall()  # noqa: S608
        return [AuditEntry(id=int(r[0]), at=r[1], user_id=r[2], actor_user_id=r[3], action=r[4], entity=r[5], entity_id=r[6], before=r[7], after=r[8], correlation_id=r[9], ip=r[10], prev_hash=r[11], hash=r[12]) for r in rows]

    def shrinkage(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Damage / negative adjustments / stock-take losses per product, valued at cost else shelf price."""
        return _row_dicts(conn, "SELECT p.id AS product_id, p.name, COALESCE(SUM(CASE WHEN m.type = 'DAMAGE' THEN -m.qty_delta ELSE 0 END), 0) AS damage_qty, COALESCE(SUM(CASE WHEN m.type = 'ADJUSTMENT' AND m.qty_delta < 0 THEN -m.qty_delta ELSE 0 END), 0) AS adjustment_qty, COALESCE(SUM(CASE WHEN m.type = 'STOCK_TAKE' AND m.qty_delta < 0 THEN -m.qty_delta ELSE 0 END), 0) AS stock_take_qty, CAST(COALESCE(SUM(CASE WHEN m.qty_delta < 0 AND m.type IN ('DAMAGE', 'ADJUSTMENT', 'STOCK_TAKE') THEN -m.qty_delta * COALESCE(p.cost_paise, p.price_paise) ELSE 0 END), 0) AS INTEGER) AS value_paise FROM stock_movement m JOIN product p ON p.id = m.product_id WHERE m.type IN ('DAMAGE', 'ADJUSTMENT', 'STOCK_TAKE') AND m.qty_delta < 0 AND m.created_at >= ? AND m.created_at < ? GROUP BY p.id, p.name ORDER BY value_paise DESC", (since, until))

    def cashier_performance(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Per cashier over completed bills + credit notes they raised + shifts they closed."""
        return _row_dicts(conn, """
            SELECT u.id AS user_id,
                   (SELECT COUNT(*) FROM sale s WHERE s.user_id = u.id AND s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ?) AS bills,
                   (SELECT COALESCE(SUM(s.total_paise), 0) FROM sale s WHERE s.user_id = u.id AND s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ?) AS sales_paise,
                   (SELECT COALESCE(SUM(s.item_discount_paise + s.bill_discount_paise), 0) FROM sale s WHERE s.user_id = u.id AND s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ?) AS discount_paise,
                   (SELECT COUNT(*) FROM sale s WHERE s.user_id = u.id AND s.status IN ('VOIDED', 'REFUND') AND s.sold_at >= ? AND s.sold_at < ?) AS voids,
                   (SELECT COUNT(*) FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE s.user_id = u.id AND s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ? AND (si.entry_method = 'LOOKUP' OR si.is_open_item = 1)) AS manual_lines,
                   (SELECT COUNT(*) FROM shift sh WHERE sh.user_id = u.id AND sh.status = 'CLOSED' AND sh.closed_at >= ? AND sh.closed_at < ?) AS shifts_closed,
                   (SELECT COALESCE(SUM(sh.variance_paise), 0) FROM shift sh WHERE sh.user_id = u.id AND sh.status = 'CLOSED' AND sh.closed_at >= ? AND sh.closed_at < ?) AS variance_paise
            FROM user u
            WHERE EXISTS (SELECT 1 FROM sale s WHERE s.user_id = u.id AND s.sold_at >= ? AND s.sold_at < ?)
               OR EXISTS (SELECT 1 FROM shift sh WHERE sh.user_id = u.id AND sh.closed_at >= ? AND sh.closed_at < ?)
            """, (since, until) * 9)

    def tax_summary(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Per rate: taxable / CGST / SGST over completed bills minus credit notes."""
        return _row_dicts(conn, "SELECT si.tax_rate_bp, COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN si.taxable_paise ELSE -si.taxable_paise END), 0) AS taxable_paise, COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN si.cgst_paise ELSE -si.cgst_paise END), 0) AS cgst_paise, COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN si.sgst_paise ELSE -si.sgst_paise END), 0) AS sgst_paise, COUNT(DISTINCT CASE WHEN s.status = 'COMPLETED' THEN s.id END) AS bills FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE s.sold_at >= ? AND s.sold_at < ? GROUP BY si.tax_rate_bp ORDER BY si.tax_rate_bp", (since, until))

    # -- sales-side (Phase 5) -----------------------------------------------------------------------
    @staticmethod
    def _day_expr(offset_minutes: int) -> str:
        return f"date(s.sold_at, '+{offset_minutes} minutes')"

    def sales_by_day(self, conn: sqlite3.Connection, *, since: str, until: str, offset_minutes: int) -> list[dict[str, Any]]:
        """Completed bills per shop day plus credit notes as refunds."""
        day = self._day_expr(offset_minutes)
        return _row_dicts(conn, f"""
            SELECT {day} AS day,
                   SUM(CASE WHEN s.status = 'COMPLETED' THEN 1 ELSE 0 END) AS bills,
                   COALESCE((SELECT SUM(si.qty) FROM sale_item si JOIN sale s2 ON s2.id = si.sale_id WHERE s2.status = 'COMPLETED' AND date(s2.sold_at, '+{offset_minutes} minutes') = {day} AND s2.sold_at >= ? AND s2.sold_at < ?), 0) AS items,
                   COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN s.subtotal_paise ELSE 0 END), 0) AS subtotal_paise,
                   COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN s.item_discount_paise + s.bill_discount_paise ELSE 0 END), 0) AS discount_paise,
                   COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN s.cgst_paise + s.sgst_paise ELSE 0 END), 0) AS tax_paise,
                   COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN s.total_paise ELSE 0 END), 0) AS total_paise,
                   COALESCE((SELECT SUM(p.amount_paise) FROM payment p JOIN sale s3 ON s3.id = p.sale_id WHERE s3.status = 'COMPLETED' AND p.mode = 'CASH' AND date(s3.sold_at, '+{offset_minutes} minutes') = {day} AND s3.sold_at >= ? AND s3.sold_at < ?), 0) AS cash_paise,
                   COALESCE((SELECT SUM(p.amount_paise) FROM payment p JOIN sale s3 ON s3.id = p.sale_id WHERE s3.status = 'COMPLETED' AND p.mode = 'UPI' AND date(s3.sold_at, '+{offset_minutes} minutes') = {day} AND s3.sold_at >= ? AND s3.sold_at < ?), 0) AS upi_paise,
                   COALESCE((SELECT SUM(p.amount_paise) FROM payment p JOIN sale s3 ON s3.id = p.sale_id WHERE s3.status = 'COMPLETED' AND p.mode = 'CARD' AND date(s3.sold_at, '+{offset_minutes} minutes') = {day} AND s3.sold_at >= ? AND s3.sold_at < ?), 0) AS card_paise,
                   COALESCE(SUM(CASE WHEN s.status IN ('VOIDED', 'REFUND') THEN s.total_paise ELSE 0 END), 0) AS refunds_paise
            FROM sale s WHERE s.sold_at >= ? AND s.sold_at < ?
            GROUP BY day ORDER BY day
            """, (since, until) * 5)  # noqa: S608 (offset is an int we format ourselves)

    def item_sales(self, conn: sqlite3.Connection, *, product_id: int, since: str, until: str, offset_minutes: int) -> list[dict[str, Any]]:
        """One product per shop day (completed bills)."""
        day = self._day_expr(offset_minutes)
        return _row_dicts(conn, f"SELECT {day} AS day, COALESCE(SUM(si.qty), 0) AS qty, COALESCE(SUM(si.line_total_paise), 0) AS amount_paise, COUNT(DISTINCT s.id) AS bills FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.product_id = ? AND s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ? GROUP BY day ORDER BY day", (product_id, since, until))  # noqa: S608

    def product_totals(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Quantity and rupees per catalogue product over completed bills (open items excluded)."""
        return _row_dicts(conn, "SELECT si.product_id, p.name, COALESCE(SUM(si.qty), 0) AS qty, COALESCE(SUM(si.line_total_paise), 0) AS amount_paise FROM sale_item si JOIN sale s ON s.id = si.sale_id JOIN product p ON p.id = si.product_id WHERE s.status = 'COMPLETED' AND si.is_open_item = 0 AND s.sold_at >= ? AND s.sold_at < ? GROUP BY si.product_id, p.name", (since, until))

    def item_totals(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Every line sold in the window grouped per item — catalogue products by id, open (typed) items by name — with the unit columns needed to print "200 g" / "2 pcs"."""
        return _row_dicts(conn, """
            SELECT CASE WHEN si.is_open_item = 1 THEN NULL ELSE si.product_id END AS product_id,
                   CASE WHEN si.is_open_item = 1 THEN COALESCE(si.open_item_name, si.name_snapshot) ELSE p.name END AS name,
                   si.is_open_item,
                   COALESCE(p.sale_mode, 'PIECE') AS sale_mode,
                   COALESCE(p.base_unit, si.unit) AS base_unit,
                   si.unit,
                   COALESCE(SUM(si.qty), 0) AS qty,
                   COALESCE(SUM(si.line_total_paise), 0) AS amount_paise,
                   COUNT(DISTINCT s.id) AS bills
            FROM sale_item si
            JOIN sale s ON s.id = si.sale_id
            LEFT JOIN product p ON p.id = si.product_id AND si.is_open_item = 0
            WHERE s.status = 'COMPLETED' AND s.sold_at >= ? AND s.sold_at < ?
            GROUP BY si.is_open_item, CASE WHEN si.is_open_item = 1 THEN COALESCE(si.open_item_name, si.name_snapshot) ELSE si.product_id END
            ORDER BY amount_paise DESC, name
            """, (since, until))

    def basket_pairs(self, conn: sqlite3.Connection, *, since: str, until: str, limit: int) -> list[dict[str, Any]]:
        """Product pairs that appear together on one bill, counted once per bill (a < b avoids mirror rows)."""
        sql = (
            "WITH lines AS (SELECT DISTINCT si.sale_id, si.product_id FROM sale_item si JOIN sale s ON s.id = si.sale_id "
            "WHERE s.status = 'COMPLETED' AND si.is_open_item = 0 AND si.product_id IS NOT NULL AND s.sold_at >= ? AND s.sold_at < ?) "
            "SELECT a.product_id AS product_a, pa.name AS name_a, b.product_id AS product_b, pb.name AS name_b, COUNT(*) AS baskets "
            "FROM lines a JOIN lines b ON a.sale_id = b.sale_id AND a.product_id < b.product_id "
            "JOIN product pa ON pa.id = a.product_id JOIN product pb ON pb.id = b.product_id "
            "GROUP BY a.product_id, b.product_id ORDER BY baskets DESC, name_a, name_b LIMIT ?"
        )
        return _row_dicts(conn, sql, (since, until, limit))

    def completed_bills(self, conn: sqlite3.Connection, *, since: str, until: str) -> int:
        """How many completed bills fall in the window (denominator for basket shares)."""
        row = conn.execute("SELECT COUNT(*) FROM sale WHERE status = 'COMPLETED' AND sold_at >= ? AND sold_at < ?", (since, until)).fetchone()
        return int(row[0]) if row else 0

    def dead_stock(self, conn: sqlite3.Connection, *, since: str) -> list[dict[str, Any]]:
        """Active products with stock > 0 and no completed sale since ``since``."""
        return _row_dicts(conn, "SELECT p.id AS product_id, p.name, ps.qty AS stock_qty, CAST(ps.qty * COALESCE(p.cost_paise, p.price_paise) AS INTEGER) AS value_paise, (SELECT MAX(s.sold_at) FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.product_id = p.id AND s.status = 'COMPLETED') AS last_sold_at FROM product p JOIN product_stock ps ON ps.product_id = p.id WHERE p.is_active = 1 AND p.is_system = 0 AND p.deleted_at IS NULL AND ps.qty > 0 AND NOT EXISTS (SELECT 1 FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.product_id = p.id AND s.status = 'COMPLETED' AND s.sold_at >= ?) ORDER BY value_paise DESC", (since,))

    def low_stock(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """At or below the minimum (minimum > 0)."""
        return _row_dicts(conn, "SELECT p.id AS product_id, p.name, COALESCE(ps.qty, 0) AS stock_qty, p.min_stock, p.reorder_qty FROM product p LEFT JOIN product_stock ps ON ps.product_id = p.id WHERE p.is_active = 1 AND p.is_system = 0 AND p.deleted_at IS NULL AND p.min_stock > 0 AND COALESCE(ps.qty, 0) <= p.min_stock ORDER BY (COALESCE(ps.qty, 0) / p.min_stock), p.name")

    def purchases_by_supplier(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Totals per supplier, biggest first."""
        return _row_dicts(conn, "SELECT sp.name AS supplier, COUNT(*) AS purchases, COALESCE(SUM(pu.total_paise), 0) AS total_paise FROM purchase pu LEFT JOIN supplier sp ON sp.id = pu.supplier_id WHERE pu.created_at >= ? AND pu.created_at < ? GROUP BY sp.name ORDER BY total_paise DESC", (since, until))

    def purchases_by_day(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[tuple[str, int, int]]:
        """``(purchase_date, count, paise)``."""
        return [(str(r[0]), int(r[1]), int(r[2])) for r in conn.execute("SELECT purchase_date, COUNT(*), COALESCE(SUM(total_paise), 0) FROM purchase WHERE created_at >= ? AND created_at < ? GROUP BY purchase_date ORDER BY purchase_date", (since, until)).fetchall()]

    def profit_lines(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Catalogue lines with their cost snapshot (completed bills and credit notes)."""
        return _row_dicts(conn, "SELECT si.product_id, si.name_snapshot, si.qty, si.pricing_unit_qty_snapshot, si.line_total_paise, si.cost_paise_snapshot, s.status FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.is_open_item = 0 AND s.sold_at >= ? AND s.sold_at < ?", (since, until))

    def investment_lines(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[dict[str, Any]]:
        """Stock that came *in* (opening · purchase · positive corrections) per product and movement type, valued.

        Value = Σ qty × unit cost, where the unit cost is the movement's own snapshot, else the product's
        current purchase price, else its selling price (``estimated_lines`` counts those last-resort rows).
        Also returns the product's current stock so the caller can show "invested vs. still on the shelf".
        """
        return _row_dicts(conn, """
            SELECT p.id AS product_id, p.name, COALESCE(c.name, '') AS category, m.type,
                   COALESCE(SUM(m.qty_delta), 0) AS qty,
                   CAST(COALESCE(SUM(m.qty_delta * COALESCE(m.unit_cost_paise, p.cost_paise, p.price_paise)), 0) AS INTEGER) AS amount_paise,
                   SUM(CASE WHEN m.unit_cost_paise IS NULL AND p.cost_paise IS NULL THEN 1 ELSE 0 END) AS estimated_lines,
                   COUNT(*) AS movements,
                   MIN(m.created_at) AS first_at,
                   MAX(m.created_at) AS last_at,
                   COALESCE(ps.qty, 0) AS stock_qty,
                   CAST(COALESCE(ps.qty, 0) * COALESCE(p.cost_paise, p.price_paise) AS INTEGER) AS stock_value_paise
            FROM stock_movement m
            JOIN product p ON p.id = m.product_id
            LEFT JOIN category c ON c.id = p.category_id
            LEFT JOIN product_stock ps ON ps.product_id = p.id
            WHERE m.qty_delta > 0 AND m.type IN ('OPENING', 'PURCHASE', 'ADJUSTMENT', 'STOCK_TAKE')
              AND m.created_at >= ? AND m.created_at < ?
            GROUP BY p.id, m.type
            ORDER BY p.name, m.type
            """, (since, until))

    def sale_times(self, conn: sqlite3.Connection, *, since: str, until: str) -> list[str]:
        """``sold_at`` of completed bills."""
        return [str(r[0]) for r in conn.execute("SELECT sold_at FROM sale WHERE status = 'COMPLETED' AND sold_at >= ? AND sold_at < ?", (since, until)).fetchall()]

    def stock_as_of(self, conn: sqlite3.Connection, *, day: str) -> list[dict[str, Any]]:
        """``stock_daily_balance`` rows for a day."""
        return _row_dicts(conn, "SELECT b.product_id, p.name, b.qty_close, b.value_cost_paise FROM stock_daily_balance b JOIN product p ON p.id = b.product_id WHERE b.day = ? ORDER BY p.name", (day,))


# ----------------------------------------------------------------------------- forecast
class SqliteForecastStore:
    """``services.forecast.ForecastStore``."""

    def daily_sales(self, conn: sqlite3.Connection, *, product_id: int, since: str, offset_minutes: int) -> list[tuple[str, Decimal]]:
        """``(shop day, qty)`` of completed bills minus credit notes."""
        rows = conn.execute(f"SELECT date(s.sold_at, '+{int(offset_minutes)} minutes') AS day, COALESCE(SUM(CASE WHEN s.status = 'COMPLETED' THEN si.qty ELSE -si.qty END), 0) FROM sale_item si JOIN sale s ON s.id = si.sale_id WHERE si.product_id = ? AND s.sold_at >= ? GROUP BY day ORDER BY day", (product_id, since)).fetchall()  # noqa: S608
        return [(str(r[0]), _dec(r[1])) for r in rows]

    def stockout_days(self, conn: sqlite3.Connection, *, product_id: int, since_day: str) -> set[str]:
        """Days whose end-of-day balance was zero or below."""
        return {str(r[0]) for r in conn.execute("SELECT day FROM stock_daily_balance WHERE product_id = ? AND day >= ? AND qty_close <= 0", (product_id, since_day)).fetchall()}

    def category_name(self, conn: sqlite3.Connection, product_id: int) -> str | None:
        """For the festival uplift table."""
        row = conn.execute("SELECT c.name FROM product p LEFT JOIN category c ON c.id = p.category_id WHERE p.id = ?", (product_id,)).fetchone()
        return row[0] if row and row[0] else None

    def insert_snapshot(self, conn: sqlite3.Connection, *, computed_at: str, product_id: int, horizon_days: int, point: Decimal, low: Decimal, high: Decimal, method: str, confidence: str, caveats: str | None) -> int:
        """One nightly row."""
        return int(conn.execute("INSERT INTO forecast_snapshot(computed_at, product_id, horizon_days, point, low, high, method, confidence, caveats) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (computed_at, product_id, horizon_days, str(point), str(low), str(high), method, confidence, caveats)).lastrowid)

    def snapshots(self, conn: sqlite3.Connection, *, product_id: int, limit: int) -> list[ForecastSnapshotRecord]:
        """Newest first."""
        rows = conn.execute("SELECT id, computed_at, product_id, horizon_days, point, low, high, method, confidence, caveats FROM forecast_snapshot WHERE product_id = ? ORDER BY id DESC LIMIT ?", (product_id, limit)).fetchall()
        return [ForecastSnapshotRecord(int(r[0]), str(r[1]), int(r[2]), int(r[3]), _dec(r[4]), _dec(r[5]), _dec(r[6]), str(r[7]), str(r[8]), r[9]) for r in rows]


# ----------------------------------------------------------------------------- assistant: prompts, chat, help index
class SqlitePromptStore:
    """``services.prompt.PromptStore``."""

    _COLS = "id, version, editable_instructions, core_hash, default_language, extra_blocked_phrases, is_active, redteam_passed, created_by, created_at, note"

    @staticmethod
    def _rec(r: tuple[Any, ...]) -> PromptVersionRecord:
        return PromptVersionRecord(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5] or "[]"), bool(r[6]), bool(r[7]), r[8], str(r[9]), r[10])

    def versions(self, conn: sqlite3.Connection) -> list[PromptVersionRecord]:
        """Newest first."""
        return [self._rec(r) for r in conn.execute(f"SELECT {self._COLS} FROM prompt_version ORDER BY version DESC").fetchall()]  # noqa: S608

    def active(self, conn: sqlite3.Connection) -> PromptVersionRecord | None:
        """The one active row."""
        r = conn.execute(f"SELECT {self._COLS} FROM prompt_version WHERE is_active = 1").fetchone()  # noqa: S608
        return self._rec(r) if r else None

    def insert(self, conn: sqlite3.Connection, *, version: int, editable_instructions: str, core_hash: str, default_language: str, extra_blocked_phrases: str, redteam_passed: bool, created_by: int | None, created_at: str, note: str | None) -> int:
        """New inactive version."""
        return int(conn.execute("INSERT INTO prompt_version(version, editable_instructions, core_hash, default_language, extra_blocked_phrases, is_active, redteam_passed, created_by, created_at, note) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)", (version, editable_instructions, core_hash, default_language, extra_blocked_phrases, 1 if redteam_passed else 0, created_by, created_at, note)).lastrowid)

    def activate(self, conn: sqlite3.Connection, version: int) -> None:
        """Exactly one active row (partial unique index)."""
        conn.execute("UPDATE prompt_version SET is_active = 0 WHERE is_active = 1")
        conn.execute("UPDATE prompt_version SET is_active = 1 WHERE version = ?", (version,))


class SqliteChatStore:
    """``ai.chat_service.ChatStore``."""

    def create_session(self, conn: sqlite3.Connection, *, user_id: int, title: str | None, created_at: str) -> int:
        """New chat session."""
        return int(conn.execute("INSERT INTO chat_session(user_id, title, created_at) VALUES (?, ?, ?)", (user_id, title, created_at)).lastrowid)

    def session_owner(self, conn: sqlite3.Connection, session_id: int) -> int | None:
        """Who the session belongs to."""
        r = conn.execute("SELECT user_id FROM chat_session WHERE id = ?", (session_id,)).fetchone()
        return int(r[0]) if r else None

    def insert_message(self, conn: sqlite3.Connection, *, session_id: int, role: str, content: str, tool_calls: str | None, tokens: int | None, provider: str | None, model: str | None, language: str | None, guardrail_decision: str | None, created_at: str) -> int:
        """One turn."""
        return int(conn.execute("INSERT INTO chat_message(session_id, role, content, tool_calls, tokens, provider, model, language, guardrail_decision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (session_id, role, content, tool_calls, tokens, provider, model, language, guardrail_decision, created_at)).lastrowid)

    def messages(self, conn: sqlite3.Connection, *, session_id: int, limit: int) -> list[ChatMessageRecord]:
        """Oldest first, last ``limit`` rows."""
        rows = conn.execute("SELECT id, session_id, role, content, tool_calls, provider, model, language, guardrail_decision, created_at FROM (SELECT * FROM chat_message WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id", (session_id, limit)).fetchall()
        return [ChatMessageRecord(int(r[0]), int(r[1]), str(r[2]), str(r[3]), r[4], r[5], r[6], r[7], r[8], str(r[9])) for r in rows]

    def insert_security_event(self, conn: sqlite3.Connection, *, at: str, user_id: int, category: str, severity: str, message_excerpt: str | None, layer: str, owner_alerted: bool) -> int:
        """Flagged prompt or blocked answer."""
        return int(conn.execute("INSERT INTO chat_security_event(at, user_id, category, severity, message_excerpt, layer, owner_alerted) VALUES (?, ?, ?, ?, ?, ?, ?)", (at, user_id, category, severity, message_excerpt, layer, 1 if owner_alerted else 0)).lastrowid)

    def mark_alerted(self, conn: sqlite3.Connection, event_id: int) -> None:
        """Owner was notified for this event."""
        conn.execute("UPDATE chat_security_event SET owner_alerted = 1 WHERE id = ?", (event_id,))

    def flags_today(self, conn: sqlite3.Connection, *, user_id: int, since: str) -> int:
        """Flagged events for a user since the shop-day start."""
        r = conn.execute("SELECT COUNT(*) FROM chat_security_event WHERE user_id = ? AND at >= ?", (user_id, since)).fetchone()
        return int(r[0]) if r else 0

    def security_events(self, conn: sqlite3.Connection, *, since: str | None, limit: int) -> list[ChatSecurityEventRecord]:
        """Newest first for the Chat-security report."""
        if since:
            rows = conn.execute("SELECT id, at, user_id, category, severity, message_excerpt, layer, owner_alerted FROM chat_security_event WHERE at >= ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        else:
            rows = conn.execute("SELECT id, at, user_id, category, severity, message_excerpt, layer, owner_alerted FROM chat_security_event ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [ChatSecurityEventRecord(int(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]), r[5], str(r[6]), bool(r[7])) for r in rows]

    def add_usage(self, conn: sqlite3.Connection, *, day: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int, embed_tokens: int, requests: int, errors: int) -> None:
        """Upsert the per-day aggregate."""
        conn.execute("INSERT INTO ai_usage(day, provider, model, prompt_tokens, completion_tokens, embed_tokens, requests, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day, provider, model) DO UPDATE SET prompt_tokens = prompt_tokens + excluded.prompt_tokens, completion_tokens = completion_tokens + excluded.completion_tokens, embed_tokens = embed_tokens + excluded.embed_tokens, requests = requests + excluded.requests, errors = errors + excluded.errors", (day, provider, model, prompt_tokens, completion_tokens, embed_tokens, requests, errors))

    def usage_since(self, conn: sqlite3.Connection, *, since_day: str) -> list[dict[str, Any]]:
        """Per provider/model totals from ``since_day``."""
        return _row_dicts(conn, "SELECT provider, model, SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, SUM(embed_tokens) AS embed_tokens, SUM(requests) AS requests, SUM(errors) AS errors FROM ai_usage WHERE day >= ? GROUP BY provider, model ORDER BY provider, model", (since_day,))


class SqliteIndexStore:
    """``ai.indexer.IndexStore``."""

    def chunks(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """All help chunks."""
        return _row_dicts(conn, "SELECT id, source, content, content_hash FROM doc_chunk ORDER BY id")

    def insert_chunk(self, conn: sqlite3.Connection, *, source: str, content: str, content_hash: str) -> int:
        """One chunk."""
        return int(conn.execute("INSERT INTO doc_chunk(source, content, content_hash) VALUES (?, ?, ?)", (source, content, content_hash)).lastrowid)

    def delete_chunks(self, conn: sqlite3.Connection, ids: Iterable[int]) -> None:
        """Remove chunks and their vectors."""
        for cid in ids:
            conn.execute("DELETE FROM embedding WHERE owner_type = 'DOC_CHUNK' AND owner_id = ?", (cid,))
            conn.execute("DELETE FROM doc_chunk WHERE id = ?", (cid,))

    def embeddings_for(self, conn: sqlite3.Connection, *, owner_type: str, provider: str, model: str) -> dict[int, bytes]:
        """owner_id -> vector blob for one provider/model."""
        return {int(r[0]): bytes(r[1]) for r in conn.execute("SELECT owner_id, vector FROM embedding WHERE owner_type = ? AND provider = ? AND model = ?", (owner_type, provider, model)).fetchall()}

    def upsert_embedding(self, conn: sqlite3.Connection, *, owner_type: str, owner_id: int, provider: str, model: str, vector: bytes, dim: int) -> None:
        """Insert or replace one vector."""
        conn.execute("INSERT INTO embedding(owner_type, owner_id, provider, model, vector, dim) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner_type, owner_id, provider, model) DO UPDATE SET vector = excluded.vector, dim = excluded.dim", (owner_type, owner_id, provider, model, vector, dim))


# ----------------------------------------------------------------------------- publishing
class SqlitePublishStore:
    """``publishing.outbox.PublishStore`` on ``publish_queue``."""

    _COLS = "id, channel, document, file_path, status, attempts, next_retry_at, last_error, created_at"

    @staticmethod
    def _item(r: tuple[Any, ...]) -> QueueItem:
        return QueueItem(int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), int(r[5]), r[6], r[7], str(r[8]))

    def insert(self, conn: sqlite3.Connection, *, channel: str, document: str, file_path: str, created_at: str) -> int:
        """Queue a delivery."""
        return int(conn.execute("INSERT INTO publish_queue(channel, document, file_path, status, attempts, created_at) VALUES (?, ?, ?, 'PENDING', 0, ?)", (channel, document, file_path, created_at)).lastrowid)

    def due(self, conn: sqlite3.Connection, *, now: str) -> list[QueueItem]:
        """Pending and ready."""
        rows = conn.execute(f"SELECT {self._COLS} FROM publish_queue WHERE status = 'PENDING' AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY id", (now,)).fetchall()  # noqa: S608
        return [self._item(r) for r in rows]

    def update(self, conn: sqlite3.Connection, item_id: int, *, status: str, attempts: int, next_retry_at: str | None, last_error: str | None) -> None:
        """Outcome of an attempt."""
        conn.execute("UPDATE publish_queue SET status = ?, attempts = ?, next_retry_at = ?, last_error = ? WHERE id = ?", (status, attempts, next_retry_at, last_error, item_id))

    def get(self, conn: sqlite3.Connection, item_id: int) -> QueueItem | None:
        """One row."""
        r = conn.execute(f"SELECT {self._COLS} FROM publish_queue WHERE id = ?", (item_id,)).fetchone()  # noqa: S608
        return self._item(r) if r else None

    def counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Per status."""
        return {str(r[0]): int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM publish_queue GROUP BY status").fetchall()}

    def recent(self, conn: sqlite3.Connection, *, limit: int) -> list[QueueItem]:
        """Newest first."""
        return [self._item(r) for r in conn.execute(f"SELECT {self._COLS} FROM publish_queue ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]  # noqa: S608

    def last_sent_at(self, conn: sqlite3.Connection) -> str | None:
        """Creation time of the newest SENT row (attempts are not timestamped separately)."""
        r = conn.execute("SELECT created_at FROM publish_queue WHERE status = 'SENT' ORDER BY id DESC LIMIT 1").fetchone()
        return str(r[0]) if r else None


# ----------------------------------------------------------------------------- cashier day book
class SqliteCashierRecordStore:
    """``services.cashier_records.CashierRecordStore`` on ``cashier_day_record``."""

    _COLS = "id, sale_id, invoice_no, business_day, sold_at, user_id, cashier_name, shift_id, status, line_count, item_qty, total_paise, payment_modes, items_json, created_at"

    @staticmethod
    def _row(r: tuple[Any, ...]) -> CashierDayRecordRow:
        items = tuple(DayRecordItem(str(i.get("name", "")), str(i.get("qty", "")), str(i.get("unit", "")), int(i.get("rate_paise", 0)), int(i.get("amount_paise", 0)), str(i.get("entry_method", "")), bool(i.get("is_open_item", False)), int(i.get("discount_paise", 0))) for i in json.loads(r[13] or "[]"))
        return CashierDayRecordRow(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4]), int(r[5]), str(r[6]), int(r[7]), str(r[8]), int(r[9]), _dec(r[10]), int(r[11]), str(r[12] or ""), items, str(r[14]))

    def insert(self, conn: sqlite3.Connection, *, sale_id: int, invoice_no: str, business_day: str, sold_at: str, user_id: int, cashier_name: str, shift_id: int, status: str, line_count: int, item_qty: Decimal, total_paise: int, payment_modes: str, items_json: str, created_at: str) -> int | None:
        """One row per sale; a repeat for the same sale is ignored (returns None)."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO cashier_day_record(sale_id, invoice_no, business_day, sold_at, user_id, cashier_name, shift_id, status, line_count, item_qty, total_paise, payment_modes, items_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sale_id, invoice_no, business_day, sold_at, user_id, cashier_name, shift_id, status, line_count, str(item_qty), total_paise, payment_modes, items_json, created_at),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def list(self, conn: sqlite3.Connection, *, since_day: str, until_day: str, user_id: int | None, limit: int) -> list[CashierDayRecordRow]:
        """Rows whose shop day is within ``[since_day, until_day]``, newest first."""
        where = "business_day >= ? AND business_day <= ?"
        params: list[Any] = [since_day, until_day]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        rows = conn.execute(f"SELECT {self._COLS} FROM cashier_day_record WHERE {where} ORDER BY sold_at DESC, id DESC LIMIT ?", params).fetchall()  # noqa: S608
        return [self._row(r) for r in rows]

    def delete_before(self, conn: sqlite3.Connection, *, day: str) -> int:
        """Delete rows whose shop day is strictly before ``day``; returns the count."""
        return int(conn.execute("DELETE FROM cashier_day_record WHERE business_day < ?", (day,)).rowcount)

    def stats(self, conn: sqlite3.Connection) -> tuple[int, str | None, str | None]:
        """``(rows, oldest day, newest day)``."""
        r = conn.execute("SELECT COUNT(*), MIN(business_day), MAX(business_day) FROM cashier_day_record").fetchone()
        return (int(r[0]), r[1], r[2]) if r else (0, None, None)
