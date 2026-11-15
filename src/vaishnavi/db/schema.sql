-- Vaishnavi Store Manager — database schema (SQLite), version 0001.
-- Source of truth for the physical schema (docs/04). Applied by Alembic migration 0001_initial and
-- testable with the standard-library sqlite3 module (tests/core/test_db_schema.py).
--
-- Conventions
--   money      INTEGER paise               datetimes  TEXT ISO-8601 UTC ("2026-11-14T04:00:00Z")
--   quantities NUMERIC in the product's base unit (pcs / g / ml / cm)
--   booleans   INTEGER 0/1 with CHECK      enums      TEXT with CHECK
--   RAISE(ABORT, 'E-…') messages are error codes the application maps to plain-language text.
--
-- Protection rules enforced HERE (in addition to the service layer — docs/05 §5a, docs/14 §2a):
--   * stock changes only through stock_movement rows; product_stock is maintained ONLY by trigger;
--   * a SALE movement must reference an existing sale; non-OWNER users may create SALE movements only;
--   * sale / sale_item / payment / stock_movement / audit_log are append-only;
--   * qty_after on every movement must equal previous cached qty + delta (ledger consistency).

PRAGMA foreign_keys = ON;

-- ============================================================================ users & attendance
CREATE TABLE role (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name IN ('OWNER', 'STAFF')),
    permissions TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE user (
    id                   INTEGER PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name         TEXT NOT NULL,
    role_id              INTEGER NOT NULL REFERENCES role(id),
    password_hash        TEXT NOT NULL,
    owner_pin_hash       TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    failed_logins        INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE app_run (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    stopped_at  TEXT,
    clean_stop  INTEGER CHECK (clean_stop IN (0, 1)),
    app_version TEXT NOT NULL,
    host        TEXT
);

CREATE TABLE work_session (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES user(id),
    app_run_id     INTEGER REFERENCES app_run(id),
    login_at       TEXT NOT NULL,
    logout_at      TEXT,
    end_reason     TEXT CHECK (end_reason IN ('LOGOUT', 'WINDOW_CLOSED', 'HEARTBEAT_LOST', 'IDLE_LOCK_TIMEOUT', 'APP_SHUTDOWN', 'FORCED_BY_OWNER', 'FORCED_BY_LOGIN')),
    last_seen_at   TEXT NOT NULL,
    confirmed_at   TEXT,
    confirmed_note TEXT,
    owner_note     TEXT,
    client         TEXT,
    idle_seconds   INTEGER NOT NULL DEFAULT 0,
    lock_count     INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT
);
CREATE INDEX ix_work_session_user_login ON work_session(user_id, login_at);
CREATE INDEX ix_work_session_open ON work_session(logout_at) WHERE logout_at IS NULL;

-- ============================================================================ catalogue
CREATE TABLE category (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_hi    TEXT,
    parent_id  INTEGER REFERENCES category(id),
    sort_order INTEGER NOT NULL DEFAULT 0,
    icon       TEXT
);

CREATE TABLE product (
    id                  INTEGER PRIMARY KEY,
    sku                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    internal_barcode    TEXT UNIQUE,
    name                TEXT NOT NULL,
    name_hi             TEXT,
    category_id         INTEGER REFERENCES category(id),
    parent_product_id   INTEGER REFERENCES product(id),
    unit                TEXT NOT NULL DEFAULT 'pcs',
    sale_mode           TEXT NOT NULL DEFAULT 'PIECE' CHECK (sale_mode IN ('PIECE', 'WEIGHT', 'VOLUME', 'LENGTH')),
    base_unit           TEXT NOT NULL DEFAULT 'pcs' CHECK (base_unit IN ('pcs', 'g', 'ml', 'cm')),
    pricing_unit_qty    NUMERIC NOT NULL DEFAULT 1 CHECK (pricing_unit_qty > 0),
    pricing_unit_label  TEXT NOT NULL DEFAULT 'pc',
    min_qty             NUMERIC NOT NULL DEFAULT 1 CHECK (min_qty > 0),
    qty_step            NUMERIC NOT NULL DEFAULT 1 CHECK (qty_step > 0),
    qty_presets         TEXT NOT NULL DEFAULT '[]',
    allow_decimal_qty   INTEGER NOT NULL DEFAULT 0 CHECK (allow_decimal_qty IN (0, 1)),
    price_paise         INTEGER NOT NULL CHECK (price_paise >= 0),
    mrp_paise           INTEGER CHECK (mrp_paise IS NULL OR mrp_paise >= 0),
    cost_paise          INTEGER CHECK (cost_paise IS NULL OR cost_paise >= 0),
    tax_rate_bp         INTEGER NOT NULL DEFAULT 0 CHECK (tax_rate_bp BETWEEN 0 AND 10000),
    price_includes_tax  INTEGER NOT NULL DEFAULT 1 CHECK (price_includes_tax IN (0, 1)),
    hsn_code            TEXT,
    min_stock           NUMERIC NOT NULL DEFAULT 0 CHECK (min_stock >= 0),
    reorder_qty         NUMERIC NOT NULL DEFAULT 0 CHECK (reorder_qty >= 0),
    image_path          TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    quick_pick          INTEGER NOT NULL DEFAULT 0 CHECK (quick_pick IN (0, 1)),
    is_system           INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
    row_version         INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at          TEXT
);
CREATE INDEX ix_product_name ON product(name COLLATE NOCASE);
CREATE INDEX ix_product_category ON product(category_id);
CREATE INDEX ix_product_active ON product(is_active, deleted_at);

CREATE TABLE product_barcode (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    barcode    TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('INTERNAL', 'MANUFACTURER', 'PACK')),
    symbology  TEXT NOT NULL DEFAULT 'EAN13' CHECK (symbology IN ('EAN13', 'EAN8', 'UPCA', 'CODE128', 'QR', 'OTHER')),
    pack_qty   NUMERIC NOT NULL DEFAULT 1 CHECK (pack_qty > 0),
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_product_barcode_product ON product_barcode(product_id);

CREATE TABLE tag (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE product_tag (
    product_id INTEGER NOT NULL REFERENCES product(id),
    tag_id     INTEGER NOT NULL REFERENCES tag(id),
    PRIMARY KEY (product_id, tag_id)
);

CREATE TABLE price_history (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES product(id),
    field           TEXT NOT NULL CHECK (field IN ('PRICE', 'MRP', 'COST', 'TAX_RATE')),
    old_value_paise INTEGER,
    new_value_paise INTEGER,
    old_bp          INTEGER,
    new_bp          INTEGER,
    effective_from  TEXT NOT NULL,
    changed_by      INTEGER REFERENCES user(id),
    reason          TEXT,
    source          TEXT NOT NULL DEFAULT 'EDITOR' CHECK (source IN ('EDITOR', 'IMPORT', 'PURCHASE', 'CONVERSION', 'SEED')),
    correlation_id  TEXT
);
CREATE INDEX ix_price_history_product_time ON price_history(product_id, effective_from);

CREATE TABLE label_print_log (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    copies     INTEGER NOT NULL CHECK (copies > 0),
    target     TEXT NOT NULL CHECK (target IN ('ESCPOS', 'PDF', 'TSPL')),
    reason     TEXT NOT NULL CHECK (reason IN ('NEW', 'RESTOCK', 'REPLACEMENT')),
    user_id    INTEGER NOT NULL REFERENCES user(id),
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE offer (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_hi    TEXT,
    type       TEXT NOT NULL CHECK (type IN ('PCT', 'AMOUNT', 'BUY_X_GET_Y')),
    value      NUMERIC NOT NULL CHECK (value >= 0),
    scope      TEXT NOT NULL CHECK (scope IN ('PRODUCT', 'CATEGORY', 'TAG', 'BILL')),
    scope_id   INTEGER,
    min_qty    NUMERIC NOT NULL DEFAULT 0,
    valid_from TEXT,
    valid_to   TEXT,
    stackable  INTEGER NOT NULL DEFAULT 0 CHECK (stackable IN (0, 1)),
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER REFERENCES user(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================================ inventory
CREATE TABLE stock_movement (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES product(id),
    type            TEXT NOT NULL CHECK (type IN ('OPENING', 'PURCHASE', 'SALE', 'SALE_VOID', 'RETURN', 'ADJUSTMENT', 'DAMAGE', 'STOCK_TAKE')),
    qty_delta       NUMERIC NOT NULL CHECK (qty_delta <> 0),
    qty_after       NUMERIC NOT NULL,
    unit_cost_paise INTEGER CHECK (unit_cost_paise IS NULL OR unit_cost_paise >= 0),
    ref_type        TEXT CHECK (ref_type IS NULL OR ref_type IN ('SALE', 'PURCHASE', 'STOCK_COUNT')),
    ref_id          INTEGER,
    reason          TEXT,
    user_id         INTEGER NOT NULL REFERENCES user(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    correlation_id  TEXT
);
CREATE INDEX ix_stock_movement_product_time ON stock_movement(product_id, created_at);
CREATE INDEX ix_stock_movement_ref ON stock_movement(ref_type, ref_id);
CREATE INDEX ix_stock_movement_type_time ON stock_movement(type, created_at);

CREATE TABLE product_stock (
    product_id INTEGER PRIMARY KEY REFERENCES product(id),
    qty        NUMERIC NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE stock_daily_balance (
    day              TEXT NOT NULL,
    product_id       INTEGER NOT NULL REFERENCES product(id),
    qty_close        NUMERIC NOT NULL,
    value_cost_paise INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, product_id)
);

-- Internal guard table: a row exists only while a trusted trigger is updating product_stock.
CREATE TABLE _write_guard (k INTEGER PRIMARY KEY);

-- ============================================================================ shifts & cash
CREATE TABLE shift (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES user(id),
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    opening_float_paise INTEGER NOT NULL DEFAULT 0 CHECK (opening_float_paise >= 0),
    expected_cash_paise INTEGER,
    counted_cash_paise  INTEGER,
    denominations       TEXT,
    variance_paise      INTEGER,
    closed_by_user_id   INTEGER REFERENCES user(id),
    status              TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    note                TEXT
);
CREATE UNIQUE INDEX ux_shift_one_open_per_user ON shift(user_id) WHERE status = 'OPEN';

CREATE TABLE cash_event (
    id                  INTEGER PRIMARY KEY,
    shift_id            INTEGER NOT NULL REFERENCES shift(id),
    type                TEXT NOT NULL CHECK (type IN ('CASH_IN', 'CASH_OUT')),
    amount_paise        INTEGER NOT NULL CHECK (amount_paise > 0),
    reason              TEXT,
    user_id             INTEGER NOT NULL REFERENCES user(id),
    approved_by_user_id INTEGER REFERENCES user(id),
    at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================================ sales (append-only, hash-chained)
CREATE TABLE customer (
    id    INTEGER PRIMARY KEY,
    name  TEXT,
    phone TEXT UNIQUE,
    note  TEXT
);

CREATE TABLE counter (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0)
);

CREATE TABLE sale (
    id                    INTEGER PRIMARY KEY,
    invoice_no            TEXT NOT NULL UNIQUE,
    client_txn_id         TEXT NOT NULL UNIQUE,
    public_id             TEXT NOT NULL UNIQUE,
    fin_year              TEXT NOT NULL,
    seq_no                INTEGER NOT NULL,
    sold_at               TEXT NOT NULL,
    user_id               INTEGER NOT NULL REFERENCES user(id),
    shift_id              INTEGER NOT NULL REFERENCES shift(id),
    customer_id           INTEGER REFERENCES customer(id),
    status                TEXT NOT NULL CHECK (status IN ('COMPLETED', 'VOIDED', 'REFUND')),
    original_sale_id      INTEGER REFERENCES sale(id),
    subtotal_paise        INTEGER NOT NULL CHECK (subtotal_paise >= 0),
    item_discount_paise   INTEGER NOT NULL DEFAULT 0 CHECK (item_discount_paise >= 0),
    bill_discount_paise   INTEGER NOT NULL DEFAULT 0 CHECK (bill_discount_paise >= 0),
    bill_discount_reason  TEXT,
    total_savings_paise   INTEGER NOT NULL DEFAULT 0 CHECK (total_savings_paise >= 0),
    approved_by_user_id   INTEGER REFERENCES user(id),
    taxable_paise         INTEGER NOT NULL CHECK (taxable_paise >= 0),
    cgst_paise            INTEGER NOT NULL DEFAULT 0 CHECK (cgst_paise >= 0),
    sgst_paise            INTEGER NOT NULL DEFAULT 0 CHECK (sgst_paise >= 0),
    roundoff_paise        INTEGER NOT NULL DEFAULT 0,
    total_paise           INTEGER NOT NULL CHECK (total_paise >= 0),
    reprint_count         INTEGER NOT NULL DEFAULT 0,
    receipt_printed       INTEGER NOT NULL DEFAULT 0 CHECK (receipt_printed IN (0, 1)),
    prev_hash             TEXT NOT NULL,
    hash                  TEXT NOT NULL UNIQUE,
    note                  TEXT,
    correlation_id        TEXT,
    UNIQUE (fin_year, seq_no)
);
CREATE INDEX ix_sale_sold_at ON sale(sold_at);
CREATE INDEX ix_sale_user_time ON sale(user_id, sold_at);
CREATE INDEX ix_sale_shift ON sale(shift_id);

CREATE TABLE sale_item (
    id                        INTEGER PRIMARY KEY,
    sale_id                   INTEGER NOT NULL REFERENCES sale(id),
    product_id                INTEGER REFERENCES product(id),
    name_snapshot             TEXT NOT NULL,
    hsn_snapshot              TEXT,
    qty                       NUMERIC NOT NULL CHECK (qty > 0),
    unit                      TEXT NOT NULL,
    qty_source                TEXT NOT NULL DEFAULT 'TYPED' CHECK (qty_source IN ('TYPED', 'PRESET', 'SCALE', 'EMBEDDED_BARCODE', 'BY_AMOUNT', 'SCAN')),
    pricing_unit_qty_snapshot NUMERIC NOT NULL DEFAULT 1,
    shelf_price_paise         INTEGER NOT NULL CHECK (shelf_price_paise >= 0),
    offer_id                  INTEGER REFERENCES offer(id),
    offer_name_snapshot       TEXT,
    discount_bp               INTEGER NOT NULL DEFAULT 0 CHECK (discount_bp BETWEEN 0 AND 10000),
    discount_paise            INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    discount_reason           TEXT,
    unit_price_paise          INTEGER NOT NULL CHECK (unit_price_paise >= 0),
    tax_rate_bp               INTEGER NOT NULL DEFAULT 0 CHECK (tax_rate_bp BETWEEN 0 AND 10000),
    taxable_paise             INTEGER NOT NULL CHECK (taxable_paise >= 0),
    cgst_paise                INTEGER NOT NULL DEFAULT 0 CHECK (cgst_paise >= 0),
    sgst_paise                INTEGER NOT NULL DEFAULT 0 CHECK (sgst_paise >= 0),
    line_total_paise          INTEGER NOT NULL CHECK (line_total_paise >= 0),
    cost_paise_snapshot       INTEGER,
    entry_method              TEXT NOT NULL CHECK (entry_method IN ('SCAN', 'LOOKUP', 'OPEN_ITEM', 'QUICK_PICK')),
    entry_reason              TEXT,
    scanned_barcode           TEXT,
    is_open_item              INTEGER NOT NULL DEFAULT 0 CHECK (is_open_item IN (0, 1)),
    open_item_name            TEXT,
    open_item_detail          TEXT,
    CHECK (is_open_item = 1 OR product_id IS NOT NULL)
);
CREATE INDEX ix_sale_item_sale ON sale_item(sale_id);
CREATE INDEX ix_sale_item_product ON sale_item(product_id);

CREATE TABLE payment (
    id             INTEGER PRIMARY KEY,
    sale_id        INTEGER NOT NULL REFERENCES sale(id),
    mode           TEXT NOT NULL CHECK (mode IN ('CASH', 'UPI', 'CARD')),
    amount_paise   INTEGER NOT NULL CHECK (amount_paise >= 0),
    tendered_paise INTEGER,
    change_paise   INTEGER NOT NULL DEFAULT 0 CHECK (change_paise >= 0),
    reference      TEXT
);
CREATE INDEX ix_payment_sale ON payment(sale_id);

CREATE TABLE open_item_review (
    id                   INTEGER PRIMARY KEY,
    sale_item_id         INTEGER NOT NULL UNIQUE REFERENCES sale_item(id),
    status               TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONVERTED', 'DISMISSED')),
    converted_product_id INTEGER REFERENCES product(id),
    reviewed_by          INTEGER REFERENCES user(id),
    reviewed_at          TEXT
);

CREATE TABLE print_pending (
    id         INTEGER PRIMARY KEY,
    sale_id    INTEGER NOT NULL REFERENCES sale(id),
    queued_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    printed_at TEXT
);

CREATE TABLE held_cart (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id),
    label      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE held_cart_line (
    id             INTEGER PRIMARY KEY,
    held_cart_id   INTEGER NOT NULL REFERENCES held_cart(id) ON DELETE CASCADE,
    product_id     INTEGER REFERENCES product(id),
    qty            NUMERIC NOT NULL CHECK (qty > 0),
    discount_paise INTEGER NOT NULL DEFAULT 0,
    payload        TEXT
);

-- ============================================================================ purchases & stock-take
CREATE TABLE supplier (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    phone TEXT,
    gstin TEXT
);

CREATE TABLE purchase (
    id                  INTEGER PRIMARY KEY,
    supplier_id         INTEGER REFERENCES supplier(id),
    supplier_invoice_no TEXT,
    purchase_date       TEXT NOT NULL,
    total_paise         INTEGER NOT NULL DEFAULT 0 CHECK (total_paise >= 0),
    user_id             INTEGER NOT NULL REFERENCES user(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE purchase_item (
    id              INTEGER PRIMARY KEY,
    purchase_id     INTEGER NOT NULL REFERENCES purchase(id),
    product_id      INTEGER NOT NULL REFERENCES product(id),
    qty             NUMERIC NOT NULL CHECK (qty > 0),
    unit_cost_paise INTEGER NOT NULL CHECK (unit_cost_paise >= 0),
    expiry_date     TEXT
);

CREATE TABLE stock_count (
    id                  INTEGER PRIMARY KEY,
    started_at          TEXT NOT NULL,
    approved_at         TEXT,
    started_by_user_id  INTEGER NOT NULL REFERENCES user(id),
    approved_by_user_id INTEGER REFERENCES user(id),
    status              TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'APPROVED', 'CANCELLED')),
    scope               TEXT NOT NULL DEFAULT 'FULL' CHECK (scope IN ('FULL', 'CATEGORY', 'SELECTED'))
);

CREATE TABLE stock_count_line (
    id                   INTEGER PRIMARY KEY,
    stock_count_id       INTEGER NOT NULL REFERENCES stock_count(id),
    product_id           INTEGER NOT NULL REFERENCES product(id),
    system_qty           NUMERIC NOT NULL,
    counted_qty          NUMERIC,
    variance             NUMERIC,
    variance_value_paise INTEGER
);

-- ============================================================================ audit, notifications, system
CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    user_id        INTEGER REFERENCES user(id),
    actor_user_id  INTEGER REFERENCES user(id),
    action         TEXT NOT NULL,
    entity         TEXT,
    entity_id      INTEGER,
    before         TEXT,
    after          TEXT,
    correlation_id TEXT,
    ip             TEXT,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE
);
CREATE INDEX ix_audit_log_at ON audit_log(at);
CREATE INDEX ix_audit_log_entity ON audit_log(entity, entity_id);
CREATE INDEX ix_audit_log_user ON audit_log(user_id, at);

CREATE TABLE notification (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES user(id),
    severity   TEXT NOT NULL CHECK (severity IN ('INFO', 'WARN', 'HIGH')),
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT,
    deep_link  TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    read_at    TEXT,
    source_ref TEXT
);
CREATE INDEX ix_notification_unread ON notification(user_id, read_at);

CREATE TABLE recovery_key (
    id              INTEGER PRIMARY KEY,
    key_hash        TEXT NOT NULL,
    issued_at       TEXT NOT NULL,
    acknowledged_at TEXT,
    used_at         TEXT
);

CREATE TABLE setup_step (
    step    TEXT PRIMARY KEY,
    done_at TEXT,
    done_by INTEGER REFERENCES user(id)
);

CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'str' CHECK (type IN ('str', 'int', 'bool', 'json', 'float')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_by INTEGER REFERENCES user(id)
);

CREATE TABLE secure_setting (
    key            TEXT PRIMARY KEY,
    ciphertext     BLOB NOT NULL,
    masked_preview TEXT NOT NULL,
    updated_by     INTEGER REFERENCES user(id),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE db_identity (
    instance_id         TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    app_version         TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    last_open_at        TEXT,
    last_clean_close_at TEXT,
    backup_id           TEXT,
    backup_taken_at     TEXT,
    restored_at         TEXT,
    restored_from       TEXT
);

CREATE TABLE backup_log (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    path       TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    verified   INTEGER NOT NULL CHECK (verified IN (0, 1)),
    trigger    TEXT NOT NULL,
    backup_id  TEXT
);

CREATE TABLE integrity_check (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('CHAIN', 'GAPS', 'STOCK', 'QUICK_CHECK', 'FULL_CHECK', 'MANIFEST')),
    ok      INTEGER NOT NULL CHECK (ok IN (0, 1)),
    details TEXT
);

CREATE TABLE hardware_override (
    kind          TEXT NOT NULL CHECK (kind IN ('PRINTER', 'SCANNER')),
    id            INTEGER PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES user(id),
    started_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    reason        TEXT
);

CREATE TABLE publish_queue (
    id            INTEGER PRIMARY KEY,
    channel       TEXT NOT NULL CHECK (channel IN ('folder', 'email', 'telegram', 'whatsapp')),
    document      TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ============================================================================ AI & forecasting
CREATE TABLE chat_session (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id),
    title      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE chat_message (
    id                 INTEGER PRIMARY KEY,
    session_id         INTEGER NOT NULL REFERENCES chat_session(id),
    role               TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content            TEXT NOT NULL,
    tool_calls         TEXT,
    tokens             INTEGER,
    provider           TEXT CHECK (provider IS NULL OR provider IN ('ollama', 'openai', 'huggingface', 'fastpath')),
    model              TEXT,
    language           TEXT CHECK (language IS NULL OR language IN ('en', 'hi', 'hinglish')),
    guardrail_decision TEXT CHECK (guardrail_decision IS NULL OR guardrail_decision IN ('ALLOW', 'REFUSE', 'REFUSE_AND_FLAG')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_chat_message_session ON chat_message(session_id, created_at);

CREATE TABLE ai_usage (
    id                INTEGER PRIMARY KEY,
    day               TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    embed_tokens      INTEGER NOT NULL DEFAULT 0,
    requests          INTEGER NOT NULL DEFAULT 0,
    errors            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (day, provider, model)
);

CREATE TABLE chat_security_event (
    id              INTEGER PRIMARY KEY,
    at              TEXT NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES user(id),
    category        TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('LOW', 'MED', 'HIGH')),
    message_excerpt TEXT,
    layer           TEXT NOT NULL CHECK (layer IN ('INTENT', 'OUTPUT')),
    owner_alerted   INTEGER NOT NULL DEFAULT 0 CHECK (owner_alerted IN (0, 1))
);

CREATE TABLE doc_chunk (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX ix_doc_chunk_source ON doc_chunk(source);

CREATE TABLE embedding (
    id         INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('PRODUCT', 'DOC_CHUNK')),
    owner_id   INTEGER NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    UNIQUE (owner_type, owner_id, provider, model)
);

CREATE TABLE prompt_version (
    id                    INTEGER PRIMARY KEY,
    version               INTEGER NOT NULL UNIQUE,
    editable_instructions TEXT NOT NULL,
    core_hash             TEXT NOT NULL,
    default_language      TEXT NOT NULL DEFAULT 'hinglish',
    extra_blocked_phrases TEXT NOT NULL DEFAULT '[]',
    is_active             INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    redteam_passed        INTEGER NOT NULL DEFAULT 0 CHECK (redteam_passed IN (0, 1)),
    created_by            INTEGER REFERENCES user(id),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    note                  TEXT
);
CREATE UNIQUE INDEX ux_prompt_version_active ON prompt_version(is_active) WHERE is_active = 1;

CREATE TABLE festival (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    category_uplift TEXT NOT NULL DEFAULT '{}',
    source          TEXT NOT NULL DEFAULT 'config' CHECK (source IN ('config', 'learned'))
);

CREATE TABLE forecast_snapshot (
    id           INTEGER PRIMARY KEY,
    computed_at  TEXT NOT NULL,
    product_id   INTEGER REFERENCES product(id),
    horizon_days INTEGER NOT NULL,
    point        NUMERIC NOT NULL,
    low          NUMERIC NOT NULL,
    high         NUMERIC NOT NULL,
    method       TEXT NOT NULL,
    confidence   TEXT NOT NULL,
    caveats      TEXT
);
CREATE INDEX ix_forecast_snapshot_product ON forecast_snapshot(product_id, computed_at);

-- ============================================================================ TRIGGERS — protection rules
-- 1. product_stock may be changed only by the trusted trigger below (E-INV-012).
CREATE TRIGGER trg_product_stock_no_direct_insert
BEFORE INSERT ON product_stock
WHEN NOT EXISTS (SELECT 1 FROM _write_guard)
BEGIN
    SELECT RAISE(ABORT, 'E-INV-012');
END;

CREATE TRIGGER trg_product_stock_no_direct_update
BEFORE UPDATE ON product_stock
WHEN NOT EXISTS (SELECT 1 FROM _write_guard)
BEGIN
    SELECT RAISE(ABORT, 'E-INV-012');
END;

CREATE TRIGGER trg_product_stock_no_delete
BEFORE DELETE ON product_stock
BEGIN
    SELECT RAISE(ABORT, 'E-INV-012');
END;

-- 2. Every movement must carry the correct running balance (E-INV-013) and SALE movements must
--    reference an existing sale (E-INV-011); non-owner users may only create SALE movements (E-INV-010).
CREATE TRIGGER trg_stock_movement_validate
BEFORE INSERT ON stock_movement
BEGIN
    SELECT CASE
        WHEN NEW.qty_after <> COALESCE((SELECT qty FROM product_stock WHERE product_id = NEW.product_id), 0) + NEW.qty_delta
            THEN RAISE(ABORT, 'E-INV-013')
        WHEN NEW.type = 'SALE' AND (NEW.ref_type IS NOT 'SALE' OR NOT EXISTS (SELECT 1 FROM sale WHERE id = NEW.ref_id))
            THEN RAISE(ABORT, 'E-INV-011')
        WHEN NEW.type IN ('SALE_VOID', 'RETURN') AND (NEW.ref_type IS NOT 'SALE' OR NOT EXISTS (SELECT 1 FROM sale WHERE id = NEW.ref_id))
            THEN RAISE(ABORT, 'E-INV-011')
        WHEN NEW.type <> 'SALE'
             AND (SELECT r.name FROM user u JOIN role r ON r.id = u.role_id WHERE u.id = NEW.user_id) IS NOT 'OWNER'
            THEN RAISE(ABORT, 'E-INV-010')
        WHEN (SELECT is_system FROM product WHERE id = NEW.product_id) = 1
            THEN RAISE(ABORT, 'E-INV-011')
    END;
END;

-- 3. The ONLY writer of product_stock.
CREATE TRIGGER trg_stock_movement_apply
AFTER INSERT ON stock_movement
BEGIN
    INSERT INTO _write_guard(k) VALUES (1);
    INSERT INTO product_stock(product_id, qty, updated_at)
        VALUES (NEW.product_id, NEW.qty_after, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(product_id) DO UPDATE SET qty = excluded.qty, updated_at = excluded.updated_at;
    DELETE FROM _write_guard;
END;

-- 4. Append-only ledgers (E-LEDGER-001 would be raised by the verifier; the DB simply refuses).
CREATE TRIGGER trg_stock_movement_no_update BEFORE UPDATE ON stock_movement BEGIN SELECT RAISE(ABORT, 'E-INV-012'); END;
CREATE TRIGGER trg_stock_movement_no_delete BEFORE DELETE ON stock_movement BEGIN SELECT RAISE(ABORT, 'E-INV-012'); END;

CREATE TRIGGER trg_sale_no_delete BEFORE DELETE ON sale BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_sale_limited_update
BEFORE UPDATE ON sale
WHEN NEW.invoice_no <> OLD.invoice_no OR NEW.client_txn_id <> OLD.client_txn_id OR NEW.public_id <> OLD.public_id
  OR NEW.fin_year <> OLD.fin_year OR NEW.seq_no <> OLD.seq_no OR NEW.sold_at <> OLD.sold_at OR NEW.user_id <> OLD.user_id
  OR NEW.shift_id <> OLD.shift_id OR NEW.status <> OLD.status OR NEW.subtotal_paise <> OLD.subtotal_paise
  OR NEW.item_discount_paise <> OLD.item_discount_paise OR NEW.bill_discount_paise <> OLD.bill_discount_paise
  OR NEW.total_savings_paise <> OLD.total_savings_paise OR NEW.taxable_paise <> OLD.taxable_paise
  OR NEW.cgst_paise <> OLD.cgst_paise OR NEW.sgst_paise <> OLD.sgst_paise OR NEW.roundoff_paise <> OLD.roundoff_paise
  OR NEW.total_paise <> OLD.total_paise OR NEW.prev_hash <> OLD.prev_hash OR NEW.hash <> OLD.hash
BEGIN
    SELECT RAISE(ABORT, 'E-LEDGER-001');
END;
CREATE TRIGGER trg_sale_item_no_update BEFORE UPDATE ON sale_item BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_sale_item_no_delete BEFORE DELETE ON sale_item BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_payment_no_update BEFORE UPDATE ON payment BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_payment_no_delete BEFORE DELETE ON payment BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_price_history_no_update BEFORE UPDATE ON price_history BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;
CREATE TRIGGER trg_price_history_no_delete BEFORE DELETE ON price_history BEGIN SELECT RAISE(ABORT, 'E-LEDGER-001'); END;

-- 5. Product bookkeeping: row_version and updated_at advance on every change.
CREATE TRIGGER trg_product_touch
AFTER UPDATE ON product
WHEN NEW.row_version = OLD.row_version
BEGIN
    UPDATE product SET row_version = OLD.row_version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;
