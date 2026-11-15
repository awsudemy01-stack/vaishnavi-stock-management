# 15 — User Guide: How to Use Vaishnavi Store Manager

Written for the people in the shop, not for developers. Two parts: **Owner** and **Billing staff**.
Screen names are given in English with the Hindi label used in the app in brackets. The AI assistant
(*Sahayak*) answers "how do I…" questions from this guide.

---

## Part A — First day (Owner)

### A1. Install and start
1. Copy the `VaishnaviStoreManager` folder to the shop PC (any folder, e.g. `Documents`).
2. Double-click **Start Vaishnavi** (`.bat` on Windows, `.command` on Mac, `.sh` on Ubuntu).
3. A black window shows progress in plain words. The first time it may take 10–20 minutes
   (it downloads the AI models). You can watch the same progress from a phone on the shop Wi-Fi at
   `http://<pc-name-or-ip>:8081`.
4. If it asks for the **computer's administrator password**, that is to install a system component
   (printer system, USB library or the AI engine). It tells you exactly why. Nothing else is changed.
5. It asks **where to keep the data**:
   - **Primary** — where the live shop database lives (default is fine).
   - **Secondary** — a **different drive** for automatic backups: a USB pen-drive you leave plugged in,
     a second disk, or a OneDrive/Google Drive folder. Do not choose the same drive; the installer will refuse.
6. The browser opens on the **Sign in** page. Choose the **Owner** tile, set your **password**, then your
   **Owner PIN** (4–6 digits — used only to approve things on the staff's screen).
7. **Write down the Recovery Key** that is shown once (12 words). Keep it at home. It is the only way to
   reset a forgotten Owner password.

### A2. Onboarding wizard (first Owner login)
The wizard walks you through, in order; you can stop and come back — the dashboard shows "Setup 6/11".

| Step | What to do |
|---|---|
| Shop profile (दुकान) | name, address, phone, GSTIN (if registered), logo |
| Tax | GST registered / composition / unregistered — ask your CA; default is GST with 0 % |
| Payments | your **UPI ID** (the QR on the receipt comes from it); optional bank details for your own reference |
| Staff accounts | one account per person; numeric password allowed |
| Products | add by hand **or** import the CSV template (name, category, price, cost, unit, tax, min stock, opening qty) |
| Labels | print barcodes for items without one (A4 sticker sheet on any printer, or the thermal printer) |
| Opening stock | enter today's counts; they become the first stock movements |
| Scanner | plug in, press **Pair scanner**, unplug/replug when asked |
| Printer (optional) | pick from the list, **Test print** |
| Backup drive | confirms the secondary location works; first backup is taken |
| Recovery key | tick "I have written it down" |

### A3. Settings you should know (Settings → सेटिंग्स)
- **Billing rules**: *Require scan* (on — keep it), *Require printed receipt* (off until you have a
  reliable printer), *Manual add policy* (Allow / Owner PIN / Block), maximum staff discount %.
- **Hardware**: printer list, scanner pairing, weighing scale.
- **Backups**: locations, *Backup now*, *Restore*.
- **AI**: language, models, *Assistant instructions* (how Sahayak talks).
- **System**: health check, log console, remote log access (shows a QR for your phone), updates.

---

## Part B — Every day (Owner)

### B1. Morning (2 minutes)
1. Switch the PC on. The app starts by itself and opens the Sign-in page.
2. Look at the top-right chips: **DB · Integrity · Scanner · Printer · AI · Backup** should be green
   (Printer may be amber if you have none yet; AI amber means the assistant is off — billing still works).
3. If something is red, the sign-in page says in plain words what to do; the **bell** (notifications)
   lists everything that needs attention.

### B2. During the day
- You can sign in on the same PC any time (staff sessions are kept) — e.g. to approve a discount, add
  a product, or look at a report.
- Staff screen asks for your **Owner PIN** when they need approval (void, big discount, cash-out). Type
  it yourself; never tell it to anyone.
- Open the **bell**: low stock, pending receipts, printer detected, flagged assistant prompts.

### B3. Evening — Day-close wizard (Shifts → Day close)
1. Every open shift is closed (staff count the drawer by denomination; you see the variance).
2. **Z-report** (today's sales, payments, taxes, voids, overrides, variance) — print or save PDF.
3. Backup is taken to the secondary drive and verified.
4. Digest is sent to your phone (if publishing is enabled).
5. *Shut down app* → switch off the PC.

### B4. Weekly / monthly
- Weekly: **Stock-take → Selected** for 10 fast or expensive items; read **Reports → Shrinkage**.
- Weekly: **Reports → Staff work history** (login/logout, unclean exits) and **Overrides**.
- Monthly: full stock-take; **Profit & margin**; check **Backups & integrity** (restore drill ✔).

---

## Part C — Billing (Staff)  (बिलिंग)

### C1. Start work
1. Tap your name tile, type your password (number pad), press **Sign in & start work**. Your login time
   is recorded.
2. If asked "You left at 18:42 yesterday without ending work — confirm?", press **Confirm** (add a note if you like).
3. Press **F11 → Open shift** and type the cash you start with (float).

### C2. Make a bill
1. **Scan** each item. The line appears with price and any offer discount.
2. **Loose items** (ghee, oil, kumkum): scan the **shelf label**, then choose a preset (50 g · 100 g · 250 g)
   or type the weight / volume; or type the **amount** (₹50 of ghee) and the app works out the quantity.
3. **Label damaged or missing?** Press **F3 Find**, type the name, code or `#ID`, check the picture,
   **Add to bill**. You can also **Print replacement label** so the next sale can be scanned.
4. **Item not in the system at all?** F3 → **Open item** tab → name, detail, quantity, price. (The owner
   may require their PIN for this.)
5. **Discount asked?** Press **F4**, enter % or ₹ and choose a reason. Above the limit the owner must
   type their PIN.
6. Press **F9 Pay**: Cash (type tendered, change shown), UPI (customer scans the QR), Card, or Split.
7. The receipt prints automatically (or is saved if there is no printer — you can show it on screen).
8. Customer changed their mind mid-bill? **F6 Hold**, serve the next person, **F7 Resume** later.

### C3. Fix a mistake
- Wrong quantity: select the line, use **+ / −** or type the quantity.
- Wrong item before paying: select the line, press **Del**.
- Already paid? Go to **My bills**, open the bill → **Void / Refund** → the owner enters their PIN.
  Or scan the **barcode printed on the receipt** to open that bill directly.

### C4. End work
1. **F11 → Close shift**: count the drawer by denomination (500 × 4, 200 × 3 …); the app shows expected vs counted.
2. Press **⏻ End work** (top-right or `Ctrl+Q`), check the exit time, **Confirm**.
3. Do **not** just close the window: if you do, you will have to confirm the exit time next morning,
   and the owner sees it as "window closed".

### C5. Ask the assistant (💬 bubble, bottom-right)
You can ask, in English or Hindi: *"kalawa ka stock?"*, *"price of camphor 50 g"*, *"which items are
low?"*, *"how do I hold a bill?"*. It cannot tell you sales totals or anything about other staff.

---

## Part D — Products & stock (Owner)  (सामान / स्टॉक)

| Task | Where | Notes |
|---|---|---|
| Add a product | Products → **New** | ID, code and barcode are generated for you; set *Selling mode* Piece / Weight / Volume |
| Print barcode labels | Products → select → **Print labels** | thermal strip, label roll or A4 sticker sheet |
| Attach the manufacturer's barcode | Product → Barcodes → **Scan to add** | both barcodes then work |
| Change a price | Product → Price → reason | recorded in **Price history** (pop-up chart: 1 m / 6 m / 1 y / custom) |
| Festival offer | Products → **Offers** → New | e.g. "Diwali 1 % on Camphor", dates; applies automatically when scanned |
| Goods received | Stock → **Purchase entry** | supplier, invoice no., qty, cost; labels for the received qty |
| Correction / damage | Stock → **Adjust / Damage** | reason required; shows in reports |
| Count the shelf | Stock → **Stock-take** | Full / category / selected; approve → shrinkage report |
| Items staff sold as "Open item" | Products → **Open-item review** | one click converts them into real products with labels |
| Reorder | Reports → **Forecast** → *Create reorder list* → PDF | send to the supplier |

---

## Part E — Reports (Owner)  (रिपोर्ट)
Every report has a date range, a chart, **Export CSV / Excel / PDF** and **Send** (to your phone if set up).

| Question | Report |
|---|---|
| How much did we sell today / this week? | Sales totals · Z-report |
| What did each staff member sell? Any shortfall in the drawer? | Cashier performance · Shifts |
| When did each person log in and out? | Staff work history |
| What is low / out of stock? What to order for Diwali? | Low stock · Forecast · Demand |
| What vanished without a bill? | Shrinkage · Stock reconciliation |
| How much profit? | Profit & margin |
| How much did we buy? | Purchases |
| Who gave discounts / typed items / overrode rules? | Discounts · Manual lines · Overrides |
| What did the assistant refuse and who asked? | Chat security |
| Price changes of an item | Price history |
| Who changed what in the system? | Audit trail |
| Are backups fine? | Backups & integrity |

---

## Part F — If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Printer chip amber "N receipts queued" | no printer / unplugged | selling continues; connect the printer → Settings → Hardware → *Print pending receipts* |
| "Scanner not detected — billing paused" | scanner unplugged | re-plug; if it is a new scanner, **Pair scanner** again |
| "AI offline" | assistant engine stopped | billing unaffected; Settings → AI → *Restart AI engine* or wait for tomorrow's start |
| "Backup drive not found — backups waiting" | USB/cloud folder missing | plug it in; backups catch up by themselves |
| "Did not close properly last time — all saved data is intact" | power cut / crash | nothing to do; if daily, send a **support bundle** (Settings → System) |
| Red **Integrity** chip | database changed outside the app or restored wrongly | open *Reports → Backups & integrity*; restore the last verified backup if needed |
| "Application files were modified" | program files on the PC differ from the release | do not use until re-installed with *Start Vaishnavi*; ask who had access to the PC |
| Forgot Owner password | — | on the PC run **Start Vaishnavi → Reset owner password**; you need the **Recovery Key** and the computer's admin password |
| The PC died | — | install on a new PC, copy the latest `vaishnavi_<date>.db` from the secondary drive's `backups` folder into the primary folder as `vaishnavi.db`, start; the app recognises the backup and tells you which period to re-enter ([14](14-database-backup-and-recovery.md)) |

Logs for a technician: `http://<pc>:8080/system/logs` (Owner login) or `http://<pc>:8081` while installing.

---

## Part G — Keeping the PC itself safe (Owner, once)
- Create a **separate, non-administrator** Windows/macOS/Ubuntu user for the shop and let the app
  auto-start in that account; keep your own administrator account for installs only.
- Let the app open in **kiosk/app mode** so staff do not see the desktop.
- Lock the BIOS boot order and set a BIOS password if the PC is in a public spot.
- Keep the backup pen-drive plugged in and take the monthly snapshot home.

---

## Part H — Hindi quick glossary (screen names)
Billing — बिलिंग · Find item — आइटम खोजें · Hold — रोकें · Pay — भुगतान · Shift — शिफ्ट · End work — काम समाप्त ·
Products — सामान · Stock — स्टॉक · Purchase — खरीद · Stock-take — गिनती · Reports — रिपोर्ट · Settings — सेटिंग्स ·
Backup — बैकअप · Owner PIN — मालिक पिन · Receipt — रसीद · Discount — छूट · Offer — ऑफ़र
