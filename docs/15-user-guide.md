# 15 â€” User Guide: How to Use Vaishnavi Store Manager

Written for the people in the shop, not for developers. Two parts: **Owner** and **Billing staff**.
Screen names are given in English with the Hindi label used in the app in brackets. The AI assistant
(*Sahayak*) answers "how do Iâ€¦" questions from this guide.

---

## Part A â€” First day (Owner)

### A1. Install and start
1. Copy the `VaishnaviStoreManager` folder to the shop PC (any folder, e.g. `Documents`).
2. Double-click **Start Vaishnavi** (`.bat` on Windows, `.command` on Mac, `.sh` on Ubuntu).
3. A black window shows progress in plain words. The first time it may take 10â€“20 minutes
   (it downloads the AI models). You can watch the same progress from a phone on the shop Wi-Fi at
   `http://<pc-name-or-ip>:8081`.
4. If it asks for the **computer's administrator password**, that is to install a system component
   (printer system, USB library or the AI engine). It tells you exactly why. Nothing else is changed.
5. It asks **where to keep the data**:
   - **Primary** â€” where the live shop database lives (default is fine).
   - **Secondary** â€” a **different drive** for automatic backups: a USB pen-drive you leave plugged in,
     a second disk, or a OneDrive/Google Drive folder. Do not choose the same drive; the installer will refuse.
6. The browser opens on the **Sign in** page. Choose the **Owner** tile, set your **password**, then your
   **Owner PIN** (4â€“6 digits â€” used only to approve things on the staff's screen).
7. **Write down the Recovery Key** that is shown once (12 words). Keep it at home. It is the only way to
   reset a forgotten Owner password.

### A2. Onboarding wizard (first Owner login)
The wizard walks you through, in order; you can stop and come back â€” the dashboard shows "Setup 6/11".

| Step | What to do |
|---|---|
| Shop profile (à¤¦à¥à¤•à¤¾à¤¨) | name, address, phone, GSTIN (if registered), logo |
| Tax | GST registered / composition / unregistered â€” ask your CA; default is GST with 0 % |
| Payments | your **UPI ID** (the QR on the receipt comes from it); optional bank details for your own reference |
| Staff accounts | one account per person; numeric password allowed |
| Products | add by hand **or** import the CSV template (name, category, price, cost, unit, tax, min stock, opening qty) |
| Labels | print barcodes for items without one (A4 sticker sheet on any printer, or the thermal printer) |
| Opening stock | enter today's counts; they become the first stock movements |
| Scanner | plug in, press **Pair scanner**, unplug/replug when asked |
| Printer (optional) | pick from the list, **Test print** |
| Backup drive | confirms the secondary location works; first backup is taken |
| Recovery key | tick "I have written it down" |

### A3. Settings you should know (Settings â†’ à¤¸à¥‡à¤Ÿà¤¿à¤‚à¤—à¥à¤¸)
- **Shop & GST** (first tab): shop name and address as printed on bills, **GST registered** on/off, your
  **GSTIN**, the default GST % for new items, and whether shelf prices include GST. Each item's own GST %
  is set in Products; the bill splits it into CGST + SGST for you.
- **Billing rules**: *Require scan* (on â€” keep it), *Require printed receipt* (off until you have a
  reliable printer), *Manual add policy* (Allow / Owner PIN / Block), maximum staff discount %.
- **Hardware**: printer list, scanner pairing, weighing scale.
- **Backups**: locations, *Backup now*, *Restore*.
- **AI**: language, models, *Assistant instructions* (how Sahayak talks).
- **System**: health check, log console, remote log access (shows a QR for your phone), updates.

---

## Part B â€” Every day (Owner)

### B1. Morning (2 minutes)
1. Switch the PC on. The app starts by itself and opens the Sign-in page.
2. Look at the top-right chips: **DB Â· Integrity Â· Scanner Â· Printer Â· AI Â· Backup** should be green
   (Printer may be amber if you have none yet; AI amber means the assistant is off â€” billing still works).
3. If something is red, the sign-in page says in plain words what to do; the **bell** (notifications)
   lists everything that needs attention.

### B2. During the day
- You can sign in on the same PC any time (staff sessions are kept) â€” e.g. to approve a discount, add
  a product, or look at a report.
- Staff screen asks for your **Owner PIN** when they need approval (void, big discount, cash-out). Type
  it yourself; never tell it to anyone.
- Open the **bell**: low stock, pending receipts, printer detected, flagged assistant prompts.

### B3. Evening â€” Day-close wizard (Shifts â†’ Day close)
1. Every open shift is closed (staff count the drawer by denomination; you see the variance).
2. **Z-report** (today's sales, payments, taxes, voids, overrides, variance) â€” print or save PDF.
3. Backup is taken to the secondary drive and verified.
4. Digest is sent to your phone (if publishing is enabled).
5. *Shut down app* â†’ switch off the PC.

### B4. Weekly / monthly
- Weekly: **Stock-take â†’ Selected** for 10 fast or expensive items; read **Reports â†’ Shrinkage**.
- Weekly: **Reports â†’ Staff work history** (login/logout, unclean exits) and **Overrides**.
- Monthly: full stock-take; **Profit & margin**; check **Backups & integrity** (restore drill âœ”).

---

## Part C â€” Billing (Staff)  (à¤¬à¤¿à¤²à¤¿à¤‚à¤—)

### C1. Start work
1. Tap your name tile, type your password (number pad), press **Sign in & start work**. Your login time
   is recorded.
2. If asked "You left at 18:42 yesterday without ending work â€” confirm?", press **Confirm** (add a note if you like).
3. Press **F11 â†’ Open shift**: count the notes and coins in the drawer and type how many of each
   (500 Ã— 2, 100 Ã— 5 â€¦). The total appears as you type. Empty drawer? Type 0 in any box.

### C2. Make a bill
1. **Scan** each item. The line appears with price and any offer discount. You can also tap an item
   **tile** on the left (the owner may lock tiles so that everything is scanned). Tapping a **ghee / oil**
   tile opens the weight box â€” choose 100 g Â· 250 g Â· 500 g Â· 1 kg, or type the weight or the â‚¹ amount.
2. **Loose items** (ghee, oil, kumkum): scan the **shelf label**, then choose a preset (50 g Â· 100 g Â· 250 g)
   or type the weight â€” "500", "0.5 kg" and "250 g" all work; or type the **amount** (â‚¹50 of ghee) and
   the app works out the quantity. Stock goes down by exactly that weight.
3. **Label damaged or missing?** Press **F3 Find**, type the name, code or `#ID`, check the picture,
   **Add to bill**. You can also **Print replacement label** so the next sale can be scanned.
4. **Item not in the system at all?** F3 â†’ **Open item** tab â†’ name, detail, quantity, price. (The owner
   may require their PIN for this.)
5. **Discount on one item?** Select the line, press **F4**, enter % or â‚¹ and choose a reason.
   **Discount on the whole bill** (customer is unhappy, regular customer, bargaining)? Press **F2 Bill
   discount** (or tap the *No bill discount* line under the buttons) â†’ pick the customer type
   (Regular / Premier / Festival / Round-off) â†’ % or â‚¹. The receipt then shows *Extra discount â€” Premier
   customer* and "You saved another â‚¹â€¦ as our Premier customer". Above the limit the owner must type their PIN.
6. Press **F9 Pay**: **Cash** (tap the note the customer gave or type it â€” change is shown), **UPI**
   (the QR appears on screen â€” the customer scans it; note the reference if you like), **Card**, or
   **Split** (type how much by cash / UPI / card; the remaining amount is shown).
7. The receipt prints automatically (or is saved if there is no printer â€” you can show it on screen).
   **Customer wants the bill on WhatsApp?** (only if the owner switched this on in Settings â†’ Shop & GST)
   Type their mobile number in the box on the "Bill done" screen (or press **F8 Customer** earlier, while
   billing) and press **Send bill on WhatsApp** â€” WhatsApp opens on this PC with the bill ready; press
   *Send* there. The number is remembered, so next time the name shows up.
8. Customer changed their mind mid-bill? **F6 Hold**, serve the next person, **F7 Resume** later.
9. **A parcel of stock arrived and the owner is away?** Stock â†’ **Goods received**: pick the item, type
   how many came (in the item's unit), and a note (who delivered / bill number). The time and your name
   are recorded and the owner is told. You can only *add* stock here.

### C3. Fix a mistake
- Wrong quantity: select the line, use **+ / âˆ’** or type the quantity.
- Wrong item before paying: select the line, press **Del**.
- Already paid? Go to **My bills**, open the bill â†’ **Void / Refund** â†’ the owner enters their PIN.
  Or scan the **barcode printed on the receipt** to open that bill directly.

### C4. End work
1. **F11 â†’ Close shift**: count the drawer by denomination (500 Ã— 4, 200 Ã— 3 â€¦) and type the **UPI total
   shown by the phone app** (and card, if any). Once your count is in, the app shows the working:
   *opening float + cash sales + cash in âˆ’ cash out = should be in the drawer* against *counted now*, and
   *UPI as per app* against what the system recorded. **Everything matches â†’ Close shift.** Anything
   different (short or over, cash or UPI) â†’ write in the note what happened, then the **owner types their
   PIN** â€” the shift cannot be closed with a difference any other way.
2. Press **â» End work** (top-right or `Ctrl+Q`), check the exit time, **Confirm**. If your shift is still
   open, the big button takes you to the count first â€” only the owner's PIN can skip it.
3. Do **not** just close the window: if you do, you will have to confirm the exit time next morning,
   and the owner sees it as "window closed".

### C5. Ask the assistant (ðŸ’¬ bubble, bottom-right)
You can ask, in English or Hindi: *"kalawa ka stock?"*, *"price of camphor 50 g"*, *"which items are
low?"*, *"how do I hold a bill?"*. It cannot tell you sales totals or anything about other staff.

**Owner** questions that are answered instantly (even with the AI model switched off): *"aaj ki sale"*,
*"kal ki sale kitni thi"*, *"is mahine ka total"*, *"har item ka sale aaj"* / *"which items sold today"*,
*"which stock is high in demand"*, *"which item has no demand"*, *"maine ab tak kitna kharch kiya"* /
*"how much have I spent so far"*, *"forecast next week"* / *"agle mahine kitna bikega"*. Comparisons and
"why" questions need the AI model.

---

## Part D â€” Products & stock (Owner)  (à¤¸à¤¾à¤®à¤¾à¤¨ / à¤¸à¥à¤Ÿà¥‰à¤•)

| Task | Where | Notes |
|---|---|---|
| Add a product | Products â†’ **New** | Name, category (pick one or **type a new name**, e.g. *Oil* â€” created on save), **Sold as** (pc / kg / l â€¦), purchase price, selling price, opening stock **in that unit** (50 for 50 litres), GST %. Code and barcode are generated for you; MRP, HSN, alerts and barcodes are under *Advanced* |
| Print barcode labels | Products â†’ select â†’ **Print labels** | thermal strip, label roll or A4 sticker sheet |
| Attach the manufacturer's barcode | Product â†’ Advanced â†’ Barcodes â†’ **Scan to add** | both barcodes then work |
| Change a price | Product â†’ Price â†’ reason | recorded in **Price history** (pop-up chart: 1 m / 6 m / 1 y / custom) |
| Festival offer | Products â†’ **Offers** â†’ New | e.g. "Diwali 1 % on Camphor", dates; applies automatically when scanned |
| Goods received (with the supplier bill) | Stock â†’ **Purchase entry** | supplier, invoice no., qty, cost; updates the purchase price; labels for the received qty |
| Goods received (quick, or by staff) | Stock â†’ **Goods received** | item, qty in its unit, note; add only â€” book the bill later with Purchase entry |
| Correction / damage | Stock â†’ **Adjust / Damage** | reason required; shows in reports |
| Count the shelf | Stock â†’ **Stock-take** | Full / category / selected; approve â†’ shrinkage report |
| Items staff sold as "Open item" | Products â†’ **Open-item review** | one click converts them into real products with labels |
| Reorder | Reports â†’ **Forecast** â†’ *Create reorder list* â†’ PDF | send to the supplier |
| Hidden an item by mistake? | Products â†’ **Show archived** â†’ open â†’ **Restore** | its code stays reserved while archived |
| Start over after the trial (delete all items, stock, bills) | Terminal: `python3 main.py --fresh-start` | asks `START FRESH` + your Owner password; archives everything first; keeps users, PIN, shop/GST settings, categories |

---

## Part E â€” Reports (Owner)  (à¤°à¤¿à¤ªà¥‹à¤°à¥à¤Ÿ)
Every report has a date range, a chart, **Export CSV / Excel / PDF** and **Send** (to your phone if set up).
Every export and PDF opens with the shop's **letterhead** â€” shop name, address, phone, owner name, the date and
time it was made and who made it (Settings â†’ Shop & GST fills these in).

| Question | Report |
|---|---|
| How much did we sell today / this week? | Sales totals Â· Z-report |
| What sold on a particular day â€” how many of each item? | **Day Sales Report** (pick any day on the calendar: item, quantity in its unit, then Total sale Â· Cash Â· UPI Â· Card) |
| How did the month go, day by day? | **Sales by month** (pick the month; every shop day plus the month total) |
| What did each cashier sell today, bill by bill, item by item, at what time? | **Cashier day book** (kept 90 days; *Delete older recordsâ€¦* needs your password) |
| What did each staff member sell? Any shortfall in the drawer? | Cashier performance Â· Shifts |
| When did each person log in and out? | Staff work history |
| What is low / out of stock? What to order for Diwali? | Low stock Â· Forecast Â· Demand |
| What vanished without a bill? | Shrinkage Â· Stock reconciliation |
| How much profit? | Profit & margin |
| How much did we buy? | Purchases |
| How much money have I put into the shop so far? | **Investment in stock** (or ask the assistant: "ab tak kitna kharch kiya") |
| Who gave discounts / typed items / overrode rules? | Discounts Â· Manual lines Â· Overrides |
| What did the assistant refuse and who asked? | Chat security |
| Price changes of an item | Price history |
| Who changed what in the system? | Audit trail |
| Are backups fine? | Backups & integrity |

---

## Part F â€” If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Printer chip amber "N receipts queued" | no printer / unplugged | selling continues; connect the printer â†’ Settings â†’ Hardware â†’ *Print pending receipts* |
| "Scanner not detected â€” billing paused" | scanner unplugged | re-plug; if it is a new scanner, **Pair scanner** again |
| "AI offline" | assistant engine stopped | billing unaffected; Settings â†’ AI â†’ *Restart AI engine* or wait for tomorrow's start |
| "Backup drive not found â€” backups waiting" | USB/cloud folder missing | plug it in; backups catch up by themselves |
| "Did not close properly last time â€” all saved data is intact" | power cut / crash | nothing to do; if daily, send a **support bundle** (Settings â†’ System) |
| Red **Integrity** chip | database changed outside the app or restored wrongly | open *Reports â†’ Backups & integrity*; restore the last verified backup if needed |
| "Application files were modified" | program files on the PC differ from the release | do not use until re-installed with *Start Vaishnavi*; ask who had access to the PC |
| Forgot Owner password | â€” | on the PC run **Start Vaishnavi â†’ Reset owner password**; you need the **Recovery Key** and the computer's admin password |
| The PC died | â€” | install on a new PC, copy the latest `vaishnavi_<date>.db` from the secondary drive's `backups` folder into the primary folder as `vaishnavi.db`, start; the app recognises the backup and tells you which period to re-enter ([14](14-database-backup-and-recovery.md)) |

Logs for a technician: `http://<pc>:8080/system/logs` (Owner login) or `http://<pc>:8081` while installing.

---

## Part G â€” Keeping the PC itself safe (Owner, once)
- Create a **separate, non-administrator** Windows/macOS/Ubuntu user for the shop and let the app
  auto-start in that account; keep your own administrator account for installs only.
- Let the app open in **kiosk/app mode** so staff do not see the desktop.
- Lock the BIOS boot order and set a BIOS password if the PC is in a public spot.
- Keep the backup pen-drive plugged in and take the monthly snapshot home.

---

## Part H â€” Hindi quick glossary (screen names)
Billing â€” à¤¬à¤¿à¤²à¤¿à¤‚à¤— Â· Find item â€” à¤†à¤‡à¤Ÿà¤® à¤–à¥‹à¤œà¥‡à¤‚ Â· Hold â€” à¤°à¥‹à¤•à¥‡à¤‚ Â· Pay â€” à¤­à¥à¤—à¤¤à¤¾à¤¨ Â· Shift â€” à¤¶à¤¿à¤«à¥à¤Ÿ Â· End work â€” à¤•à¤¾à¤® à¤¸à¤®à¤¾à¤ªà¥à¤¤ Â·
Products â€” à¤¸à¤¾à¤®à¤¾à¤¨ Â· Stock â€” à¤¸à¥à¤Ÿà¥‰à¤• Â· Purchase â€” à¤–à¤°à¥€à¤¦ Â· Stock-take â€” à¤—à¤¿à¤¨à¤¤à¥€ Â· Reports â€” à¤°à¤¿à¤ªà¥‹à¤°à¥à¤Ÿ Â· Settings â€” à¤¸à¥‡à¤Ÿà¤¿à¤‚à¤—à¥à¤¸ Â·
Backup â€” à¤¬à¥ˆà¤•à¤…à¤ª Â· Owner PIN â€” à¤®à¤¾à¤²à¤¿à¤• à¤ªà¤¿à¤¨ Â· Receipt â€” à¤°à¤¸à¥€à¤¦ Â· Discount â€” à¤›à¥‚à¤Ÿ Â· Offer â€” à¤‘à¤«à¤¼à¤°
