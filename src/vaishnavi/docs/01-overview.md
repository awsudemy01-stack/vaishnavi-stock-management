# 01 — Overview

## 1. What is Vaishnavi Store Manager (VSM)?

Vaishnavi Store Manager is a desktop-class, browser-rendered application that runs **entirely on one PC inside the shop** (Windows, macOS or Ubuntu). It replaces the "notebook + calculator + trust" way of running a counter with:

1. A **billing screen** the cashier cannot bypass, mis-price, or quietly delete from.
2. A **stock ledger** where every unit entering or leaving the shop is a recorded *movement*.
3. A **receipt** for every customer (so the customer becomes the auditor — "no bill = free item").
4. **Reports** that tell the owner, every evening, whether the cash in the drawer matches the system.
5. A **local AI assistant** (Ollama) that explains the app and answers business questions without
   any data ever leaving the shop.

No internet connection is required for any core function.

## 2. Why does a tiny shop need this?

| Pain today | How Vaishnavi Store Manager addresses it |
|------------|--------------------------|
| Cashier takes cash and does not record the sale | Receipt mandatory; **stock decreases only via a committed sale under a staff login — no other stock change is possible for staff, enforced in code and by database triggers**; stock-take vs. system reveals shrinkage |
| Cashier under-bills a friend / over-bills a stranger | Prices locked; owner-defined offers auto-applied; ad-hoc discounts capped with reasons; every shelf price, discount and final amount shown on receipt |
| Item has no barcode, cashier "types whatever" | F3 Find / Manual is allowed but every lookup/open line is marked on the receipt (`†` / `*`), under-price-checked and reported per cashier |
| Cashier deletes a bill after taking cash | Sales are **append-only** and **hash-chained**; voids need owner PIN and leave a trail |
| Owner cannot tell how much should be in the drawer | Shift open (float) → sales → shift close (count) → variance report |
| Owner doesn't know what is running out before Navratri | Min-stock alerts, seasonal top-sellers, reorder list |
| Staff don't know how to use software | In-app AI assistant trained on the user guide, Hindi-friendly |
| Staff try to misuse the assistant ("owner's bank details?", "how to crash the app?", "how to hide a sale?") | Guardrails refuse before the LLM runs; the model never holds such data; attempts are logged and the owner is alerted |
| Owner wants to know "what sells, what doesn't, what to order for Diwali" | AI assistant answers from live data + statistical forecasts; proactive nightly insights |
| Owner is at home and wants the day's picture | Daily/weekly PDF digest published to OneDrive / email / Telegram / WhatsApp (opt-in) |
| PC is switched off every night | One script performs pre-flight checks and starts everything each morning |
| "Something broke" with no information | DEBUG-level, module-tagged, rotating logs + in-app log viewer |

## 3. Personas — two login roles

| Persona | Role / login | Needs |
|---------|--------------|-------|
| **Owner** | `OWNER` — username + password, plus a short **Owner PIN** for on-counter approvals | Full control: bank/UPI details, prices & MRP, offers and discount values, products & barcodes, purchases, **manual stock updates** (opening, adjustments, damage, stock-take approval), total sales & purchases per day, full sales history, **profit & margin**, users, settings, hardware, backups, logs, application administration, the **AI assistant's instructions (system prompt)**, and an assistant that can answer **anything about the billing application** |
| **Billing staff ("cashier")** | `STAFF` — own username + password (numeric allowed) | Sell: scan, F3 Find, bill, print receipt, open/close own shift, see stock of an item, ask the assistant **inventory / price / how-to** questions only; **no** stock changes other than through a completed sale, no price edits, no offers, no deletion, no financial reports, no settings |
| **Customer** | Receives receipt | Clear itemised bill with shelf price → discount → final, taxes, date/time, shop name, UPI QR |
| **Developer / support** | Logs, docs (not a login role) | Trace any failure from log line to root cause |

Only these two roles exist in v1. Several staff accounts can exist (one per person, so every bill,
override and chat message is attributable); the permission model can grow a third role later if needed.

## 4. Scope

### In scope (v1)
- Single shop, single PC, **two roles** (Owner, Billing Staff) with one account per person; attendance (login/logout) tracking.
- Products with image, price, MRP, cost, tax, HSN, selling mode (piece / weight / volume), permanent ID ↔ code ↔ barcode(s), category, min-stock, Hindi name; price history.
- Billing by scan (strict mode), F3 Find / open item, cart with Discount column, owner-defined offers + capped ad-hoc discounts, GST breakup, Cash / UPI QR / Card / Split, receipt on 80/58 mm POS printers with PDF archive and print queue.
- Stock movements (staff: sale only; owner: all), purchases, adjustments, stock-take, low-stock alerts, barcode label generation on the POS printer.
- Shifts, cash reconciliation, audit log (hash-chained), hash-chained sales, void/refund workflow, Owner-PIN overrides.
- Sales history, reports with charts, forecast & reorder plan, CSV/Excel/PDF export, dashboard, notification centre, optional publishing to the owner's phone.
- Local AI assistant (role-specific: staff = inventory/price/how-to; owner = everything) via Ollama, guard-railed, EN/HI/Hinglish, floating chat widget.
- Cross-platform self-installing bootstrapper, pre-flight doctor in plain language, log console + metrics over HTTP/REST, two-location automatic backups with copy-back recovery, rotating logs.

### Explicitly out of scope (v1) — captured in [Roadmap](13-roadmap.md) as future
- Multi-branch / cloud sync (PostgreSQL excluded by requirement).
- Online payments gateway integration (UPI handled as *static QR + manual confirmation*).
- Full accounting (ledgers, P&L to CA standards) — only sales-side GST summaries.
- E-invoicing / GSTN upload.
- Customer loyalty programme (schema leaves room for it).

## 5. Vocabulary

| Term | Meaning in this project |
|------|-------------------------|
| **Product ID** | Permanent numeric identifier of an item (`#1042`), printed on labels and receipts; the internal barcode is derived from it |
| **Code (SKU)** | Human-readable product code, auto-generated (e.g. `CAM-0050`) |
| **Barcode** | Scannable code; the **internal EAN-13** (`29` + ID + check digit) generated by the app, and/or the manufacturer's code — a product may have several |
| **Find item (F3)** | Counter lookup by name / code / ID when a label is damaged or missing; adds a `LOOKUP` line (`†` on receipt) and can print a replacement label |
| **Movement** | Any ± change in stock: `PURCHASE`, `SALE`, `RETURN`, `ADJUSTMENT`, `DAMAGE`, `STOCK_TAKE` |
| **Invoice / Bill** | A completed sale; gets a gap-free sequential number `INV-2026-000123` |
| **Void** | Cancelling a *completed* invoice (requires Owner PIN); the original row is never deleted |
| **Shift** | Period between a cashier's drawer open (with float) and close (with count) |
| **Z-report** | End-of-day summary: sales by payment mode, taxes, voids, variance |
| **Shrinkage** | Stock that disappeared without a sale/damage record (= theft or error) |
| **Pre-flight** | The start-up checklist that must pass before the app accepts a sale; results are written for the shop owner, not a developer |
| **Bootstrapper** | `main.py` — the one program that installs, checks and starts Vaishnavi Store Manager on Windows, macOS or Ubuntu |
| **Log console** | Browser page (`/system/logs`) and REST API where the Owner watches every log live, from any device on the shop network |
| **Selling mode** | How an item is sold: by **piece**, by **weight** (grams; price per 100 g / kg) or by **volume** (ml; price per 100 ml / L) |
| **Pricing unit** | The quantity the price refers to for weighed/measured items, e.g. "₹60 per 100 g" |
| **Degraded mode** | App running without AI (Ollama down), without a printer (receipts queued), or without the backup/publishing destination — billing still works. Scanner absence is **not** degraded mode: it pauses billing for staff (strict mode) |
| **Keyboard-wedge** | USB barcode scanner that types the code followed by Enter, like a keyboard |
| **Offer** | Owner-defined, dated discount (e.g. "Diwali Offer 1 %") auto-applied at scan; cashier cannot remove it |
| **Ad-hoc discount** | Discount typed by the cashier with a reason, capped by settings; above cap needs Owner PIN |
| **Shelf price** | Catalogue price shown on the label; never overwritten — receipts show shelf price → discount → final |
| **Manual entry (F3)** | Adding an item without scanning: either **Find item** (catalogue lookup for a damaged label) or an **open item** typed free-text; always marked on the receipt (`†` / `*`) and reported |
| **Open item** | Free-text line (name, detail, qty, price) not linked to a catalogue product; no stock effect; reviewed by Owner for conversion into a product |
| **Guardrail** | Deterministic rule in the assistant that refuses out-of-scope or dangerous requests (secrets, bank details, code, bypass, fraud) before the LLM runs and reports them to the owner |
| **Strict mode** | `billing.require_scan` (default on): scanner must be present and every line scanned; `billing.require_printer` (default **off**, Owner opt-in): every sale must print. Owner-only, audited overrides |
| **Primary / Secondary location** | Primary = folder holding the live database; Secondary = different drive/folder where verified backup snapshots are written automatically; both chosen at installation |
| **Snapshot** | A complete, verified, single-file copy of the database made with SQLite's backup API — safe to copy back as `vaishnavi.db` for recovery |
| **Price history** | Append-only record of every selling-price / MRP / cost / tax-rate change per item, viewable in a pop-up chart for 1 month / 6 months / 1 year or custom dates |
| **Paired scanner** | The USB scanner (VID:PID) registered once in Settings so the doctor can detect it |
| **Digest** | Daily / weekly PDF report published to the owner (synced folder, email, Telegram, WhatsApp) |
| **Forecast** | Statistical demand estimate (moving averages + festival uplift) with a confidence band — never an LLM guess |
| **Notification centre** | The bell icon: one list of everything needing attention (low stock, backup drive missing, printer detected, pending receipts, flagged prompts, integrity) fed by alert rules |
| **Recovery key** | 12-word phrase shown once at setup; together with the computer's admin password it is the only way to reset a forgotten Owner password |
| **Onboarding wizard** | Guided first-day setup for the Owner with a persistent checklist (shop → tax → payments → staff → products → labels → opening stock → scanner → printer → backup → recovery key) |
| **Code-integrity check** | Doctor compares installed application files against the release manifest; modified files block start-up and alert the Owner |

## 6. Guiding principles

1. **Billing never depends on AI, internet, or the printer.** It **does** depend on the scanner by design ("strict mode"): no scan → no sale; only an Owner PIN can bypass it, and every bypass is logged and reported. A printer is strongly recommended — receipts auto-print when one is connected and are archived as PDF and queued when it is not; the Owner may later switch on *Require printed receipt*.
2. **Nothing is deleted, and nothing is lost.** Soft-delete products; never delete sales, movements, audit rows or price history. The database is backed up automatically to a **secondary location** chosen at installation and can be restored by the app — or by simply copying a snapshot back ([14](14-database-backup-and-recovery.md)).
3. **Every action has an actor.** No anonymous writes — a `user_id` on every mutating row.
4. **Fail loudly in logs, gracefully in UI.** DEBUG logs everywhere; cashier sees a friendly message with an error code that maps to a log line.
5. **Modular.** UI ↔ Services ↔ Repositories ↔ DB; AI and hardware are plug-ins behind interfaces.
6. **Python only, any OS.** No Node toolchain; NiceGUI renders the UI from Python. Windows, macOS and Ubuntu are equal citizens — OS differences live in one adapter package.
7. **Configurable, not hard-coded.** Tax labels, rates, receipt text, discount limits, models — all in `config/settings.toml` + DB settings.
8. **The app installs and checks itself.** The owner never runs developer commands; the bootstrapper installs what is missing (asking for the OS admin password only when truly needed and saying why), and the doctor explains any problem in plain English/Hindi before the app starts.
9. **Every operation is observable.** From the first second of installation to daily use, logs are written and can be watched live in a browser on the shop network (`/system/logs`), read-only and Owner-only.
