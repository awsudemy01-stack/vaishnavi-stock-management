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
  **Phase 7 code complete: 766 stdlib-only tests. All seven phases are code-complete.**

### Fixed — Phase 7
- `python -m vaishnavi --config-dir … <command>`: options given *before* the command were silently reset to
  their defaults by the sub-parser (argparse parents); `--lang` and `--config-dir` now work in either position.
- `SqliteTxn` closed the connection only after `BEGIN` succeeded; a damaged file leaked the handle (Windows kept
  the file busy). It now closes on any failure.

### Removed — Phase 7
- Never-imported Phase-1 placeholders: `db/repositories/` (ORM twins of `sqlite_stores`), `hardware/scanner.py`,
  `hardware/scale.py`, `hardware/printer/{discovery,os_print}.py`, `api/{auth,data}.py`,
  `ui/components/{cart_table,data_grid,payment_dialog,product_tile,qty_pad,receipt_preview,wizard}.py`.
  `tests/core/test_hardening.py` fails if a docstring-only module reappears.
