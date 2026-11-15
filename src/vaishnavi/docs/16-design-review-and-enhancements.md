# 16 — Design Review, Requirement Traceability & Enhancements

**Purpose.** Before any code is written, this document records a full re-read of docs 01–15 against
every requirement the owner gave (in order, across all conversation turns), the inconsistencies found
and fixed, the gaps found and closed, and the enhancements added with their rationale. It is the
"are we building the right thing?" gate for Phase 1.

## 1. Requirement traceability matrix

Every statement the owner made, where it is designed, and the verdict of this review.

| # | Owner's words (condensed) | Designed in | Verdict |
|---|---|---|---|
| 1 | Local puja shop; staff at counter; no scanner/barcodes/stock app yet | 01, 02 §1, 11 | ✔ |
| 2 | Staff must not be able to steal; every sale tracked; scan before sell | 05 §5, §5a (triggers), §10; 02 §4 threat model | ✔ |
| 3 | Receipt with qty, price, name, taxes, date, time, shop name | 05 §2, §8; FR-D8 | ✔ (GST labels configurable; CA to confirm rates) |
| 4 | Stock app with current qty, price, image | 04, 09 §5–6 | ✔ |
| 5 | SQLite, not Postgres | 12 ADR-002, 14 | ✔ |
| 6 | History of item sales | 05 §9, 09 §7 | ✔ |
| 7 | Professional commercial frontend + chatbot on local Ollama; suggest LLM + embedding models | 06 §2, 09, mock-ups | ✔ |
| 8 | Extensive debug logging; trace root cause from logs | 08 | ✔ |
| 9 | Python only | 12 ADR-001 | ✔ |
| 10 | Check environment/packages before running; daily restart: DB, Ollama, models up; check scanner etc. | 07 | ✔ |
| 11 | Neat, modular, commercial code; Mermaid diagrams; multiple markdowns (what it is, how to use, frontend options, backend components) | 01, 03, 09, 10, **15 (new)** | ✔ — "how to use" was missing → **doc 15 added** |
| 12 | AI chat = owner's analyst: current sales, low/out of stock, demand, forecast | 06 §1, §3 | ✔ |
| 13 | Scanner mandatory; printer optional (revised) | 05 §10, 11 §0 | ✔ (02 R2 wording was stale → fixed) |
| 14 | Owner monitors from home; PDF by mail/WhatsApp (nice-to-have) | 05 §11, 10 publishing | ✔ |
| 15 | Discount column: shelf ₹10 → 1 % → ₹9, printed on receipt | 05 §8a, mock-up | ✔ |
| 16 | Manual add when no barcode (name, detail, qty, price, discount) | 05 §8b–8c | ✔ |
| 17 | Dev PC is a company machine — nothing runs here; target is the shop PC | README, 12 ADR-021 | ✔ |
| 18 | Chat guardrails (no passwords, bank, code, owner details, nuisance); Hindi | 06 §7, §1a | ✔ |
| 19 | Generate barcode for new items; ID ↔ name ↔ barcode; find by name/code/ID | 05 §8b, 11 §2 | ✔ |
| 20 | Barcodes & receipts print on the POS printer | 11 §2–3, 12 ADR-024 | ✔ |
| 21 | Two users: staff (sell) and owner (bank, prices, discounts, purchases, sales, history, profit, admin, AI prompt) | 05 §5, 06 §1a, 09 §10 | ✔ |
| 22 | Weighed/measured items (100 g ghee, 200 ml oil) | 05 §8d, 11 §8 | ✔ |
| 23 | OS-independent; app installs everything, asks admin password first; human-readable errors | 07, 12 ADR-027/028 | ✔ |
| 24 | Log console over HTTP/REST from a separate browser | 08 §9 | ✔ |
| 25 | DB protected; survives daily restarts; auto-backup to a secondary location chosen at install; copy-back recovery must work | 14 | ✔ |
| 26 | Printer not a mandatory check; detect when installed; choose it in app | 05 §10, 11 §0 | ✔ |
| 27 | Price history 30 d / 6 m / 1 y in a pop-up chart | 05 §9a, 09 §5 | ✔ |
| 28 | Crash/hang: catalogue must never be lost; sales must match inventory | 14 §2, §2a | ✔ |
| 29 | Stock changes only via staff sale; owner may edit manually | 05 §5a | ✔ |
| 30 | AI: staff = inventory/price only; owner = anything about the app | 06 §1a | ✔ |
| 31 | Attendance: login time, End-work popup, owner sees work history | 05 §6a | ✔ |
| 32 | Mock-up decisions: user tiles, staff keypad, health panel, saffron, tiles with stock counts, **no Hindi on billing screen**, chat bubble bottom-right, name *Vaishnavi Puja Store*, product *Vaishnavi Store Manager* | 09 §2, §4, mock-ups | ✔ (one Hindi-name typo in 09 → fixed) |
| 33 | Delivery practice: skeleton first, one small module at a time, owner confirmation before every unit | 13 Working agreement, §7 below | ✔ |
| 34 | One root `main.py` doing everything start to bottom (OS check, libraries, packages, components, install, set-up, start) + a markdown on how to install and run | **17 (new)**, 07 §1–2, 10 §2, 03 §2, 02 R31 | ✔ — entry point renamed `bootstrap.py` → `main.py` everywhere; stages 0–12, resumable; offline bundle added |
| 35 | Shop PC may lack GPU/RAM for Ollama — at install ask: **OpenAI API**, **Hugging Face API**, or **local Ollama** | 06 §2 (rewritten), 02 R32 + FR-H14, 07 §3, 10 §2/§5, 04 AI tables, 08 E-AI codes, 12 ADR-041, 17 §3.2, 18 §4 | ✔ — pluggable `LLMProvider` (ollama · openai · huggingface · none); hardware-based recommendation; key tested then stored in `secure_setting`; data notice + monthly budget; same guardrails/fast path for all; switchable in Settings |
| 36 | Installation must be **user-interactive** | 07 §1 (2a), 02 FR-I9, 10 §2 `wizard.py`, 12 ADR-042, 17 §3.1 | ✔ — numbered questions with defaults, EN/HI, back/skip/quit, resumable; only 5 decisions asked, rest detected; answers file for unattended re-install |
| 37 | API key: interactive entry or "edit this file"? → **interactive paste first; env var / `secrets.env` only as a one-time import (file deleted after); never a key in a config file** | 06 §2.5, 17 §3.2, 10 §2 `ai_provider.py`, 08 `E-SEC-005/006`, 12 ADR-041 | ✔ |

**Conclusion:** no requirement is unaddressed. Two things the owner asked for were only implicitly
covered and are now explicit: a task-based **user guide** (doc 15) and a **graphs/charts** strategy (§4.4).

## 2. Inconsistencies found in this review (all fixed in this pass)

| Doc | Was | Now |
|---|---|---|
| 01 §1 | "runs entirely on one Windows PC" | one PC — Windows, macOS or Ubuntu |
| 01 §4 | in-scope list missing weighed items, offers, price history, attendance, log console, two-location backups | list completed |
| 02 R2 | "manual add needs Owner PIN" | manual add per policy (default ALLOW), flagged & reported |
| 02 §5 | "Strict mode (scan + print mandatory)"; duplicate "digest (future)" row | scanner mandatory, printer optional; duplicate removed |
| 05 §4 | hash canonical missing shelf price / offer / entry method / base-unit qty | canonical updated |
| 05 §6 | "F12 Close shift" (F12 is Lock) | F11 Shift menu |
| 05 §8 | receipt header "VAISHNA PUJA STORE"; kumkum priced per gram; path `data/receipts` | corrected; pricing-unit format; `<primary>/receipts/` |
| 05 §10 | "shelf label encodes the SKU" | encodes the internal EAN-13 |
| 06 | "drawer" wording; "two-part prompt"; `price_history` range `30d` | widget; three-part; `1m` |
| 09 §2 | Hindi shop name typo | वैष्णवी पूजा स्टोर |
| 09 §4 | garbled ASCII cart sketch | clean sketch matching the approved mock-up |
| 09 nav | "Users & PINs" | Users & passwords |
| 10 | section numbers out of order; `seed.py` "admin" | renumbered; "Owner account" |
| 11 §1 | "appear to Windows as keyboards" | to the operating system |
| 13 Gantt | "strict mode (scanner+printer)" | scanner mandatory · printer optional |
| 14 §7 | "backups as per §3" | §4 |
| 04 | `hardware_override.admin_user_id` | `owner_user_id` |

## 3. Gaps found and closed

| Gap | Why it matters | Closed by |
|---|---|---|
| No task-oriented **"how to use"** document | the brief asked for it; also the corpus the assistant answers how-to questions from | **doc 15 — User Guide** (owner & staff tasks, EN with Hindi screen names); indexed by the assistant |
| **Owner forgets the Owner password** — there is no role above Owner | shop would be locked out of its own data | **Recovery key**: 12-word phrase shown once at setup (printable), stored hashed; `main.py --reset-owner-password` needs the key **and** OS admin elevation; audited; doctor WARNs if the key was never acknowledged |
| **Double-submitted checkout** (double-click, websocket retry) could create two invoices | duplicate sales = wrong stock and wrong cash | **Idempotent checkout**: UI mints a `client_txn_id` (ULID) per cart; `sale.client_txn_id` is UNIQUE; a retry returns the existing invoice |
| **Staff modifying the application files** on the PC ("manipulate the code so the owner couldn't know") | the owner explicitly feared this | **Code-integrity check**: a signed manifest of file hashes ships with each release; the doctor verifies installed files and hash-pins dependencies (`pip --require-hashes`); mismatch → red banner "Application files were modified", app refuses to start until the Owner re-installs/confirms; audited |
| **Go-live is hard**: hundreds of items to enter, labels to print, opening stock to set | without a guided path the owner may give up or skip strict mode | **Onboarding wizard** on first Owner login with a persistent checklist (shop → tax → payments → staff → products/import → labels → opening stock → scanner → printer → backup drive → recovery key) |
| Alerts scattered across chips, toasts and digest | owner misses things | **Notification centre** (bell icon, per-user, read/unread) fed by an **alert-rules** engine |
| Audit log itself could be edited by someone with DB access | weakens "who did what" | audit log is **hash-chained** like sales and verified hourly |
| Owner PIN brute-force by staff | PIN is short by design | 3 wrong Owner PINs → 10-min lock + owner notification + audit |
| Reports mention charts but no charting approach | "graphs" requested | §4.4 charting strategy + ADR-034 |
| Returns via receipt barcode mentioned but no flow | friction | *Sales history → scan receipt* starts the refund flow |
| Receipt PDFs / images grow forever | disk | retention: receipts 2 financial years, images ≤ 400 KB each (resized) |

## 4. Enhancements added in this pass (with rationale)

### 4.1 Application interface
| Enhancement | Rationale | Phase |
|---|---|---|
| **Onboarding wizard** + setup checklist card on the dashboard | first week is where POS projects fail | 2 |
| **Notification centre** (bell): low stock, backup drive missing, printer detected, pending receipts, flagged chat prompts, unconfirmed session, insights, integrity | one place instead of five | 2 |
| **Day-close wizard** (Owner): close open shifts → Z-report → variance review → backup → digest → shut down | makes the evening routine a 2-minute guided flow | 5 |
| **Reorder list → Purchase-order PDF** (from Forecast/Low stock) to send to a supplier | turns forecasts into an action | 5 |
| **Audit trail viewer** (*Reports → Audit trail*): who / what / when, before→after diff | the audit log existed but had no screen | 5 |
| **Export everything** (ZIP of CSVs) | data portability, CA requests | 5 |
| Receipt barcode → **scan to start a return** | speed, fewer typing errors | 4 |

### 4.2 Database consistency
| Enhancement | Rationale | Phase |
|---|---|---|
| **Idempotent checkout** (`client_txn_id` UNIQUE) | prevents duplicate invoices on retries | 3 |
| **`product_stock` cache maintained by a DB trigger** (`AFTER INSERT ON stock_movement`), not by app code | cache can never drift from the ledger | 1 |
| **`stock_daily_balance`** (end-of-day qty per product, written by the nightly job) | cheap "stock as of date" reports and faster reconciliation | 5 |
| **`row_version` on product** (optimistic concurrency) | owner editing the same product in two tabs | 2 |
| **CHECK constraints** on money/qty/status columns | invariants hold even for raw inserts | 1 |
| **Migration dry-run** on a temp copy before touching the live DB | a failed migration can never leave the live DB half-upgraded | 1 |
| **Audit log hash chain** | tamper-evident history of actions, not just sales | 4 |
| Reconciliation extended to `stock_daily_balance` | three-way check | 5 |

### 4.3 Log monitoring over HTTP / API
| Enhancement | Rationale | Phase |
|---|---|---|
| **`/api/v1/system/metrics`** (JSON + Prometheus text): bills/hour, checkout latency p50/p95, errors/min, queue sizes (print, backup, publish), DB size, backup age, AI latency, heartbeat ages | standard shape; `/system/status` draws live charts from it | 1 |
| **Alert rules** (`config/alerts.toml`): backup age > 24 h, integrity fail, errors/min > N, heartbeat lost, disk < 2 GB, printer queue > 20, flagged prompts ≥ threshold → notification centre + digest/Telegram | turns logs into actions | 2 |
| **Access log with correlation id, user and ms for every HTTP request** (including the console itself) | complete trace | 1 |
| **Code-integrity result** exposed on `/system/status` | owner sees "application files intact ✔" | 1 |
| **Date-range support bundle** download | remote support | 7 |

### 4.4 Graphs & visual analytics (charting strategy — new)
- **Library:** NiceGUI's built-in **ECharts** (`ui.echart`) for all interactive charts — pure Python
  configuration, no JS build step, touch-friendly, PNG export; **matplotlib** (headless) renders the
  same series as PNG for PDF digests and receipts-free reports. One `charts.py` component builds
  consistent specs (saffron palette, Indian number formatting, dark-mode aware).
- **Chart catalogue:**

| Screen | Chart | Insight |
|---|---|---|
| Dashboard | sales trend (7/30 days), **hourly heatmap** (day × hour) | when customers come → staffing, opening hours |
| Dashboard | payment-mode donut, category share donut | cash exposure, mix |
| Reports → Sales totals | daily/weekly/monthly bars with compare-period overlay | growth, festival spikes |
| Reports → Demand | top-N bars, dead-stock value bars | reorder vs stop buying |
| Reports → Forecast | history line + forecast band + festival shading | confidence at a glance |
| Reports → Stock | days-of-cover bars (red < lead time) | what runs out first |
| Price history pop-up | step chart price/MRP/cost + offer shading | realised price explained |
| Reports → Profit | margin % by category, loss-making lines | pricing decisions |
| Reports → Cashier | bills/hour per staff, variance trend, discounts per staff | performance & risk |
| Staff work history | timeline bars per day (login→logout, idle hatched) | attendance at a glance |
| Backups & integrity | backup-age sparkline, unclean-shutdown counts | ops health |
| `/system/status` | metrics sparklines (errors/min, latency, queues) | live health |
- Every chart has *Export PNG* and feeds the PDF digest; charts are **Owner-only** except the staff's own-shift sparkline.

### 4.5 Reports
| Enhancement | Rationale | Phase |
|---|---|---|
| Hourly sales heatmap; weekday profile | staffing and opening-hours decisions | 5 |
| **Basket analysis** (items bought together, simple co-occurrence) | puja kits / cross-placement | 5 |
| GSTR-1 B2C CSV export format specified | CA hand-off | 5 |
| Stock-as-of-date report (from `stock_daily_balance`) | "what did we have on 1-Oct?" | 5 |
| Expiry report (optional per-purchase `expiry_date` for ghee/oil/edibles) | waste reduction | 7 (optional) |

### 4.6 Chat assistant
| Enhancement | Rationale | Phase |
|---|---|---|
| **Fast path (no LLM)**: deterministic intents — "stock camphor", "price ghee", "low stock", "aaj ka total" — answered directly from tools in < 100 ms, with EN/HI templates; also the **offline mode** when Ollama is down | instant answers at the counter; AI outage still useful | 6 |
| Index **doc 15 (user guide)** for how-to answers (in addition to 09) | better help | 6 |
| Quick-question chips per role already designed; add **"Ask about this item"** button on F3 Find and product cards | context shortcut | 6 |
| Thumbs-down → optional one-line reason, surfaced to Owner in *Chat quality* list | tune instructions | 6 |

### 4.7 Security & operations
| Enhancement | Rationale | Phase |
|---|---|---|
| Owner **recovery key** (12 words) | lock-out protection | 1 |
| **Code-integrity manifest** + **hash-pinned dependencies** | tamper detection, supply-chain safety | 1 |
| Owner PIN lockout + alert | brute-force | 4 |
| Audit hash chain | tamper-evident actions | 4 |
| OS hardening checklist in the user guide: separate non-admin OS account for the shop, app files read-only to it, auto-login into kiosk, BIOS boot order | staff cannot bypass the app at OS level | 15 |
| Session cookies `HttpOnly`/`SameSite=Strict`; API tokens scoped read-only | web hygiene | 1 |

## 5. How the implementation will be verified (tooling available to me)

| Capability | How it will be used |
|---|---|
| **Static analysis (Pylance / mypy / ruff)** through the Python fact-grounded-coding, type-annotation and refactoring skills | every module fully typed; diagnostics must be zero before a phase closes; no execution needed on the dev PC |
| **Stdlib-only core test tier** (new design rule) | `TaxEngine`, `measure`, EAN-13, hash chain canonicalisation, invoice formatting, guardrail classifier, money formatting, forecast methods (NumPy-free reference implementations) are written with **no third-party imports**, so their tests can run with the bare system Python — making it *possible* to verify the business core on the development PC **without installing anything**, if the owner permits running `python -m unittest` here (see open question Q1) |
| **Full test suite** (pytest, NiceGUI `User` fixture, golden receipts, kill-loop, red-team) | runs on the shop PC via `vaishnavi selftest` and in CI on three OSes |
| **Browser tooling** | screenshots of real NiceGUI pages on the shop PC for visual acceptance (same approach as the mock-ups) |
| **Problems / usages / semantic search** in the editor | refactoring safety, dead-code detection |

## 6. Open questions for the owner (answers not required to start Phase 1)

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | May I run **stdlib-only unit tests** with the existing Python on this development PC (no installs, no network)? | No — everything verified on the shop PC only |
| Q2 | GST status of the shop (registered / composition / unregistered) and rates per category — to be confirmed with the CA | tax scheme GST, default rate 0 %, labels CGST/SGST, GSTIN blank |
| Q3 | Shop address, phone, UPI ID for the receipt | placeholders; entered in the onboarding wizard |
| Q4 | Is a second counter/tablet likely in the first year? | No — LAN binding stays off |
| Q5 | Should staff see **today's bill count/total for their own shift** on their dashboard card? | Yes (own shift only) |
| Q6 | Expiry tracking for ghee/oil — wanted in v1? | Optional field, off by default |

## 7. Build-readiness checklist for Phase 1

- [x] Requirements traced (§1) and docs reconciled (§2)
- [x] Data model final for Phase 1 tables incl. `client_txn_id`, `product_stock`, `notification`, audit chain, CHECK constraints
- [x] Error-code areas and plain-language message files planned (EN/HI)
- [x] Stdlib-only core tier identified; third-party boundaries defined (import-safety rule)
- [x] Cross-platform bootstrapper steps and elevation policy defined
- [x] Backup/recovery behaviour defined and testable
- [x] Log console + metrics endpoints defined
- [x] Login mock-up approved; POS mock-up approved
- [x] **Delivery practice fixed**: directory skeleton first, then one small module/slice at a time, each fully complete (code + logging + tests + docs) before the next — see [13 — Working agreement](13-roadmap.md#working-agreement-for-implementation-phases)
- [x] **Confirmation gate fixed**: every unit — including the skeleton — starts only after the owner explicitly confirms; until then, no code
- [ ] Owner says **"go"**

Phase 1 will therefore start with **only** the directory tree and placeholder files, followed by a
Phase-1 unit checklist (roughly 20–25 small units, e.g. `core/money`, `core/errors` + messages,
`core/logging`, `core/settings`, `platform/paths`, …, `main.py` survey step, …, `db/engine`,
`0001_initial` migration, …, `ui/login`), and each unit will be delivered and reported separately.
The working rhythm per unit is: *owner confirms → unit implemented completely → status note → stop
and wait for the next confirmation.*

Phase 1 will produce, file by file: `pyproject.toml`, `main.py` + `bootstrap/` (stdlib: cli, stages,
survey, python_env, pkg_manager, elevate, locations, ollama_install, offline, autostart, minilog,
handover, messages), OS wrappers, `src/vaishnavi/{core,platform,bootstrap,db,services(user, work_session skeleton,
settings, backup, audit),api(system),ui(login, shell, status chips),scheduler}`, Alembic `0001_initial`
with triggers and CHECK constraints, seed, `config/*.toml` templates, `messages/{en,hi}.toml`,
`tests/` (core tier + fakes), `CHANGELOG.md`, and the "first run on the shop PC" checklist — which is
[17 — How to Install and Run](17-install-and-run.md), kept in step with what is actually implemented.
