# 18 — Technology Stack at a Glance

> One page answering *"what is this built with?"* for every layer. The **why** behind each choice is an
> ADR in [12 — Tech-Stack Decisions](12-tech-stack-decisions.md); the exact package list with versions
> will live in `pyproject.toml` + the hash-pinned lock once Phase 1 lands.

**Rule of the whole stack: Python only.** No Node.js, no JavaScript build step, no cloud service, no
server to rent. Everything runs in one Python process on the shop PC (plus the Ollama service), on
Windows, macOS or Ubuntu.

```mermaid
flowchart TB
  subgraph Browser["Browser on the shop PC (and tablet/phone on shop Wi-Fi)"]
    UI[NiceGUI pages<br/>Quasar / Material components · ECharts charts · websocket live updates]
  end
  subgraph Proc["One Python process — python -m vaishnavi"]
    NG[NiceGUI] --> FA[FastAPI<br/>/api/v1 · /system/logs · /healthz]
    FA --> SV[Services layer<br/>billing · inventory · reports · forecast · auth · backup]
    SV --> SA[SQLAlchemy 2 + Alembic]
    SV --> AI[Assistant<br/>ollama client · tools · guardrails · NumPy vector search]
    SV --> HW[Hardware adapters<br/>python-escpos · pyusb/hidapi · pyserial]
    SV --> PUB[Publishing<br/>reportlab/weasyprint PDF · smtplib · Telegram/WhatsApp API]
    SCH[APScheduler jobs] --> SV
    LOG[stdlib logging → files + log console] -.-> Proc
  end
  SA --> DB[(SQLite · WAL<br/>primary location)]
  DB -. snapshots .-> BK[(Backups<br/>secondary location)]
  AI --> OL[AI provider — chosen at install<br/>Ollama local :11434 · OpenAI API · Hugging Face API · none]
  HW --> SC[USB barcode scanner]
  HW --> PR[ESC/POS receipt printer]
  HW --> WS[Weighing scale]
  UI <--> NG
  MAIN[main.py · stdlib-only installer/launcher] -. installs & starts .-> Proc
  MAIN -. installs .-> OL
```

---

## 1. Frontend (what the staff and owner see)

| Concern | Tool | What it is | Why this one | ADR |
|---------|------|------------|--------------|-----|
| UI framework | **NiceGUI** | Python library that renders a modern web UI (Vue + Quasar/Material components under the hood) and pushes live updates over websockets | pure Python (no Node), real keyboard events for the scanner wedge and F-keys, dialogs, image tiles, a persistent floating widget for the chat, tablet access over LAN, bundled FastAPI | [ADR-001](12-tech-stack-decisions.md#adr-001-ui-framework-nicegui) |
| Component look | Quasar (via NiceGUI) + saffron theme | Material-design components, themed with the shop's saffron/ॐ branding approved in the mock-ups | consistent professional look with zero custom CSS build | 09 §1 |
| Charts | **ECharts** (bundled with NiceGUI) | interactive charts for the dashboard, price-history pop-up, sales trends, forecast bands | interactive, zoomable, no extra install | [ADR-034](12-tech-stack-decisions.md#adr-034-charts-echarts-through-nicegui-for-the-ui-matplotlib-for-pdfs) |
| Chat widget | NiceGUI floating card, bottom-right | streaming replies via websocket; EN / हिंदी / Hinglish | same framework, no second UI stack | 06, 09 §9 |
| Browser | system browser in app/kiosk mode (Edge/Chrome/Chromium/Safari) | opened by `main.py` on `http://127.0.0.1:8080` | nothing to install; printing fallback via browser print dialog | 07 |
| Receipt / label printing | **ESC/POS** bytes to the POS printer (primary); browser print / PDF fallback | 80 mm / 58 mm receipts and barcode labels | universal thermal-printer language | [ADR-010](12-tech-stack-decisions.md#adr-010-receipt-rendering-one-view-model-escpos-primary-htmlpdf-archive), [ADR-024](12-tech-stack-decisions.md#adr-024-escpos-is-the-primary-print-target-for-receipts-and-labels) |

---

## 2. Backend (the application server)

| Concern | Tool | What it is | Why this one | ADR |
|---------|------|------------|--------------|-----|
| Language / runtime | **Python 3.11 – 3.13** | one interpreter for everything | owner's requirement; wheels available on all three OSes | [ADR-012](12-tech-stack-decisions.md#adr-012-python-version-policy) |
| Web/API server | **FastAPI + uvicorn** (inside NiceGUI) | REST API (`/api/v1/...`), `/healthz`, `/system/logs`, static files | comes free with NiceGUI; typed request/response models | [ADR-001](12-tech-stack-decisions.md#adr-001-ui-framework-nicegui) |
| Settings / validation | **pydantic** + **pydantic-settings**, TOML files (`tomllib`) | typed configuration from `config/*.toml`, env vars and the DB `setting` table | validation with clear errors; TOML is human-editable | [ADR-009](12-tech-stack-decisions.md#adr-009-configuration-toml-file--db-settings-table) |
| Background jobs | **APScheduler** (in-process) | backups, hourly integrity check, hardware polling, alerts, nightly insights | no separate worker process to babysit | [ADR-013](12-tech-stack-decisions.md#adr-013-process-model-single-process-in-process-scheduler) |
| Logging | stdlib **`logging`** (queue handler, timed rotation, JSON lines option) + in-app log console | every module logs with correlation IDs; viewable in the browser and via REST | no dependency; plays well with uvicorn/SQLAlchemy loggers | [ADR-008](12-tech-stack-decisions.md#adr-008-logging-stdlib-logging-with-queue-rotation-json-option), [ADR-030](12-tech-stack-decisions.md#adr-030-log-console-as-a-first-class-read-only-httprest-surface) |
| Templates | **Jinja2** | HTML receipts, report HTML → PDF | standard, simple | 10 §4 |
| IDs | **python-ulid** | sortable unique IDs for log correlation, client transaction ids | time-ordered, URL-safe | 10 §16 |
| Money | integer paise + `decimal.Decimal` | all amounts stored as integers, GST rounded half-up | no float drift | [ADR-003](12-tech-stack-decisions.md#adr-003-money-as-integer-paise-decimal-in-code) |
| OS abstraction | `src/vaishnavi/platform/` adapters | the only code allowed to ask "which OS am I on?" | identical behaviour on Windows/macOS/Ubuntu | [ADR-027](12-tech-stack-decisions.md#adr-027-cross-platform-by-construction-platform-adapters-and-os-neutral-core) |

---

## 3. Database

| Concern | Tool | What it is | Why this one | ADR |
|---------|------|------------|--------------|-----|
| Engine | **SQLite** (built into Python), **WAL** mode, `synchronous=FULL` | a single file `vaishnavi.db` in the primary location | single PC, < 10 writes/s, zero administration, survives power cuts; Postgres excluded by requirement | [ADR-002](12-tech-stack-decisions.md#adr-002-database-sqlite-wal-via-sqlalchemy-20--alembic), [ADR-031](12-tech-stack-decisions.md#adr-031-database-durability-wal--synchronousfull-single-writer-no-live-file-copies) |
| ORM | **SQLAlchemy 2.0** | typed models, unit-of-work sessions, `BEGIN IMMEDIATE` writer serialisation | mature; keeps a door open to MariaDB if a second branch ever appears | ADR-002 |
| Migrations | **Alembic** | versioned schema changes (`0001_initial`, …), dry-run on a temp copy before upgrading | safe upgrades on the shop PC | ADR-002 |
| Integrity in the DB itself | SQLite **triggers** + **CHECK** constraints | staff can change stock only through a committed sale; `product_stock` cache maintained by trigger; append-only tables | protection even if someone bypasses the app | 05 §5a, 14 |
| Tamper evidence | SHA-256 **hash chain** on sales and audit rows | each row hashes the previous one; verified hourly | detects edits to the DB file | [ADR-005](12-tech-stack-decisions.md#adr-005-append-only-hash-chained-sales) |
| Backups | SQLite **online backup API** → standalone snapshot files in the secondary location, weekly restore drill | copy-back by hand is supported | consistent snapshots without stopping the app | [ADR-032](12-tech-stack-decisions.md#adr-032-two-locations-chosen-at-install-snapshots-are-standalone-and-copy-back-friendly) |
| Vector store (AI) | SQLite BLOB columns + **NumPy** cosine search | embeddings for products/help docs | thousands of vectors only; no second database | [ADR-007](12-tech-stack-decisions.md#adr-007-vector-search-sqlite-blobs--numpy) |

---

## 4. AI assistant

| Concern | Tool | What it is | Why this one | ADR |
|---------|------|------------|--------------|-----|
| Runtime — **chosen at install, switchable later** | **Pluggable provider**: `ollama` (local) · `openai` (OpenAI API) · `huggingface` (HF Inference API) · `none` | one `LLMProvider` interface; the installer recommends from the hardware survey (local if RAM ≥ 12 GB or a capable GPU, else cloud) and the owner decides | the shop PC is unknown — the owner must not be locked into local inference | [ADR-041](12-tech-stack-decisions.md#adr-041-pluggable-ai-provider-ollama-local--openai-api--hugging-face-api--none) |
| Local provider | **Ollama** (service on `:11434`) | runs open LLMs on the shop PC, CPU or GPU, fully offline | privacy (sales data never leaves the shop), no internet after first download, no monthly cost | [ADR-006](12-tech-stack-decisions.md#adr-006-ai-runtime-ollama-models-qwen253b-instruct--nomic-embed-text) |
| Cloud providers | **OpenAI API** (`gpt-4o-mini` default, `text-embedding-3-small`) · **Hugging Face Inference API** via its OpenAI-compatible router (`Qwen/Qwen2.5-7B-Instruct` default, `BAAI/bge-m3` embeddings) | both driven by the `openai` SDK with a different `base_url`; key stored encrypted; monthly token budget; data notice shown | works on any PC; best quality/speed; owner accepts that questions + allow-listed figures leave the shop | ADR-041, 06 §2.5 |
| Clients | `ollama` Python package · `openai` SDK · `httpx` | chat with **tool calling**, embeddings, streaming | official clients; imported lazily inside `ai/providers/` | 10 §5 |
| Local chat model (default) | **`qwen2.5:3b-instruct`** (~2 GB) | answers in English / Hindi / Hinglish, calls the app's tools to fetch numbers | good Hindi quality, native tool-calling, usable on CPU | ADR-006, 06 §2 |
| Local chat model (tiers chosen by `main.py`) | `qwen2.5:1.5b-instruct` (≤ 8 GB RAM) · `qwen2.5:3b-instruct` (16 GB, no GPU — most likely) · `qwen2.5:7b-instruct` (NVIDIA ≥ 6 GB / Apple silicon, or opt-in on 32 GB) | picked automatically from the hardware survey; changeable in Settings | fits the unknown shop PC | 06 §2 |
| Local embedding model | **`nomic-embed-text`** (default) · **`bge-m3`** (better Hindi, bigger) | turns product names / help text into vectors for search | fast on CPU; bge-m3 when Hindi typing is common | ADR-006 |
| Numbers & forecasts | deterministic **`ForecastService`** (moving average, seasonal, trend — NumPy, reference versions stdlib-only) | the LLM *narrates*, it never computes sales figures | no hallucinated totals | [ADR-017](12-tech-stack-decisions.md#adr-017-forecasting-deterministic-statistics-llm-only-narrates) |
| Guardrails | six deterministic layers + role profiles + red-team corpus (pytest) | blocks passwords/bank/code/owner-detail/fraud questions; staff limited to inventory & prices | safety does not depend on the model | [ADR-022](12-tech-stack-decisions.md#adr-022-assistant-guardrails-are-layered-and-mostly-deterministic) |
| Fast path | intent matcher without the LLM | "low stock?", "today's sale?" answered instantly | speed on CPU-only PCs | [ADR-039](12-tech-stack-decisions.md#adr-039-assistant-fast-path-deterministic-intents-before-the-llm) |

---

## 5. Hardware integration

| Device | Tools | Notes |
|--------|-------|-------|
| Barcode scanner (USB, keyboard-wedge) | browser keyboard events via NiceGUI; presence detection with **pywin32**/WMI (Windows), **hidapi** (macOS), **pyudev**/hidapi (Linux); paired by VID:PID | mandatory for staff billing (strict mode) — [ADR-016](12-tech-stack-decisions.md#adr-016-strict-mode-scanner-is-a-hard-dependency-of-staff-billing-printer-is-optional-by-default) |
| Receipt / label printer (thermal, ESC/POS) | **python-escpos** over USB (**pyusb**/libusb), network (TCP 9100), Windows spooler (**pywin32**) or CUPS (**pycups**/`lp`) | optional; hot-plug discovery; print queue when absent |
| Weighing scale | **pyserial** (RS-232/USB-serial protocols) or weight-embedded barcodes | optional |
| Barcode generation | **python-barcode** (EAN-13 image), **qrcode** (UPI QR on receipt), **Pillow** (images) | internal EAN-13 `29` + product ID + check digit — [ADR-023](12-tech-stack-decisions.md#adr-023-product-identity-permanent-id--derived-internal-ean-13-multiple-barcodes-per-product) |

All hardware packages are imported lazily inside `hardware/` adapters with `Null*` fallbacks, so a missing
driver never stops the app from starting.

---

## 6. Reports, documents, publishing

| Concern | Tool | Notes |
|---------|------|-------|
| PDF reports & label sheets | **reportlab** (primary), **weasyprint** (optional, HTML→PDF) | daily digest, price history, stock & profit reports |
| Charts in PDFs | **matplotlib** (headless PNG) | ECharts is browser-only |
| Spreadsheet export | **openpyxl** (XLSX), stdlib `csv` | owner's exports |
| Email | stdlib **smtplib** | digest to owner |
| Telegram / WhatsApp | **requests** against the official Bot API / Cloud API (**Twilio** optional) | outbox with retry; owner's choice — [ADR-018](12-tech-stack-decisions.md#adr-018-remote-publishing-via-outbox--official-channel-apis) |

---

## 7. Security

| Concern | Tool | Notes |
|---------|------|-------|
| Password hashing | **argon2-cffi** | Owner / Staff passwords, Owner PIN, recovery key — [ADR-011](12-tech-stack-decisions.md#adr-011-authentication-two-roles-username--password-owner-pin-for-overrides), [ADR-037](12-tech-stack-decisions.md#adr-037-owner-recovery-key) |
| Secrets at rest (bank/UPI details, API tokens) | **cryptography** (Fernet) with the key in the OS store via **keyring** (Windows DPAPI · macOS Keychain · Linux SecretService) | never readable by the AI layer — [ADR-026](12-tech-stack-decisions.md#adr-026-owner-secrets-encrypted-at-rest-unreachable-from-the-ai-layer) |
| Code integrity | SHA-256 release manifest + `pip --require-hashes` | detects modified program files — [ADR-036](12-tech-stack-decisions.md#adr-036-code-integrity-manifest-and-hash-pinned-dependencies) |
| Optional HTTPS on LAN | self-signed certificate (`openssl` / cryptography) on `:8443` | for tablet/phone access |

---

## 8. Installation, start-up, operations

| Concern | Tool | Notes |
|---------|------|-------|
| Installer / launcher | **`main.py`** + `bootstrap/` — **standard library only** (`venv`, `subprocess`, `urllib`, `http.server`, `getpass`, `ctypes` for UAC); **interactive wizard** (numbered questions, defaults, EN/HI, back/skip/quit, answers file) | the only program the owner runs — [17](17-install-and-run.md), [ADR-014](12-tech-stack-decisions.md#adr-014-packaging-stdlib-bootstrapper--venv-on-every-os-pyinstaller-later), [ADR-028](12-tech-stack-decisions.md#adr-028-the-application-installs-its-own-dependencies-asking-for-admin-rights-only-when-needed), [ADR-042](12-tech-stack-decisions.md#adr-042-interactive-installer-with-an-answers-file) |
| Python packages | `venv` + `pip` with a **pip-tools** hash-pinned lock; optional offline wheelhouse | reproducible on all OSes |
| System components | `winget` (Windows) · Homebrew (macOS) · `apt` (Ubuntu/Debian) for libusb, CUPS, Devanagari fonts, Ollama | elevated only for these steps |
| Autostart & watchdog | Task Scheduler · launchd · systemd `--user` | restarts on crash or hang |
| Health checks | the **doctor** (`python -m vaishnavi doctor`), plain-language EN/HI | [07 §5](07-startup-and-health-checks.md#5-pre-flight-doctor--ordered-checks) |
| Log console | stdlib mini server `:8081` during install; FastAPI `/system/logs`, `/api/v1/system/*` afterwards | [08 §9](08-logging-and-troubleshooting.md#9-log-console-over-http--rest) |
| Packaging (later) | PyInstaller single-folder build | Phase 8 option |

---

## 9. Development & quality tooling

| Purpose | Tool |
|---------|------|
| Tests | **pytest**, pytest-asyncio; fakes for printer/scanner/Ollama; **stdlib-only core tier** runnable with `python -m unittest` ([ADR-040](12-tech-stack-decisions.md#adr-040-stdlib-only-core-test-tier)) |
| Lint / format | **ruff** |
| Type checking | **mypy** (strict on `core`, `services`, `db`) |
| Architecture rules | **import-linter** (e.g. `core` may not import `ui`; only `platform/` and `bootstrap/` may test `sys.platform`) |
| Dependency lock | **pip-tools** (`pip-compile --generate-hashes`) |
| CI | GitHub Actions matrix: Windows · macOS · Ubuntu |
| Diagrams / docs | Markdown + Mermaid (this folder) |

---

## 10. What is deliberately *not* used

| Not used | Reason |
|----------|--------|
| PostgreSQL / MySQL server | excluded by requirement; SQLite fits a single-PC shop |
| React / Vue / Node toolchain | Python-only requirement; NiceGUI provides the components |
| Cloud LLM APIs as the *only* option | sales data should be able to stay in the shop; local Ollama stays the default — OpenAI / Hugging Face are an **owner-selected alternative** for weak PCs (ADR-041), never silent |
| LangChain / LiteLLM | three providers behind a ~60-line protocol do not justify heavy abstraction layers |
| Chroma / FAISS / Qdrant | thousands of vectors only — NumPy in SQLite is enough |
| Celery / Redis | one process is enough; APScheduler runs in-process |
| structlog / loguru | stdlib logging keeps one mental model with uvicorn/SQLAlchemy |
| pandas | reports are SQL → dataclasses; avoids a heavy dependency on a small PC |
| Electron / PyQt desktop shell | the browser in app mode gives the same result with no extra install |

Complete planned package list by purpose: [10 §16](10-backend-components.md#16-third-party-packages-planned).
