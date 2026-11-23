# Changelog — Vaishnavi Store Manager

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.
Each entry names the Phase-1 unit from `docs/13-roadmap.md` it belongs to.

## [Unreleased]

### Added
- **Skeleton** — directory tree for every documented package (208 placeholder modules containing only
  a responsibility docstring), READMEs for the two `bootstrap` packages, `config/`, `tests/`, and a
  `.gitignore` that excludes the venv, secrets and generated answers.
- **Unit 1 — Project metadata**: `pyproject.toml` with the full dependency set (minimum bounds; exact
  versions come from a hash-pinned `requirements.lock`), OS environment markers (`pywin32`, `pyudev`),
  optional extras `cups`, `pdf-html`, `dev`; `vaishnavi` console script; `ruff` / `mypy` / `pytest`
  configuration; **import-linter contracts** that encode the layer rules of `docs/03 §2`, the
  stdlib-only core tier (ADR-040), and the "drivers / LLM clients / NiceGUI only in their adapters"
  rules; `sys.platform` banned outside `bootstrap/` and `vaishnavi/platform/`; `py.typed`;
  `tests/core/test_project_metadata.py` (stdlib-only) guarding these rules.
- **Unit 2 — Installer runner**: `main.py` (old-Python-safe version guard with plain instructions,
  `E-SYS-001`, then hands over to `bootstrap.cli.main`); `bootstrap/cli.py` (all documented flags →
  frozen `Options`, cross-validation, `Mode` registry, orchestration with exit codes 0 OK · 1 WARN ·
  2 FAIL · 3 cancelled · 4 internal error — never a traceback on screen); `bootstrap/stages.py`
  (`Stage` protocol, `StageResult`, `StageRunner` with `STAGE_START/OK/WARN/SKIP/FAIL` logging and
  elapsed ms, `StateStore` for resumable installs in `logs/install/state.json`, `build_default_pipeline`);
  `bootstrap/console.py` (✔ ! ✖ – ⟳ with ASCII fallback for legacy Windows consoles);
  `bootstrap/i18n.py` + `messages/{en,hi}.toml`; `bootstrap/logsetup.py` (DEBUG install log from the
  first millisecond, run id on every line, secret redaction). Error codes `E-SYS-050/090/099`.
  41 stdlib-only tests.
- **Unit 3 — Mini log console**: `bootstrap/minilog.py` — stdlib `ThreadingHTTPServer` on `:8081`
  (fallback 8082–8085; on Windows address reuse is disabled so a busy port is detected) serving the
  install logs to any browser on the shop Wi-Fi: `/` auto-refreshing HTML tail with level colouring
  and `?level=` filter (tracebacks stay with their parent line), `/raw`, `/files`, `/state`,
  `/healthz`; only `*.log` and `state.json` inside the install-log folder are reachable. First stage
  of the pipeline (`log_console`), `--logs` mode, hand-over helper `stop_log_console`, `E-SYS-007`.
  `StageContext.runtime` added for live objects shared between stages. 10 tests.
- **Unit 4 — Interactive wizard**: `bootstrap/wizard.py` — `Question` (choice / text / path / secret /
  yes-no, recommended default, validator, skippable), `Wizard.ask` / `ask_all` (numbered options,
  Enter = recommended, `b`ack re-asks for real, `s`kip, `q`uit, EOF = quit, Hindi/English yes-no
  forms, re-ask with a plain-language reason on bad input), `AnswerStore` writing
  `config/install-answers.toml` atomically after every answer (secrets never persisted; replay with
  `--answers`), precedence *command-line pre-fill → saved answer → `--yes` recommended → ask*,
  no-terminal detection (`E-SYS-009`) and no-default-for-`--yes` (`E-SYS-008`); `LanguageStage` as
  pipeline stage 1 (skipped when `--lang`/`$VAISHNAVI_LANG` given). `stages.ControlFlow` base class so
  owner-quit / no-terminal signals pass through the runner to `cli.main` instead of counting as
  installer bugs. `Console.ask`, `Options.lang_explicit`. 22 tests.
- **Unit 5 — Hardware survey**: `bootstrap/survey.py` — `Survey` dataclass (OS family/name/version,
  arch, Python, CPU, RAM, GPUs with vendor/VRAM, Apple silicon, disk free, printers, serial ports,
  USB HID devices, internet, package managers, Ollama present, probe errors) collected by best-effort
  probes with timeouts (`ctypes` GlobalMemoryStatusEx / `/proc/meminfo` / `sysctl`, `nvidia-smi`,
  PowerShell CIM, `lspci`, `lpstat`, registry SERIALCOMM, `/dev/tty*`, `system_profiler`); results
  to `logs/install/survey.json`; `recommend_ai()` implements the docs/06 §2 table (provider, local
  tier 1.5b/3b/7b, reason); owner-facing summary line in EN/HI; `SurveyStage` cached per ISO week.
  Probe failures are logged (`E-SYS-010`) but never shown as a warning to the owner. 20 tests, plus a
  guard test that message files contain no console-encoding damage (which caught two mangled lines).
- **Unit 6 — Python environment**: `bootstrap/python_env.py` — `PythonCheckStage` (3.11–3.13
  window; missing `ensurepip`/`venv` → `E-SYS-001` with the Ubuntu apt line; newer interpreter →
  WARN `E-SYS-011`), `VenvStage` (`.venv` beside `main.py`, reused when built by the same
  interpreter, rebuilt otherwise), `PackagesStage` (hash-pinned `requirements.lock` →
  `pip install --require-hashes` + editable app without deps; no lock → WARN `E-SYS-041`;
  `--offline <bundle>` → `--no-index --find-links bundle/wheels`; pip output streamed line by line
  into the install log with a counting progress bar on screen; network errors in pip output select
  the proxy/firewall hint; post-install import verification inside the venv; skipped on later runs
  while lock/pyproject/interpreter are unchanged). `StateStore.save` retries `os.replace` on
  Windows permission races. i18n parity test now requires identical EN/HI key sets. 16 tests.
- **Unit 7 — Elevation + system components**: `bootstrap/elevate.py` — `run_elevated(cmd)` with
  one mechanism per OS (Windows UAC through `ShellExecuteExW("runas")`, output captured via a temp
  log; `sudo -S` with the password written to stdin only and three attempts; `pkexec` when there is
  no terminal; direct run when already admin), outcomes `OK / FAILED / DECLINED / UNAVAILABLE`,
  `ELEVATED_CMD`/`ELEVATED_EXIT` logging, never raises, never logs or stores the password.
  `bootstrap/pkg_manager.py` — component catalogue per OS (Linux: libusb-1.0-0, cups, Noto/Lohit
  Devanagari; macOS: libusb via brew, CUPS/fonts verified only; Windows: Nirmala UI verified only,
  nothing installed), package-manager detection (apt / brew / winget), `SystemComponentsStage`:
  detect → show the exact command and why admin rights are needed → ask yes/no → install with one
  elevation per command → re-detect; declining or failure is WARN (`E-SYS-004` / `E-SYS-003`) with
  the concrete consequence (USB printer / system printers / Hindi on PDFs). 20 tests.
- **Unit 8 — AI provider stage**: `bootstrap/ai_provider.py` — the four-option question
  (local Ollama · OpenAI · Hugging Face · none) with the survey's recommendation pre-selected
  (downgraded to *none* on an offline PC without Ollama); cloud path: plain-language data + cost
  notice and "never sent" line → key from `VAISHNAVI_*_API_KEY` env var or `config/secrets.env`
  (one-time import, `SECRET_IMPORTED`, offer to delete the file) or masked paste with a shape check
  → **live test** (one 5-token chat completion + one embedding, stdlib `urllib`; HF embeddings via the
  feature-extraction endpoint) → failures classified *auth / billing / network / model* with a
  retry / other-provider / no-AI menu → verified key kept **in memory only**
  (`ctx.runtime["pending_secrets"]`) for the database stage to encrypt; `[ai]`/`[ai.<provider>]`
  written to `config/settings.toml` without touching other sections. `bootstrap/ollama_install.py` —
  locate / install (winget · brew · official script, elevated) / start detached / wait for the API /
  list models / streaming pull with progress; model table per hardware tier. Error codes
  `E-AI-001/002/004`. 31 tests.
- **Unit 9 — Locations + handover + autostart**: `bootstrap/locations.py` — primary / secondary
  question with sensible defaults (another drive suggested when visible), validation (absolute,
  writable probe file, ≥ 2 / 5 GB free, not nested, same-device detection via volume serial on
  Windows / `st_dev` elsewhere), same-disk backups accepted only after an explicit warning
  (auto-accepted with WARN `E-SYS-012` under `--yes`), `config/locations.toml`; on later starts the
  file is reused and re-verified — primary unreachable → FAIL `E-SYS-013`, backup drive unplugged →
  WARN `E-SYS-014` (backups queue). `bootstrap/handover.py` — `run_app()` runs
  `.venv/python -m vaishnavi <cmd>` streaming output into the install log, **secrets passed as JSON on
  stdin** (never argv/env), `app_available()`. `bootstrap/autostart.py` — user-level start-at-logon
  per OS (Task Scheduler `ONLOGON` / LaunchAgent with `KeepAlive` / systemd `--user` with
  `Restart=on-failure` + linger), `AutostartStage` (asked once, default yes, WARN `E-SYS-005` on
  failure) and the `--uninstall-autostart` mode. Wizard fixes found on the way: `--yes` defaults are
  now validated (creating the folders), re-asks use `force=True` so a rejected saved answer is not
  replayed; `cli` loads mode modules lazily to avoid an import cycle. End-to-end test fully isolated
  (fake pip / home / Task Scheduler) after it briefly created two empty folders in the real home.
  19 tests.
- **Unit 10 — Wrappers + offline + TLS**: `Start Vaishnavi.bat` / `Start Vaishnavi.command` /
  `start-vaishnavi.sh` (prefer the venv interpreter, fall back to `py -3` / `python3`, plain-language
  "install Python" instructions per OS with `E-SYS-001`, UTF-8 console, window stays open on error);
  `bootstrap/offline.py` — bundle layout `offline/{bundle.json, wheels/<os>-<arch>-py<ver>/, ollama/<os>/,
  models/}`, `validate_bundle()` and an early `OfflineStage` that fails fast with "built for …"
  (`E-SYS-015`) before pip is ever tried, `--make-offline-bundle <os>` mode (pip download for the
  lock with platform tags, Ollama installer download, metadata); `python_env` consumes the validated
  wheels folder; `bootstrap/tls.py` — self-signed LAN certificate via `openssl` when present, else a
  clean "unavailable" for the app to handle later. 12 tests. **Block A (installer spine) complete:
  194 stdlib-only tests.**
- **Unit 11 — Money** (`src/vaishnavi/core/money.py`, stdlib only, ADR-003): all amounts are
  integer paise; `Decimal` only at the edges with **half-up** rounding. `to_paise` / `from_paise` /
  `parse_inr` (accepts `₹ 1,250`, `Rs. 10`) / `format_inr` (Indian lakh/crore grouping), `multiply`
  (fractional quantities for weighed/measured items), `apply_percent`, `TaxSplit` with
  `split_inclusive_tax` (shelf price includes GST — `net + tax == gross` exactly for every rate) and
  `add_exclusive_tax`, `halves()` for CGST/SGST (odd paisa to CGST), `round_off` to whole rupees with
  the ± line, `allocate` (largest-remainder split that never loses a paisa). Docstring examples run
  as part of the suite. `tests/core/__init__.py` now puts `src/` on `sys.path` so the core tier
  imports `vaishnavi` from the source tree with bare Python. 26 tests.
- **Unit 12 — Identifiers** (`src/vaishnavi/core/ids.py`, stdlib only, ADR-023): own 26-char
  Crockford **ULID** (time-ordered, monotonic within a millisecond, thread-safe) for correlation and
  client-transaction ids; GS1 modulo-10 check digit and `is_valid_gs1` for EAN-8 / UPC-A / EAN-13 /
  GTIN-14 manufacturer codes; the shop's **internal EAN-13** `29` + 10-digit product id + check digit
  with `internal_barcode` / `is_internal_barcode` / `parse_internal_barcode` (regenerable from the ID
  alone when a label is damaged); `normalise_scan` for keyboard-wedge quirks (CR/LF, `~` prefix,
  UPC-A presented as EAN-13); Indian financial-year label and gap-free invoice number
  `VPS/25-26/000123` format/parse. 18 tests + doctests.
- **Unit 13 — Clock · events · permissions** (stdlib only): `core/clock.py` — `Clock` protocol,
  `SystemClock`, `FrozenClock` (advance/set for tests), IST as a fixed offset (no tzdata needed on
  Windows), UTC-in-storage / IST-on-screen helpers, `today_ist` (a sale at 00:30 belongs to the shop
  day), `business_day_bounds`; `core/events.py` — synchronous `EventBus` with subclass matching and
  **isolated** subscribers (a failing dashboard handler is logged, never breaks a sale), frozen
  domain events (`ProductChanged`, `StockChanged`, `SaleCommitted`, `ShiftClosed`,
  `WorkSessionEvent`, `HardwareStatusChanged`, `Alert`); `core/permissions.py` — `Role` (OWNER /
  STAFF), `Permission` (27 capabilities), `Grant` (ALLOW / PIN / POLICY / DENY), the matrix of
  docs/05 §5 as a table checked by tests, `OpenItemPolicy` resolution, `Actor` with
  `pin_verified`, `check()` / `require()` raising `PinRequired` (`E-AUTH-011`) or
  `PermissionDenied` (`E-AUTH-010`). 14 tests.
- **Unit 14 — Errors + messages** (stdlib only): `core/errors.py` — `VaishnaviError` base carrying
  `code` (`E-<AREA>-<nnn>`, validated against the subclass's area at construction), `params`,
  technical `detail`, `cause`, and `severity`; one subclass per area (CFG, DB, AUTH, CAT, INV, BILL,
  SHIFT, LEDGER, PRN, SCN, SCALE, PUB, FCST, AI, GRD, SEC, BKP, SYS, MONEY, LOGAPI, ALERT);
  `str(err)` is the log form, `user_message(lang)` / `what_to_do(lang)` the owner form,
  `as_problem()` the JSON form (technical detail only when asked); `error_for(code)` and
  `describe(code)` for the doctor / log console. `core/messages/{en,hi}.toml` — **88 codes**, each
  with *what it means* and *what to do* in both languages. Guard tests: identical EN/HI code sets,
  every `E-…` code referenced anywhere in the docs, installer or app has a text, no console-encoding
  damage. Constructor parameter is `error_code` so message placeholders named `{code}` (barcodes)
  cannot clash. 15 tests.
- **Unit 15 — Logging** (stdlib only, docs/08): `core/logging.py` — `setup_logging(logs_dir)`
  builds the documented families (`app/vaishnavi.log` DEBUG 30 d + `vaishnavi.jsonl`,
  `errors/errors.log` WARNING+ 90 d, `audit/audit.jsonl` 365 d, `ai/ai.log` and
  `hardware/hardware.log` 14 d, `access/http.log` 7 d) with midnight rotation behind a
  `QueueHandler`/`QueueListener` so disk writes never delay a sale; `ContextVar`-based
  correlation id / user / role / shift stamped on every record by `ContextFilter` (runs on the
  caller's thread), `with_correlation()` scope, `set_user_context()`; `KeyValueFormatter` (IST text
  line exactly as in docs/08 §3) and `JsonFormatter`; redaction of credential-like text including
  `pin` / `recovery_key` and of sensitive `extra` keys; `config/logging.toml` template with console
  level, per-logger levels and retention; `LOGGING_READY` event. 14 tests (routing per family,
  redaction, per-thread context, TOML loading).
- **Unit 16 — Settings** (stdlib only — deliberate deviation from "pydantic Settings", recorded in
  ADR-009): `core/settings.py` — nine typed dataclass sections (`shop`, `tax`, `receipt`,
  `billing`, `server`, `ai` with `ollama` / `openai` / `huggingface` / `cloud` budget, `backup`,
  `hardware`, `locations`), precedence *defaults → settings.toml → locations.toml → environment
  (`VAISHNAVI_SECTION__KEY`) → database overrides (`with_overrides()` returns a validated copy)*;
  `Literal` choices and type coercion, unknown keys ignored (an older app can read a newer file),
  cross-field validation that names the dotted key in `E-CFG-001` (ports, discount cap, GSTIN when
  registered, nightly time, heartbeat minimum, cloud model presence); any `api_key` / `token` /
  `password` inside a TOML section refuses start-up with `E-SEC-006`. `config/settings.toml` template
  with owner-facing comments for every key; `core.settings` added to the stdlib-only import-linter
  contract. 12 tests.
- **Unit 17 — Security + secrets + paths**: `core/security.py` — password / Owner-PIN hashing with
  stdlib **`hashlib.scrypt`** in a self-describing `$scrypt$…` string (constant-time verify,
  `needs_rehash`, `$argon2…` accepted when that package exists — recorded in ADR-011), plain
  password/PIN acceptance rules, `LockoutPolicy` (3 failures → 15 min, clock-driven, per subject),
  recovery-key generation (12 words, ≥ 2048-word list) and normalisation, `SecretBox` protocol with
  `FernetBox` (lazy `cryptography`) and `NullBox`, `masked_preview`. `platform/secrets.py` — master
  key in the OS keyring (lazy `keyring`) with a 0600 hidden file fallback (`E-SEC-001`) that is
  migrated into the keyring automatically when one becomes available; `forget_master_key`.
  `platform/paths.py` — `AppPaths` layout under primary/secondary (data, logs, receipts/YYYY/MM,
  images, quarantine, exports, outbox, tmp, .keys, snapshots, backup-queue), `ensure()` tolerant of
  an unplugged backup drive, `program_dir()` / `config_dir()` discovery. `argon2-cffi` removed from
  the dependencies and the installer's verify list. 21 tests (Fernet round-trip skips when
  `cryptography` is absent — it runs on the shop PC). **Block B complete: 314 stdlib-only tests.**
- **Unit 18 — Engine + schema**: `db/schema.sql` — the physical schema as plain SQL (51 tables with
  CHECK constraints and indexes, money as INTEGER paise, times as ISO-8601 UTC text) plus **20
  protection triggers**: `product_stock` writable only by the stock-movement trigger (guard-table
  technique → `E-INV-012`), every movement's `qty_after` must equal previous + delta (`E-INV-013`),
  SALE / SALE_VOID / RETURN must reference an existing sale (`E-INV-011`), a non-OWNER user may create
  only SALE movements (`E-INV-010`), the system open-item product never moves stock, append-only
  `sale` (only `reprint_count` / `receipt_printed` / `note` editable), `sale_item`, `payment`,
  `audit_log`, `price_history`, `stock_movement` (`E-LEDGER-001`), one open shift per user, one
  active prompt version, automatic `row_version` bump. `db/schema.py` — stdlib loader that splits
  statements correctly around trigger bodies (shared by Alembic, tests and the doctor).
  `db/engine.py` — SQLAlchemy engine with the ADR-031 pragmas, `BEGIN IMMEDIATE` for writer
  connections, single-writer lock file (`E-DB-030`). `db/session.py` — `UnitOfWork` (commit /
  rollback / duration log) and `ReadOnlySession` that raises on any flush so AI tools and reports
  cannot write. 20 sqlite3 tests exercise every trigger on this PC; the ORM layer is verified by
  `selftest` on the shop PC.
- **Unit 19 — Initial migration + models**: `db/alembic/` (`alembic.ini`, `env.py` using the
  application engine and the Base metadata, `script.py.mako`) with revision `0001_initial` that
  replays `schema.sql` statement-by-statement — Alembic and plain sqlite3 therefore create byte-identical
  objects, and the doctor can compare them. `db/migrate.py`: `upgrade_to_head` first upgrades a
  `copy_database` snapshot under `tmp/` (the live file is touched only if the rehearsal succeeds),
  `verify_schema_objects` (missing tables vs. `schema.sql`), `head_revision` / `current_revision`.
  `db/models/{base,user,catalog,inventory,sales,shift,audit,ai,system}.py` — SQLAlchemy 2.0
  `Mapped[...]` classes generated from the SQL (INTEGER→int, TEXT→str, NUMERIC→Decimal, BLOB→bytes;
  DB defaults remain server-side so triggers and ORM agree). A stdlib AST cross-check test proves every
  table, column and nullability matches between the models and `schema.sql` in both directions; 5 tests.
  Fixed: sqlite3 connections are now closed explicitly (Windows kept the copied database locked).
- **Unit 20 — Seed + first services**: `db/seed.py` — plain-SQL, idempotent `apply_seed()` on any
  DB-API connection: roles, the Owner account with the `!SETUP` password **sentinel** (login refuses it
  with `E-AUTH-005` until Owner setup runs on the shop PC — no default password to guess), ten puja
  categories (EN/HI), the `OPEN-ITEM` system product, `catalog.units` / `tax.presets` settings
  (presets only — no tax rate is assumed), counters, the eleven onboarding steps, `prompt_version` 1,
  `db_identity`; returns a `SeedReport`. `db/records.py` — stdlib record types shared by repositories
  and services. **Services are stdlib and depend on store Protocols** (SQLAlchemy stays in
  `db/repositories/`): `AuditService` (hash-chained rows, canonical JSON, secret scrubbing,
  `verify_chain()`, JSONL mirror), `SettingsService` (overrides validated through `Settings` *before*
  storing, `E-CFG-002` for unknown names, `E-SEC-006` for secret names, data keys, cache +
  `SettingsChanged` event, invalid stored override → logged and ignored, never a crash),
  `SecureSettingsService` (allow-listed keys `E-SEC-010`, Fernet via `SecretBox`, masked previews,
  Owner-only `reveal`, `import_pending()` for the installer handover, `E-SEC-011` when the key changed),
  `UserService` (persisted 5/15 lockout `E-AUTH-003`, identical error + timing for unknown user and wrong
  password `E-AUTH-001`, `complete_owner_setup`, rehash-on-login, Owner PIN overrides with 3/15 lockout
  and dual-id audit, account rules incl. last-Owner protection `E-AUTH-006`). ORM repositories `UserRepo`,
  `SettingRepo`, `AuditRepo`, `SystemRepo`. Also: `clock.iso_utc()/parse_iso_utc()`,
  `settings.known_keys()/get_dotted()`, `vaishnavi.__version__` (pyproject now `dynamic`),
  `masked_preview(keep=0)` fix, error codes `E-AUTH-001/003–009/012`, `E-CFG-002`, `E-SEC-010/011`
  in EN and HI. 27 stdlib tests (seed on sqlite3; services against in-memory fakes).

### Fixed — full code review after unit 20 (owner asked for a thorough re-check against expectations)
- **Open-item manual add defaulted to `PIN` in code while the requirement (FR-D2a) says `ALLOW`** —
  staff can now add a no-barcode line without calling the owner; the controls are the per-bill cap,
  the `*` mark on the receipt, the per-cashier report and the review queue (`settings.toml`,
  `BillingSettings`, `permissions` defaults aligned).
- Installer **"Ready to open the shop."** was printed although no database/doctor/app-start stage
  exists yet → now "Setup steps complete." (the readiness line moves to the start stage, unit 26).
- Installer **restarts re-asked every question** because the local answers file was only loaded with
  `--answers`; unattended autostart therefore failed with `E-SYS-009`. The PC's own answers are now
  always loaded; replayed/prefilled answers are returned **normalised** (`"2"` → option value,
  `"y"` → `"yes"`, `~` expanded).
- **Packages stage**: a rebuilt `.venv` at the same path kept the old "installed" cache; verification
  used `find_spec` (discoverable ≠ importable) and was skipped on cached starts. Now: the runner never
  skips the stage, every start **really imports** each required package inside the venv, pip runs
  only when lock/pyproject/venv identity changed or an import fails.
- **Hung-child timeouts**: `run_streaming` (pip) and `handover.run_app` blocked in `readline()`; the
  deadline only applied after EOF. Output is now drained by a thread and `wait(timeout)` enforces it.
- **Imported API key could be lost**: `config/secrets.env` was deleted while the key existed only in
  memory. Deletion is now deferred to `delete_secrets_file_if_pending()` after encrypted storage commits.
- **Missing data/backup folders were silently re-created** on the internal disk when a drive was
  unplugged (`ensure_writable` on reuse). Recorded locations are now verified with `verify_existing`
  (must already exist): primary gone → `E-SYS-013` FAIL, secondary gone → `E-SYS-014` WARN; the
  same-device flag is recomputed instead of trusted.
- Runner: a cache hit overwrote the `OK` record with `SKIP`, so a cached stage ran on run 1, skipped on
  run 2 and **ran again on run 3**; cached **warnings vanished** from the exit code and warning codes
  were not shown. Fixed; state is now **checkpointed after every stage**.
- **Tracebacks bypassed secret redaction** (installer and app loggers) — the whole formatted record,
  including `exc_info`, is redacted now.
- Windows UAC helper read `%ERRORLEVEL%` before the command ran (always 0) → delayed expansion
  `!ERRORLEVEL!` (verified: exit 7 is reported as 7); Win32 handle argtypes declared for 64-bit.
- Linux `libusb` detection globbed the wrong path component (multiarch installs reported missing).
- Autostart: systemd `ExecStart` now quotes arguments (paths with spaces), `SuccessExitStatus=1 3`
  so a WARN exit is not a "crash"; launchd `KeepAlive` removed (would relaunch on WARN); enable /
  bootstrap failures are reported as `E-SYS-005` WARN instead of success; unregister reports failures.
- `main.py` is pure ASCII so a Python 2 interpreter reaches the version message.
- `cli.main` now has an outer boundary: failures *before* logging/messages exist print a plain ASCII
  line (`E-SYS-099`) instead of a traceback.
- Wizard `b` on a stand-alone question (how stages ask) no longer **cancels the whole install**; it
  re-asks with a hint. New **single-installer lock** (`logs/install/.lock`, `E-SYS-016`) prevents a
  double-click during autostart from rebuilding `.venv` under the running installer.
- e2e CLI test could run real `apt-get` on a Linux CI box — system-component detection/installation is
  now faked and asserted never to run.
- App: `UserService` re-locked after a single wrong password once a lock expired (stale counter);
  `make_engine` used `SingletonThreadPool` (leaks a connection per dead web-server thread) → QueuePool;
  `WriterLock` truncates the pid file; migration `0001` executes DDL via `exec_driver_sql` (no `:name`
  bind parsing on `strftime` formats).
- 14 new/updated tests cover the above (380 total).
- **Unit 21 — Ledger** (tests first, docs/13 rule 7): `services/ledger.py` — `canonical_sale()` exactly
  as docs/05 §4 (sorted keys, compact, quantities normalised so `2.00 == 2`, no database ids),
  `sale_hash()`, `verify_sales_chain()`, `LedgerService` with `chain_head()`, `hash_for_new_sale()`
  (checkout links onto the head inside `BEGIN IMMEDIATE`), `verify_sales()` (hash **and** `prev_hash`
  link → `E-LEDGER-001` with sale id / expected / found, so an edited *and* a deleted row are caught),
  `verify_gaps()` (COUNT = MAX per financial year, every missing number listed → `E-LEDGER-002`),
  `reconcile_stock()` (Σ movements = cached qty; the day's completed sale lines = SALE movements; open
  items excluded → `E-LEDGER-003`), `verify_audit()`, `run_all()` → `IntegrityStatus`, writes
  `integrity_check` rows, logs `INTEGRITY_OK/FAIL`. `db/ledger_store.py` — stdlib sqlite3 store so the
  doctor and restore wizard verify a database *file* without the ORM. `db/records.py` gains
  `SaleRecord`/`SaleItemRecord`/`PaymentRecord`. docs/05 §4 corrected (`tax_rate_bp`, link check,
  normalisation). 19 tests on the real schema.
- **Unit 22 — Guardian + backup** (tests first): `db/guardian.py` — start-up triage of the live file:
  missing (`E-DB-002`), damaged → quarantined (`E-DB-003`), new, foreign instance vs the fingerprint
  written at the last clean close (`E-DB-021`), restored copy recognised by the `backup_id` only
  snapshots carry (`E-DB-020`; `restored_at/restored_from` stamped, banner gets the backup time),
  unclean close (`E-DB-022`, counted), normal; `mark_open()` / `mark_clean_close()` (WAL checkpoint
  `TRUNCATE` + `state/last_db_fingerprint.json`), `vacuum()`. `services/backup.py` — snapshots with
  the SQLite **online backup API** (consistent while billing continues; uncommitted work never copied),
  `backup_id` stamped **in the copy only**, `integrity_check` (failure discards, `E-BKP-003`), sidecar
  manifest (schema/app version, instance id, last invoice, chain head, sha256, size, row counts),
  secondary reachability with the **unmounted-mount-point trap** closed (same volume as the data while
  the owner chose a separate drive = not mounted → queue), local queue `E-BKP-001` + `flush_queue()`,
  3-day local copies, `backup_log` rows, `BackupDone`; retention `prune()` (daily / one-per-ISO-week /
  one-per-month / `pre-*` 90 days, tiers independent), `verify_snapshot()` (sha256 vs manifest +
  integrity + full ledger pass), weekly `restore_drill()` (`E-BKP-010`), `restore()` (verify →
  `pre-restore` snapshot → copy via backup API → injected migrate → chain check → atomic swap →
  `RestoreDone`; damaged or foreign snapshots refused, live file untouched), `due_on_start()` (> 20 h).
  New events `BackupDone`, `RestoreDone`, `DbRestoredDetected`; `AppPaths.state/fingerprint/local_backups`;
  `E-DB-002` (EN+HI). 25 tests on real SQLite files.
- **Unit 23 — Recovery key** (tests first): `core/wordlist.txt` — the BIP-39 English list (2048 words,
  unique 4-letter prefixes so typos are obvious; BSD-2-Clause). `services/recovery.py` — `issue()`
  (12 words, stored only as a scrypt hash, previous key superseded), printable `recovery_sheet()`,
  `acknowledge()` + `needs_attention()` (doctor warns until the Owner ticks "written down"),
  `verify()` tolerant of case/spacing (`E-AUTH-040`; 5 wrong → 15 min `E-AUTH-042`),
  `reset_owner_password()` — elevation required (`E-AUTH-041`), then key, then password rule (a weak
  password never consumes the key), sets + unlocks the Owner, retires the key, **issues a new one**;
  audited `OWNER_PASSWORD_RESET`. `RecoveryRepo` (ORM). `bootstrap/recovery.py` — the
  `--reset-owner-password` installer mode: key asked with a shape check and **never logged** (new
  `Question.sensitive` flag), password twice, **administrator rights proven** by running a harmless
  elevated command (declined → `E-SYS-004`, nothing changed), app presence check, hand-over through
  stdin JSON; the new recovery sheet comes back as `SECRET|` lines that `handover.run_app` shows but
  keeps out of every log. Installer messages EN+HI. 15 tests.
- **Unit 24 — Doctor + reporters** (tests first): `vaishnavi/bootstrap/doctor.py` — `CheckResult`
  (status, one plain line, hint, technical detail, code, data, elapsed ms), `run_doctor()` (every check
  runs; a crashing check is `E-SYS-099` with the traceback in the log; `--fix` lets a check repair a
  WARN/FAIL with safe actions only), exit codes 0/1/2, `render_text()` in English or Hindi with a ULID
  reference, codes and "what to do" hints, `render_json()` (the API object). Ten checks in the
  docs/07 order: python_env, packages (real imports), filesystem (missing primary `E-SYS-013` — never
  re-created; low disk), config (`E-CFG-001`, `E-SEC-006`), database (guardian triage → `E-DB-002/003/
  010/020/021/022`, Owner setup pending `E-AUTH-005`; fix = create + seed + stamp Alembic head + WAL),
  ledger (`E-LEDGER-001/002` fail, `E-LEDGER-003` warn), backup (never / stale / drive missing
  `E-BKP-001`; fix = snapshot now), secrets (`E-SEC-001/005`), network (port in use `E-SYS-017`, clock
  behind the last recorded event `E-SYS-030`), autostart (`E-SYS-005`). Support: `core/i18n.py`
  (`Texts` for non-error texts, `[doctor]` tables EN+HI), `db/schema.py::HEAD_REVISION/stamp_head/
  current_revision` (stdlib view of `alembic_version`), `platform/autostart.py` probe, the
  `python -m vaishnavi` dispatcher (`__main__.py` + `commands/` registry — registry kept out of
  `__main__` to avoid the duplicate-module trap) with `doctor [--json] [--fix] [--checks] [--lang]`
  runnable on a bare interpreter. **Fixed**: the seed pre-set `db_identity.last_open_at`, so a freshly
  created database was reported as an unclean close at its first real start. 20 tests.
- **Unit 25 — Platform adapters + hardware/AI checks** (tests first): `platform/devices.py` (USB
  devices by VID:PID on Windows/Linux/macOS via stdlib, `find_device()`; failures → empty list, never
  a crash), `printing.py` (OS printer lists, network `host:9100` probe, `printer_status()` for
  none/usb/network/system), `serial_ports.py`, `fonts.py` (Devanagari font lookup), `browser.py`
  (Chromium app mode with plain-browser fallback), `elevation.py`. `bootstrap/ollama_manager.py` —
  find binary, `is_running`, detached `start` that waits for the port, `list/has/missing_models`,
  streamed `pull` with progress, `ensure_models`, `warm_up`, `embed_probe`; every call has a timeout.
  `ai/providers/base.py` — the `LLMProvider` protocol every backend implements (chat / stream / embed /
  health, tool specs); `null_provider` (`E-AI-010`), `fake_provider` (scripted answers, deterministic
  vectors). Doctor checks: **system_components** (`E-SYS-003`, says what each missing component
  means), **code_integrity** (`MANIFEST.sha256.json` writer + verifier; changed/added/removed files →
  `E-SYS-040` FAIL; no manifest = development copy, WARN), **hardware** (no scanner paired / paired
  scanner absent → `E-SCN-001` WARN with `billing_paused_for_staff` for strict mode; printer never a
  blocker, `E-PRN-001` WARN with "receipts will queue"; scale `E-SCALE-001` only when configured),
  **ai_provider** (none → "assistant off"; Ollama binary/service/models → `E-AI-002/001/004`, `--fix`
  starts the service, pulls missing models and warms up; cloud → internet `E-AI-020`, key stored
  `E-AI-011`, budget `E-AI-030`). Texts EN+HI. 26 tests (fake Ollama HTTP server, recorded OS outputs).
- **Unit 26 — App spine** (tests first; 22 tests, three of them run the real `python -m vaishnavi`
  commands as subprocesses). App side: `db/sqlite_stores.py` (stdlib stores behind every service
  protocol — the whole start-up path runs without the ORM wheel), `db/lock.py` (`WriterLock` moved out
  of the SQLAlchemy module; `db/engine.py` re-exports it), `app_services.py` (services built once per
  process; `FernetBox` when `cryptography` and the master key are available, otherwise `NullBox` which
  **refuses** to store secrets — nothing is ever written in clear), `services/notifications.py`
  (severity-checked alerts, open-alert dedupe, per-user read marks, subscriptions to `BackupDone`,
  `StockChanged(low_stock)`, `DbRestoredDetected`), `scheduler/simple.py` (stdlib interval / daily-at-IST
  scheduler, failure isolation, status for the doctor) + `scheduler/jobs.py` (docs/07 §7 job table as
  data, bound to callables; unbound jobs skipped, not errors), `api/system.py` (`healthz` — heartbeat
  older than 60 s → 503; `status` chips; `logs_tail` with level/family filter that cannot leave the
  logs folder; `metrics` JSON + Prometheus text; `doctor`; FastAPI `mount()`),
  `bootstrap/startup.run_app` (`APP_START` → data-root guard `E-SYS-013` → `WriterLock` `E-DB-030` →
  create + seed when missing → `DB_TRIAGE` (`E-DB-003` refuses, file quarantined) → schema check /
  Alembic upgrade → `mark_open` → `app_run` row → `DOCTOR_LIGHT` (status chips; DB or config FAIL
  refuses) → backup-on-start → `SCHEDULER_STARTED` → `SERVER_READY` + `state/heartbeat.json` →
  `APP_CLEAN_CLOSE` with fingerprint and `clean_stop = 1`). Commands (`commands/`): `run`, `init-db
  --json` (stdin `{"secrets": {…}}` hand-over; exit 2 + `E-SEC-001` when a secret could not be stored
  encrypted so the installer keeps its copy), `reset-owner-password` (hand-over on a pipe = the
  installer's elevation proof; prints `RESET_OK` and the new sheet as `SECRET|` lines), `verify-chain
  --json` (exit 2 + the first failing code), `backup --json --trigger … | --list | --flush`. Command
  file logging never creates the data root (console-only with `E-SYS-013` when it is missing).
  Installer side: `bootstrap/app_stages.py` — `DatabaseStage` (runs `init-db`, deletes `secrets.env`
  and the in-memory copy **only after** the app confirms every key was stored), `DoctorStage` (exit
  0/1/2 → OK/WARN/FAIL, report echoed in the owner's language), `StartStage` (launches `run` detached,
  polls `/healthz`, prints **"Ready to open the shop: http://127.0.0.1:8080"**, adopts an instance that
  is already running) and `Watchdog` (dead or hung app → restart with `E-SYS-020`; more than N restarts
  per hour → `WATCHDOG_GAVE_UP` and a plain-language failure). Pipeline now ends `autostart → database
  → doctor → start` (`--install-only` stops after the doctor). Installer messages EN+HI for the three
  stages. Import-linter layers refined to `commands → bootstrap → ui | api | scheduler → …`; ADR-013
  amended (stdlib scheduler, APScheduler optional). Installer e2e test extended to the new stages.
- **Unit 27 — UI shell** (tests first; 20 tests on bare Python — NiceGUI is only reached through
  lazily-imported `render_*` functions and `ui/server.py`). `ui/theme.py` — the approved mock-up's
  palette, fonts and shell CSS as constants, `brand_colors()` for Quasar, level/severity colours,
  `format_clock()` in shop time with Hindi weekdays. `ui/i18n/{en,hi}.json` + `UiTexts` (Hindi overlays
  English, a missing label renders as its key and is logged once, `hinglish` → English labels; a test
  keeps both files in step). `ui/state.py` — `ClientUser` / `ClientState`: sign-in/out, activity
  `touch()`, idle lock (`should_lock`, `lock/unlock`, `lock_timed_out` → session ends), `actor()` for
  service calls, the *unclean exit* banner flag, tolerant JSON round-trip for NiceGUI's per-browser
  storage (garbage → anonymous, never a crash). `ui/guards.py` — the rail (`NAV_ITEMS` with required
  permission; Settings is not rendered for staff), `route_permission()` by longest prefix,
  `decide()` → ALLOW / LOGIN / LOCKED / FORBIDDEN (`E-AUTH-010` logged) / REDIRECT_HOME,
  `redirect_for()` (`/login?next=/pos`), `page()` decorator for NiceGUI. `components/status_chip.py`
  — chips in mock-up order with a plain-language hint per level (red Scanner = "billing paused for
  staff"), grey "not checked yet", `worst_level()`, `chips_from_hardware()`. `components/
  notification_bell.py` — `bell_view()` (Owner: everything; staff: rows addressed to them + counter
  broadcasts), severity colour, relative time EN/HI. `ui/layout.py` — `build_shell()` view-model
  (brand, clock, chips, role-filtered rail, user badge with session-since and shift, language toggle,
  assistant bubble only for signed-in users with AI on, shortcuts, footer) and `health_summary()` for
  the login pane's *morning health check*; thin renderers. `ui/session.py`, `ui/server.py`
  (`serve(state, on_ready)`: mounts `/healthz` + system API, `/` and `/lock` frames, page registrars,
  stable `state/ui.secret` so a restart keeps browsers signed in). Start-up publishes **Scanner** and
  **Printer** chips from the hardware check, shows the assistant grey "off" for provider `none`, and
  injects `AppState.doctor_runner`; `api/system.py` therefore no longer imports `bootstrap/`
  (layers: `commands → bootstrap → ui → api | scheduler → …`).
- **Unit 28 — Login + attendance** (tests first; 15 tests on real SQLite files). `services/work_session.py`
  — the three layers of docs/05 §6a: `start()` opens a session at the **server** time and ends any other
  open session of the same user as `FORCED_BY_LOGIN` at its last heartbeat; `end_work_preview()` derives
  duration / bills / ₹ from `sale` by time window and reports an open shift; `end()` → `LOGOUT` (an open
  shift needs the Owner PIN, `E-AUTH-020`); `heartbeat()` answers *False* once the session is gone so the
  page returns to login; `window_closed()` records the browser beacon as `WINDOW_CLOSED`; the 15-s
  `monitor()` job closes 60 s of silence as `HEARTBEAT_LOST` with `last_seen_at` as the exit time and
  raises the `SESSION_UNCLEAN` notification; idle lock → `record_lock` / `record_unlock(locked_seconds)` /
  `idle_lock_timeout(locked_since)`; `close_all(APP_SHUTDOWN)` on a clean stop and `recover_unclean()` at
  the next start; unclean exits must be confirmed at the next login (`pending_confirmation`,
  `require_confirmed` → `E-AUTH-021`, `confirm`); Owner `force_end` / `annotate`; `who_is_logged_in`,
  `history()` (Owner everyone, staff their own) with derived figures, `unclean_streak()`. Everything is
  audited and published as `WorkSessionEvent`. `WorkSessionRecord`, `SqliteWorkSessionStore`.
  `UserService.login_tiles()` (names and roles only) and `reauthenticate()` — the lock screen re-checks
  the user's own password with the login's lockout counter and **without** creating a login.
  `api/session.py` — heartbeat + closing-beacon handlers and the injected browser script
  (`beforeunload` → `sendBeacon`). `ui/pages/login.py` — the approved mock-up as code: user tiles
  (owners first, *Other* for a typed username), number pad only for staff, **"Sign in & start work →
  HH:MM"**, amber banner *"Ramesh — you left at yesterday 06:42 without ending work"*; `sign_in()` fills
  the browser state (open shift picked up) and decides the next page: `/confirm-exit`, `/onboarding`
  (`E-AUTH-005`), `/change-password`, `/pos` for staff, `/` for the Owner. Shell additions: heartbeat
  script and a 30-s idle check on signed-in pages, `/lock` (own password; lock count and idle seconds
  recorded), `/end-work` popup (exit time, today's summary, open-shift warning with Owner PIN, note),
  `/change-password`; `guards.Decision.CONFIRM_EXIT`. Start-up wires `app_run_id`, recovers forgotten
  sessions after triage, binds the `session_monitor` job and closes all sessions on a clean stop.
- **Unit 29 — System pages** (tests first; 9 tests). `ui/pages/logs.py` — the Owner's log console as a
  view-model: lines read through the sandboxed `logs_tail`, parsed into columns, filtered by level, free
  text and **correlation id**, with the plain-language EN/HI text of every `E-` code; download of the
  current view. `ui/pages/status.py` — health, uptime, chips, database size/revision, scheduler jobs,
  last backup (age, verified, last invoice, queue), a live integrity pass, who is logged in,
  heartbeats. `ui/pages/settings_backups.py` — locations and drive presence, schedule/retention,
  snapshot table, weekly restore-drill status, *Backup now* (audited), the manual-recovery card, and the
  **Restore wizard**: choose → confirm (snapshot time, its last invoice against the live one, warning
  when bills will be missing) → Owner password (`reauthenticate`; wrong → stays, `E-AUTH-001`) →
  `BackupService.restore` with pre-restore snapshot and verification → audited `RESTORE` → restart
  required. `ui/pages/settings_system.py` — version/Python/program folder, **redacted** `settings.toml`,
  code-integrity (`ok` / `dev-copy` / `changed`) and autostart status via the injected doctor runner,
  schema revision vs head, remote log-access URLs (LAN one when enabled), *Run doctor*, **support
  bundle** zip (doctor/status/metrics, redacted config, recent non-personal logs — never the audit or
  access logs), Owner-only audited *shut down* / *restart* requests honoured by the server loop.
  `ui/pages/settings.py` tabs; `UserService.display_names()`.
- **Unit 30 — Selftest + first-run checklist** (tests first; 9 tests). `commands/selftest.py` — `python -m
  vaishnavi selftest [--json] [--pattern] [--no-live]`: runs the **shipped** `tests/core` suite in-process
  (so the shop PC proves the code on its own interpreter), then the full live doctor on that PC; exit 0
  clean · 1 notes · 2 failed; the JSON report is kept in `<primary>/logs/selftest/`. `bootstrap/selftest.py`
  — the installer modes `--selftest` (hands over, streams, 0/1/2), `--doctor [--fix] [--lang]` and
  `--where` (lists program, setup-log, config, data, database, logs, receipts and backups folders from
  `locations.toml`; works before the setup finished; warns when live data and backups share one disk).
  `ui/pages/onboarding.py` — **step 1 of the first-day wizard**, which the first-run checklist showed
  to be missing: Owner password + Owner PIN replace the seeded `!SETUP` sentinel, the 12-word recovery
  key is issued and shown once with a printable sheet and an "I have written it down" tick
  (`validate_owner_setup`, `complete_owner_setup`, `acknowledge_recovery_key`); `/onboarding` is
  reachable without a session only while the setup is pending. docs/17 §5 flag table made honest
  (`--restore` / `--update` command-line forms marked as planned; the in-app Restore wizard and the
  manual copy-back exist) and **§5.1 "First run on the shop PC — the checklist"** added; README status
  rewritten for Phase 1 complete. **Phase 1 code complete: 565 stdlib-only tests.**

### Added — Phase 2 (catalogue, identity & inventory)
- **Unit 31 — Catalogue core** (tests first; 10 tests). `core/barcode.py` — EAN-13 check digit (GS1
  weights 1,3 from the left), `internal_ean13(product_id)` with the `29` in-store prefix, `is_internal`,
  `validate`. **Docs/05's worked example carried a wrong check digit** (ID 1042 is `2900000010420`); fixed
  in the doc and pinned by a test. `services/catalog.py` — `CatalogService`: `create` (SKU `PREFIX-NNNN`,
  internal barcode row, `E-CAT-004` duplicate code), `update` with `row_version`, `add_barcode`
  (`INTERNAL` / `MANUFACTURER` / `PACK`; `E-CAT-020` bad check digit, `E-CAT-010` already on another
  product), `resolve_barcode` (falls back to the derived internal code), `search` by `#ID` / code /
  barcode / EN+HI name / fuzzy, archive & restore, categories, tags, quick picks. `services/price_history.py`
  — every price / MRP / cost / tax change appended with old → new / user / reason; windows 1 m / 6 m /
  1 y / custom and `chart_points`.
- **Unit 32 — Inventory** (tests first; 8 tests). `services/inventory.py` — `opening_stock` (once per
  product), `adjust` with mandatory reason, `damage`, `E-INV-003` BLOCK on negative stock, low-stock
  `StockChanged` notification, `purchase` (supplier, cost → `PURCHASE` price-history row, `label_count`
  for *print N labels*), stock-take `start → count → approve | cancel`, `reconcile` (stock = Σ movements,
  mismatches listed), `valuation`, `stock_daily_balance` job bound in `bootstrap/startup.py`.
- **Unit 33 — Import/export + labels** (tests first; 7 tests). `hardware/labels.py` — `ean13_modules`
  (95-module L/G/R encoding, G = R reversed), `barcode_svg`, `render_escpos` (native `GS k`, 58/80 mm),
  `render_html_sheet` (A4 65-up / 40-up), `render_pdf_sheet` (reportlab, optional). `services/labels.py`
  — `LabelModel` + `LabelService` with the renderer **injected** by AppServices (services never import
  hardware), print log per product. `services/import_export.py` — CSV `preview_csv` (per-row problems,
  nothing written) → `apply` → `export_csv` (UTF-8 BOM so Excel opens Hindi names). Labels derive the
  internal code from the id when the barcode column is empty.
- **Unit 34 — Catalogue UI** (tests first; 8 tests). `ui/pages/products.py` (grid, editor, price-history
  pop-up, CSV import page), `stock.py` (movements timeline, adjust, purchase, stock-take), `labels.py`
  (queue, sheet / strip choice), `settings_hardware.py` (printer discovery list, select + paper width,
  scanner pair / unpair, `test_print_payload` = sample header + sample label). Page models are pure
  functions over the services and golden-tested; NiceGUI renders are thin. *Hardware* tab added to
  Settings; 298 i18n keys with EN/HI parity.
- **Unit 35 — Onboarding wizard + setup checklist** (tests first; 6 tests). `services/onboarding.py` —
  11 steps whose done-ness is **probed from live data** (shop name set, staff user exists, products
  imported, opening stock entered, scanner paired, …) so a step ticks itself when the work is done from
  its own page; `skip` / `unskip` is the only stored state; `checklist(texts)` for the dashboard card.
  `ui/pages/onboarding.py` — steps 2–11 (`submit_shop_profile` with GSTIN regex, `submit_tax`,
  `submit_payments` to the encrypted store + QR switch, `submit_staff`, `build_opening_stock` /
  `submit_opening_stock`, `build_wizard`), rendered at `/onboarding?step=` inside the shell for the Owner;
  link steps (products, labels, scanner, printer, backup drive, recovery key) open the owning page.
  Dashboard `/` shows *Setup n/11* with a progress bar until every step is done or skipped.
  **605 stdlib-only tests.**

### Added — Phase 3 (POS billing, strict mode from day one)
- **Unit 36 — TaxEngine + measured quantities** (tests first; 18 tests). `services/tax.py` — per line
  gross = shelf × qty / pricing-unit, offer and/or ad-hoc discount (stack only when the offer allows,
  otherwise the higher benefit wins and the receipt names it), inclusive / exclusive GST split, CGST/SGST
  halves; per bill the bill-level discount is **pro-rated across lines by net (largest remainder)** so the
  per-rate tax summary stays exact, round-off to the rupee (`billing.tax_rounding`); `enabled=False` zeroes
  every rate for an unregistered shop. The docs/05 §8 receipt example reproduces to the paisa.
  `services/measure.py` — `QuantitySpec`, `validate_qty` (`E-MEAS-001` below minimum · `E-MEAS-002` off
  step · `E-MEAS-003` fraction of a piece), `round_to_step`, `qty_for_amount` ("₹50 of ghee" → 80 g),
  `format_qty` (1000 g → "1 kg"), weight/price-embedded **scale barcodes** (`hardware.scale_barcode_format`
  = `21PPPPPWWWWWC`, check digit verified). `core/clock.financial_year` ("26-27"); `MeasureError` area.
- **Unit 37 — Offers, cart, idempotent checkout** (tests first; 14 tests). `services/offers.py` —
  PCT / AMOUNT / BUY_X_GET_Y offers scoped to a product, category, tag or the bill, validity window,
  stackable flag, `best_for(product, qty)`, Owner-only CRUD (`OFFER_CREATED / UPDATED / DEACTIVATED`).
  `services/billing.py` — `Cart` / `CartLine`; `lookup_scan` (scale codes first, then the barcode table),
  `add_scan` (weighed items need a quantity; rescanning bumps the quantity), `add_lookup` (F3 → `LOOKUP`),
  `add_quick_pick` (staff refused in strict mode, `E-BILL-070`), `add_open_item` (`manual_add_policy`
  ALLOW / PIN / BLOCK, cap per bill `E-BILL-071`, under-price check `E-BILL-072` against the nearest
  catalogue match), `set_line_discount` / `set_bill_discount` (reason mandatory, above the cap → Owner PIN
  `E-BILL-060`), `remove_offer` (Owner / PIN, `E-BILL-061`), hold / resume (JSON in `held_cart`).
  **`checkout`** runs in one `BEGIN IMMEDIATE`: replay check on `client_txn_id` → open shift
  (`E-BILL-042`) → per-FY counter `invoice:<fy>` → `LedgerService.hash_for_new_sale` → `sale`, `sale_item`,
  `payment` rows → one `SALE` movement per catalogue line (negative stock BLOCK / WARN / ALLOW) → audit
  (`SALE_CREATED`, `OPEN_ITEM_LINE`, `MANUAL_LINE`, `DISCOUNT_OVERRIDE`) → `SaleCommitted`. The same cart
  twice returns the first invoice; a failing movement rolls back the counter too; eight concurrent
  checkouts produce gap-free, unique numbers and an intact chain. `SqliteBillingStore`; records
  `OfferRecord`, `SaleView` (+ `as_ledger_record()`), `SaleItemView`, `PaymentView`, `HeldCartRecord`;
  settings `billing.void_window_hours`, `billing.tax_rounding`, `hardware.scale_barcode_format`.
- **Unit 38 — Receipts, renderers, print queue** (tests first; 10 tests). `services/receipt.py` — the
  view-model from the committed sale: shelf → discount → amount per line with the offer note beneath,
  `†` lookup / `*` open-item markers and a legend only when used, tax rows only when registered, "You
  saved", payments with change, UPI payload (`receipt.show_qr_upi`, id from the encrypted store), the
  **receipt EAN-13** (`core/barcode.receipt_ean13`: `28` + FY + sequence) so scanning a bill opens it;
  archive to `<primary>/receipts/YYYY/MM/<invoice>.html` (+ `.pdf` when weasyprint is present).
  `hardware/receipts.py` — ESC/POS for 80 mm (48 columns) and 58 mm (32 columns, two-line items, wrapped
  notes) with native EAN-13, QR, drawer kick on cash, partial cut; code-page transliteration (`₹`→`Rs`,
  `†`→`^`), Devanagari only through an optional raster hook; `text_lines()` parser; HTML with the SVG
  barcode. `hardware/printer/escpos.py` — `NetworkTransport` (`:9100`), `SystemTransport` (win32print RAW
  or `lp -o raw`, `copy /B` fallback), `UsbTransport` (`/dev/usb/lp*` or the OS printer), `FileTransport`
  (`hardware.printer_kind = "file"` for testing); every failure is `E-PRN-001`. `services/print_queue.py`
  — `after_sale` (archive always → print if ready → else queue + `E-PRN-001`), `print_pending`
  (button + 60 s job), `reprint` (`DUPLICATE`, counter, `REPRINT` audit, above `reprint_limit` → Owner
  `E-AUTH-011`), `chip()` for the POS. `SqlitePrintQueueStore`. Labels and the Hardware test print now
  go through the real transports.
- **Unit 39 — HardwareGuard + hot-plug poll** (tests first; 6 tests). `services/hardware_guard.py` —
  `refresh()` probes the paired scanner, the configured printer and new printer names, publishes
  `HardwareStatusChanged` once per change (scanner lost → notification; new printer → toast);
  `can_add_line` (typed code `E-BILL-070`, scanner absent `E-SCN-001`; probe failures never lock the
  counter), `can_checkout` (billing pre-hook: `require_printer` + not ready → `E-PRN-002` unless an
  override is active), `scan_mode`, time-boxed Owner / PIN overrides in `hardware_override` (≤ 12 h,
  reason mandatory, `HARDWARE_OVERRIDE` / `_ENDED` audited), `may_enable_require_printer`. Jobs
  `hardware_poll` (10 s) and `print_queue_flush` (60 s) bound in `bootstrap/startup.py`.
- **Unit 40 — POS page** (tests first; 11 tests). `ui/pages/pos.py` — `CartRegistry` (one open bill per
  cashier; committed carts replaced), `build_pos` (rows with the **Discount column**, markers, totals
  block, next-invoice preview, scanner / printer chips, banners, quick-pick tiles locked for staff in
  strict mode, `F9 Pay ₹…`), `classify_entry` (scanner vs typed by keystroke speed) + `handle_scan_input`
  (ADDED / NEEDS_QTY / UNKNOWN "Ask Owner" / REJECTED → F3 / PAUSED / RECEIPT → `/sales/<id>`), quantity pad
  (presets, step snapping, by-amount, live amount), F3 Find + Open-item tab, F4 discount (PIN field on
  demand), F9 payment (quick cash exact / next ₹10 / ₹100 / ₹500, change, split), `do_checkout` with
  receipt preview + print status, hold / resume, keyboard map. Settings → Hardware: *Require printed
  receipt* switch (refused without a ready printer), Owner overrides, test print to the real printer.
  95 `pos.*` i18n keys EN/HI. **Phase 3 code complete: 661 stdlib-only tests.**

### Added — Phase 4 (security, shifts & integrity)
- **Unit 41 — ShiftService** (tests first; 5 tests). `services/shift.py` — `open` with the float (one open
  shift per user, `E-SHIFT-002`), `cash_in`, `cash_out` (Owner PIN for staff; never more than the drawer
  holds, `E-SHIFT-004`), `expected_cash` = float + cash sales (tendered − change) − cash refunds + in − out,
  `close` by denomination count or total (variance = counted − expected, a shortfall needs a note, the
  Owner may close anyone's shift, `ShiftClosed` event), `x_report` / `z_report` with bills, items, totals
  by payment mode, discounts, manual lines, voids, invoice range and the **sales-chain head** (an
  off-system anchor once printed), `report_lines` for the thermal printer, `variance_history`,
  `cashier_variance_summary`. `SqliteShiftStore`; records `ShiftRecord`, `CashEventRecord`; codes
  `E-SHIFT-001 / 003 / 004`.
- **Unit 42 — Void / refund, open-item review, `/sales`** (tests first; 6 tests). `BillingService.void`
  / `refund` / `void_status` — a credit note is a new `sale` row (`VOIDED` or `REFUND`, `original_sale_id`,
  its own invoice number, hashed into the chain) with reversing `SALE_VOID` / `RETURN` movements carrying
  the **Owner's id** (the trigger refuses staff) and a positive `CASH` payment that leaves the actor's
  drawer; staff need the PIN and the approver is recorded; `billing.void_window_hours` binds staff, not
  the Owner (`E-BILL-090`); per-line refundable quantities (`E-BILL-091`); already voided → `E-BILL-092`.
  The receipt shows *Against bill …* and the reason under a `CREDIT NOTE` label. `services/open_item_review.py`
  — queue grouped by name, `convert` → product (`CONVERSION`), `dismiss`. `ui/pages/sales_history.py` —
  `/sales` (staff own · Owner all with cashier filter · search by invoice / public id / receipt code),
  `/sales/<id>` (receipt text, reprint, void, per-line refund, related credit notes), `/sales/review`.
- **Unit 43 — Reports, integrity banner, Settings → Users / Payments** (tests first; 7 tests).
  `services/report.py` + `SqliteReportStore` — discount audit, manual lines, voids & refunds, override
  audit, shrinkage, cashier performance, tax summary by rate, integrity (run now), audit trail, `to_csv`
  (UTF-8 BOM). Fixed while testing: the default report window ended at *now*, which excluded a movement
  stamped in the same second — it now ends at the end of the shop day. `ui/pages/reports.py` — index
  cards + one generic table page + CSV export; staff see only *Staff work history*.
  `ui/layout.integrity_banner` + red bar in the shell for the Owner when the hourly job marks the ledger
  chip red. `ui/pages/settings_users.py` (create staff, de/reactivate, reset password, Owner PIN) and
  `ui/pages/settings_payments.py` (masked previews, reveal after password re-entry, clear, receipt QR
  auto-on). Settings tabs are now Backups · Hardware · Users · Payments · System.
- **Unit 44 — PIN dialog, `/shifts`, permission property test** (tests first; 6 tests).
  `ui/components/pin_dialog.py` — the one PIN prompt for every staff-screen override (`verify` → staff
  actor with `pin_verified` + the Owner's id as approver; lockout and `OVERRIDE` / `OVERRIDE_FAILED`
  audit come from `UserService`); the POS uses it too. `ui/pages/shifts.py` — open with float, cash in /
  out (PIN), X report, **blind** denomination count (expected shown after the count; the Owner always
  sees it), close → Z report, print (`PrintQueueService.print_lines`), history; a cashier without an open
  shift lands on `/shifts?open=1` after login. `tests/core/test_phase4_security.py` — property test: every
  Owner-only permission denies STAFF, 20 Owner-only service calls raise `PermissionDenied`, PIN-grant calls
  raise `PinRequired`, raw SQL as staff cannot insert a non-`SALE` movement / edit a sale / delete a
  movement, and a committed sale deducts stock exactly once. **Phase 4 code complete: 685 stdlib-only tests.**

### Added — Phase 5 (history, reports, dashboard, forecasting)
- **Unit 45 — Sales-side reports** (tests first; 4 tests). `ReportService` gains `sales_by_day` (IST shop
  days; credit notes as refunds), `sales_summary`, `item_sales`, `top_sellers` (qty / rupees, share),
  `dead_stock` (valued), `low_stock` (both roles), `purchases` / `purchases_by_day`, `profit` (margin from the
  cost snapshot on each line; lines without cost counted separately), `hourly_heatmap`, `stock_as_of`
  (`stock_daily_balance`). All reconcile with raw SQL in tests. New cards on `/reports`.
- **Unit 46 — Forecast engine** (tests first; 10 tests). Pure `forecast/` package: `series.py` (daily series,
  stock-out censoring), `methods.py` (SMA-7/28, EWMA α 0.3, last-year, **median ensemble** with band and
  confidence), `festivals.py` (`config/festivals.toml` uplift windows per category, `learn_uplift` from last
  year), `planner.py` (days of cover, stock-out date, reorder to packs, NEW/DEAD/SLOW/STEADY/FAST).
  `services/forecast.py` (`forecast`, `plan`, `snapshot_all`, `snapshots`, `accuracy`; `E-FCST-001` under
  `forecast.min_history_days`), `SqliteForecastStore`, `ForecastSnapshotRecord`, `ForecastSettings`,
  nightly job `forecast_nightly`, *Reports → Reorder plan / Demand classes*, sample festival calendar.
- **Unit 47 — Dashboard, insights, notifications page, SVG charts** (tests first; 9 tests).
  `services/insights.py` — `InsightsService.for_owner / for_staff` return facts with i18n keys (sales vs
  yesterday, low stock, stock-outs within 7 days, reorder count, dead stock, manual-line share ≥ 15 %,
  drawer variance, receipts waiting), worst first. `ui/pages/dashboard.py` now owns `/` (tiles, 14-day
  sparkline, payment mix, bullets, low stock, reorder suggestions, who is at the counter, setup checklist,
  status chips; staff see their own shift only). `ui/pages/notifications.py` — `/notifications` with the
  bell's visibility rule, mark read / mark all. `ui/components/charts.py` — dependency-free SVG
  sparkline + share bars (replaces the planned ECharts/matplotlib pair). `/` removed from `ui/server.py`.
- **Unit 48 — Basket analysis + Export everything** (tests first; 1 test). `ReportService.basket_pairs`
  (pairs on one bill, counted once per bill, share of completed bills) → *Reports → Bought together*;
  `reports.export_all_zip` — one BOM-CSV per Owner report + `README.txt` in a ZIP (stdlib), button on
  `/reports`; staff get nothing. **Phase 5 code complete: 709 stdlib-only tests.**

### Changed — Phase 5
- `ForecastService.plan` suggests the gap to the minimum for items without usable history instead of 0.
- Reports column labels: every Phase-5 column now has an EN/HI label (`reports.col.*`).
- Carried to Phase 7 (need the PDF renderer): Day-close wizard, purchase-order PDF, XLSX/PDF export.

### Added — Phase 6 (AI assistant — owner's analyst, guard-railed, bilingual)
- **Unit 49 — Providers** (tests first; 9 tests, fake HTTP server). `ai/providers/_http.py` (stdlib
  `urllib`; one place maps failures to `E-AI-001/004/012/020/021`, the key never enters a message),
  `OllamaProvider` (`/api/chat` JSON + NDJSON stream with native tools, `/api/embed`, `/api/tags` health),
  `OpenAICompatProvider` (`/chat/completions` JSON + SSE, `/embeddings`, `/models`; OpenAI **and** the
  Hugging Face router by `base_url`; `on_usage` token sink), `factory.build_provider` (disabled / key
  missing → `NullProvider` with the reason code) and `CircuitBreaker` (3 failures → open 60 s, instant
  replay, half-open probe). `ChatMessage.tool_calls` added so tool exchanges round-trip in each vendor's shape.
- **Unit 50 — Guardrails, output filter, fast path, red-team corpus** (tests first; 16 tests).
  `ai/guardrails.py` — ordered multilingual classifier (SECRETS · FINANCIAL_PII · OWNER_PII · APP_INTERNALS ·
  SECURITY_BYPASS · VULN_DISCOVERY · FRAUD_ASSIST · OWNER_ONLY · OFF_TOPIC · HARASSMENT_ABUSE · owner
  `extra_blocked`), canned refusals EN/HI/Hinglish, `detect_language`, `RateLimiter`, `OffTopicCounter`,
  `quick_check_instructions`. `ai/output_filter.py` — credential patterns, paths, code/SQL, and figures not
  present in tool results. `ai/fastpath.py` — deterministic intents with bilingual templates, answered from the
  same tools, no LLM. `ai/redteam/corpus.py` — 60 blocked + 10 owner-only + 12 allowed prompts and 5 jailbreak
  framings shared by tests and the live run.
- **Unit 51 — Tools, ChatService, help index, prompts, widget, Settings → Assistant** (tests first; 10 tests).
  `ai/tool_protocol.py` (ToolDef, strict arg validation, trimming, staff key stripping, prompted-JSON block
  parser), `ai/tools.py` (26 read-only tools per docs/06 §3 with role gating; 9 for staff), `ai/embeddings.py`
  (float32 packing, cosine, keyword score), `ai/indexer.py` (user guide + how-to chunks by heading, hash-skipped,
  hybrid search), `ai/prompts/` (locked `core.md` hashed, `profile_staff.md`, `profile_owner.md`,
  `build_system_prompt`), `services/prompt.py` (`PromptService` propose/activate/rollback with the quick-check and
  audit), `ai/chat_service.py` (`ChatService.ask` pipeline; persistence to `chat_session`/`chat_message`/
  `chat_security_event`/`ai_usage`; owner alert at `flag_alert_threshold`; monthly budget `E-AI-030`),
  `SqlitePromptStore` / `SqliteChatStore` / `SqliteIndexStore`, records `PromptVersionRecord` /
  `ChatMessageRecord` / `ChatSecurityEventRecord`, `ui/components/chat_widget.py` (model + dialog from the
  bubble), `ui/pages/settings_ai.py` (Assistant tab), *Reports → Chat security*, services `ai_provider`
  (breaker) / `tools` / `indexer` / `prompts` / `chat` + `rebuild_ai_provider` on `[ai]` setting changes,
  jobs `ai_health` (chip), `help_index`, `ai_keepalive` (Ollama). Error codes `E-GRD-005/006`.
  **Phase 6 code complete: 744 stdlib-only tests.**

### Changed — Phase 6
- Providers use stdlib HTTP instead of the `ollama`/`openai` SDKs (nothing to install; identical behaviour).
- `FakeProvider(embeddings=False)` lets tests exercise the keyword-only help search.

### Added — Phase 7 (hardening, operations & remote publishing)
- **Unit 52 — Report publishing** (tests first; 10 tests). `PublishingSettings` (`[publishing]`: enabled, channels,
  folder, SMTP, Telegram, WhatsApp, digest time, weekly weekday, max attempts); `publishing/renderer.py` (digest,
  weekly, any table → self-contained HTML, PDF via optional weasyprint; SVG sparkline), `publishing/outbox.py`
  (`publish_queue` with backoff 1·5·15·60·240 min, `FAILED` after `max_attempts`), channels **folder / SMTP /
  Telegram / WhatsApp** (stdlib; credentials from `secure_setting` `publish.*`), `services/publisher.py`
  (`ReportPublisher`: `daily_digest`, `weekly_report`, `publish_table`, `render_document`, `flush`, `chip`,
  `test_channel`; disabled → local document only), `SqlitePublishStore`, `PublishQueueRecord`,
  `ui/pages/settings_publishing.py` (Settings → Publishing), jobs `digest_daily` / `weekly_report` /
  `outbox_flush`, publish chip, `E-PUB-001`. The digest carries today's top sellers and the sales-chain head.
- **Unit 53 — Day-close wizard, purchase order, XLSX/PDF export** (tests first; 5 tests). `ui/pages/day_close.py`
  (`/shifts/day-close`: close shifts → Z reports → backup → digest → finish with `DAY_CLOSED` audit and optional
  app stop), `services/purchase_order.py` (reorder plan grouped by last supplier, estimated cost, document / send),
  `services/export.py` (`to_xlsx` — stdlib one-sheet workbook), reports page **XLSX / PDF / Send** buttons
  (`export_xlsx`, `export_document`, `send_document`).
- **Unit 54 — Hardening & operations** (tests first; 6 tests). `commands/restore.py` (`restore --list / --id /
  --path / --yes`; refuses while the app's heartbeat is fresh, `E-SYS-016`), `bootstrap/update.py`
  (`plan_update` validates a ZIP/folder release; `apply_update` stages the old files in `.previous/`, copies,
  runs the install + self-test checks and **rolls back on failure**), `bootstrap/maintenance.py` (`main.py
  --restore` guided, `main.py --update <release>`), `platform/watchdog.py` (heartbeat → start / ok / restart),
  `platform/tls.py` + `server.https_enabled` (self-signed HTTPS listener), installer messages `[restore]` /
  `[update]` EN+HI.
  **Phase 7 code complete: 763 stdlib-only tests. All seven phases are code-complete.**

### Fixed — first live run (macOS 15-Nov, NiceGUI 4.0.0)
- **App crashed right after the installer finished** (`APP_EXITED_EARLY code=4`, `RuntimeError: ui.page cannot be
  used in NiceGUI scripts when UI is defined in the global scope`): `ui/server.serve()` created the 10-second
  heartbeat with `ui.timer` *outside* any page, which NiceGUI ≥ 3 treats as a "NiceGUI script" and refuses when
  `@ui.page` routes exist. Now `app.timer`. The installer, database creation and doctor had all passed.
- Adapted to the NiceGUI 3/4 API the code was not written against: `ui.html(..., sanitize=False)` everywhere (and
  the receipt / Z-report `<pre>` bodies are HTML-escaped — product names may contain `<`), `ui.download(...)` →
  `ui.download.content(...)` / `ui.download.file(...)`, CSV import handler reads `await e.file.read()`. Dependency
  pin tightened to `nicegui>=3.0,<5`. `tests/core/test_ui_shell.NiceGuiCompatibilityTest` scans the UI sources so
  none of these can come back.

- `python -m vaishnavi --config-dir … <command>`: options given *before* the command were silently reset to
  their defaults by the sub-parser (argparse parents); `--lang` and `--config-dir` now work in either position.
- `SqliteTxn` closed the connection only after `BEGIN` succeeded; a damaged file leaked the handle (Windows kept
  the file busy). It now closes on any failure.

### Fixed — live run 2 (macOS 15-Nov, app running, owner walked the screens)
- **Sahayak bubble did nothing** (no server log either): the chat dialog asked Quasar for `position=bottom-right`,
  which is not a `q-dialog` position (standard/top/right/bottom/left). Quasar's default-transition lookup threw in
  the browser and the dialog never rendered. Now `position=right` with the card pushed to the bottom corner;
  `NiceGuiCompatibilityTest` rejects any other `position=` value. `CHAT_WINDOW_OPEN` is logged on every open.
- **DB chip stayed amber ("Owner account not set up yet") after the Owner finished setup**: the chip was set once
  from the start-up doctor and never refreshed. New 60-second `db_status` job re-runs only the database check and
  logs `DB_CHIP_CHANGED` when the colour moves.
- **"A required setting is missing or invalid: shop.gstin … The app cannot start without it" banner** when the
  Tax step's *GST registered* switch was flipped: the wizard had no GSTIN field and the generic `E-CFG-001` text
  was alarming. The Tax step now has a GSTIN input (validated, 15 characters), refuses *registered* without one
  with a field-level hint, never wipes a stored GSTIN, and the rule has its own friendly code `E-CFG-003`
  ("GST registered needs the shop's GSTIN … Nothing was changed").
- **Label downloads "corrupted"** (`labels.pdf` = valid PDF with 0 pages, `labels.escpos` = 0 bytes): the grid's
  *Labels* button opened `/labels` with nothing selected and *Print* rendered an empty job. `LabelService.print_job`
  now refuses an empty job (`ValueError`, nothing logged); the Labels page has its own product picker (multi-select
  with search, live preview and count), honours `?purchase=<id>` (products received in that purchase — the Stock
  page already linked there), and the Products grid lets the Owner tick rows and send exactly those to Labels.
- **Staff got NiceGUI's 404 on *Shifts*** — root cause not reproducible here (the route registers, verified by the
  new `RouteRegistrationTest` against a stub NiceGUI; NiceGUI 4 only 404s on a genuine route miss). Made the
  failure impossible to miss on the next run: a page module that fails to import is logged at ERROR with its
  traceback (`UI_PAGE_IMPORT_FAILED`, `E-UI-001`) instead of a quiet debug line; the full route list is logged at
  start (`UI_ROUTES`) and every shell link is checked against it (`UI_ROUTE_MISSING`); every 404 is logged with
  path, query and referer (`HTTP_404`, `E-UI-002`) and answered by a small bilingual page with a *Home* link.
  New `UiError` area; docs/08 §5 lists the codes.

### Fixed — live run 3 (macOS 16-Nov, the daily start)
- **Second cached start crashed in the *Software packages* stage** (`E-SYS-099` after ~0.9 s, exit 4): the cached
  branch returned `StageResult(..., **previous_data, verified_only=True)`, and the previous record — written by
  the cached branch the day before — already contained `verified_only`, so Python raised *"got multiple values
  for keyword argument"*. It never showed before because the `pyproject.toml` pin change forced a reinstall in
  between. Data is now rebuilt explicitly; the test runs three consecutive starts (lock and unpinned variants).
  Every shop day after the first cached day would have failed to start — the most serious bug so far.
- **"Port 8080 is already in use by another program" when yesterday's app was still running**: the owner closed
  the installer window (the app deliberately survives that) and double-clicked *Start Vaishnavi* again; the
  doctor's network check refused the day although the start stage would have adopted the instance. The check
  now asks `/healthz` on the busy port: when it is Vaishnavi (`"app": "vaishnavi"` added to the payload) the check
  **passes** with "already running on port … (version, uptime)" and the start stage opens the browser on it;
  only a foreign listener is still `E-SYS-017`.
- Installer `state.json` now keeps each stage's `detail` (the crash repr) so a failed run is diagnosable from the
  state file alone.

### Fixed — live run 3b (16-Nov, the owner used the app)
- **Assistant hallucinated** ("Aaj tak sandalwood powder ka koi stock nahi hai" for "stock kya hai"; "list" /
  "kaun sa or abhi" treated as item names; "match box" → flat "not found"):
  - Fast path: new **`stock_overview`** tool + intent ("stock kya hai", "stock list", "sab stock", "kaun sa item …",
    "स्टॉक लिस्ट") answers with counts and the best-stocked items instead of guessing or extracting nonsense;
    question words (kaun/sa/or/abhi/list/sab …) are stop words; an unknown item first tries `search_products`
    ("Did you mean: Match stick (12)?") and the answer is **tentative** — when a model is available the question
    goes on to it with the fast text as fallback.
  - **Grounding guard** in `ChatService`: a data question (stock/price/sales/items … EN/HI/Hinglish) answered with
    **no tool call** is sent back once with a system nudge ("you have no knowledge of this shop except through
    tools — call one now"); if the model still refuses to look, a fixed honest line replaces its text
    (`AI_UNGROUNDED_ANSWER` / `AI_UNGROUNDED_FINAL` in `ai.log`, category `UNGROUNDED`). Core prompt rule 2 now says
    item names, not only numbers, must come from tools.
- **Billing page said nothing about the missing shift until Pay**: amber banner "No shift is open for you … Open
  shift" with a button on the POS screen when the cashier has no open shift; the Pay dialog's `E-BILL-042` now
  closes and jumps to `/shifts?open=1` instead of leaving the cashier at a dead end.
- **Status page** lists the registered pages, any missing shell route (`E-UI-001`) and the last ten 404s with
  their referer (`E-UI-002`) — the owner can read them without opening log files.
- Installer "this window keeps watch" note now says the setup log console (port 8081) closes with the window and
  where the app's own log viewer is (the owner saw "Safari cannot open the page" after closing it).
- **`access/http.log` was empty**: uvicorn gives `uvicorn.access` its own stderr handler with `propagate=False`,
  so not one request line reached our file. `server.capture_uvicorn_access()` (startup hook) removes those
  handlers and lets the records propagate — every `GET /path … 404` now lands in `http.log`.
- **Route-matching probe**: `server.match_report()` asks the Starlette router which endpoint a GET for each shell
  path would really reach and logs `UI_ROUTE_MATCH` at registration *and* again once uvicorn is serving;
  a path with no match logs `UI_ROUTE_UNREACHABLE` (`E-UI-001`) and shows on the status page. The Mac log proved
  `/shifts` is registered by the decorator yet unknown to the router at request time; this pins down which.
- **`main.py --stop`**: the application deliberately outlives the installer window, so after copying new code the
  owner kept talking to *yesterday's* process ("already running on port 8080, up 138 min") and saw none of the
  fixes. `--stop` reads the pid from `state/heartbeat.json`, sends SIGTERM (uvicorn shuts down → clean close),
  waits for the port to free, and says what happened (`stop.*` messages EN+HI). Documented in docs/17 §5.
- **`/shifts` 404 — narrowed, not yet explained**: with the probes in place the shop PC's log shows all 15 page
  modules registering, every page present in FastAPI's table **except `/shifts`** (absent already at
  `stage=registered`, so nothing removes it later), and no exception. `register_pages` now logs one line per
  module (`UI_PAGE_REGISTERED module=shifts added=… removed=…`), `tools/probe_routes.py` registers five
  parameter variants against the installed NiceGUI/FastAPI and prints which survive, and the page's query
  parameter `open` (a builtin name; the page's only peculiarity) was renamed `then_pos` — `/login?next=` also
  shadows a builtin and works, so this is hygiene, not the proven cause.

### Fixed / Added — live run 6 (21-Nov; the owner's three requests)

- **Cashier day book** (owner: "record each cashier's sales per day — every item per invoice with the timestamp;
  keep 90 days; deletable earlier with the admin's approval"): new table `cashier_day_record` (migration
  **0004_cashier_day_record**, `HEAD_REVISION`/`SCHEMA_VERSION` → 0004) — one row per bill / credit note with the
  items as JSON, shop day, time, cashier, status, quantity, total and payment modes, written by the new
  `CashierRecordService` on `SaleCommitted` (idempotent, never blocks the sale). **Reports → Cashier day book**:
  filter by day / range / cashier (staff see their own only, `REPORT_OWN_SHIFT`), items as "Camphor 50g ×2 pcs @
  ₹10.00 = ₹20.00", per-cashier subtotals and the day total, CSV/XLSX/PDF/Send like every report. Retention
  `billing.day_record_retention_days` (90; 1–3650) enforced by the nightly `day_book_prune` job (audited
  `CASHIER_RECORDS_PRUNED`); the Owner's *Delete older records…* asks for a cut-off day, a reason and the Owner
  password (`users.reauthenticate`) and is audited `CASHIER_RECORDS_PURGED`. The permanent `sale` ledger is untouched.
- **Letterhead on every document** (owner: "the sales report was a plain record — no shop name, date, time,
  address, owner, contact"): `publishing.renderer.Letterhead` (shop name · address · phone · email · GSTIN when
  registered · owner · system date & time IST · prepared by) rendered as a branded header with a saffron rule and
  a footer on `render_table` (every Export PDF / Send, purchase orders, channel tests), the daily digest and the
  weekly report; the same lines are written as title rows above the column header in **CSV**, **XLSX** and the
  *Export everything* ZIP (`ReportService.to_csv(letterhead=…)`, `export.to_xlsx(title_rows=…)`).
  `ReportPublisher.letterhead()` builds it from the effective settings. New setting **`shop.owner_name`** (Settings →
  Shop & GST → "Owner name (printed on reports)"); `config/settings.toml` now carries the shop's address
  ("Khandve Nagar, Wagholi" / "Pune"), phone (+911234567890) and owner ("Radhe").
- **"How much have I spent / invested in the shop so far"** (owner: the assistant could not answer, in English or
  Hinglish): inbound stock movements (`OPENING`, `PURCHASE`, positive `ADJUSTMENT`/`STOCK_TAKE`) now **snapshot the
  item's cost** in `stock_movement.unit_cost_paise` on the day they happen, so the history is kept from day one.
  New `ReportService.investment()` → per item (qty received, opening ₹, purchases ₹, corrections ₹, invested ₹,
  stock today and its value at cost, since when, "estimated" when no purchase price exists → selling price) with
  per-category subtotals and the grand total; **Reports → Investment in stock**; AI tool **`investment_summary`**
  (Owner); the fast path routes "kharch / spent / invest / paisa lagaya / निवेश" to it in EN · HI · Hinglish —
  "Stock mein investment (all time, 14-Nov-2026 se): ₹1,000.00 — opening stock ₹1,000.00 + purchases ₹0.00 +
  corrections ₹0.00, 1 items mein. Aaj shelf par rakhe stock ki cost value ₹960.00 hai. Category-wise: …" —
  while supplier-worded questions ("supplier wise purchase") keep going to `purchases_summary`. Prompt core,
  grounding nudge and guardrail topic lists updated.
- **Sales by month / Day Sales Report** (owner, after seeing the letterhead): *Sales by day* is now **Sales by month** —
  a month picker (`?month=YYYY-MM`, default this month) lists every shop day of that month and closes with a
  **TOTAL** row. New **Day Sales Report**: a calendar date picker (`?day=YYYY-MM-DD`, any day of any year) shows
  the quantity sold of each item **in its own unit** ("10 pcs", "200 g", "100 ml"; open items grouped by typed
  name), bills and rupees per item, then **TOTAL SALE · Cash · UPI · Card** (and Refunds / NET when any).
  `ReportService.item_totals()` / `day_sales()` over the new `SqliteReportStore.item_totals` query; both reports
  carry the chosen window in the document title and export like every other report. Document subtitles no longer
  repeat the generation time (the letterhead's *Date & time* is the single source) — they carry the row count only.
- **Installer asks for the shop identity** (owner: "keep placeholders; the installation should ask for shop name,
  address, owner, contact"): new stage **7a Shop details** (`bootstrap/shop_profile.py`, between *Data locations*
  and *Autostart*) asks shop name · owner · address lines · phone (validated 10–13 digits) · email and writes the
  `[shop]` block of `config/settings.toml` (other sections and unasked keys such as `name_hi`/`language` kept; a
  complete block is reused silently; `--yes` keeps the default name and WARNs to finish in Settings → Shop & GST).
  `config/settings.toml` ships with empty placeholders; the in-app first-run wizard's *Shop profile* step gained the
  owner field. EN/HI installer messages; `test_bootstrap_shop_profile` (+3).
- Tests: new `test_cashier_records` (recording, staff scoping, summary, 90-day prune, Owner purge + audit, page
  model with subtotals, password-approved purge, 0004 replay); `test_sales_reports` (+1 investment end-to-end:
  service, tool, page); `test_publishing` (+2 letterhead renderer and settings → PDF/CSV/XLSX);
  `test_ai_guardrails` / `test_ai_chat` / `test_reports_security` / `test_db_schema` updated.

### Fixed / Added — live run 5b (20-Nov, Mac on 0.2.0; the owner's three observations)

- **Whole-bill discount was practically unreachable** (owner: "I don't see the final-bill discount"): the tier
  dialog existed but opened only via F4 *with no line selected*, and the cart always has a selected line. New
  **F2 Bill discount** action button (highlighted with the tier + amount once given), the *No bill discount*
  line is clickable, F4's hint now says "selected line", key legend updated (`pos._keys` F2, `.vsm-act.active`).
  The receipt already printed "Extra discount — <tier>" and the two bold "You saved" lines.
- **"^ label damaged – looked up" removed from the customer's receipt** (owner: back-office information). The
  `†`/`*` markers and the legend are no longer put into the `ReceiptModel`; they stay on the billing screen, in the
  review queue and in the per-cashier / manual-lines reports. Receipt goldens regenerated; docs/05 §8 sample.
- **Shift close reconciliation** (owner: "opening float + today's sales must match what the cashier enters;
  otherwise no close without the Owner PIN and a comment"):
  - `ShiftService.reconcile()` → `Reconciliation` (opening float, cash sales, cash in, cash out, expected,
    counted, cash variance, system totals by mode, **declared UPI / card** from the phone app, per-mode variance,
    `matches`). `close()` takes `declared_by_mode`, `approver_id`, `tolerance_paise`; **any** difference (cash
    beyond `billing.shift_variance_tolerance_paise`, default 0; or a declared mode ≠ system) needs a note ≥ 3
    chars (`E-SHIFT-005`) **and** the new permission `SHIFT_CLOSE_VARIANCE` (PIN for staff, plain for the Owner).
  - Migration **0003_shift_reconcile**: `shift.declared_json`, `shift.approved_by_user_id`
    (`HEAD_REVISION=0003_shift_reconcile`, `SCHEMA_VERSION=0003`); Z report prints "UPI as per app", "UPI
    difference", "Difference approved by"; audit `SHIFT_CLOSED` carries `declared`, `mode_variance_paise`,
    `matched`.
  - `/shifts` close section: denomination grid + "UPI as per app ₹" / "CARD as per app ₹" boxes; the **working**
    appears once the count is typed (blind count kept for staff; the Owner sees it at once): opening float →
    + cash sales → + cash in → − cash out → = should be in the drawer → counted now → difference, then each
    non-cash mode system · declared · difference, and a green "Everything matches" or red "Does not match: cash
    −₹20.00, UPI −₹100.00". With a difference staff see "write the note, then the Owner enters the PIN"; Close
    opens the PIN dialog (`ask_pin(SHIFT_CLOSE_VARIANCE)`). Hindi strings added.
- Tests: `test_shifts` (+2: any difference → note + PIN, tolerance; declared UPI reconciled like cash),
  `test_phase4_security.ShiftsPageTest` (+1 UPI declaration; close flow updated), `test_db_models_match_schema`
  (+1 replay of 0003), `test_pos_page` (+1 F2/F4 keys), `test_receipts` updated. 809 tests.

### Fixed / Added — live run 5 (18-Nov, after the owner's second round)
- **Version 0.2.0** — shown in the page footer so the owner can tell at a glance whether the shop PC runs the
  code just pulled (several reports this round — shift-open denominations, final-bill discount — were features
  already shipped yesterday, i.e. the Mac was on the old build).
- **Ghee / oil tile "disabled", "Quantity is below the minimum (10 ml)"**: tapping a weighed tile called
  `add_quick_pick(qty=1)` — one *gram* / *millilitre* — which the 10 g/ml minimum rejected, so the weight pad never
  appeared. Tiles and F3 Find now open the **quantity pad** for measured items (`scan_for_product`,
  `add_measured(entry=QUICK_PICK|LOOKUP)`), with presets, "0.5 kg" / "250 g" typing and ₹-amount; Enter confirms.
- **"No Oil category"**: default categories gained *Oil & Wicks*, *Threads & Malas*, *Prasad & Dry Fruits*,
  *Others*; `top_up_reference_rows` re-runs the idempotent seed on every start so existing databases get them. The
  product editor's category box now accepts a **typed new name** (created on save, case-insensitive reuse) —
  there was no screen to add a category before.
- **Chat "connection lost, trying to reconnect"**: the model call ran synchronously inside NiceGUI's event loop
  (10–30 s on a small PC), starving the WebSocket heartbeat. The widget now awaits `run.io_bound(send)` and shows a
  "Sahayak is thinking…" spinner; provider errors become a polite bubble instead of a dead widget.
- **`main.py --fresh-start`** (owner: "delete all entries and start from scratch"): stops the app, requires the
  typed word `START FRESH` and the Owner password, archives database + receipts to `<backups>/fresh-start-<date>/`,
  builds a new database and carries over users/passwords/PIN, settings, secrets, categories, units, assistant
  instructions and onboarding progress; everything else starts empty; the guardian fingerprint is removed so the
  new instance is not called FOREIGN (`services/fresh_start.py`, `commands/fresh_start.py`, bootstrap mode
  `FRESH_START`). Refuses while the app is running. Documented in docs/17 §5.
- **`main.py --stop` was followed by an automatic restart**: the `main.py` window that started the app keeps a
  **watchdog** (relaunch on crash/hang, max 3/h). `--stop` from another terminal looked like a crash to it. Now
  every deliberate stop (`--stop`, Settings → System → Shut down, `--fresh-start`) writes `state/stop-requested`;
  the watchdog consumes it on process exit, logs `WATCHDOG_STOPPED_BY_OWNER`, tells the owner and exits instead of
  relaunching. The app clears a leftover marker at start so a manual start is never blocked. *Restart* (after a
  restore) deliberately leaves no marker so the watchdog still brings the app back
  (`platform/watchdog.request_stop/consume_stop_request`, `bootstrap/selftest.write_stop_marker/stop_requested`,
  `Watchdog(stop_requested=)`).
- **`--stop` still relaunched on the Mac (20-Nov)**: the marker is only understood by a watchdog running the
  *new* code — the `main.py` window started before the code copy kept the old watchdog in memory and relaunched
  on every `--stop` (up to 3×/h). `--stop` now ends the watchdog process itself **first**: the start stage records
  its pid in `state/watchdog.json` (`selftest.write_watchdog_record/clear_watchdog_record`), `stop_watchdog`
  verifies the pid is alive *and* its command line contains `main.py` (so a reused pid after a reboot is never
  killed; stale records are removed), sends SIGTERM, then writes the marker and stops the app. On POSIX the new
  watchdog catches SIGTERM (`app_stages.supervise`) and exits as "stopped by owner" instead of dying mid-loop.
  One manual `kill` of the pre-20-Nov watchdog is needed when upgrading (docs/17 §5). 805 tests.
- Opening stock for a litre item: "50" with unit *l* stores 50 000 ml (`stock_text_for` → "50 l") — covered by a
  test alongside the new-category case.
- **`tools/manifest.py`** (owner: "which command tells me which file is not yet updated on the shop PC?"): stdlib
  script that prints `sha256[:16]  size  path` for every code file (main.py, launchers, pyproject.toml, `bootstrap/`,
  `src/`, `tests/core/`, `tools/`, `docs/`, `config/` — not logs, caches, venv, data or receipt goldens); line
  endings are normalised so
  Windows CRLF and Mac LF checkouts hash the same. `--out there.txt` saves it (use this, not `>`, on PowerShell,
  which writes UTF-16); `--compare there.txt` lists only `DIFFERENT` / `MISSING` / `EXTRA` files and exits 1 when
  anything differs, `IDENTICAL — N files match` otherwise. `git rev-parse --short HEAD` + `git status --short` on
  both machines remains the quick first check. Documented in docs/17 §5.

### Added / Fixed — live run 4 (17-Nov, Windows VM; the owner's ten observations)
- **AI answers business questions deterministically** (obs. 1 — "kitna paisa ab tak kharch kiya", "aaj pura din
  kitna item bika", "har item ka sale", "which stock is high in demand", "no demand", "forecast next few days",
  "next day sale growth" all fell to the 7B model, which did not call a tool and was then replaced by the honest
  "I could not check" line). The fast path (`ai/fastpath.py`) gained Owner intents with period/horizon parsing in
  EN · Hindi · Hinglish — `spend` → `purchases_summary` (new period `all_time` for "so far / ab tak"),
  `item_sales` / `high_demand` → `demand_ranking` (period today / yesterday / this week / this month …),
  `no_demand` → `dead_stock(30 d)`, `forecast` → `forecast_sales` + `reorder_plan` ("next week" 7 d, "agle
  mahine" 30 d, "next day" 1 d), `sales_period` → `sales_summary(period)`; answered in < 100 ms with no model at
  all. Comparisons / "why" / festival questions still go to the model (`_COMPLEX`). Tool descriptions carry the
  Hinglish synonyms, the core prompt has a "which tool for which question" table, the grounding nudge lists the
  business tools, and the guardrail's Owner-only vocabulary now includes *sold / bika / kharch / spent / agle*
  so staff get the polite refusal instead of an "offline" shrug.
- **Staff can book goods received** (obs. 2): `Permission.RECEIVE_STOCK` (Owner ALLOW · Staff ALLOW),
  `InventoryService.receive_goods(actor, product, qty, note=)` — positive quantity only, note ≥ 3 chars
  mandatory, type `PURCHASE`, audit `STOCK_RECEIVED`, `StockChanged(by_staff=True, user_id)` → Owner
  notification "Goods received by staff: X +n" with a link to the movement. DB trigger
  `trg_stock_movement_validate` admits `PURCHASE` with `qty_delta > 0` from any user (everything else stays
  Owner-only, so stock can never be *reduced* by staff). Stock page: "Goods received" button for everyone; a
  staff row-click opens the receive dialog (quantity in the item's pricing unit, note, time + name shown).
- **Weighed items on the bill** (obs. 3): the product editor derived `sale_mode` from a separate "Sold by" select
  (default PIECE) while the unit said kg → mixed state, so 500 g of ₹1000/kg ghee neither priced nor deducted.
  The unit now drives everything (`_unit_for`: kg → WEIGHT/g/×1000, l → VOLUME/ml, m → LENGTH/cm, else PIECE)
  and the pad accepts "500", "0.5 kg", "1.5kg", "250 g" (`parse_qty_text`). Opening stock and alerts are typed in
  the pricing unit ("10" for 10 kg) and stored in base units.
- **POS redesigned to the mock-up** (obs. 4, `docs/mockups/pos.html`): scan bar with F3/F6/F7 buttons, category
  pill tabs, tiles for **every** active item (quick picks first) with an emoji icon from the name (`icon_for`),
  price per unit and "N left / ⚠ low / ✖ out"; right-hand bill card with table (Item · Qty · Shelf · Discount ·
  Amount), totals grid, **TOTAL** in saffron, "Customer saves", and the action grid Pay / F4 Discount / Esc Clear.
  Second pass after the owner's check: the **left rail** is now the mock-up's 64 px icon rail (icon above a tiny
  label, saffron square when active) instead of a wide text list; cart rows use the bordered **qty pill**
  (`− 2 pc +`), mono `#id · code`, saffron `†`/`*` markers, discount chips with the small second line ("offer ·
  locked" / the ad-hoc reason), a ✕ remover; amber **notice bar**; tile image 70 px; key legend as `code` chips
  (`row_item_html`, `row_discount_html`, `keys_html` are pure and tested). **Customer saves**: shown in green under
  TOTAL on screen (already) and on the receipt as `Item discounts` / `Bill discount (reason)` rows plus a now
  **bold** "You saved ₹… today" line right under TOTAL (green in the PDF/HTML copy); receipt goldens regenerated.
  **Per-line discount arithmetic on the receipt** (owner's sketch): a discounted item prints its **shelf** amount
  on the item row and, beneath it, `discount 1% (Diwali Offer)   -0.20  = 19.80` (ad-hoc: `discount 10.00 (regular
  customer)`), so the customer can follow every line to the total; long offer names wrap instead of being cut
  (`ReceiptLine.discount_row`, thermal + HTML). **Extra discount on the final bill** (owner: unhappy customers
  want "something more"): the F4 bill discount is now presented as a reward — totals row `Extra discount — Premier
  customer`, and under TOTAL two bold lines *"You saved ₹1.00 on items"* / *"You saved another ₹9.00 as our
  Premier customer"* followed by a thank-you (`receipt.savings_message`, default "Happy shopping! Aapka din shubh
  ho — see you again soon."). Nothing of this prints when there is no bill discount (plain "You saved … today"
  stays). Same two green lines on the POS bill card (`PosModel.saved_rows`). TOTAL remains the amount paid.
- **Bill on WhatsApp — optional, off by default** (owner's request, then "make it optional, I doubt the
  limitations"): switch `receipt.whatsapp_share` under Settings → Shop & GST → *Receipt extras*. When off nothing
  WhatsApp-related appears (F8 stays as plain customer capture, `ReceiptShareService.share` refuses with a
  plain message). When on, **F8 Customer** on the POS captures the mobile number (+ name) —
  normalised to `+91 …`, stored in the hitherto unused `customer` table (one row per number, name kept) and
  linked through `sale.customer_id`; green 📱 chip in the bill header; survives hold/resume. The "Bill done"
  dialog has a phone box (prefilled) and **Send bill on WhatsApp**: with the Meta Cloud API configured under
  Settings → Publishing the text is posted directly; otherwise a `wa.me` click-to-chat link opens WhatsApp
  Desktop/Web with the whole bill (58 mm receipt in a monospace block under the bold shop name) pre-filled
  and the cashier presses *Send*. API errors fall back to the link; audit `RECEIPT_SHARED`
  (`services/receipt_share.py`, `BillingService.set_customer / customer_of / remember_customer`,
  `normalise_phone`, `CustomerRecord`). F8 added to the key map and the action grid.
  **Bill-level discount** (F4 with no line selected): customer tier (Regular 2 % · Premier 5 % · Festival ·
  Round-off; editable via data key `billing.discount_tiers`), % or ₹, reason printed on the receipt, over-cap →
  Owner PIN; shown as its own totals row and at the bottom of the bill card.
- **GST** (obs. 5): new **Settings → Shop & GST** tab (first tab) — shop name/address/phone/email, GSTIN
  (15-character pattern, required when registered, `E-CFG-003`), state code, "GST registered" switch, default
  GST % for new items, "shelf prices include GST", and a live receipt-header preview. Reuses the onboarding
  wizard's validated submits so the two paths cannot disagree. Per-item CGST/SGST is the item's GST % (the
  editor shows the live split).
- **Product editor simplified** (obs. 6/7): essentials only — name, category, "Sold as" unit (with a hint), purchase
  price, selling price, opening stock, GST %; code/MRP/HSN/low-stock alert/usual order qty/tags/barcodes live
  under "Advanced". Hindi name and the "Sold by" select are gone; "Restock" is now "Usual order quantity (for the
  reorder list)".
- **UPI payment did nothing** (obs. 8): the Pay dialog now changes with the mode — CASH quick notes + tendered +
  change; **UPI shows a QR** (`upi://pay?pa=…&am=…` via the `qrcode` package, SVG) with the shop's UPI id and a
  reference box; CARD a slip reference; SPLIT three amounts with the live shortfall.
- **Shift open counts the drawer** (obs. 9): denomination grid (₹500 … ₹1; ₹2000 removed — withdrawn 2023) with a
  live total; staff must fill it (type 0 for an empty drawer), the Owner may type a total. Stored as JSON in the
  new `shift.opening_denominations` column. **End work** for staff: the primary button is "Close shift & count the
  drawer"; Confirm is disabled until an Owner PIN is typed (server already enforced it).
- **"Item with code 2 saved but invisible"** (obs. 10): the grid's one-click **Archive** (no confirmation) hid the
  item while its code stayed reserved, and `E-CAT-004` talked about a *barcode*. Archive now asks for
  confirmation; the grid has a "Show archived" switch and a status column; an archived item's editor shows a
  banner with **Restore**; a duplicate code names the other item and its state (`E-CAT-005`: "…already used by
  'X' (archived) — restore it or choose another code").
- **Receipt rate column for weighed items** printed `500.00/1000g`, which the narrow column cut to `500.00/100` —
  now `/kg`, `/100g`, `/L` (`ReceiptService._line`). Verified end-to-end: 100 g at ₹500/kg → ₹50.00, 50 g at
  ₹5000/kg → ₹250.00, stock 10 kg → 9.9 kg (`test_weighed_items_price_proportionally_and_deduct_the_exact_weight`).
- Schema **0002_staff_receive**: re-creates the movement trigger from `schema.sql` (`schema.statement_for()`) and
  adds `shift.opening_denominations`; `HEAD_REVISION`/`SCHEMA_VERSION` → 0002; existing databases are upgraded
  by `ensure_schema_current` at start (dry-run on a copy first). Tests replay the 0002 SQL with sqlite3 since
  Alembic is not importable on the dev machine; a new test asserts exactly one migration head and that
  `schema.py` names it.

### Removed — Phase 7
- Never-imported Phase-1 placeholders: `db/repositories/` (ORM twins of `sqlite_stores`), `hardware/scanner.py`,
  `hardware/scale.py`, `hardware/printer/{discovery,os_print}.py`, `api/{auth,data}.py`,
  `ui/components/{cart_table,data_grid,payment_dialog,product_tile,qty_pad,receipt_preview,wizard}.py`.
  `tests/core/test_hardening.py` fails if a docstring-only module reappears.
- Repository clean-up after the code-complete milestone: `bootstrap/tls.py` (openssl shell-out, never wired;
  superseded by `platform/tls.py`) and its tests, the empty `tests/unit`, `tests/integration`, `tests/fakes`
  tiers (everything is in the stdlib-only `tests/core` tier that ships with the app), `__pycache__` folders
  and a stray `logs/` created by a local test run. `pytest testpaths` → `tests/core` only.
