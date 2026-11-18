# 10 — Backend Components

Package root: `src/vaishnavi/`. Every module below lists its **responsibility**, **public interface**
and **logging contract**. All public functions are type-hinted and raise `VaishnaviError` subclasses.

## 1. `core/`

| Module | Responsibility | Key API |
|--------|----------------|---------|
| `settings.py` | Load `config/settings.toml` + `locations.toml` + env (`VAISHNAVI_SECTION__KEY`) into typed dataclass `Settings` (stdlib, see ADR-009 note); merge DB `setting` overrides via `with_overrides()`; cross-field validation → `E-CFG-001`; secret in TOML → `E-SEC-006` | `Settings.load()`, `settings.with_overrides()`, `settings.as_dict()` |
| `logging.py` | Configure handlers/formatters/queue, correlation-id filter, redaction filter | `setup_logging(cfg)`, `with_correlation(cid)` |
| `errors.py` | `VaishnaviError(code, message, hint, details)` + subclasses (`ConfigError`, `DbError`, `AuthError`, `InventoryError`, `BillingError`, `LedgerError`, `PrinterError`, `AiError`) | — |
| `security.py` | password / Owner-PIN hashing (argon2), constant-time verify, lockout counter; Fernet helpers for `secure_setting` (key from `platform/secrets.py`) | `hash_secret()`, `verify_secret()`, `encrypt()`, `decrypt()` |
| `ids.py` | ULID generation, invoice formatting | `new_ulid()`, `format_invoice(fy, seq)` |
| `money.py` | `Paise` helpers, Indian number formatting, half-up rounding | `to_paise()`, `fmt_inr()` |
| `clock.py` | UTC now, IST display, financial year | `now_utc()`, `fin_year(date)` |
| `permissions.py` | `Permission` enum, two roles (`OWNER` = all, `STAFF` = selling subset), `@requires()` decorator | `has_permission(user, perm)` |
| `events.py` | Tiny in-process pub/sub (`ProductChanged`, `SaleCommitted`, `ShiftClosed`) for AI re-index, dashboard refresh | `bus.publish()`, `bus.subscribe()` |

## 2. `bootstrap/`

| Module | Responsibility |
|--------|----------------|
| `doctor.py` | `Check` protocol (`name`, `run() -> CheckResult`), ordered registry, runner with timing, **plain-language + technical** text reporters (EN/HI), JSON reporter, exit code mapping, `--fix` flag |
| `checks/` | one file per check group: `python_env.py`, `packages.py`, **`code_integrity.py`** (release manifest of file hashes + dependency hash lock), `system_components.py` (libusb/CUPS/fonts), `filesystem.py`, `config.py`, `database.py`, `ledger.py` (sales + audit chains, reconciliation), `backup.py`, `secrets.py`, `ollama.py`, `hardware.py` (printer, scanner, scale), `network.py`, `autostart.py` |
| `ollama_manager.py` | locate binary per OS, install via platform adapter, start service (detached), wait for port, list/pull models with progress, warm-up |
| `startup.py` | `run_app(settings, paths, config_dir, serve, open_browser, lang)` — the daily start: `APP_START` → data-root guard (`E-SYS-013`, never re-created) → `WriterLock` (`E-DB-030`) → create + seed when missing → `DB_TRIAGE` (`E-DB-003` refuses) → schema check / Alembic upgrade (`E-DB-010`) → `mark_open` → `app_run` row → `DOCTOR_LIGHT` (sets status chips; DB/config FAIL refuses) → backup-on-start → `SCHEDULER_STARTED` → `SERVER_READY` (+ `state/heartbeat.json`) → `APP_CLEAN_CLOSE` (fingerprint, `clean_stop = 1`). The server is injected (`serve(state, on_ready)`): NiceGUI on the shop PC, a stub in tests; exit 0 clean · 2 refused |

### `commands/` and `app_services.py` (inside the venv)

| Module | Responsibility |
|--------|----------------|
| `commands/__init__.py` | command registry (`@register`), `Env`, `read_stdin_secrets()` (the installer → app hand-over: one JSON object `{"secrets": {…}}` on stdin, never argv/env), `start_file_logging()` (logs to `<primary>/logs`; console-only with `E-SYS-013` when the data root is missing — it is never created here) |
| `commands/run.py` | `run [--no-browser] [--port]` → `startup.run_app` |
| `commands/init_db.py` | `init-db [--json]` — create/seed/upgrade, import pending secrets; summary `created · seeded_rows · migrated · revision · secrets_stored · secrets_failed · owner_setup_pending`; exit 2 + `E-SEC-001` when a secret could not be stored encrypted (the installer then keeps its copy) |
| `commands/reset_owner_password.py` | app half of `--reset-owner-password`: stdin secrets `recovery_key` + `new_password`; a hand-over on a pipe is the installer's elevation proof (a TTY is refused, `E-AUTH-041`); prints `RESET_OK` and the new recovery sheet as `SECRET\|` lines |
| `commands/verify_chain.py` | `verify-chain [--json] [--no-record]` — `LedgerService.run_all`; exit 2 with the failing codes |
| `commands/backup.py` | `backup [--json] [--trigger T] \| --list \| --flush` |
| `commands/restore.py` | `restore [--list] [--id ID \| --path FILE] [--yes] [--json]` — `BackupService.restore` (verify → pre-restore snapshot → copy-in → check); refuses while `state/heartbeat.json` is fresh (`E-SYS-016`); driven interactively by `main.py --restore` |
| `commands/doctor.py` | `doctor [--json] [--fix] [--checks] [--ascii]` — works on a bare interpreter |
| `commands/selftest.py` | `selftest [--json] [--pattern GLOB] [--no-live] [--ascii]` — runs the **shipped** `tests/core` suite in-process (`run_suite`), then the full live doctor; `exit_code_for()` 0 clean · 1 notes · 2 failed; report kept in `<primary>/logs/selftest/` |
| `app_services.py` | `AppServices.build()` wires the services once per process on the stdlib sqlite stores; `build_secret_box()` → `FernetBox` (keyring/file master key) or `NullBox` (refuses, `E-SEC-001`) |
| *(root)* `bootstrap/app_stages.py` | installer stages 11–13: `DatabaseStage` (runs `init-db --json` with the pending secrets on stdin; deletes `config/secrets.env` and the in-memory copy **only after** the app confirms every key is stored), `DoctorStage` (exit 0/1/2 → OK/WARN/FAIL, report echoed), `StartStage` (detached `run`, polls `/healthz`, **"Ready to open the shop: URL"**, adopts a running instance) and `Watchdog` (dead/hung → restart `E-SYS-020`, N per hour then `WATCHDOG_GAVE_UP`); `fetch_health()`, `web_port()` from `settings.toml` via `tomllib` |

### `main.py` (repository root, **stdlib only**)
The single entry point the owner runs on every OS ([17](17-install-and-run.md)). Runs before the virtual environment exists, so it may import only the standard library:
`argparse`, `platform`, `subprocess`, `venv`, `urllib`, `json`, `http.server`, `threading`, `logging`, `getpass`, `shutil`, `ctypes` (Windows UAC).
It parses flags, starts the install log + mini log server, runs the ordered stdlib stages below (each idempotent, logging `STAGE_START/OK/SKIP/FAIL`), then **hands over** to `.venv/…/python -m vaishnavi doctor|run|…` for everything that needs third-party code.

| Step module (inside `bootstrap/` package, still stdlib-only) | Responsibility |
|----|----|
| `cli.py` | flag parsing into a frozen `Options` (`--install-only --doctor --fix --lang --offline --update --restore --reset-owner-password --selftest --logs --where --no-ai --ai-provider --answers --yes --no-browser --debug --uninstall-autostart --make-offline-bundle`), cross-validation (`--fix` needs `--doctor`, `--no-ai` vs `--ai-provider`), `Mode` enum + `register_mode` handler registry, orchestrating `main()` (open log → load messages → header → dispatch → result line; Ctrl-C → exit 3, bug → exit 4 with apology + log path, never a traceback on screen) |
| `stages.py` | `Stage` protocol (`key`, `title_key`, `applies`, `cache_key`, `run`), `StageResult` (OK/WARN/SKIP/FAIL + message/hint/detail/error_code/data), `StageRunner` (ordered, WARN continues, FAIL stops, uncaught exception → `E-SYS-099` exit 4, `STAGE_START/OK/WARN/SKIP/FAIL key= ms=` logging), `StateStore` (`logs/install/state.json`, atomic write, per-stage cache keys for resume, run history capped at 20), `InstallPaths`, `ExitCode`, `build_default_pipeline()` — the explicit ordered list each unit appends to |
| `console.py` | ✔ / ! / ✖ / – / ⟳ reporter with automatic ASCII fallback on consoles that cannot encode the glyphs (legacy Windows cp1252); header, hint, progress bar; every line mirrored to the log |
| `i18n.py` + `messages/{en,hi}.toml` | dotted-key message loader (`runner.header`), Hindi overlays English, missing key → key itself + one WARNING, bad placeholder never crashes |
| `logsetup.py` | install log `<program>/logs/install/<date>_<run-id>.log` at DEBUG from the first millisecond, run-id stamp on every line, **secret redaction filter** (`api_key= / password= / token= / Bearer … / sk-… / hf_…` → `***`), optional console echo (`--debug`), keeps the last 30 logs |
| `survey.py` | OS/arch, Python version, RAM, GPU (`nvidia-smi`/`system_profiler`/`lspci`), disk, printers, serial ports → `logs/install/survey.json`; produces the **AI provider recommendation** and local model tier |
| `wizard.py` | interactive question engine: numbered options, default in brackets, EN/HI text from `messages/`, `b`ack / `s`kip / `q`uit, masked secret input (`getpass`), answers persisted to `config/install-answers.toml` (secrets excluded), `--answers` / `--yes` / `--ai-provider` pre-fills |
| `ai_provider.py` | the AI question (ollama · openai · huggingface · none) with recommendation from the survey (adjusted to *none* when offline without Ollama), data/cost notice; for cloud providers obtains the key **interactive-first**: one-time import from `VAISHNAVI_OPENAI_API_KEY` / `VAISHNAVI_HF_API_KEY` env vars or `config/secrets.env` (then offers to delete the file; logs `SECRET_IMPORTED source=…`, never the value), else masked `getpass` prompt with a shape check (`sk-…` / `hf_…`); runs a stdlib `urllib` **live test** (chat completion + embedding; HF embeddings via the feature-extraction endpoint) and classifies failures *auth / billing / network / model* with a retry / other-provider / no-AI menu; the verified key is kept **only in memory** (`ctx.runtime["pending_secrets"]`) and encrypted into `secure_setting` by the database stage — it never touches a file; writes the `[ai]` / `[ai.<provider>]` block of `config/settings.toml` (other sections preserved); refuses any `api_key` found in TOML (`E-SEC-006`, doctor) |
| `ollama_install.py` | **only when provider = ollama**: locate the binary (PATH + well-known paths), install (winget `Ollama.Ollama` · `brew install ollama` · official Linux script, elevated), start `ollama serve` detached and wait for `/api/tags`, list models, stream `POST /api/pull` with a progress bar, model table per hardware tier (`qwen2.5:{1.5b,3b,7b}-instruct`, `nomic-embed-text` / `bge-m3`) |
| `elevate.py` | explain → ask for OS admin password → run one command elevated (UAC `runas` / `sudo -S` / `pkexec`); logs `ELEVATED_CMD` (never the password) |
| `pkg_manager.py` | detect `winget` / `brew` / `apt`; install system components (libusb, CUPS, Devanagari fonts) |
| `python_env.py` | locate suitable Python, create `.venv`, `pip install --require-hashes` from the hash-pinned lock (network or `offline/wheels`) with human-readable progress |
| `locations.py` | ask/validate primary + secondary data locations (different device, writable, free space), write `config/locations.toml` |
| `recovery.py` | `--reset-owner-password`: asks the 12-word recovery key (shape-checked against the shipped wordlist, never logged — `Question.sensitive`), the new password twice, **proves administrator rights** by running a harmless command through UAC/sudo/pkexec (`E-SYS-004` stops before any change), checks the app is installed (`E-SYS-002`), then hands key + password to `python -m vaishnavi reset-owner-password` through stdin JSON; the new recovery sheet is shown from `SECRET\|` lines that never reach a log |
| `selftest.py` | three support modes: `--selftest` (hands over to `python -m vaishnavi selftest`, streams the output, maps 0/1/2 → OK/WARN/FAIL, 30-min cap), `--doctor [--fix] [--lang]` (hands over to `doctor`, echoes the plain-language report), `--where` (no app needed: program, setup-log and config folders plus — from `locations.toml` — primary, database, logs, receipts and backups; WARN when nothing is recorded yet; notes `E-SYS-012` when both live on one disk) |
| `app_stages.py` | stages 11–13 — see "`commands/` and `app_services.py`" below |
| `offline.py` | read a prepared `offline/` bundle; `--make-offline-bundle` builds one on a connected PC (Ollama installer and model import for the bundle are wired through `ollama_install.py`) |
| `autostart.py` | register / remove Task Scheduler task / launchd plist / systemd user service + watchdog |
| `maintenance.py` | `--restore` (list snapshots via `restore --list --json` → pick → confirm → `restore --id … --yes`) and `--update <zip\|folder>` (`backup --trigger pre-update` → `update.plan_update/apply_update` with pip-from-lock + `selftest` checks → rollback on failure); messages `[restore]` / `[update]` EN+HI |
| `update.py` | `plan_update(program_dir, source)` (ZIP unpacked safely; `pyproject.toml` + `src` + `bootstrap` + `main.py` required; versions read from `pyproject`), `apply_update(plan, run_check)` (old files → `.previous/`, copy, `install` then `selftest` checks, `_rollback`), `UpdateError` |
| `tls.py` | *(removed in Phase 7 — superseded by `vaishnavi/platform/tls.py`)*: the installer's `openssl` shell-out was never wired; HTTPS material is now created inside the venv with `cryptography`, once, when `server.https_enabled` is set |
| `minilog.py` | stdlib `http.server` on `:8081` tailing `logs/install` + `logs/startup` (HTML auto-refresh + `/raw`) |
| `handover.py` | locate the venv interpreter, run `python -m vaishnavi <cmd>` streaming output into the install log, map its exit code |
| `messages/{en,hi}.toml` | all user-facing strings of the installer |

## 3. `db/`

| Module | Responsibility |
|--------|----------------|
| `schema.sql` + `schema.py` | **Physical schema as plain SQL** (tables, CHECKs, indexes, protection triggers) — applied by Alembic `0001_initial`, loadable with stdlib `sqlite3` so the triggers are tested on any machine (`tests/core/test_db_schema.py`); `load_statements()` splits on complete statements so trigger bodies stay whole |
| `engine.py` | SQLAlchemy engine for SQLite with pragmas (`WAL`, `synchronous=FULL`, `foreign_keys`, `busy_timeout=5000`, `temp_store=MEMORY`), `BEGIN IMMEDIATE` for writer sessions; re-exports `WriterLock` |
| `lock.py` | stdlib single-writer advisory lock file next to the database (`msvcrt` / `fcntl`); `E-DB-030` when a second instance starts — used by the start-up spine without the ORM |
| `sqlite_stores.py` | stdlib `sqlite3` implementations of every service store protocol (`SqliteTxn`, user, recovery, audit, setting/secure-setting, system, notification) so the doctor, the commands and the start-up path run on a bare interpreter; the ORM repositories implement the same protocols for the UI |
| `guardian.py` | start-up triage and clean-shutdown handling (see `DbGuardian` below) |
| `session.py` | `UnitOfWork` context manager; read-only session factory for AI tools |
| `models/` | one module per aggregate: `user.py`, `catalog.py`, `inventory.py`, `sales.py`, `shift.py`, `audit.py`, `ai.py`, `system.py` — SQLAlchemy 2.0 `Mapped[...]` classes **generated from `schema.sql`** (INTEGER→`int`, TEXT→`str`, NUMERIC→`Decimal`, BLOB→`bytes`; nullable unless `NOT NULL`/PK; DB defaults stay server-side). `tests/core/test_db_models_match_schema.py` cross-checks tables, columns and nullability in both directions with the stdlib `ast` module |
| `sqlite_stores.py` *(replaces the planned `repositories/`)* | every store protocol the services declare, on stdlib `sqlite3`: `SqliteTxn` (one `BEGIN IMMEDIATE` transaction per `with`, handle closed on any failure), catalog · inventory · labels · users · settings (+ secure) · audit · recovery · notifications · work sessions · billing · print queue · hardware · shifts · reports · forecast · prompts · chat · help index · publish queue. The ORM models under `models/` remain the schema's typed twin for Alembic; the ORM query classes were never needed and were removed in Phase 7 |
| `records.py` | stdlib frozen dataclasses exchanged between repositories and services (`UserRecord`, `AuditEntry`, `PASSWORD_SENTINEL`, `GENESIS_HASH`) — services never see ORM objects, so the dependency direction stays services → db and the services are testable with in-memory fakes |
| `alembic/` | `alembic.ini`, `env.py` (uses the application engine + `Base.metadata`), `script.py.mako`, `versions/0001_initial.py` (replays `schema.sql` statements so Alembic and sqlite3 create identical objects) |
| `migrate.py` | `upgrade_to_head(db_path)` — **dry-run on a copy first** (`copy_database` into `tmp/`, upgrade, `verify_schema_objects`), then the live file; `head_revision()` / `current_revision()` for the doctor and the About page |
| `seed.py` | **plain-SQL, idempotent** `apply_seed(conn)` on any DB-API connection (installer stage, `python -m vaishnavi seed`, tests): roles, the Owner account with the `!SETUP` password sentinel (login refuses it until Owner setup — nobody can claim the account with a default password), 10 puja categories (EN/HI), `OPEN-ITEM` system product, `setting` rows `catalog.units` / `tax.presets` (presets, never a guessed rate), counters, 11 `setup_step` rows, `prompt_version` 1, `db_identity`; returns a `SeedReport`; `is_owner_setup_pending(conn)`; `--demo` dataset (later unit) |

## 4. `services/`

| Service | Responsibilities | Emits events | Audits |
|---------|------------------|--------------|--------|
| `UserService` | login (username + password, scrypt — ADR-011 note), **persisted lockout** (5 wrong → 15 min, `E-AUTH-003`), same error and timing for unknown user / wrong password (`E-AUTH-001`), two roles (`OWNER`, `STAFF`), seeded-Owner **setup flow** (`complete_owner_setup` replaces the `!SETUP` sentinel with password + PIN once; login says `E-AUTH-005` until then), create/reset/deactivate accounts (last active Owner protected, `E-AUTH-006`), password change with re-entry, rehash-on-login, Owner password & **Owner PIN** management, override verification (`verify_owner_pin` → Owner `Actor(pin_verified=True)`, in-memory 3/15 lockout `E-AUTH-030`), `login_tiles()` (names + roles for the login screen, no secrets), `reauthenticate()` (lock-screen unlock with the own password — same lockout counter, never a new login) | — | LOGIN, LOGIN_FAILED, UNLOCK, UNLOCK_FAILED, OWNER_SETUP_DONE, USER_CREATED/UPDATED/ACTIVATED/DEACTIVATED, PASSWORD_CHANGED/RESET, PIN_CHANGED, OVERRIDE, OVERRIDE_FAILED |
| `WorkSessionService` | `start(user_id, client)` — session at the **server** time; one active per user (others end as `FORCED_BY_LOGIN` at their last heartbeat); returns the unconfirmed unclean session for the banner · `end_work_preview()` (duration, bills, ₹ derived from `sale` by window, open shift) · `end()` → `LOGOUT`, open shift needs the Owner PIN (`E-AUTH-020`) · `heartbeat()` (False once closed) · `window_closed()` beacon → `WINDOW_CLOSED` · `monitor()` job (60 s silence → `HEARTBEAT_LOST` at `last_seen_at`) · `record_lock / record_unlock(locked_seconds) / idle_lock_timeout(locked_since)` · `close_all(APP_SHUTDOWN)` + `recover_unclean()` · `pending_confirmation / require_confirmed (E-AUTH-021) / confirm` · Owner `force_end / annotate` · `who_is_logged_in`, `history()` (Owner all, staff own; derived figures), `unclean_streak()`; stateless — the browser's `ClientState` carries `locked_since`. Endpoints and the browser script live in `api/session.py` | `WorkSessionEvent(kind=LOGIN · LOGOUT · WINDOW_CLOSED · HEARTBEAT_LOST · IDLE_LOCK_TIMEOUT · FORCED_BY_LOGIN · FORCED_BY_OWNER · CONFIRMED)` | LOGIN, LOGOUT, SESSION_ENDED reason=…, SESSION_CONFIRMED, SESSION_ANNOTATED |
| `SecureSettingsService` | encrypted Owner-only values (UPI ID, bank account, cloud API keys, publishing credentials): Fernet behind the `SecretBox` protocol with the key held in the OS secret store (`platform/secrets.py` → keyring); **allow-listed key names only** (`SECURE_KEYS`, `E-SEC-010`); masked previews (`•••• 1234`; API keys show nothing); `reveal()` Owner-only, `reveal_for_system()` for in-process consumers (receipt QR, provider factory); `import_pending()` takes the installer's in-memory secrets; a value that no longer decrypts → `E-SEC-011` (re-enter); **no read path exposed to the AI tool layer** (import-linter contract) | — | SECURE_SETTING_CHANGED / CLEARED (key name only) |
| `PromptService` | Owner-editable assistant instructions: `active()`, `versions()`, `propose(owner, text, note)` (stored with the quick-check result — a failing version is kept for the record but can never be activated), `activate` / `rollback`, `extra_blocked()`; the locked core's SHA-256 is written on every row. Composition itself lives in `ai/prompts.build_system_prompt` | — | PROMPT_VERSION_CREATED, PROMPT_ACTIVATED |
| `CatalogService` | products CRUD with `row_version`, **Product ID / SKU `PREFIX-NNNN` / internal EAN-13** (`core/barcode.py`: GS1 check digit, `29` prefix, `internal_ean13(id)` so the code is always derivable from the id), multi-barcode table (`INTERNAL` / `MANUFACTURER` / `PACK`; `E-CAT-004` duplicate code, `E-CAT-020` bad check digit, `E-CAT-010` barcode already on another product), `resolve_barcode(code) → product`, `search` by `#ID` / code / barcode / EN+HI name / fuzzy, archive & restore, categories, tags, quick picks. CSV import/export lives in `ImportExportService`, labels in `LabelService` | `ProductChanged` | PRODUCT_*, PRICE_CHANGED, BARCODE_ADDED |
| `InventoryService` | **single entry point `post_movement()`** for all stock changes: running balance, negative-stock → `E-INV-003` BLOCK, role rules (`SALE` only from inside `BillingService.checkout()` with the committed sale; `SALE_VOID`/`RETURN` only with an approved void; all other types `@requires(STOCK_WRITE)` = OWNER); `opening_stock` once per product, `adjust` with mandatory reason, `damage`, `purchase` (supplier, cost → `PURCHASE` price-history row, `label_count` for *print N labels*), stock-take `start → count → approve | cancel`, `reconcile` (stock = Σ movements), `valuation`, low-stock query + `StockChanged` notification, `stock_daily_balance` job; DB triggers mirror the same rules | `StockChanged` | STOCK_OPENING, STOCK_ADJUSTED, STOCK_DAMAGE, PURCHASE_RECORDED, STOCK_TAKE_STARTED / APPROVED / CANCELLED, PERMISSION_DENIED |
| `ImportExportService` | CSV `preview_csv` (per-row problems, nothing written) → `apply` (creates / updates through `CatalogService`, so every row is audited as `PRODUCT_*` and every price change lands in price history) → `export_csv` (UTF-8 BOM so Excel opens Hindi names); template download; `IMPORT_PREVIEW / IMPORT_APPLIED / EXPORT_CSV` log lines | — | (via CatalogService) |
| `LabelService` | `LabelModel` (name EN/HI, SKU, barcode, price, MRP), queue of label jobs (per product × count, from purchases or by hand), render through an **injected renderer** (`hardware/labels.py`: ESC/POS strip with native `GS k`, HTML A4 sheet 65-up / 40-up, PDF via optional reportlab) — services never import hardware; print log per product | — | LABEL_PRINTED, LABEL_REPRINTED |
| `TaxEngine` | pure functions (`services/tax.py`): `line(LineInput)` → gross = shelf × qty / pricing-unit, offer and/or ad-hoc discount (stack only when the offer allows, else the higher wins and is named), inclusive / exclusive split, CGST/SGST halves; `bill(BillInput)` → bill discount pro-rated by net (largest remainder), Σ, groups by rate, round-off (`billing.tax_rounding`); `enabled=False` zeroes rates for an unregistered shop | — | — |
| `BillingService` | `Cart` / `CartLine` in memory; `lookup_scan` (scale codes then barcode table) · `add_scan` · `add_lookup` (F3 → `LOOKUP`) · `add_quick_pick` (staff refused in strict mode `E-BILL-070`) · `add_open_item` (policy ALLOW/PIN/BLOCK, cap `E-BILL-071`, under-price `E-BILL-072`) · `set_qty` · `set_line_discount` / `set_bill_discount` (reason mandatory, cap → PIN `E-BILL-060`) · `remove_offer` (Owner/PIN `E-BILL-061`) · `hold` / `held_carts` / `resume` · **`checkout`** (one `BEGIN IMMEDIATE`: replay on `client_txn_id` → open shift `E-BILL-042` → counter `invoice:<fy>` → hash with chain head → sale / items / payments → `SALE` movements with negative-stock policy → audit; the same cart twice returns the first invoice) · `add_checkout_hook` (HardwareGuard) · **`void` / `refund` / `void_status`** (credit note = new `VOIDED` / `REFUND` sale row with its own invoice number in the chain, reversing `SALE_VOID` / `RETURN` movements under the Owner's id, cash out of the actor's drawer; staff need the PIN + approver; `void_window_hours` for staff `E-BILL-090`; per-line refundable quantities `E-BILL-091`; already voided `E-BILL-092`) · reads `get_sale`, `find_sale` (invoice / public id / receipt EAN-13), `search_sales`, `recent_sales`, `today_summary`, `next_invoice_preview` | `SaleCommitted`, `StockChanged` | SALE_CREATED, OPEN_ITEM_LINE, MANUAL_LINE, DISCOUNT_OVERRIDE, SALE_VOIDED, REFUND |
| `OfferService` | CRUD for offers (PCT / AMOUNT / BUY_X_GET_Y; scope PRODUCT / CATEGORY / TAG / BILL; validity; stackable), `applicable(product, qty)`, `best_for` (highest benefit), `bill_offers`, `usage` (lines, ₹ given); Owner-only (`MANAGE_OFFERS`) | — | OFFER_CREATED, OFFER_UPDATED, OFFER_DEACTIVATED |
| `OpenItemReviewService` | `pending()` — open-item lines on completed bills nobody reviewed yet, grouped by name (count, ₹, typical price, details); `convert(actor, ids, name, price, category)` → `CatalogService.create` (source `CONVERSION`, ID / SKU / barcode generated) + rows marked `CONVERTED`; `dismiss`. Owner-only (`MANAGE_PRODUCTS`) | — | OPEN_ITEM_CONVERTED, OPEN_ITEM_DISMISSED |
| `LedgerService` | `canonical_sale()` / `sale_hash()` (docs/05 §4, quantities normalised, no db ids), `hash_for_new_sale()` for checkout (inside `BEGIN IMMEDIATE`), `chain_head()` (Z-report, backup manifest), `verify_sales()` (hash **and** link check → `E-LEDGER-001` with sale_id/expected/found), `verify_gaps()` (COUNT = MAX per FY, missing numbers → `E-LEDGER-002`), `reconcile_stock()` (Σ movements = cached; day's sale lines = SALE movements; open items excluded → `E-LEDGER-003`), `verify_audit()`, `run_all()` → `IntegrityStatus` + `integrity_check` rows; `INTEGRITY_OK/FAIL` log lines. Store protocol `LedgerStore`; stdlib `db/ledger_store.py` (raw sqlite3 — doctor/restore verify a file without the ORM) | — | INTEGRITY_* |
| `ShiftService` | `open(actor, opening_float_paise)` (one open per user, `E-SHIFT-002`) · `cash_in` · `cash_out` (`CASH_OUT` = PIN for staff; ≤ drawer, `E-SHIFT-004`) · `expected_cash` = float + cash sales (tendered − change) − cash refunds + in − out · `close(actor, shift_id, denominations | counted, note)` (variance = counted − expected, shortfall needs a note, Owner may close any shift) · `x_report` / `z_report` (bills, items, totals by mode, discounts, manual lines, voids, invoice range, **chain head**) · `report_lines` · `variance_history` · `cashier_variance_summary` | `ShiftClosed` | SHIFT_OPENED, SHIFT_CLOSED, CASH_IN, CASH_OUT |
| `ReportService` | Owner control reports straight from the ledger tables (stdlib SQL → dataclasses): `discount_audit`, `offer_usage`, `manual_lines`, `voids`, `overrides` (every audit row with an approver or an override action), `shrinkage`, `cashier_performance`, `tax_summary` (credit notes subtracted), `integrity()` (run the ledger pass now), `audit_trail`; **sales side** (Phase 5): `sales_by_day` (IST shop days, credit notes as refunds), `sales_summary`, `item_sales`, `top_sellers` (qty / rupees, share), `basket_pairs` (bought together, once per bill), `dead_stock` (valued), `low_stock` (both roles), `purchases` / `purchases_by_day`, `profit` (cost snapshot per line; lines without cost counted), `hourly_heatmap`, `stock_as_of`; `to_csv` (UTF-8 BOM, dataclass header). Default window: last 90 days to the end of today; date-only bounds accepted | — | — |
| `AuditService` | `record(session, ACTION, user_id, actor_user_id, entity, entity_id, before, after)` inside the caller's transaction; rows are **hash-chained** (`sha256` over canonical JSON + `prev_hash`, genesis `0×64`) on an append-only table, so a tampered or deleted row breaks the chain (`verify_chain()` → first broken id; nightly check in unit 21); secret-looking keys scrubbed to `***`; mirrored as JSON lines to `audit/audit.jsonl` through the `vaishnavi.audit` logger | — | — |
| `BackupService` | snapshots via `sqlite3.Connection.backup()` → `backup_id` stamped **in the copy only** → `integrity_check` (`E-BKP-003` discards) → manifest (schema/app version, instance id, last invoice, chain head, sha256, size, row counts) → secondary when really mounted (same volume as the data while a separate drive was chosen = not mounted) else `backup-queue/` (`E-BKP-001`, `flush_queue()`) + 3-day local copies; `backup_log`; `prune()` daily / weekly / monthly / `pre-*` tiers; `verify_snapshot()`; weekly `restore_drill()` (`E-BKP-010`); `restore()` (verify → `pre-restore` snapshot → copy-in → migrate → chain check → atomic swap); `due_on_start()` > 20 h | `BackupDone`, `RestoreDone` | BACKUP_*, RESTORE, RESTORE_DRILL |
| `DbGuardian` (in `db/guardian.py`) | start-up database triage: `quick_check` (damaged file → `quarantine/`, `E-DB-003`); `db_identity` + fingerprint comparison → NORMAL / UNCLEAN (`E-DB-022`) / **RESTORED** (`E-DB-020`, `backup_id` present → `restored_at/from` stamped) / FOREIGN (`E-DB-021`) / NEW / MISSING (`E-DB-002`); `mark_open()`, `mark_clean_close()` (WAL checkpoint + `state/last_db_fingerprint.json`); monthly `vacuum()` | `DbRestoredDetected` | DB_RESTORED_DETECTED, DB_UNCLEAN_CLOSE |
| `PriceHistoryService` | append `price_history` rows for every price/MRP/cost/tax change (called by Catalog, import, purchase inside their transactions) with old → new / user / reason; `history(product, window)` for 1 month / 6 months / 1 year / custom dates; `chart_points` for the pop-up; stats (most changed, biggest jumps) | — | PRICE_CHANGED |
| `PrintQueueService` | `after_sale(sale_id)` on `SaleCommitted`: archive always → print when the configured printer is ready → else `print_pending` row + `E-PRN-001` logged (never raised to the cashier); `print_pending()` (button + 60 s job, oldest first, stops at the first failure); `reprint(actor, sale_id)` (`DUPLICATE`, `reprint_count`, above `billing.reprint_limit` → Owner/PIN `E-AUTH-011`); `chip()` (configured / ready / pending → green · amber · grey) | — | REPRINT |
| `SettingsService` | typed get/set over the `setting` table: file settings are the base, rows are **Owner overrides**; a value is coerced and validated through `Settings.with_overrides` *before* it is stored (an invalid value is never written, `E-CFG-001` names the key); unknown names rejected (`E-CFG-002`); secret-looking names rejected (`E-SEC-006`); small reference lists without a file counterpart (`catalog.units`, `tax.presets`) as `DATA_KEYS`; `effective()` cached and invalidated on every write; a stored override that became invalid after an upgrade is logged and ignored instead of blocking start-up | `SettingsChanged` | SETTING_CHANGED, SETTING_RESET |
| `ReceiptService` | `build(sale)` → `ReceiptModel` from the **committed** `SaleView` (shelf → discount → amount per line, offer note, `†` / `*` markers + legend, tax rows only when registered, "You saved", payments with change, UPI payload when `receipt.show_qr_upi`, receipt EAN-13 `28+FY+seq`, `CREDIT NOTE` / `DUPLICATE` label); `escpos(model, paper_mm)` / `html(model)` through the injected `hardware/receipts` renderer; `archive(model)` → `<primary>/receipts/YYYY/MM/<invoice>.html` (+ PDF via weasyprint when present); `mark_printed` | — | — |
| `ReceiptShareService` | Bill → customer's WhatsApp. `text_for(sale)` = bold shop name + bill no. + the 58 mm receipt in a monospace block; `link_for(phone, text)` = `https://wa.me/<digits>?text=…` click-to-chat; `share(actor, sale_id, phone=, name=)` resolves the number (typed now → `BillingService.remember_customer`, else the sale's customer), posts via the Meta Cloud API when Settings → Publishing has a phone-number id + token (`post` injected by `app_services` from the publisher — services never import `publishing`), otherwise returns the link; API failure → link; audit `RECEIPT_SHARED` with `via` and the last 4 digits only. Never raises | — | — |
| `ForecastService` | `forecast(product, horizon)` → point / low / high / method / confidence / daily rate / classification / caveats from the `forecast/` engine (series from first sale day to yesterday, stock-out days censored, festival uplift from `config/festivals.toml`); `E-FCST-001` below `forecast.min_history_days`; `plan(actor)` → every active item's stock, rate, days of cover, stock-out date, reorder quantity (a NEW item gets the gap to its minimum), worst cover first; `snapshot_all()` nightly (`forecast_nightly` job) → `forecast_snapshot`; `snapshots`, `accuracy` (MAPE / bias once the horizon has passed) | — | — |
| `InsightsService` | `for_owner(actor)` / `for_staff(actor)` → ranked `Insight(kind, severity, key, link, params)` **facts with i18n keys** (never prose; the UI or the chat layer phrases them): sales today vs yesterday, low stock, stock-outs within 7 days, reorder count, dead stock value, manual-line share ≥ 15 % per cashier, last drawer variance, receipts waiting. Staff get only low stock / print queue. Forecast failures are logged and skipped — the dashboard never breaks on advice | — | — |
| `ReportPublisher` | `daily_digest(day)` / `weekly_report(end)` compose figures from the authorised report / forecast / insight / shift / ledger services into the renderer's data classes, write the document under `<primary>/reports/<YYYY-MM>/`, queue one delivery per enabled channel and try to send; `publish_table` / `render_document` for any report or purchase order; `flush()` retries due rows (job every 5 min); `chip()` grey · green · amber · red; `test_channel`; renderer, outbox and channel factory are injected by AppServices (services never import `publishing`); disabled → local document only | — | — |
| `PurchaseOrderService` | `build(actor)` groups the reorder plan by each item's **last supplier** (from `PURCHASE` movements; else *Unassigned*) with the last unit cost as the estimate; `document()` / `send()` through the publisher | — | — |
| `NotificationService` | creates/reads/marks notifications (per user, severity `INFO/WARN/HIGH`, deep link, `source_ref`); **open-alert dedupe** — an identical unread alert is not repeated until it was read; subscribed to alert firings and domain events (`PrinterAppeared`, `BackupDone` queued/failed, `StockChanged` low-stock, `SessionEnded` unclean, `DbRestoredDetected`) | `NotificationCreated` | NOTIFICATION_CREATED |
| `MetricsService` + `AlertService` | in-process counters/histograms (checkout latency, errors/min, queue sizes…), `/api/v1/system/metrics` JSON + Prometheus text; `alerts.toml` evaluation every 60 s → notifications (+ publisher for HIGH) | `AlertFired` | ALERT_FIRED |
| `OnboardingService` | 11-step first-day checklist (`shop_profile, tax, payments, staff, products, labels, opening_stock, scanner, printer, backup_drive, recovery_key`); **done-ness is probed from live data** (settings, users, catalogue, movements, paired hardware) rather than stored, so a step ticks itself when the work is done from its own page; `skip` / `unskip` is the only persisted state; `status()`, `progress()`, `next_step()`, `is_complete()`, `checklist(texts)` for the dashboard "Setup n/11" card. The wizard forms live in `ui/pages/onboarding.py` | `SetupChanged` | SETUP_STEP_DONE, SETUP_STEP_SKIPPED, SETUP_STEP_REOPENED |
| `RecoveryService` | generate/verify the Owner **recovery key** (12 BIP-39 words from `core/wordlist.txt`, scrypt-hashed, shown once as a printable sheet), `acknowledge()` / `needs_attention()` (doctor WARN until acknowledged), rate-limited `verify()` (`E-AUTH-040/042`), `reset_owner_password()` — needs `elevated=True` (`E-AUTH-041`, proven by the installer), retires the key and issues a new one | — | RECOVERY_KEY_ISSUED, RECOVERY_KEY_ACKNOWLEDGED, RECOVERY_KEY_REJECTED, OWNER_PASSWORD_RESET |
| `HardwareGuard` | strict-mode gatekeeper + hot-plug watcher (`services/hardware_guard.py`): `refresh()` / `poll()` (10 s job) probe the paired scanner, the configured printer and new printer names, cache `HardwareState`, publish `HardwareStatusChanged` once per change; `can_add_line(actor, via_scan)` (typed code `E-BILL-070`, scanner absent `E-SCN-001`; probe failure = unknown, never a lock-out), `can_checkout` (billing hook: `require_printer` + not ready → `E-PRN-002`), `scan_mode`, `start_override` / `active_override` / `end_override` (`hardware_override`, ≤ 12 h, reason, Owner or PIN + approver), `may_enable_require_printer` | `HardwareStatusChanged` | HARDWARE_OVERRIDE, HARDWARE_OVERRIDE_ENDED |

### `BillingService.checkout()` contract
```python
def checkout(self, cart: Cart, payments: list[PaymentIn], actor: User, shift: Shift,
             override: Override | None = None) -> SaleResult:
    """Atomically commit a sale.
    Raises BillingError(E-BILL-042) if no open shift, E-BILL-050 if payments < total,
    InventoryError(E-INV-003) on blocked negative stock, AuthError(E-AUTH-010) on cap breach
    without override. Logs DEBUG per step with cid; INFO SALE_COMMITTED on success."""
```

## 5. `ai/`

| Module | Responsibility |
|--------|----------------|
| `providers/base.py` | `LLMProvider` protocol (`chat`, `stream`, `embed`, `health`, `capabilities`), `ChatMessage` (with `tool_calls` for echoing tool exchanges), `ToolSpec`, `ToolCall`, `ChatResponse`, `ChatChunk`, `ProviderHealth`, `Capabilities`; failures are `AIError` with `E-AI-0xx` |
| `providers/_http.py` | the only network code of the tier: stdlib `urllib` `post_json` / `get_json` / `post_lines` (NDJSON + SSE); maps every failure once — 401/403 `E-AI-012`, 429 `E-AI-021`, 404 local `E-AI-004`, network `E-AI-001` (local) / `E-AI-020` (cloud); the key never enters an exception or a log line |
| `providers/ollama_provider.py` | Ollama HTTP API: `/api/chat` (JSON and NDJSON stream, native tools, `keep_alive`, `num_ctx`), `/api/embed`, `/api/tags` health with model-presence check |
| `providers/openai_compat_provider.py` | OpenAI-style `/v1` API by `base_url` — serves **OpenAI** and the **Hugging Face router**: `/chat/completions` (JSON + SSE, tools with JSON-string arguments), `/embeddings`, `/models` health; `on_usage(provider, model, prompt, completion, embed)` sink feeds `ai_usage` and the budget guard in `ChatService` |
| `providers/null_provider.py` | provider = none (or key missing): every call raises the *reason* code (`E-AI-010` / `E-AI-011`); the chat service routes everything to the fast path |
| `providers/fake_provider.py` | scripted replies / tool calls, deterministic hash embeddings (switchable off), call recording — tests and the red-team harness |
| `providers/factory.py` | `build_provider(settings.ai, api_key)` → provider or `NullProvider` with the reason; `CircuitBreaker` wrapper (3 failures → open 60 s, replays the last error instantly, half-open probe closes); AppServices rebuilds the provider on `[ai]` setting changes |
| `tool_protocol.py` | `ToolDef`, `validate_args` (tiny JSON-schema checker: types, required, enum, bounds, `limit ≤ 50`, dates), `trim_result`, `strip_keys` (staff allow-list), `parse_tool_block` (prompted **JSON tool-call fallback**: exactly one fenced block, object arguments only), `ToolError` (`E-GRD-001/005`) |
| `embeddings.py` | float32 `pack`/`unpack` for the `embedding` table, `cosine`, `tokens`, `keyword_score` (fallback ranking) |
| `indexer.py` | `DocIndexer`: chunks **`15-user-guide.md` + how-to sections of `09-frontend-guide.md`** by heading, hash-skips unchanged text, embeds with the current provider/model, `search` = hybrid 0.6·cosine + 0.4·keyword (keyword-only without embeddings); architecture/internals docs are never indexed |
| `tools.py` | `ToolRegistry`: 26 read-only tools per docs/06 §3 (9 for STAFF), built on the authorised services; `for_role`, `run` (validate → role gate → handler → strip → trim, `AI_TOOL_CALL` log), `run_logged` (errors become data for the model), `period_bounds` (today … this_year, explicit dates clamped to 2 years), `link_for` (Open-report chip) |
| `guardrails.py` | `classify(text, role, extra_blocked)` → `Verdict(ALLOW / REFUSE / REFUSE_AND_FLAG, category, severity, matched, language)` from ordered EN/Hinglish/Devanagari regex lists (flagged categories first), `OWNER_ONLY` for staff, off-topic vocabulary, allowed-topic check; `refusal()` canned EN/HI/Hinglish; `detect_language`; `RateLimiter`; `OffTopicCounter`; `quick_check_instructions` |
| `output_filter.py` | `scan(answer, tool_results, question)`: PIN-in-context, UPI id, IFSC, Luhn card, 9–18-digit accounts, file paths, code fences / SQL, and **numbers absent from tool results and the question** (counts ≤ 10 exempt) → `FilterResult` |
| `redteam/corpus.py` | `BLOCKED` (60 prompts with expected category), `OWNER_ONLY` (10), `ALLOWED_STAFF` (12), `JAILBREAK_FRAMINGS` (5) — EN/HI/Hinglish; used by `tests/core/test_ai_guardrails.py` and the optional live run |
| `prompts/` | `core.md` (**locked guardrail core**, SHA-256 recorded on every `prompt_version`), `profile_staff.md` / `profile_owner.md`, `build_system_prompt(role, language, owner_instructions, shop_name, native_tools)` — core → role profile → Owner instructions (→ tool hint for prompted-JSON providers) |
| `fastpath.py` | `match(text)` → greeting · stock of X · price of X · low stock · out of stock · today's sale (Owner) · how do I …; `answer()` renders bilingual templates from the same tool results; ≤ 6-word rule sends comparisons to the model; Owner-only intents return None for staff; used first on every message and exclusively when the provider is off/unreachable |
| `chat_service.py` | `ChatService.ask`: length → rate limit → lock → guardrail (refusal + `chat_security_event` + Owner alert at `flag_alert_threshold`) → fast path → provider (system prompt, last 8 turns, help excerpts for how-to questions, ≤ `max_tool_rounds`, prompted-JSON fallback) → output filter → persist (`chat_session`/`chat_message` with `guardrail_decision`, provider/model) → `ai_usage` + monthly budget `E-AI-030`; `history`, `security_events`, `usage`, `system_prompt` |

## 6. Import-safety rule (because the development PC runs nothing, and the target OS varies)

Every optional integration (`ollama`, `escpos`, `pywin32`, `pyusb`, `hidapi`, `pyudev`, `pyserial`,
`pycups`, `weasyprint`, `requests`, `keyring`) is imported **lazily inside the adapter** and wrapped: if
the import fails or the OS does not apply, AppServices binds a `Null*` implementation and the doctor
reports it in plain words. Core modules (`core`, `db`, `services`) depend only on SQLAlchemy, pydantic
and the stdlib, so `vaishnavi selftest` can run the business-logic tests on Windows, macOS or Linux even
before Ollama or a printer exists. OS-specific wheels are declared as environment markers in
`pyproject.toml` (`pywin32; sys_platform == "win32"`, `pyudev; sys_platform == "linux"`).

## 7. `hardware/`

| Module | Responsibility |
|--------|----------------|
| `receipts.py` | **Receipt renderers** (built): `render_escpos(model, paper_mm, hindi_raster, drawer_kick)` for 80 mm / 48 columns and 58 mm / 32 columns — native EAN-13 (`GS k`), QR (`GS ( k`), emphasis / double height, drawer kick, partial cut; `to_printer_text` transliterates for code-page printers (`₹`→`Rs`, `†`→`^`, dashes), Devanagari only through the optional raster hook; `text_lines(bytes)` strips the command stream (tests, log preview); `render_html(model)` self-contained HTML with the SVG barcode and `@page` sized to the paper; `render_pdf` via weasyprint when present |
| `printer/escpos.py` | **Transports** (built): `NetworkTransport` (`host:9100` socket), `SystemTransport` (Windows `win32print` RAW, else `copy /B` to the shared printer; POSIX `lp -d <name> -o raw`), `UsbTransport` (`/dev/usb/lp*` matched by VID:PID on Linux, else the OS printer on the USB port), `FileTransport` (debug / `printer_kind = "file"`), `transport_for(HardwareSettings)`; failures raise `PrinterError(E-PRN-001)` |
| `printer/discovery.py` | *planned refinement* — today discovery is `services/hardware_guard.discover_printer_names()` (OS printers + USB printer-class devices) and `ui/pages/settings_hardware.discover_printers()`; network subnet probing arrives with Phase 7 |
| `labels.py` | `LabelModel` lives in `services/labels.py`; here the renderers: `ean13_modules` / `barcode_svg`, `render_escpos` (EAN-13 strip + cut), `render_html_sheet` (A4 65-up / 40-up), `render_pdf_sheet` (reportlab optional) |
| `scanner.py` | *thin* — HID scanners are keyboards; the speed heuristic is `ui/pages/pos.classify_entry`, pairing/presence is `services/hardware_guard.scanner_probe` over `platform/devices`, weight-embedded codes are `services/measure.decode_scale_barcode` |
| `scale.py` | `WeighingScale` protocol (`read() -> Reading(weight_g, stable)`, `tare()`), `SerialScale` with protocol presets (`pyserial`), `NullScale` — *Phase 7 hardening* |

## 8. `services/measure.py` — quantities for weighed/measured items
Pure functions (built, Unit 36): `QuantitySpec` from the product (`spec_for`), `validate_qty` (`E-MEAS-001/002/003`),
`round_to_step`, `qty_for_amount` (₹ → base units on the step, never below the minimum), `line_amount`
(price per pricing-unit × qty / pricing-unit), `format_qty` (`250 g`, `1 kg`, `1.5 L`, `2 pcs`), and the
weight / price-embedded scale barcode decoder (`ScaleCodeFormat.parse("21PPPPPWWWWWC")`, `decode_scale_barcode`).
Used by `BillingService`, `ReceiptService`, the POS quantity pad and the receipt renderer.

## 9. `platform/` — OS adapters (the only place that knows the OS)

| Module | Windows | macOS | Linux |
|--------|---------|-------|-------|
| `paths.py` | `%USERPROFILE%\VaishnaviStore` | `~/VaishnaviStore` | `~/VaishnaviStore` (XDG optional) |
| `secrets.py` | `keyring` → DPAPI | `keyring` → Keychain | `keyring` → SecretService; encrypted-file fallback with doctor WARN |
| `devices.py` | PnP enumeration (`pywin32`/WMI) | IOKit via `hidapi` | `pyudev` / `hidapi`; udev rule helper |
| `printing.py` | spooler list/status/raw print (`pywin32`) | CUPS (`pycups` / `lp`) | CUPS |
| `serial_ports.py` | `COMx` via `pyserial.tools.list_ports` | `/dev/tty.*` | `/dev/ttyUSB*`, `/dev/ttyACM*` |
| `autostart.py` | Task Scheduler (`schtasks`) | launchd plist | systemd `--user` unit |
| `browser.py` | Edge/Chrome `--app=` | `open -a` | `xdg-open` / Chromium `--app=` |
| `elevation.py` | UAC `runas` | `sudo` / askpass | `pkexec` / `sudo` |
| `fonts.py` | Nirmala UI / Noto | Devanagari Sangam / Noto | Noto Sans Devanagari |

Every adapter has a `Null`/`Unsupported` implementation; the doctor reports which backend is active.

## 10. `publishing/`

| Module | Responsibility |
|--------|----------------|
| `renderer.py` | `DigestData` / `WeeklyData` → self-contained HTML by string templates (no Jinja2), `render_table` for any report, `digest_summary` (5 lines for chat channels), `write_document` → PDF when `default_pdf_engine()` finds weasyprint, else `.html` |
| `channels/__init__.py` | `Channel` protocol + stdlib `http_post` (JSON / form / multipart) with `E-PUB-003` mapping |
| `channels/folder.py` | copy into `<folder>/<YYYY-MM>/` with a `.txt` summary beside the document (`E-PUB-001` when no folder) |
| `channels/smtp.py` | `smtplib` STARTTLS + login, `EmailMessage` with the document attached; injectable `smtp_factory` |
| `channels/telegram.py` | Bot API `sendMessage` + `sendDocument` (multipart); injectable `post` |
| `channels/whatsapp.py` | Meta Cloud API text message with the summary (documents need hosted media → the folder/e-mail channel carries the file) |
| `outbox.py` | `Outbox` on `publish_queue`: `enqueue`, `due`, `mark_sent`, `mark_failed` (backoff 1·5·15·60·240 min, `FAILED` after `max_attempts`), `counts`, `recent`, `last_sent_at` |

## 11. `forecast/`

| Module | Responsibility |
|--------|----------------|
| `series.py` | `DailySeries` from `(day, qty)` rows between first sale day and yesterday (gaps = 0); `fill_censored` replaces stock-out days (`stock_daily_balance.qty_close ≤ 0`) with the mean of open days |
| `methods.py` | per-day rates `sma(7/28)`, `ewma(α 0.3)`, `last_year` (≥ 364 days), `ensemble` = **median** of the rates (exact when it equals an input), band = max(1.28·σ·√h, spread·h/2), confidence high (≥ 28 days, CoV ≤ 0.25) / medium / low |
| `festivals.py` | `load_festivals(path)` from `config/festivals.toml` (`[[festival]]` name/start/end/uplift per category, `"*"` = all), `uplift_for(day, category)`, `upcoming`, `learn_uplift` from last year's actuals, `apply_uplift` |
| `planner.py` | `days_of_cover`, `stockout_date`, `reorder_qty` = rate × (lead + cover) + min − stock rounded up to packs, `classify` → NEW (< 14 days) · DEAD (0 in 60 days) · FAST (≥ 10/day) · STEADY (≥ 1) · SLOW |

## 12. `api/` (same services as the UI)

| Router | Purpose | Auth | Default |
|--------|---------|------|---------|
| `/healthz` | liveness for watchdog | none | on |
| `/api/v1/system/logs…`, `/api/v1/system/doctor`, `/api/v1/system/status`, **`/api/v1/system/metrics`**, **`/api/v1/system/notifications`**, `/api/v1/system/support-bundle` | **log console & system API** ([08 §9](08-logging-and-troubleshooting.md#9-log-console-over-http--rest)) | Owner session or Bearer token | on (localhost); LAN when *Remote log access* enabled |
| `/system/logs`, `/system/status` | lightweight HTML pages (served by FastAPI + Jinja2, no NiceGUI dependency, work on any phone browser) | Owner session | on |
| `/api/v1/products`, `/stock`, `/sales`, `/reports` | data API for a LAN tablet / second counter | Bearer token | off (`api.enabled=false`) |

`api/logs.py` reads `logs/*.jsonl` (and parses text logs when JSONL is absent) with cursor paging,
applies the redaction filter, and streams via SSE; it never imports UI code.

`api/system.py` (unit 26) holds the framework-free handlers behind these routes so they are testable
on a bare interpreter: `AppState` (heartbeats per loop, status chips, `Metrics` counters/histograms),
`healthz()` (any heartbeat older than 60 s → `ok: false`, HTTP 503 via `http_status_for`), `status()`,
`doctor(checks=…)` (through `AppState.doctor_runner`, injected by start-up so this layer never imports
`bootstrap/`), `log_families()` / `logs_tail(family, level, limit)` (path-safe, level filter),
`metrics()` JSON and `metrics_prometheus()` text; `mount(app, state)` attaches them to FastAPI.

`api/session.py` (unit 28) — `heartbeat(sessions, payload)` and `closing(sessions, payload)` behind
`POST /api/session/heartbeat` / `/api/session/closing` (beacon bodies accepted as JSON or `text/plain`),
plus `HEARTBEAT_JS`, the script the shell injects on signed-in pages: a 15-s heartbeat that sends the
browser back to `/login` when the server says the session is gone, and `beforeunload` → `sendBeacon`
so a closed window is recorded as `WINDOW_CLOSED` within seconds.

## 13. `ui/`

| Module | Responsibility |
|--------|----------------|
| `layout.py` | **view-model first**: `build_shell(app_state, client)` → `ShellModel` (brand, shop-time clock, chips, role-filtered rail with active item, user badge — name, initials, role label, *session since*, shift #, Lock / End work — language toggle, assistant bubble only when signed in and AI is on, shortcut list, footer); `health_summary()` → the plain-language *morning health check* of the login pane. Thin `render_head/topbar/rail/chat_bubble/footer/health_summary` are the only NiceGUI code (lazy import). Window-close capture (`beforeunload` + `sendBeacon`) and the 15-s heartbeat arrive with the work-session unit |
| `state.py` | `ClientUser`, `ClientState` for NiceGUI `app.storage.user`: language, user, work session, shift, `session_since`, activity `touch()`, idle lock (`should_lock`, `lock/unlock`, `lock_timed_out`), `pending_exit_confirmation`, `actor()`; tolerant JSON round-trip (garbage → anonymous) ; `initials()` |
| `guards.py` | `NAV_ITEMS` (key, path, label key, icon, required permission), `nav_for(role)` (DENY → not rendered; PIN grants shown), `route_permission(path)` longest prefix, `decide(path, client)` → `ALLOW · LOGIN · LOCKED · FORBIDDEN (E-AUTH-010 logged) · REDIRECT_HOME`, `redirect_for()`, `page()` decorator — a courtesy; services enforce |
| `session.py` | `load_client(app)` / `save_client(app, client)` — the storage bridge |
| `server.py` | `serve(state, on_ready)` called by `startup.run_app`: mounts `/healthz` + system API, registers `/` and `/lock` frames and the page registrars (`PAGE_REGISTRARS`), fires `on_ready` from NiceGUI's startup hook, stable `state/ui.secret` for session cookies, `frame()` renders the shell around a page |
| `components/` | `status_chip` (`chip_views()` in mock-up order with a plain-language hint per level, grey *not checked yet*, `worst_level()`, `chips_from_hardware()` → Scanner / Printer), `notification_bell` (`bell_view()`: Owner sees all, staff their own rows + counter broadcasts; severity colour; `relative_time()` EN/HI), `product_tile`, `cart_table`, `qty_pad`, `pin_dialog`, `payment_dialog`, `denomination_grid`, `data_grid`, **`charts.py`** (dependency-free **SVG**: `sparkline_svg`, `bars_svg` — no JS chart library, renders in the e-mail digest too; the ECharts/matplotlib pair was dropped), `chat_widget` (floating bubble + compact window), `receipt_preview`, `wizard` |
| `pages/` | `login` (unit 28: `build_login_model()` — tiles owners-first + *Other*, keypad only for staff, "Sign in & start work → HH:MM", attendance banner; `sign_in()` → next page `/confirm-exit` · `/onboarding` · `/change-password` · `/pos` · `/`; `/confirm-exit` page), `onboarding` (unit 30: step 1 of the first-day wizard — Owner password + PIN replace the `!SETUP` sentinel, recovery key shown once with an acknowledge tick; `validate_owner_setup`, `complete_owner_setup`, `acknowledge_recovery_key`; later steps arrive with their units), `logs` (unit 29: `build_logs_model()` — parsed columns, level / text / **cid** filters, plain-language `E-` code text, download), `status` (unit 29: `build_status_model()` — health, uptime, chips, DB size/revision, jobs, last backup, live integrity pass, who is logged in), `settings` (tabs) with `settings_backups` (unit 29: locations, snapshots, *Backup now*, **`RestoreWizard`** CHOOSE → CONFIRM (Owner password) → DONE, drill status, manual-recovery card) and `settings_system` (unit 29: redacted config, code integrity, autostart, schema status, remote-access URLs, *Run doctor*, support bundle, shut down / restart requests), `dashboard` (unit 47: owns `/` — `build_dashboard()` → Owner tiles · 14-day sparkline · payment mix · insight bullets · low stock · reorder suggestions · who is at the counter · setup checklist · chips; staff: own shift, low stock, counter notices), `notifications` (unit 47: `/notifications` — bell history with the bell's visibility rule, mark read / mark all), `pos`, `products`, `labels`, `stock_*`, `sales_history`, `invoice`, `reports` (one registry, generic table + CSV, **Export everything (ZIP)**; keys incl. `basket`, `heatmap`, `forecast`, `demand`, `audit`), `shifts` (+ `day_close` — Phase 7), `settings_*` (other tabs). Shell-owned pages in `server.py`: `/lock` (own password; lock/idle recorded), `/end-work` (exit time, summary, open-shift warning + Owner PIN, note), `/change-password` |
| `i18n/` | `en.json`, `hi.json` (same key set — tested), `UiTexts.load(lang).t(key, **params)`; Hindi overlays English; `hinglish` → English labels |
| `theme.py` | the approved mock-up's palette/fonts as constants, `css()`, `brand_colors()` for `ui.colors`, `level_color()` / `severity_color()`, `format_clock()` (shop time, Hindi weekdays) |

## 14. `scheduler/`

`simple.py` — the stdlib `SimpleScheduler` (ADR-013 amended): `every(seconds=…)` and `daily(at="HH:MM")`
in shop time (IST), one background thread, each job isolated (`JOB_FAILED name= …` logged, counters kept,
the next job still runs), `status()` for `/system/status` and the doctor, `trigger(name)` for the
Settings → System page. `jobs.py` — the job table of [07 §7](07-startup-and-health-checks.md#7-scheduled-jobs-apscheduler-in-process)
as data (`JobSpec`: name, action key, interval or daily time) built from settings, and `bind_jobs()`
attaching the callables provided by `startup.run_app`; an action that has no implementation yet is
skipped with a DEBUG line, never an error. Each job runs inside `with_correlation()`.

## 15. AppServices (dependency wiring)

`app_services.py` wires concrete implementations once at startup (settings → stores → services; later
engine → repos → ai → hardware) and exposes them to UI/API through `AppState.extra["services"]`;
tests build an AppServices on a temporary SQLite file with a `ReversibleBox`, `NullPrinter`, and the
`fake_provider`.

## 16. Third-party packages (planned)

Layer-by-layer overview with the reasons: [18 — Technology Stack at a Glance](18-technology-stack.md).

| Purpose | Package |
|---------|---------|
| UI / server | `nicegui` (bundles FastAPI, uvicorn) |
| ORM / migrations | `sqlalchemy>=2`, `alembic` |
| Config / validation | `pydantic`, `pydantic-settings`, `tomli` (py<3.11 only) |
| Hashing | `argon2-cffi` |
| Images | `pillow` |
| Barcodes / labels / PDF | `python-barcode`, `qrcode`, `reportlab` |
| Receipts | `jinja2`, `python-escpos`; `weasyprint` only via the `pdf-html` extra (needs pango/cairo; `reportlab` is the primary PDF engine) |
| Export | `openpyxl` |
| AI | `ollama` (local provider), `openai` (OpenAI **and** Hugging Face router via `base_url`), `httpx`, `numpy` — provider packages imported lazily inside `ai/providers/` |
| Hardware detection | `pywin32` (Windows), `pyusb`, `hidapi`, `pyudev` (Linux), `pyserial` (scale) — all optional, lazy; `pycups` only via the `cups` extra (needs libcups headers; adapter falls back to the `lp` command) |
| Secrets | `keyring` (DPAPI / Keychain / SecretService), `cryptography` (Fernet) |
| Publishing | `requests` (Telegram/WhatsApp APIs), stdlib `smtplib` |
| Scheduling | `apscheduler` |
| IDs | `python-ulid` |
| Charts | ECharts via `nicegui` (bundled), `matplotlib` (headless PNG for PDFs) |
| Dev | `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `import-linter`, `pip-tools` (hash-pinned lock) |
