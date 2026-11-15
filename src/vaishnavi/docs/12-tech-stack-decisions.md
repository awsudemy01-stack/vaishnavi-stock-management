# 12 — Tech-Stack Decisions (ADRs)

Each decision records context, options considered, choice, and consequences.

## ADR-001 UI framework: NiceGUI

**Context.** Python-only requirement; needs a professional look, real-time cart updates, keyboard
events (scanner wedge, F-keys), image tiles, dialogs, a persistent floating chat widget, printable views, and
must run on a PC with no Node.js.

| Option | Pros | Cons |
|--------|------|------|
| Streamlit | easiest | whole-script rerun model breaks keyboard-driven POS; no true keyboard events; weak layout control |
| Gradio | great for AI demos | not a business-app toolkit |
| Flet (Flutter) | native look, desktop window | younger ecosystem; packaging quirks; printing harder |
| Reflex | React-grade UI | requires Node toolchain at build time |
| Dash | mature | callback plumbing heavy for POS interactions |
| Tkinter/PyQt | true desktop | dated look or licence cost (Qt), harder theming, no tablet access |
| **NiceGUI** | Quasar/Material components, FastAPI inside, websockets live updates, `ui.keyboard` events, `app.storage`, native-window mode, printing via browser, tablet access over LAN | newer framework; UI code is Python-heavy |

**Decision.** NiceGUI. **Consequences.** Same process can expose REST (`/api/v1`) and `/healthz`;
receipts printed through browser or ESC/POS; the chat widget streams via websockets.

## ADR-002 Database: SQLite (WAL) via SQLAlchemy 2.0 + Alembic

Postgres excluded by requirement. Single PC, < 10 writes/sec → SQLite is ideal. WAL for concurrent
reads during writes, `BEGIN IMMEDIATE` for writer serialisation (gap-free counters). SQLAlchemy keeps
the door open to MariaDB if a second branch appears. Alembic for schema evolution.
**Rejected:** DuckDB (analytics-oriented, weak concurrent writes), TinyDB (no transactions),
raw `sqlite3` (too much boilerplate, no migrations).

## ADR-003 Money as integer paise, `Decimal` in code

Avoid float drift on tax splits; GST half-up rounding to paise; round-off line at bill level.

## ADR-004 Stock as ledger of movements, not a mutable column

Enables audit, shrinkage detection, history and crash safety. `qty_after` snapshot gives O(1) reads.

## ADR-005 Append-only, hash-chained sales

Tamper evidence against both UI misuse and direct DB edits. Cost: ~0.2 ms per sale, one verifier job.

## ADR-006 AI runtime: Ollama; models `qwen2.5:3b-instruct` + `nomic-embed-text`

CPU-only machine → 3B class. Qwen2.5 chosen for Hindi/Hinglish quality and native tool-calling
support in Ollama; nomic for speed. Configurable; `qwen2.5:7b` and `bge-m3` documented as upgrades.
**Rejected:** llama.cpp direct (more ops burden than Ollama). Cloud LLMs were originally rejected
(privacy, internet); **superseded in part by ADR-041**: Ollama remains the *default local* provider, and
cloud APIs are now an *owner-selected alternative* for PCs that cannot carry a local model.

## ADR-007 Vector search: SQLite blobs + NumPy

Catalogue and docs are tiny (thousands of vectors). A dependency-free NumPy cosine search is faster
to operate than Chroma/FAISS/Qdrant and keeps "SQLite only". `sqlite-vec` optional later.

## ADR-008 Logging: stdlib `logging` with queue, rotation, JSON option

No extra dependency; correlation ids via `ContextVar`; `structlog`/`loguru` rejected to keep one
mental model and compatibility with uvicorn/SQLAlchemy loggers.

## ADR-009 Configuration: TOML file + DB settings table

File for machine/deployment values (paths, ports, models); DB for owner-editable business values
(tax labels, discount cap) so they are changeable from the UI and audited.

*Implementation note (unit 16, 14-Nov-2026).* The settings loader is **stdlib dataclasses +
`tomllib`** rather than pydantic-settings: identical behaviour (typed fields, `Literal` choices,
coercion, cross-field validation, `VAISHNAVI_SECTION__KEY` environment overrides, DB overrides via
`Settings.with_overrides`), but it lives in the stdlib-only core tier (ADR-040) so the installer's
doctor can read settings before any package exists and the loader is verifiable on the development
PC. pydantic stays for FastAPI request/response models. A secret-looking key in any TOML section
(`api_key`, `token`, `password`) refuses start-up with `E-SEC-006`.

## ADR-010 Receipt rendering: one view-model, ESC/POS primary, HTML/PDF archive

One receipt view-model feeds the ESC/POS renderer (primary — see ADR-024), the HTML/PDF renderer
(archive copy of every receipt, A4 fallback) and the digest. Jinja2 for the HTML variant.

## ADR-011 Authentication: two roles, username + password, Owner PIN for overrides

Exactly two roles (`OWNER`, `STAFF`). Both log in with username + password (argon2; staff may use a
numeric password for speed; lockout after N failures). The Owner also holds a short **Owner PIN**
used only for on-counter overrides on a staff screen, so the Owner password is never typed in front
of staff. Permissions enforced server-side; UI hiding is never the only guard. A third role is not
shipped (YAGNI) but the permission model is a plain enum → set mapping, so one can be added later.

*Implementation note (unit 17, 14-Nov-2026).* Hashing uses stdlib **`hashlib.scrypt`**
(RFC 7914, memory-hard; `$scrypt$ln=15,r=8,p=1$salt$hash`, constant-time verify, `needs_rehash`
for parameter upgrades) instead of argon2-cffi: equivalent protection for a single-PC shop, one
fewer compiled dependency, and testable on the development PC. `verify_secret` also accepts
`$argon2…` strings when that package is present, so a future migration needs no data change.
Lockout: 3 failures → 15 min (`E-AUTH-030`). Secret *encryption* stays on `cryptography` Fernet
behind a `SecretBox` protocol; the master key lives in the OS keyring (keyring package) with a
0600 file fallback reported as `E-SEC-001` and auto-migrated into the keyring when one appears.

## ADR-012 Python version policy

Target **3.12** (broadest wheel availability on all three OSes); code stays 3.11+ compatible.
`main.py` accepts any 3.11–3.13 found on the machine; for a newer interpreter it dry-runs the
dependency resolution and, if any wheel is missing, guides installation of 3.12 for the current OS
(`winget` / `brew` / `apt`) and uses it for the venv.

## ADR-013 Process model: single process, in-process scheduler

Simplicity on one PC. In-process jobs for backup/verify/alerts; the OS scheduler (Task Scheduler / launchd /
systemd user unit) only for *starting* the app and the watchdog.

*Amended (unit 26).* The scheduler is the stdlib `scheduler/simple.py` (`SimpleScheduler`: interval and
daily-at-IST jobs, one thread, per-job failure isolation and counters, injectable clock for tests) rather
than APScheduler. Reasons: the job table (docs/07 §7) needs only "every N seconds" and "daily at HH:MM";
the whole start-up spine stays importable on a bare interpreter (ADR-040), so the doctor can report a
scheduler problem instead of failing to import; and one less dependency to pin and hash. APScheduler
remains an optional swap behind the same `every()/daily()` surface if cron-style schedules are ever needed.

## ADR-014 Packaging: stdlib bootstrapper + venv on every OS; PyInstaller later

`main.py` (stdlib only) is the installer and launcher on Windows, macOS and Linux; it creates the
venv and installs everything. PyInstaller bundles considered for Phase 7 once dependencies stabilise;
venv is more debuggable and updatable during active development.

## ADR-015 Testing strategy

Service layer unit tests on in-memory SQLite (tax engine, checkout atomicity, hash chain, shift
variance); golden-file tests for receipt HTML; doctor checks tested with fakes; AI client faked.
UI smoke tests via NiceGUI's `User` test fixture for login → sale → receipt.

## ADR-016 Strict mode: scanner is a hard dependency of staff billing; printer is optional by default

**Context.** Owner's anti-theft rule: every item scanned. A printer may not exist yet, and the shop
must never stop because of paper or a cable. **Decision.** `require_scan` defaults `true`;
`require_printer` defaults **`false`** and can only be enabled while a printer is ready. Without a
printer, sales commit, receipts are archived as PDF and queued (`print_pending`), and the Printer chip
turns amber with a count. Printer discovery runs continuously so a newly plugged printer is offered to
the Owner. HID scanner presence detected via one-time USB pairing (VID:PID) and device polling; staff
billing pauses while it is absent (Owner-PIN time-boxed override). **Consequences.** Day-one operation
without a printer; owner retains the option of strictness; unprinted-receipt counts become a
visible metric.

## ADR-017 Forecasting: deterministic statistics, LLM only narrates

**Context.** Owner asks "what will be the future sale?" — an LLM alone would hallucinate numbers.
**Decision.** `ForecastService` (NumPy-only: SMA/EWMA/last-year ensemble + festival uplift + planner)
produces numbers, bands, confidence and caveats; stored nightly in `forecast_snapshot`; the chat tool
returns that payload and the system prompt forbids self-computed figures. Heavier libraries
(Prophet, statsmodels) rejected for install weight on Python 3.14 and small-data behaviour; may be
reconsidered once > 2 years of history exist. **Consequences.** Forecasts are identical in the UI,
the digest and the chat; testable with synthetic series; accuracy measurable later.

## ADR-018 Remote publishing via outbox + official channel APIs

**Context.** Owner wants PDFs by mail/WhatsApp; shop may be offline. **Decision.** `publish_queue`
outbox with retry; channels: synced folder (zero-config), SMTP, Telegram Bot API, WhatsApp via Meta
Cloud API/Twilio. Browser-automation WhatsApp libraries rejected (fragile, ToS risk). Secrets in a
separate git-ignored `secrets.toml`. **Consequences.** Shop never blocks on internet; the owner gets
the same PDFs the app prints.

## ADR-019 Discounts: owner-defined offers + capped ad-hoc discounts, shelf price immutable

**Context.** Festive offers must show on the receipt; cashiers must not invent discounts.
**Decision.** `Offer` entity auto-applied at scan (non-removable by cashier); ad-hoc discounts need a
reason and are capped; every line stores shelf price, offer, % and ₹ so the receipt shows
"₹10.00 → 1 % → ₹9.90" and totals show "You saved". **Consequences.** Discount audit per cashier and
per offer; tax computed after discounts with bill discount pro-rated.

## ADR-020 Manual entry is allowed but always visible

**Context.** Some items have no barcode; the owner wants cashiers to bill them by typing name,
detail, qty, price, discount. **Decision.** `F3 Find / Manual` with two paths (Find item by name / code / ID →
locked price; open item → free text against system product `OPEN-ITEM`), policy `ALLOW | PIN | BLOCK`
(default ALLOW), per-shift open-item cap, under-price sanity warning, `*` on receipt, *Manual lines*
report and an Owner review queue converting open items into products. Search and tiles remain
disabled for cashiers so the manual path is explicit and countable. **Consequences.** Flexibility
without a silent bypass; manual-line ratio becomes a theft indicator.

## ADR-021 Development vs. target environment

**Context.** Code is authored on a company PC where nothing may be installed or run; the app runs on
the owner's personal shop PC whose specs are unknown. **Decision.** No step of the plan depends on
executing anything on the development PC. `main.py` detects OS and hardware and chooses models/settings;
`vaishnavi selftest` runs the full test suite on the shop PC; optional integrations are lazy-imported
with `Null` fallbacks so the package imports anywhere. **Consequences.** First real execution happens
on the shop PC; the doctor and selftest are the acceptance gate, and docs include a "first run"
checklist.

## ADR-022 Assistant guardrails are layered and mostly deterministic

**Context.** Staff may try to extract PINs, bank details, code, or ask for ways to defraud or crash
the system. **Decision.** Six layers (data isolation, role gate, pre-LLM intent filter, prompt policy,
tool hardening, output filter). The decisive guarantees are **deterministic**: the model never receives
sensitive data, and blocked categories are refused before any LLM call. Flagged attempts are audited
and alert the owner. Red-team corpus is part of CI. **Consequences.** Slightly narrower assistant
(no chit-chat), but immune to prompt-engineering for data it never holds; owner gains an early-warning
signal about staff intent.

## ADR-023 Product identity: permanent ID → derived internal EAN-13; multiple barcodes per product

**Context.** New items need barcodes; damaged labels must be recoverable by name/code/ID; labels
and receipts must print on a POS thermal printer. **Decision.** Every product has a permanent numeric
ID, a human code, and N barcodes (`product_barcode`). The internal barcode is **EAN-13** = `29` +
10-digit ID + check digit — numeric, universally scannable, natively printable by ESC/POS, and
regenerable from the ID. Code-128-of-SKU rejected (alphanumeric codes print poorly on cheap thermal
heads and are not derivable from the ID); QR rejected for labels (needs 2D scanner). Manufacturer
barcodes attach as extra rows. **Consequences.** Any torn label can be reprinted from the ID; F3 Find
resolves name/code/ID to the same product; scan resolution is a single indexed lookup.

## ADR-024 ESC/POS is the primary print target for receipts and labels

**Context.** The shop will use an 80 mm (or 58 mm) POS thermal printer. **Decision.** Receipts and
labels are rendered from view-models to ESC/POS via `python-escpos` (native QR/barcode, raster for
Devanagari, cut, drawer); HTML/PDF remains for archive and A4 fallback; OS print-system path (Windows spooler / CUPS) for
driver-only printers. **Consequences.** Fixed-width layout engine with 48/32-column variants; printer
status polling feeds strict mode; test-print covers every element.

## ADR-025 Owner-editable assistant instructions on top of a locked core

**Context.** The Owner wants to tune the assistant's persona and answers. **Decision.** The system
prompt = locked guardrail core (code, hashed) + Owner instructions (DB, versioned). Edits are
previewed, red-team quick-checked, activated, audited and reversible. Because layers 1, 3 and 6 of the
guardrails do not depend on the prompt, no edit can expose data the model never receives.
**Consequences.** Flexibility for the Owner without a security regression path; prompt history is a
first-class artefact.

## ADR-026 Owner secrets encrypted at rest, unreachable from the AI layer

**Context.** Bank/UPI details and publishing credentials must be editable by the Owner but never
leak to staff, logs or the assistant. **Decision.** `secure_setting` table with Fernet ciphertext;
key stored in the OS secret store via `keyring` (DPAPI / Keychain / SecretService; encrypted-file fallback with a doctor warning); masked previews only; password re-entry to edit; the AI
tool registry has no accessor and the output filter blocks UPI/account/IFSC patterns as a backstop.
**Consequences.** DB backups do not contain plaintext secrets; moving to a new PC requires the Owner
to re-enter them (documented in the restore procedure).

## ADR-027 Cross-platform by construction: `platform/` adapters and OS-neutral core

**Context.** The owner may run the shop on Windows, macOS or Ubuntu. **Decision.** All OS-specific
behaviour (paths, secrets store, device enumeration, printing system, serial ports, autostart, browser
launch, elevation, fonts) lives in `vaishnavi/platform/` behind small interfaces with `Null`
fallbacks; `sys.platform` may be tested only there and in `bootstrap/`; OS-specific packages use
environment markers; CI matrix runs on all three. **Consequences.** Same features everywhere; a new
OS is a new adapter set, not a rewrite.

## ADR-028 The application installs its own dependencies, asking for admin rights only when needed

**Context.** The owner must not be asked to run developer commands; some installs need admin rights.
**Decision.** `main.py` surveys, installs Python packages unprivileged into a venv, and requests
elevation (UAC / sudo / pkexec) **per system-level step** with a plain explanation of why and what
will run; the app itself never runs elevated; declining is handled gracefully. **Consequences.**
Minimal attack surface, clear audit trail of elevated commands, and a shop owner can set up the PC alone.

## ADR-029 Weighed / measured items: base-unit stock + price per pricing unit

**Context.** Ghee by grams, oil by millilitres. **Decision.** `sale_mode` with base-unit stock
(`g`/`ml`), price per pricing unit (100 g, kg, 100 ml, L), min/step/presets, quantity pad with
by-amount, optional serial scale, and decoding of weight/price-embedded barcodes. Tax, offers and
discounts reuse the per-line engine unchanged. **Consequences.** Loose items are first-class in stock,
receipts and reports; no separate code path for "loose sale".

## ADR-030 Log console as a first-class, read-only HTTP/REST surface

**Context.** The owner wants to watch every operation — from installation to daily use — from a
browser on the network. **Decision.** JSON-lines logs feed `/api/v1/system/logs` (filter, page, SSE
stream, download) and a lightweight `/system/logs` page; Owner session or Bearer token; localhost by
default, LAN opt-in, optional TLS; a stdlib mini log server covers the pre-install and failed-start
cases. Strictly read-only. **Consequences.** Remote support without screen sharing; no state can be
changed through the console; access is itself audited.

## ADR-031 Database durability: WAL + synchronous=FULL, single writer, no live-file copies

**Context.** Daily power-offs; the owner fears corruption. **Decision.** SQLite in WAL mode with
`synchronous=FULL`, `BEGIN IMMEDIATE` writes, a single-writer lock, `wal_checkpoint(TRUNCATE)` plus a
fingerprint at clean shutdown, `quick_check` at every start and full `integrity_check` nightly. The
application never copies the live `.db`; all copies go through the online-backup API.
**Consequences.** A few ms per commit; corruption from restarts is designed out; any remaining damage
(disk failure, external edits) is detected at start and recoverable from snapshots.

## ADR-032 Two locations chosen at install; snapshots are standalone and copy-back friendly

**Context.** Owner wants a primary (live) and secondary (backup) location and asks whether copying
the backup over the primary works. **Decision.** Installer requires two distinct locations
(different drive recommended, same folder refused). Snapshots are complete single-file databases with
a manifest; schedule: start-if-stale, shift close, nightly, pre-change, on-demand, optional 15-min.
Default **unencrypted** so manual copy-back always works; encryption is opt-in. A `db_identity` table
lets the app recognise a restored copy, migrate it, verify the chain and tell the owner the missing
period; a foreign DB is refused. Weekly automatic restore drill proves backups are usable.
**Consequences.** Recovery works both via the in-app wizard and by hand; owners must re-enter
encrypted secrets on a new PC (documented).

## ADR-033 Price history is append-only and separate from sale snapshots

**Context.** Owner wants 1 month / 6 months / 1 year / custom-range price history in a pop-up chart. **Decision.** `price_history` rows for every
change of price / MRP / cost / tax rate (old → new, who, when, why, source), written in the same
transaction as the change; sales keep their own `shelf_price_paise` / `cost_paise_snapshot`, so
history serves analysis only. Offers overlay the chart but never alter history. **Consequences.**
Zero impact on billing correctness; cheap, auditable price analytics; margin reports remain exact.

## ADR-034 Charts: ECharts through NiceGUI for the UI, matplotlib for PDFs

**Context.** The owner wants graphs; the stack is Python-only. **Decision.** Interactive charts use
NiceGUI's bundled ECharts (`ui.echart`) configured from Python — no JS toolchain, touch-friendly, PNG
export; the same series are rendered headlessly with matplotlib for PDF digests. One `charts.py` helper
owns palette, Indian number formatting and dark mode. Plotly/Bokeh rejected (heavier, second charting
grammar). **Consequences.** Consistent look on screen and paper; chart specs are unit-testable data.

## ADR-035 Idempotent checkout keyed by a client transaction id

**Context.** Browser double-clicks and websocket retries can resubmit a checkout. **Decision.** The POS
mints a ULID per cart; `sale.client_txn_id` is UNIQUE; the service catches the constraint and returns the
existing invoice. **Consequences.** No duplicate invoices, stock deductions or cash; retry-safe API.

## ADR-036 Code-integrity manifest and hash-pinned dependencies

**Context.** The owner fears staff tampering with the program on the shop PC; supply-chain safety.
**Decision.** Each release ships a manifest of SHA-256 per installed file (signed with the release key);
the doctor verifies it and refuses to start on mismatch (`E-SYS-040`); the bootstrapper installs
dependencies with `pip --require-hashes` from a lock produced by `pip-tools`. **Consequences.** Local
edits are detected on the next start and audited; reproducible installs on all OSes.

## ADR-037 Owner recovery key

**Context.** There is no role above Owner; a forgotten password would lock the shop out. **Decision.**
A 12-word recovery phrase is generated at setup, shown once, stored only as an argon2 hash; resetting
the Owner password requires the phrase **and** OS administrator elevation, via the bootstrapper, and is
audited. **Consequences.** Recovery is possible without a developer and without weakening normal login.

## ADR-038 Notification centre, metrics endpoint and alert rules

**Context.** Health signals were spread over chips, toasts and the digest. **Decision.** A per-user
`notification` table and bell icon; an in-process `MetricsService` exposed as JSON and Prometheus text;
a 60-second `AlertService` evaluating `alerts.toml` thresholds into notifications (and Telegram/digest
for HIGH). **Consequences.** One place to look; log/metric data becomes actionable; standard metrics
shape for any future monitoring tool.

## ADR-039 Assistant fast path (deterministic intents before the LLM)

**Context.** Counter questions must be instant and must work when Ollama is down. **Decision.** A small
intent matcher (EN/HI/Hinglish patterns) answers stock / price / low-stock / today's-total / how-to
questions directly from tools and the user guide with templated text; the LLM handles everything else.
Role rules are shared. **Consequences.** Sub-100 ms answers for the common 80 %; graceful offline mode.

## ADR-040 Stdlib-only core test tier

**Context.** Nothing may be installed on the development PC, yet business logic should be verifiable
early. **Decision.** The pure business core (tax, measure, EAN-13, hash canonicalisation, invoice and
money formatting, guardrail classifier, forecast reference methods) imports only the standard library
and is tested with `unittest`; SQLAlchemy/NiceGUI/NumPy are used only by the layers around it.
**Consequences.** The core can be checked anywhere with a bare Python; the design stays decoupled.

## ADR-041 Pluggable AI provider: Ollama (local) · OpenAI API · Hugging Face API · none

**Context.** The shop PC's RAM/GPU are unknown; the owner may not have a machine that runs a local
model comfortably, and does not want to be locked in before knowing. Ollama does not need a GPU but
is slow on small CPUs.

| Option | Pros | Cons |
|--------|------|------|
| Ollama only (previous) | private, free, offline | may be unusably slow or impossible on a weak PC |
| Cloud only | works on any PC, best quality | monthly cost, internet dependency, data leaves the shop |
| **Pluggable provider, chosen interactively at install, switchable in Settings** | owner decides with facts in front of them; one code path for guardrails/tools; degrades the same way | one more abstraction; two embedding spaces to manage |

**Decision.** `ai/providers/` implements an `LLMProvider` protocol with `OllamaProvider`,
`OpenAICompatProvider` (serves OpenAI and the Hugging Face OpenAI-compatible router via `base_url`),
`NullProvider` and `FakeProvider`. The installer recommends from the hardware survey (`ollama` if
RAM ≥ 12 GB or a capable GPU, else cloud) and the owner chooses. Keys live only in `secure_setting`
(Fernet + OS keyring), are redacted everywhere, and are unreachable from tools. **Key entry is
interactive-first** (masked prompt in the installer or Settings); an env var / `config/secrets.env` is
accepted only as a one-time import for unattended re-installs and the file is then deleted; a key
inside any TOML config is rejected at start-up (`E-SEC-006`) — owner's decision 14-Nov-2026, because
billing staff share the PC and a readable key file would expose the owner's API bill. Cloud use shows a
plain-language data notice, enforces a monthly token budget (`E-AI-030`) and falls back to the fast
path when offline — identical to Ollama-down behaviour. Guardrails run before any request leaves the
PC. The embedding index is tagged with provider+model and rebuilt on change. Models with no native
tool support use a strict prompted-JSON tool protocol.
**Rejected:** LangChain/LiteLLM abstraction layers (heavy dependencies for three providers; the protocol
is ~60 lines), running both local and cloud simultaneously (cost/complexity; may return as an opt-in
"cloud fallback" later), Anthropic/Gemini (can be added as another `OpenAICompatProvider` base URL or
a small adapter if asked).
**Consequences.** Works on any PC; privacy is a visible owner choice, not an accident; `pyproject`
gains `openai` + `httpx`; doctor gains provider-aware AI checks; Settings gains a Provider tab.

## ADR-042 Interactive installer with an answers file

**Context.** The owner is not a developer; the install must ask only what truly needs a human decision
and must be repeatable on a replacement PC.
**Decision.** `bootstrap/wizard.py` is a stdlib question engine: numbered options, default in brackets,
EN/HI texts, `b`/`s`/`q`, masked secrets, resumable. Questions are limited to language, AI provider
(+ key), data locations, autostart and LAN access; everything else is detected. Non-secret answers are
written to `config/install-answers.toml`; `--answers` replays them, `--yes` accepts defaults,
`--ai-provider` pre-answers one question.
**Rejected:** a GUI installer (needs third-party packages before the venv exists; the terminal wizard
plus the `:8081` log page is enough and identical on all OSes), a fully silent installer (hides the one
decision — AI provider — the owner must consciously make).
**Consequences.** Predictable, documented prompts ([17 §3.1](17-install-and-run.md#31-the-installer-is-interactive--how-the-questions-work)); unattended re-install possible; secrets never
touch the answers file.

