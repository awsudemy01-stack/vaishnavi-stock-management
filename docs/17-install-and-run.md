# 17 — How to Install and Run Vaishnavi Store Manager

> **One file does everything: `main.py`.** You never run `pip`, never download Ollama, never set up a
> database by hand. On Windows, macOS or Ubuntu you run `main.py` (or double-click its wrapper) and it
> checks the computer from top to bottom, installs whatever is missing — asking your permission and the
> computer's administrator password only when it truly needs it — sets the application up, verifies it,
> and starts it. Every later day the same `main.py` re-checks everything in a few seconds and opens the shop.

This page is the **operational guide**. The design behind it is in
[07 — Installation, Startup & Health Checks](07-startup-and-health-checks.md); the daily use of the app
is in [15 — User Guide](15-user-guide.md).

---

## 1. Before you start (5 minutes, once)

| You need | Why | Notes |
|----------|-----|-------|
| The shop PC — Windows 10/11, macOS 12+, or Ubuntu 22.04+/Debian | the app runs here | 8 GB RAM minimum for billing/stock. For the **local** AI assistant (Ollama) 16 GB is comfortable and **no graphics card is needed** — it just answers slower on CPU. If the PC is weaker, choose a cloud AI provider at install (§3.2) |
| **Python 3.11 or newer** | `main.py` is a Python program — the only thing that cannot install itself | see §1.1; the wrappers detect a missing Python and show the exact steps |
| Internet connection **for the first run** | downloads Python packages (~150 MB) and, if you choose local AI, the models (~2 GB) | after that the shop works fully offline **unless** you chose a cloud AI provider — then the assistant (only the assistant) needs internet; see §8 for a no-internet install from a USB stick |
| *(optional)* an **OpenAI API key** or **Hugging Face token** | only if you choose a cloud AI provider instead of local Ollama | created on the provider's website; entered once, stored encrypted on the PC; can be added or changed later in *Settings → Assistant* |
| The computer's **administrator password** | only if a *system* component is missing (USB library, printer system, fonts, Ollama) | asked at most once per run, with a plain explanation of what will be installed; you may decline |
| A second place for backups — a USB drive, a second disk, or a folder synced to the cloud | the **secondary location** that keeps a copy of your data ([14](14-database-backup-and-recovery.md)) | you will be asked for it during the first run |
| Barcode scanner plugged in (USB) | mandatory for billing by staff | printer is optional and can be added any time later |

### 1.1 Getting Python (only if the wrapper says it is missing)

| OS | How |
|----|-----|
| Windows | *Start Vaishnavi.bat* offers to run `winget install Python.Python.3.12` for you; or download from python.org and **tick "Add python.exe to PATH"** |
| macOS | *Start Vaishnavi.command* offers `brew install python@3.12` if Homebrew exists; otherwise download the python.org `.pkg` |
| Ubuntu / Debian | `sudo apt install python3 python3-venv python3-pip` (the wrapper prints this line) |

---

## 2. Get the application onto the shop PC

Copy the release folder (zip from the release, or a USB stick) anywhere you like, e.g.

```
Windows:  C:\VaishnaviStoreManager\
macOS:    /Applications/VaishnaviStoreManager/
Ubuntu:   ~/VaishnaviStoreManager/
```

Inside you will find:

```
VaishnaviStoreManager/
├── main.py                      ← the only thing you ever run
├── Start Vaishnavi.bat          ← Windows double-click wrapper  → main.py
├── Start Vaishnavi.command      ← macOS double-click wrapper    → main.py
├── start-vaishnavi.sh           ← Ubuntu wrapper                → main.py
├── bootstrap/                   ← the installer's own steps (pure standard-library Python)
├── src/vaishnavi/               ← the application
├── config/                      ← settings templates
├── docs/                        ← this documentation
└── offline/                     ← (optional) packages + models for a no-internet install, see §8
```

The application's **data never lives in this folder** — it lives in the *primary location* you choose
(default `~/VaishnaviStore`), so updating or moving the program folder never touches your database.

---

## 3. First run — installation (10–20 minutes, mostly waiting)

Double-click the wrapper for your OS, or open a terminal in the folder and run:

```bash
python3 main.py            # Windows: py main.py   or   python main.py
```

`main.py` now works through the stages below. **Each stage prints a ✔ / ! / ✖ line in plain language,
and everything is logged** (`logs/install/<date>.log`, viewable live in a browser at
`http://127.0.0.1:8081` from the very first second).

### 3.1 The installer is interactive — how the questions work

`main.py` is a **step-by-step wizard**, not a silent script. Whenever a decision is yours, it stops and asks:

```
 ?  Which AI engine should the assistant use?   (you can change this later in Settings → Assistant)
      [1] Local — Ollama on this PC   · private, free …   (recommended)
      [2] OpenAI API (internet)       · …
      [3] Hugging Face API (internet) · …
      [4] No AI assistant for now     · …
    Choice [1]: _
```

- The **recommended answer is in brackets** — press Enter to accept it.
- Type `b` to go **back** one question, `s` to **skip** (only where skipping is allowed), `q` to **quit**;
  re-running `main.py` resumes where you stopped.
- Every question has a one-line *what this means* and, with `--lang hi`, is asked in Hindi.
- Only these things are ever asked: language · AI provider (+ key) · primary and secondary data folders ·
  start automatically at log-on? · allow phones/tablets on the shop Wi-Fi to open the app? Everything else
  (OS, Python, packages, drivers, printers, scanner) is **detected**, never asked.
- Your answers (never keys/passwords) are saved to `config/install-answers.toml`; on a replacement PC
  `main.py --answers config/install-answers.toml` repeats the install without questions, and `--yes`
  accepts every default.

### 3.2 Choosing the AI provider

| Option | Pick it when | What it needs | Privacy / cost |
|---|---|---|---|
| **1 Local (Ollama)** | the PC has ≥ 12 GB RAM (a graphics card helps but is not required) and you want everything to stay in the shop | ~3 GB download once | nothing leaves the PC; free |
| **2 OpenAI API** | the PC is weak or you want the fastest, best answers (incl. Hindi) | internet while chatting; an OpenAI API key with billing enabled | your questions and the looked-up figures go to OpenAI; pay per use (normally a few rupees/day) |
| **3 Hugging Face API** | like 2 but you prefer open models hosted remotely | internet; a Hugging Face token (free tier is rate-limited; PRO is a flat fee) | same data notice as 2 |
| **4 None** | you do not want the assistant yet | — | — ; stock/price/total questions are still answered instantly from the database (fast path) |

The installer **recommends** an option from what it found on the PC, states clearly what is sent to a cloud
provider and what is never sent (passwords, bank/UPI details, customer numbers, your database), and
**tests the key** before accepting it. Billing never depends on this choice, and you can switch any time in
*Settings → Assistant → Provider* ([06 §2](06-ai-assistant.md#2-ai-provider-and-model-selection)).

**How to give the key (options 2 and 3)** — you never edit a file:

| Way | When | What happens |
|---|---|---|
| **Paste it when asked** (normal) | during install, or later in *Settings → Assistant → Provider* | typed characters are hidden; the key is tested, then stored **encrypted** on this PC; nothing is written to a plain file |
| **Environment variable** `VAISHNAVI_OPENAI_API_KEY` / `VAISHNAVI_HF_API_KEY` | unattended re-install with `--answers` | read once, stored encrypted, not asked again |
| **`config/secrets.env` file** with the same variable names | same as above, when an env var is awkward (e.g. double-click wrapper) | read once, stored encrypted, then the installer **offers to delete the file** (say yes); a file left behind makes the health check warn |

Putting the key inside `settings.toml` or any other config file is **not supported** — the app refuses to
start and tells you to remove it. If a cloud provider is chosen but no key is stored (new PC, restored
database), the health check says *"Open Settings → Assistant → Provider and paste the key"*.

### 3.3 Stages

| # | Stage | What `main.py` does | You may be asked |
|---|-------|---------------------|------------------|
| 0 | Start log + log console | creates `logs/install/…`, starts the mini log server on `:8081` | — |
| 1 | **Survey the computer** | OS and version, CPU/RAM/GPU, disk space, Python version, printers, USB devices, serial ports → `survey.json`; chooses the AI recommendation | language (English / हिंदी) |
| 2 | **Python check** | needs 3.11–3.13; otherwise prints the OS-specific install line and stops | — |
| 3 | **Private environment** | creates `.venv` next to `main.py` (first time only) | — |
| 4 | **Python packages** | installs the exact, hash-pinned package list into `.venv`; shows `23/41 …` progress; retries on flaky network | — |
| 5 | **System components** | detects missing libusb (USB printer/scanner), CUPS/spooler (printing), Devanagari fonts | **administrator password**, with the exact reason and command shown; declining is allowed |
| 6 | **AI provider** | asks the question in §3.2 with a recommendation | your choice |
| 6a | ↳ *Local*: **Ollama + models** | installs Ollama if missing (winget / brew / official script), starts the service, pulls the chat and embedding models with progress bars | administrator password *if the Ollama installer needs it* |
| 6b | ↳ *OpenAI / Hugging Face*: **key** | shows the data + cost notice; takes the key from `VAISHNAVI_*_API_KEY` env var or `config/secrets.env` if present, otherwise asks for it (masked); runs a test chat and a test embedding; stores the key encrypted; offers to delete `secrets.env` | the API key (unless supplied via env/file) |
| 7 | **Data locations** | asks for the **primary** (live data) and **secondary** (backups) folders; checks they are different drives/folders, writable, with enough space | the two folder paths (defaults offered) |
| 8 | **Autostart + watchdog** | registers *start at log-on* (Task Scheduler / launchd / systemd user) and the hang-watchdog | yes/no; administrator password only if the user-level autostart is impossible |
| 9 | **Database** | runs `init-db` inside the private environment: creates `vaishnavi.db`, applies all migrations, seeds roles/units/tax presets and the Owner account, stores the AI key encrypted (only then is `config/secrets.env` removed); if a database already exists (re-install, restored copy) it is detected and verified instead ([14 §6](14-database-backup-and-recovery.md)) | — |
| 10 | **Health check (doctor)** | runs all checks of [07 §5](07-startup-and-health-checks.md#5-pre-flight-doctor--ordered-checks); prints the plain-language report (`!` notes let the shop open; a red line stops here with a *What to do* hint) | — |
| 11 | **Start** | starts the application detached, waits until `/healthz` answers, prints **Ready to open the shop: http://127.0.0.1:8080** and opens the browser in app mode; the window then stays as the **watchdog** (you may minimise it — closing it does not close the shop). The app takes the **first backup** to the secondary location during this first start | — |

`python main.py --install-only` stops after stage 10 (nothing is started).

A full example of what this looks like on screen is in
[07 §3 → "What the owner sees during first install"](07-startup-and-health-checks.md#3-bootstrap-flow).

### 3.4 After it starts — the onboarding wizard

The browser opens on the **first-day wizard** ([09 §2a](09-frontend-guide.md)): set the Owner password,
write down the **12-word Recovery Key** (shown once — print it or copy it to paper and keep it at home),
enter the shop details for the receipt, pair the barcode scanner (scan a test code), optionally set up the
printer, create the Billing-Staff login, and add or import your first products. Everything in the wizard
can be changed later under *Settings*.

---

## 4. Every day after that

Nothing to do: when you log in to the PC, autostart runs `main.py`, which:

1. starts the log console, 2. re-checks packages/components/Ollama (nothing to install → under 3 s),
3. makes sure Ollama and the models are up, 4. runs the health check, 5. starts the app and opens the
browser on the **login screen**.

If autostart did not fire (or you closed the app), double-click the wrapper — it is the same `main.py`.

The top bar shows status chips **DB · Integrity · Scanner · Printer · AI · Backup · Publish**; a yellow
chip means "works, but look at this", red means "fix before selling" ([15 §2](15-user-guide.md)).

To close for the day: shut the PC down normally. The app flushes and backs up on the way out
([07 §9](07-startup-and-health-checks.md#9-shutdown)); if sales happened since the last backup, a final
snapshot is written to the secondary location first.

---

## 5. All `main.py` commands

Same on every OS. Options can be combined. Run `python3 main.py --help` for the list.

| Command | Use it when | What happens |
|---------|-------------|--------------|
| `main.py` | every start | survey → install anything missing → doctor → start app → open browser |
| `main.py --install-only` | setup day, after copying a new release, before handing the PC to staff | everything up to and including the doctor, then stops (does not start the app) |
| `main.py --doctor` | "something is not right" | only the health check, full report, exit code 0/1/2 (see §7) |
| `main.py --doctor --fix` | the doctor said *WARN* | re-runs the checks and performs the safe automatic fixes (start Ollama, pull a missing model, run an overdue backup, re-register autostart) |
| `main.py --lang hi` | you prefer Hindi | all installer and doctor messages in हिंदी (the app language is set separately in *Settings*) |
| `main.py --no-browser` | starting from a service or remotely | start without opening a browser window |
| `main.py --no-ai` | PC too weak, or you do not want the assistant | same as answering **4 None** to the AI question; the app runs with the assistant on fast path only |
| `main.py --ai-provider ollama\|openai\|huggingface\|none` | you already know the answer | pre-answers the AI question (a key is still asked for cloud providers unless already stored) |
| `main.py --answers <file>` | re-install on a replacement PC | reads `config/install-answers.toml` from the old PC; asks nothing except secrets |
| `main.py --yes` | unattended setup with defaults | accepts the recommended answer to every question (AI → local if the PC can carry it, else none) |
| `main.py --offline <folder>` | shop PC has no internet | installs packages, Ollama and models from the `offline/` bundle (§8) |
| `main.py --update <zip-or-folder>` | a new release arrived | backup → replace program files → install packages → migrate DB → selftest → restart; **rolls back** if selftest fails *(planned — Phase 7; today the installer says "not available in this version")* |
| `main.py --restore` | database lost or damaged | guided restore from the secondary location (lists snapshots, verifies, swaps in) — see [14 §5](14-database-backup-and-recovery.md). *Today: use **Settings → Backups → Restore…** inside the app, or the manual copy in [14 §5.2](14-database-backup-and-recovery.md#52-manual-restore-by-copying-the-file--yes-this-works); the command-line form is planned for Phase 7* |
| `main.py --reset-owner-password` | Owner forgot the password | asks for the 12-word Recovery Key **and** the computer's administrator password; sets a new password; audited |
| `main.py --selftest` | after install/update, or when asked by support | runs the shipped test suite on this PC **plus** the live health check; exit 0 ok · 1 notes · 2 failed; the report is kept in `<primary>/logs/selftest/` |
| `main.py --logs` | you want to look at logs without starting the app | opens the mini log console on `:8081` only |
| `main.py --uninstall-autostart` | you no longer want the app to start on log-on | removes the Task Scheduler / launchd / systemd entry; nothing else is removed |
| `main.py --stop` | you copied new code, or want a clean restart | asks the running application to shut down (SIGTERM → clean close), waits until port 8080 is free; then start again with `main.py` / *Start Vaishnavi*. The app survives closing the installer window on purpose, so this is the owner's "turn it off" switch from the terminal (inside the app: Settings → System → Shut down) |
| `main.py --where` | "where is my data?" | prints program folder, setup logs, configuration, primary location (database, logs, receipts) and the backups folder — works even before the setup finished |

Inside the app the same actions are available to the Owner under *Settings → System* (Run health check,
Support bundle, Shut down) and *Settings → Backups* (Backup now, Restore…).

### 5.1 First run on the shop PC — the checklist

Do this once, on setup day, before staff use the counter. Tick each line.

1. **Copy the release folder** to the shop PC (e.g. `C:\Vaishnavi` or `~/Vaishnavi`) and double-click
   **Start Vaishnavi** (`.bat` / `.command` / `start-vaishnavi.sh`). Answer the questions (language, AI
   provider, the two data locations). Expect the last line **"Ready to open the shop: http://127.0.0.1:8080"**.
2. In the browser, sign in as **owner** — the first login opens the **set-up step**: choose the Owner password and
   the **Owner PIN**, then write down the **12-word recovery key** on paper and keep it at home.
3. **Health check:** *Settings → System → Run health check*. Every line should be ✔ or `!` (notes). A red ✖
   line must be fixed before the shop opens — follow its *What to do* text.
4. **Selftest:** close the app window and run `python main.py --selftest` once (or `Start Vaishnavi` → it is
   in *Settings → System* later). It must end with **SELFTEST OK** (notes are fine). The report is saved under
   `<primary>/logs/selftest/`.
5. **Backup drive:** plug in the USB disk (or confirm the second disk) you chose as the *secondary* location,
   then *Settings → Backups → Backup now*. The snapshot must show **verified ✔** and the *Backup* chip must be green.
6. **Restore test (5 minutes, worth it):** *Settings → Backups → Restore…* → pick the snapshot you just made →
   enter the Owner password → *Restart*. The app comes back, the banner says it runs on that backup — you have
   proven the recovery path before you need it.
7. **Staff accounts:** *Settings → Users* → one account per person, a **numeric password** (6+ digits) so they can
   use the number pad on the login tiles.
8. **Attendance check:** sign in as a staff member, then **close the browser window with the X**. Sign in again
   as that person: the amber banner *"you left at … without ending work"* must appear and ask for confirmation.
   That is the anti-"just close the window" rule working.
9. **Scanner and printer:** *Settings → Hardware* → pair the scanner (scan a test code) and pick the printer; the
   *Scanner* chip must be green before staff can bill (strict mode).
10. **Restart the PC** once. The app must come back by itself (autostart) and the installer window must show
    *Ready to open the shop* again without asking anything.
11. **First products and a real bill:** *Products → New* (or *Import CSV*), print one label sheet, then sign in as
    staff and **scan** that label on the billing screen: discount a line, hold and resume the bill, pay by cash
    and by UPI, print the receipt; scan the **receipt barcode** on the billing screen → the bill opens; void it as
    the Owner (PIN) and check *Reports → Voids & refunds* and the credit-note receipt.
12. **Shift and day close:** open a shift as staff, make two bills, then as the Owner run *Shifts → Day close*:
    close the shift by count (the variance must match what you expect), read the Z report (chain head at the
    bottom), back up, create the digest, *Finish*. The dashboard tiles and *Reports → Sales by day* must show the
    same figures as the Z report.
13. **Assistant:** open the 💬 bubble as **staff** and ask *"camphor kitna hai"* (answered), then *"aaj ka total"*
    (refused: only the owner), then *"how to delete a bill"* (refused and reported — check *Reports → Chat
    security*). As the **Owner** ask *"today's sale"* and *"which items should I reorder"* — the numbers must match
    the reports. With Ollama chosen, the first answer can take 20–40 s while the model loads.
14. **Publishing (optional):** *Settings → Publishing* → tick *Synced folder* and point it at your OneDrive/Google
    Drive folder → *Test* → the test file must appear on your phone. Add e-mail or Telegram if you want the digest
    pushed; *Send today's digest now* and open the file.
15. **Restore and update commands:** with the app closed, `python main.py --restore` lists the snapshots and
    restores the one you pick (a pre-restore copy is kept); `python main.py --update <release.zip>` applies a new
    release and rolls back by itself if its self-test fails.

If any step does not behave as written, run `python main.py --doctor`, read the red/yellow lines, and if needed
send the file from *Settings → System → Support bundle* (it contains no passwords, no audit trail).

---

## 6. Watching what is happening

| Where | When | What you see |
|-------|------|--------------|
| The installer window | always | the ✔ / ! / ✖ lines, in your language |
| `http://127.0.0.1:8081` (or `http://<shop-pc-ip>:8081` from a phone on the shop Wi-Fi) | during install and start-up, before the app is up | live tail of `logs/install` and `logs/startup`; auto-refreshing; `/raw` for the plain file |
| `http://127.0.0.1:8080/system/logs` | once the app is running (Owner login) | full log console: filter by level, module, error code, time; download; metrics; notifications ([08 §9](08-logging-and-troubleshooting.md#9-log-console-over-http--rest)) |
| `~/VaishnaviStore/logs/` (primary location) | any time, e.g. to send to support | `startup/`, `app/`, `errors/`, `audit/` — rotated daily, plain text + JSON lines |
| `<program folder>/logs/install/` | installer runs | one `<date>_<run-id>.log` per run of `main.py` (DEBUG level, secrets masked, last 30 kept) plus `state.json` (what each stage did, so an interrupted install resumes). Lives next to `main.py` because the data location is not known until stage 7 |

Every message that says `!` or `✖` carries a reference like `ref 01JCXYZ…`; searching the log console
for that reference shows the full technical detail behind the plain-language line.

---

## 7. If something goes wrong

`main.py` ends with one of three results and never leaves you guessing:

| Result | Exit code | Meaning | What happens |
|--------|-----------|---------|--------------|
| **Ready to open the shop** | 0 | all checks passed | app starts |
| **Ready, with notes** | 1 | something optional needs attention (printer not set up, backup drive unplugged, no internet for reports, AI off) | app starts; the notes are shown as yellow chips and in the notification bell |
| **Cannot start** | 2 | something essential is missing or broken (Python packages, database integrity, program files modified, primary folder not writable, port in use) | app does **not** start; the window stays open with *what is wrong* and *what to do*; the log console stays up on `:8081` |

Common first-run messages and their fixes:

| Message you see | What to do |
|-----------------|------------|
| *Python 3.11 or newer was not found* | install Python as in §1.1, then run the wrapper again |
| *Could not download packages — no internet* | connect the PC, or use the offline bundle (§8); re-run `main.py` — it continues where it stopped |
| *Administrator password was not given — USB printer cannot be used until libusb is installed* | that is allowed; run `main.py --doctor --fix` later when you are ready to enter the password |
| *The backup folder is on the same disk as the data* | choose a different drive/USB/cloud-synced folder; a same-disk backup protects only against mistakes, not disk failure (it is accepted with a warning if you insist) |
| *Ollama could not be started — the assistant is turned off* | the shop works normally; see [08 §7](08-logging-and-troubleshooting.md#7-known-symptoms-seeded-grows-with-each-phase) *AI* rows or run `main.py --doctor --fix` |
| *The API key was not accepted (wrong key / no billing / no internet)* | check the key on the provider's website and that billing is enabled; try again, choose another provider, or continue without AI — the key can be entered later in *Settings → Assistant* |
| *AI key not set* (after moving to a new PC or restoring) | keys are not part of the answers file; open *Settings → Assistant → Provider* and paste it, or run `main.py --ai-provider openai` |
| *A plain-text secret file was found (`config/secrets.env`)* | the key was already imported and encrypted; delete the file (the installer offers to do it) |
| *An API key is written in `settings.toml`* | not supported — remove the line and paste the key in *Settings → Assistant → Provider* instead |
| *AI monthly limit reached* | the assistant answers from the database only until the Owner raises the limit in *Settings → Assistant* (next month it resets automatically) |
| *Port 8080 is already in use* | another copy is running — check the system tray / `main.py --where`; or set `server.port` in `config/settings.toml` |
| *Application files were modified* | someone changed the program files; re-run the wrapper (it re-installs the release) and check who had access ([16 §3](16-design-review-and-enhancements.md)) |
| *Database did not pass the integrity check* | the installer offers to restore the latest verified snapshot from the secondary location; say yes ([14 §6](14-database-backup-and-recovery.md)) |

The complete symptom → cause → fix table is [08 §7](08-logging-and-troubleshooting.md#7-known-symptoms-seeded-grows-with-each-phase).
**Re-running `main.py` is always safe** — every stage is idempotent and skips what is already done.

---

## 8. Installing without internet (offline bundle)

Many small shops have no fixed internet. A release can be shipped with an `offline/` folder prepared on any
connected PC with `python3 main.py --make-offline-bundle <target-os>`:

```
offline/
├── wheels/<os>-<arch>/      ← all Python packages for that OS (hash-verified)
├── ollama/<os>/             ← Ollama installer
└── models/                  ← qwen2.5:3b, nomic-embed-text as Ollama blobs
```

On the shop PC run `python3 main.py --offline ./offline`. Stages 4 and 6a read from the bundle instead
of the network; everything else is identical. Python itself must still be present (§1.1). Cloud AI
providers are not usable without internet — choose local Ollama or none for an offline shop.

---

## 9. Moving to a new PC, re-installing, uninstalling

- **New PC / repaired PC**: install as in §3, and at stage 8 point the *primary* to an empty folder and the
  *secondary* to your existing backup drive — `main.py` notices the backups and offers to restore the latest
  one. Or copy the old primary folder by hand first; `main.py` detects the copied database and verifies it
  ([14 §6](14-database-backup-and-recovery.md)).
- **Re-install the program only**: delete the program folder, copy the release again, run `main.py`. The
  data folder is untouched.
- **Uninstall completely**: `main.py --uninstall-autostart`, then delete the program folder. Your data stays in
  the primary and secondary locations until *you* delete them; the app never deletes them.

---

## 10. For developers — what `main.py` is and is not

- `main.py` is a **thin stdlib-only entry point** at the repository root. It may import only the Python
  standard library because it runs *before* the virtual environment exists. It parses the command line,
  starts the install log and mini log server, and runs the ordered stages implemented in the stdlib-only
  `bootstrap/` package (`survey`, `python_env`, `pkg_manager`, `elevate`, `wizard`, `ai_provider`,
  `ollama_install`, `locations`, `autostart`, `recovery`, `minilog`, `offline`) — see [10 §2](10-backend-components.md#2-bootstrap).
- Once the venv exists it **hands over** to the application for everything that needs third-party code:
  `.venv/bin/python -m vaishnavi doctor|run|selftest|restore|update`, streaming their output into the same
  log. The application never re-implements installer logic, and the installer never imports the application.
- OS differences live **only** in `bootstrap/` and `src/vaishnavi/platform/`
  ([03 §2](03-architecture.md), [12 ADR-025](12-tech-stack-decisions.md)).
- Every stage: is idempotent, logs `STAGE_START/STAGE_OK/STAGE_SKIP/STAGE_FAIL` with elapsed ms, maps failures
  to an `E-SYS-xxx` code with EN/HI messages from `bootstrap/messages/`, and never logs secrets
  (`ELEVATED_CMD` records the command, not the password).
- **Dependency lock.** `pyproject.toml` declares minimum versions only. The installer installs from
  `requirements.lock`, a hash-pinned file produced on a connected machine with
  `pip-compile --generate-hashes --extra dev --extra cups --extra pdf-html --output-file requirements.lock pyproject.toml`
  (pip-tools). If the lock is absent the installer falls back to `pip install -e .` and the doctor reports
  **WARN `E-SYS-041` "dependencies not hash-verified"** so a release is never silently unpinned.
- **Architecture rules are enforced by tools, not reviews**: `lint-imports` (import-linter contracts in
  `pyproject.toml`) checks the layer order, the stdlib-only core tier, and that LLM clients / device
  drivers / NiceGUI stay inside their adapter packages; `ruff` bans `sys.platform` outside `bootstrap/` and
  `vaishnavi/platform/`; `tests/core/test_project_metadata.py` guards the configuration itself.
- Exit codes: 0 PASS · 1 WARN · 2 FAIL · 3 user cancelled · 4 internal installer error (traceback in
  `logs/install/`, plus a plain-language apology on screen with the log path).

```mermaid
flowchart LR
  W[Start Vaishnavi wrapper<br/>.bat · .command · .sh] --> M[main.py<br/>stdlib only]
  M --> B[bootstrap/ steps<br/>survey · venv · packages · system · Ollama · models · locations · autostart]
  B -->|hand over inside .venv| V[python -m vaishnavi<br/>doctor → run]
  V --> A[Vaishnavi Store Manager<br/>http://127.0.0.1:8080]
  M -.logs.-> L[(logs/install · logs/startup)]
  L -.tail.-> C[Mini log console :8081]
  A --> D[/system/logs · /api/v1/system/*]
```

Build status: this guide describes the designed behaviour; see [13 — Roadmap](13-roadmap.md) for what is
implemented in which phase (the installer is Phase 1).
