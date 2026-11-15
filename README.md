# Vaishnavi Store Manager (VSM) — Billing, Stock & AI Assistant for Vaishnavi Puja Store

> **Status: all seven phases code-complete (units 1–54, 15-Nov-2026) — ready for the first live run on the shop PC.**
> Installer (`main.py` with `--restore` / `--update`), database schema + migrations, hash-chained ledger,
> backup/restore, recovery key, doctor, start-up spine, UI shell, login + attendance, **products & stock, labels,
> POS billing (scan-first, discounts, open items, hold/resume, receipts & ESC/POS printing), shifts with blind
> count, voids/refunds, anti-theft reports, sales reports, dashboard, forecasting & reorder plan, the guard-railed
> bilingual AI assistant (Ollama / OpenAI / Hugging Face / off), report publishing (folder · e-mail · Telegram ·
> WhatsApp), the day-close wizard and purchase orders** are implemented and covered by **766 stdlib-only tests**
> that run on a bare Python interpreter. Nothing has been executed on the development PC beyond those tests; the
> first live run is the shop PC's, following the **[first-run checklist](docs/17-install-and-run.md#51-first-run-on-the-shop-pc--the-checklist)**.
> What remains needs that PC: the three-OS disaster drill, the 24-h soak, the live-model red-team run and a real
> phone receiving a digest (see the Phase 7 status note in the [Roadmap](docs/13-roadmap.md)).
> Product name: **Vaishnavi Store Manager** (short **VSM**; technical identifier `vaishnavi` — CLI `python -m vaishnavi`, data folder `~/VaishnaviStore`).

Vaishnavi Store Manager is a **100 % Python, offline-first, single-shop, cross-platform (Windows · macOS · Ubuntu)** point-of-sale and stock-management
system designed for **Vaishnavi Puja Store**, a small Indian puja-items shop (camphor, agarbatti, matchboxes, kumkum/colours,
deity dresses for Krishna / Radha / Durga, temple décor, etc.).

Its primary business goal is **cash-leak prevention**: every rupee that crosses the counter must
leave a tamper-evident trail, while keeping billing fast enough for a busy festival morning.

## What it does (at a glance)

| Area | Capability |
|------|------------|
| Billing (POS) | Scan items, cart with **Shelf price · Discount · Final** columns, named **offers** auto-applied (e.g. "Diwali Offer 1 %"), controlled ad-hoc discounts, **F3 Find / Manual** (damaged-label lookup by name / code / ID, or open item), GST (CGST + SGST) breakup, Cash / UPI / Card, gap-free invoice numbers, **auto-printed receipt** showing every discount and "You saved ₹…" |
| Strict mode | **Scanner is mandatory** for staff billing (manual lines are flagged, marked on the receipt and reported). **Printer is optional**: the app runs and staff can sell without one — receipts are archived as PDF and queued, a newly connected printer is detected and offered in Settings, and the Owner may later switch on *Require printed receipt*. Owner-PIN overrides are time-boxed, flagged and reported |
| Two roles | **Owner** (everything: bank/UPI details, prices, offers & discount values, purchases, manual stock updates, daily sales & purchase totals, full history, **profit & margin**, users, system administration, **AI instructions editor**) and **Billing Staff** (sell only: scan, bill, print, own shift, stock lookup). Password login; Owner PIN for on-counter approvals; every action attributable |
| Attendance | Every login/logout is recorded with server time; staff end work through a popup that confirms the exit time; closing the window or killing the browser is still captured (beacon + heartbeat) and must be confirmed at next login; **Reports → Staff work history** shows per person per day: app start, login, logout, duration, how it ended, bills, sales |
| Anti-theft | Append-only hash-chained sales ledger, audit log of every action, shift open/close with cash reconciliation, void/refund approval, shrinkage & override reports |
| Stock | Products with image, price, cost, HSN, tax rate, **selling mode piece / weight / volume** (100 g ghee at ₹60/100 g, 200 ml oil…), stock in base units, min-stock alerts, every stock change recorded as a movement, stock-take (cycle count); optional weighing scale and weight-embedded barcodes; **price history** per item (pop-up chart: 1 month / 6 months / 1 year / custom dates) |
| Database safety | SQLite with WAL + full sync, integrity check at every start, never corrupted by daily power-offs; **two locations chosen at install** — primary (live) and secondary (verified backup snapshots: on start, every shift close, nightly, before updates); in-app Restore wizard **or** simply copy a snapshot back — the app recognises it, migrates it and tells you what period is missing |
| Barcodes | Every item gets a permanent **Product ID ↔ code ↔ barcode**; internal EAN-13 generated automatically for new items and **printed as labels on the POS thermal printer** (or label printer / A4 sheets); manufacturer barcodes can be attached too. Damaged label? **F3 Find item** by name / code / ID → bill it and print a replacement label on the spot |
| Receipts | Designed for **80 mm / 58 mm ESC/POS POS printers**: fixed-width layout, native UPI QR and invoice barcode, Hindi via raster, auto-cut, cash-drawer kick; PDF copy archived; A4/Windows fallback |
| History & Reports | Item-wise / day-wise sales history, Z-report, demand ranking, dead stock, **forecast & reorder plan**, CSV/Excel/PDF export |
| AI Assistant | **Owner's analyst** on a **pluggable AI provider ? local Ollama (default), OpenAI API or Hugging Face API, chosen at install**: "what is today's sale?", "which items are low / out of stock?", "what is in high demand / no demand?", "what will sell for Diwali?" — figures only from read-only tools and a deterministic forecasting engine; English / हिंदी / Hinglish. **Six-layer guardrails**: cannot reveal PINs, bank/UPI/card details, owner details, code or internals; refuses and **reports to the owner** any bypass / fraud / crash / loophole request; no chit-chat |
| Remote monitoring (opt-in) | Daily digest / weekly report PDF published to a synced folder, email, Telegram or WhatsApp; Telegram commands for quick numbers from home |
| Install & daily boot | **The app installs itself** on Windows, macOS or Ubuntu: one stdlib-only `main.py` surveys the machine, installs packages and system components (asking for the OS admin password only when needed, with an explanation), **asks interactively** which AI provider to use (local Ollama ? installs it and pulls models; OpenAI / Hugging Face ? tests and stores the key; or none), asks for the primary/secondary data locations, registers autostart, then runs a **pre-flight doctor** that speaks to the shop owner in plain English/Hindi ("what is missing · what to do") before the app starts; an **onboarding wizard** guides the first day |
| Logging & log console | DEBUG-level rotating logs per module from the first second of installation, correlation IDs, plain-language error codes; **live log console in any browser on the LAN** (`http://<shop-pc>:8080/system/logs`, REST `/api/v1/system/logs`, **metrics** `/api/v1/system/metrics`, optional HTTPS), Owner-only, read-only; a mini log server on `:8081` even during install or failed starts; **alert rules → notification bell** |
| Safety extras | Owner **recovery key** (12 words) for a forgotten password; **code-integrity check** so modified program files are detected at start; idempotent checkout; hash-chained audit log; charts on every report |

## Documentation map

| # | Document | Read this when you want to know… |
|---|----------|----------------------------------|
| 01 | [Overview](docs/01-overview.md) | What the app is, who uses it, vocabulary |
| 02 | [Requirements & Anti-Theft Analysis](docs/02-requirements.md) | Functional / non-functional requirements, threat model of counter theft |
| 03 | [Architecture](docs/03-architecture.md) | Layers, components, deployment, key sequence diagrams |
| 04 | [Data Model](docs/04-data-model.md) | ERD, every table and why it exists |
| 05 | [Billing & Anti-Theft Controls](docs/05-billing-and-anti-theft.md) | Invoice flow, tax engine, hash chain, shifts, voids, reports |
| 06 | [AI Assistant](docs/06-ai-assistant.md) | Provider choice (Ollama local ? OpenAI ? Hugging Face ? none), models, RAG, tool-calling, guard-rails |
| 07 | [Installation, Startup & Health Checks](docs/07-startup-and-health-checks.md) | Cross-platform self-install, elevation policy, daily boot, pre-flight doctor, degraded modes |
| 08 | [Logging & Troubleshooting](docs/08-logging-and-troubleshooting.md) | Log layout, formats, **log console over HTTP/REST**, how to trace a problem to root cause |
| 09 | [Frontend Guide](docs/09-frontend-guide.md) | Every screen, option and keyboard shortcut |
| 10 | [Backend Components](docs/10-backend-components.md) | Package layout, module responsibilities, interfaces |
| 11 | [Hardware Guide](docs/11-hardware-guide.md) | Scanner, printer, label printing, cash drawer, weighing scale, per-OS notes |
| 12 | [Tech-Stack Decisions (ADRs)](docs/12-tech-stack-decisions.md) | Why NiceGUI, SQLite, SQLAlchemy, pluggable AI provider, interactive installer, etc. |
| 13 | [Roadmap](docs/13-roadmap.md) | Phased delivery plan, milestones, acceptance criteria |
| 14 | [Database Protection, Backup & Recovery](docs/14-database-backup-and-recovery.md) | Why SQLite is safe here, primary/secondary locations, backup schedule, **how to recover (including copying a snapshot back by hand)** |
| 15 | [User Guide](docs/15-user-guide.md) | **How to use it** — first day, daily routine, billing step by step, products & stock, reports, troubleshooting, Hindi glossary (also the assistant's help corpus) |
| 16 | [Design Review & Enhancements](docs/16-design-review-and-enhancements.md) | Requirement traceability, inconsistencies fixed, gaps closed, enhancements (UI, DB consistency, metrics/alerts, charts, reports, chat, security), open questions, build-readiness |
| 17 | [How to Install and Run](docs/17-install-and-run.md) | **Start here on the shop PC** — the single entry point `main.py`: prerequisites, first run stage by stage, daily start, every command/flag, watching logs, what to do on failure, offline install, moving PC |
| 18 | [Technology Stack at a Glance](docs/18-technology-stack.md) | **What it is built with** — frontend, backend, database, AI models, hardware, reports, security, installer, dev tooling; what is deliberately not used |
| — | [Mock-ups](docs/mockups/) | Approved HTML mock-ups: [login](docs/mockups/login.html), [billing](docs/mockups/pos.html) |

## Environments: where code is written vs. where it runs

| | Development PC (this repo is edited here) | **Target: owner's personal shop PC** |
|---|---|---|
| Purpose | Authoring code & docs only | Runs the app daily |
| Constraints | Company machine: **no Ollama, no package installs, nothing executed here** | Full control; **Windows 10/11, macOS 12+ or Ubuntu 22.04+** |
| Specs | irrelevant to the design | **Unknown until `main.py` runs** — it detects OS, RAM / GPU / printers / ports and picks models & settings accordingly |

Consequences for the implementation:
- Code is written to be **importable without optional dependencies present** (hardware, AI, publishing adapters are lazy-imported behind interfaces with `Null` fallbacks).
- Verification happens on the target: `python3 main.py` → doctor → `vaishnavi selftest` (runs the unit-test suite with fakes, then live checks). Nothing is assumed to have been executed on the development PC. CI runs the suite on all three OSes.
- OS differences are confined to `vaishnavi/platform/` adapters and the bootstrapper; the rest of the code never checks the OS.
- Model choice is a table in [06 §2](docs/06-ai-assistant.md#2-ai-provider-and-model-selection), not a hard-coded value.

## Quick start (run on the shop PC)

Same on every OS — double-click the wrapper for your system (`Start Vaishnavi.bat` · `Start Vaishnavi.command` ·
`start-vaishnavi.sh`) or run:

```bash
python3 main.py          # first run: installs everything (asks for admin password only if system parts are needed)
                         # later runs: checks everything in ~3 s, then starts the app and opens the browser
python3 main.py --doctor # health check only, plain language (EN / HI)
python3 main.py --selftest   # the shipped test suite + live checks on this PC — the acceptance gate
python3 main.py --where  # where is my data / database / logs / backups?
```

Watch what is happening from any browser on the shop network:
`http://<shop-pc>:8081` during installation, `http://<shop-pc>:8080/system/logs` afterwards (Owner login).

Full instructions — prerequisites, every stage of the first run, the **first-run checklist**, all `main.py` flags,
offline install and recovery — are in [17 — How to Install and Run](docs/17-install-and-run.md).

### Developers: running the tests here

The Phase-1 test tier needs nothing but the standard library (`python -m unittest discover -s tests/core -t .`);
it runs on Windows, macOS and Linux with the system Python 3.11+. Tests that need a third-party wheel skip themselves.

See [Roadmap](docs/13-roadmap.md) for what gets built in which order.
