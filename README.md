# IBFT Reversal & Reconcile (Django)

One Django project (`reversal_project/`), two related apps sharing one
login system and one "passed" review workflow:

- **[`core`](#reversal-generator-core-app)** — takes the daily
  `ibft-transaction_*.xlsx` export and produces a `need_to_reversal_*.xlsx`
  file: which transactions need a manual reversal, grouped and formatted
  exactly like the manual process it replaces.
- **[`reconcile`](#reconcile-reconcile-app)** — a broader, from-scratch
  reconciliation of the *same* kind of export against **every** bank
  statement you have (up to 18 banks), producing a full SUCCESS / FAILED /
  REVERSAL audit rather than just a reversal to-do list. No separate
  reversal file needed — everything is derived from the transaction file
  itself.

Both apps can also skip the manual upload step entirely and **pull the
day's transactions straight from the switch's own database** for any date
range (see [Fetching data straight from the switch
DB](#fetching-data-straight-from-the-switch-db)). On top of the two apps,
a shared background **scheduler** (see
[Scheduler](#scheduler-automated-alerts--reports)) watches for dispute/
timeout transactions every minute and emails the previous day's report
every morning — no manual "check and forward" step needed for either.

For the deep-dive on exactly how each app's business rules and data models
work internally, see [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md).
This file is the practical "what does it do and how do I run it" version.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt

python manage.py migrate        # also seeds the two hardcoded logins
python manage.py runserver
```

Then open http://127.0.0.1:8000/ for the reversal generator, or
http://127.0.0.1:8000/reconcile/ for Reconcile — same login for both. As
`admin`, check http://127.0.0.1:8000/dashboard/ and
http://127.0.0.1:8000/audit-log/ (each page embeds both apps' sections).

To use **"Fetch from DB"** or the **General** dashboard tab (both read
straight from the switch's own database — see
[Fetching data straight from the switch DB](#fetching-data-straight-from-the-switch-db)),
also set `SWITCH_DB_HOST` / `SWITCH_DB_PORT` / `SWITCH_DB_NAME` /
`SWITCH_DB_USER` / `SWITCH_DB_PASSWORD` in `.env`. Without these, every
other feature works fine — only DB-fetch and the General tab show a clear
"not configured" error instead of a crash.

## Login

There is no self-registration — two hardcoded accounts are seeded by a data
migration (`core/migrations/0003_seed_users.py`), shared by both apps:

| Username        | Password    | Role                                             |
|-----------------|-------------|---------------------------------------------------|
| `ashok.koirala` | `ashok@123` | Regular operator — can upload/generate            |
| `admin`         | `admin@123` | Admin — also sees the central Audit Log & Dashboard |

Change these passwords (via `/admin/` or the Django shell) before using this
anywhere beyond your own machine — they're intentionally simple/hardcoded
per the current requirements.

Beyond these two seeded accounts, an admin (`is_superuser`) can create as
many additional logins as needed from the **Extra page's "Make user" tab**,
each with **per-feature access control** — a checkbox per module (Reversal,
Reconcile, Verification format, Check statements, Audit log, Bank contacts,
Issuer bank accounts, Make users, Scheduler), not just a blanket
admin/regular split. A superuser always has every feature regardless of
these checkboxes; anyone else only sees/can-use the modules ticked for
them. Every user manages their own email signature (see the Verification
format tab) no matter their role.

## Passed workflow (review before it's shared)

Both apps use the same review gate. A newly generated file/run only shows
up in **your own "My activity" list** — not the central shared list, and
not the analytics dashboard — until you (or an admin) review it and click
**"Mark as passed"** (on the result page, or right from the activity
table). Once passed:

- it moves into the central **"Shared passed reports"** list, visible to
  everyone
- it's included in the analytics dashboard
- for the reversal app specifically, it also becomes the baseline the
  *next* upload's double-reversal check compares against (see below)

Anyone can unmark their own passed report; admins can mark/unmark anyone's.

## Fetching data straight from the switch DB

Both the Reversal upload page and the Reconcile upload page offer a second
mode alongside "Upload a file": **"Fetch from DB"** — pick a From/To date
and the app pulls the matching transactions directly out of the switch's
own production database (`core/switch_db.py`), builds the same
`ibft-transaction_*.xlsx`-shaped workbook a manual export would have been,
and runs the normal reversal/reconcile flow against it. No separate
download-then-upload step needed for a routine day.

That connection is **read-only at the database session level**, not just
by convention — the switch DB isn't a Django `DATABASES` alias, and every
connection is explicitly flipped into a Postgres `READ ONLY` transaction
before any query runs, so a write is rejected by Postgres itself if one is
ever attempted. This same connector also powers the **"General" dashboard
tab** (`/dashboard/general/`) — live Member/Aggregator/Issuer/Acquirer/
amount-bucket analytics for any date range, computed straight from the
switch DB independent of whether anyone has uploaded or fetched a file for
that period at all — and both scheduled jobs described next.

Connection details come from `SWITCH_DB_*` in `.env` (see
`reversal_project/settings.py`); the feature is unavailable (with a clear
error, not a crash) until those are set.

## Scheduler (automated alerts & reports)

A background scheduler (`core/scheduler.py`, using APScheduler in-process
rather than an OS cron job — this needs to be toggleable from inside the
web app itself, and to work on Windows, which has no cron) runs two jobs:

- **Dispute/timeout alert** — every minute, checks the switch DB for
  transactions that newly show `Overall Status = TIMEOUT` and emails a
  table with one row per transaction (Aggregator, Payment Processor,
  Network Reference Id, Reason, Overall Status) to configured recipients.
  Reason is the destination message if the debit side already succeeded,
  otherwise the debit side's own message. A dedup ledger guarantees the
  same transaction is never alerted twice, even though each check's
  window intentionally overlaps the previous one.
- **Daily transaction report** — every day at 09:00, emails the previous
  day's Issuer-wise / Acquirer-wise / Aggregator-wise breakdown (same
  numbers as the Dashboard's General tab export), as an Excel attachment
  — an automated management/CEO-level report with no manual step.

Both jobs are managed from the **Extra page's "Scheduler" tab**: turn
either on/off, and manage who receives each one (comma-separated To/Cc per
recipient group, multiple groups merged into one send). Only users with
the `can_scheduler` permission (or a superuser) can see this tab.

Next to the dispute/timeout alert's Start/Stop row is a **"Desktop
alerts"** checkbox — any logged-in user can turn this on for themselves.
While it's on and this tab is open (even minimized/backgrounded — it
just can't be fully closed), a new dispute/timeout pops a browser
notification and reads it aloud, independent of whether anyone's
configured to receive the email above.

## Extra page (operational utilities)

A single tabbed page (`/bank-statement/`) hosts everything that isn't
"upload a file and get a result" — each tab only visible to users with the
matching permission (see [Login](#login) above):

- **Check bank statement** — the double-reversal / already-credited
  cross-check described under [Bank statement upload](#bank-statement-upload-multiple-files)
  below.
- **Add bank account** — add a Debtor-Bank → Debit-Account mapping
  (`BankAccount`) so a new bank's reversal rows get the right account
  without a code change.
- **Verification format** — upload a dispute-transaction export and
  convert it (entirely in memory — nothing is saved unless you click
  download) into the bank's required 13-column verification format. Rows
  are grouped by Creditor Bank, and any group matching a configured **bank
  contact** gets a one-click **"Send mail"** button that emails that
  bank's rows straight to the configured To/Cc addresses — replacing what
  used to be a manual email. Add/edit bank contacts from the same tab.
- **Mail signature** — every user manages their own signature(s) (name,
  title, mobile, company, etc.) used to sign outgoing verification and
  notification emails; the most recently updated active one wins if you
  keep more than one.
- **Make user** — create, edit, and delete logins, including the
  per-feature access checkboxes described in [Login](#login) (superuser-
  only tab, since it can grant admin rights).
- **Scheduler** — described above.

## Project layout

```
reversal_project/
├── manage.py
├── requirements.txt
├── reversal_project/        # Django project settings/urls (mounts "/", "reconcile/", "admin/")
├── core/                    # the reversal generator app
│   ├── models.py            # ProcessingLog (audit trail), BankAccount,
│   │                        # VerificationBankContact, MailSignature,
│   │                        # UserAccess, ScheduledReportRecipient,
│   │                        # SchedulerJobState, AlertedTimeoutTransaction
│   ├── forms.py              # upload form (incl. DbFetchForm)
│   ├── services.py           # <-- all the business logic lives here
│   ├── switch_db.py          # read-only "Fetch from DB" connector
│   ├── general_report.py     # live analytics straight from the switch DB
│   ├── scheduler.py          # dispute/timeout alert + daily report jobs
│   ├── permissions.py        # per-user feature-gating (UserAccess)
│   ├── views.py
│   ├── urls.py
│   ├── admin.py
│   └── templates/core/
│       ├── base.html
│       ├── login.html
│       ├── upload.html
│       ├── result.html
│       ├── audit_log.html
│       ├── dashboard.html
│       ├── general_report.html
│       └── bank_statement_upload.html   # tabbed "Extra" utilities page
└── reconcile/                # the broader reconciliation app
    ├── models.py              # ReconcileRun — one row per reconciliation run
    ├── banks.py                # the 18-bank SCT network registry
    ├── transactions.py         # reads the TransactionReport / ibft-transaction export
    ├── statements.py           # reads bank statements — csv/xlsx/pdf, several shapes
    ├── engine.py                 # the reconciliation engine (~970 lines)
    ├── report.py                  # builds the downloadable .xlsx report
    ├── dashboard.py                # dashboard aggregation + Excel exports
    ├── forms.py
    ├── views.py
    ├── admin.py
    ├── urls.py
    └── templates/reconcile/
        ├── reconcile.html        # upload form + shared/own activity panels
        ├── result.html            # per-run result page
        └── day_detail.html         # "click a day" rolled-up view
```

---

## Reversal generator (`core` app)

Produces a `need_to_reversal_*.xlsx` file from the daily
`ibft-transaction_*.xlsx` export:

- **failed** sheet: `FAILED` transactions, excluding "Insufficient funds"
- **coop** sheet: manual-reversal transactions for every aggregator other
  than IME REMIT / CITY REMIT, grouped by Member Name (alphabetical), each group's
  "S NO" restarting at 1
- **imeremit** sheet: manual-reversal transactions where Aggregator = `IME REMIT`
- **cityremit** sheet: manual-reversal transactions where Aggregator = `CITY REMIT`
- **timeout** sheet: any `TIMEOUT` transactions, unchanged

For every kept reversal row, three fields are (re)built:
- **Debit Account Number** — the fixed clearing account `0002335524115`,
  **except** when the original transaction's **Debtor Bank is Prabhu Bank**,
  in which case the dedicated account `99901170130555` is used instead
  (Prabhu Bank reversals are rare but need their own account).
- **Credit Account Number** — the *original* row's Debit Account Number (the
  money goes back to wherever it was originally pulled from)
- **narration** (in the duplicated "Member Transaction Id" column) —
  `REV` + the member transaction id with its leading zeros and leading
  prefix/letters stripped + `-` + Session Id
  (e.g. `REQ_1783249894525` &rarr; `REV1783249894525-100168`,
  `IME-00113275637-572` &rarr; `REV00113275637-572-100168`)

All id-like columns (Member Transaction Id, Network Reference Id, Session Id,
Debit/Credit Account Number) are written as **text**, not numbers, in the
output workbook. Long numeric ids (17+ digits) silently lose trailing
precision if Excel is allowed to store them as a number — this was traced
back as the likely cause of a past mismatch between a source file's Member
Transaction Id and what showed up in the generated reversal file (the
narration still matched because it was already being built as a string, not
a number). Forcing text format everywhere makes that class of bug
impossible going forward. If it ever happens again, please save the exact
source value + what showed up in the generated file, so the real cause can
be confirmed and fixed for good.

### Bank statement upload (multiple files)

The "Check against bank statement" page accepts one **Global IME Bank**
statement and up to **3 Prabhu Bank** statement files (e.g. separate daily
exports), combined into one file automatically before checking. The file
inputs support the browser's native multi-select (ctrl/shift-click all the
files in one dialog), **and** also let you open "Choose files" more than
once — each pick is *added* to your selection (shown as removable chips)
instead of replacing it, up to the 3-file Prabhu limit. This avoids the
common trap where a plain multi-file `<input>` silently drops everything
except the most recent pick if files are selected one at a time across
separate dialog opens.

### Double-reversal prevention

Before writing a new reversal file, the app looks at the **most recently
generated file that has already been marked "passed"** (not just any
generated file) and collects every Network Reference Id that already
appears in its `coop` / `imeremit` / `cityremit` sheets. Any row in
the new upload whose Network Reference Id is already in that set is skipped
(and counted as `duplicate_skipped` in the audit log / result page / day
reconciliation popup) instead of being reversed a second time.

### UI

- **Generate page** (`/`): a 3-column layout — upload form on the left,
  the central **"Shared passed reports"** list in the middle (bigger, since
  it's the shared source of truth), and your own **"My activity"** on the
  right. Both activity tables are paginated 10 rows at a time, and file
  columns are icon-only download buttons (hover for the filename).
- **Audit Log** (`/audit-log/`, admin only): every file from every user,
  passed or not, with Prabhu-reroute / unrecognized-bank / duplicate-skip
  counts per row.
- **Dashboard** (`/dashboard/`, admin only): all-time totals (including
  amount + charge for success, failed, manual reversal, and system
  reversal) plus a day-by-day breakdown. **Click any day** to open a full
  reconciliation popup: manual reversal vs. system reversal vs. failed vs.
  success, each with count/amount/charge, the day's failure-reason
  breakdown, and the usual double-reversal / Prabhu-reroute / unrecognized-
  bank checks. Charge follows the same business rule as the generated file
  itself — NCHL-routed transactions carry their charge inside the amount,
  so their charge is treated as 0 everywhere charge is summed. Also
  includes two **separate** reports: a **Member-wise report** (success /
  failed / reversal count + amount rolled up per Member Name, across every
  aggregator) and an **Aggregator-wise report** (same, rolled up per
  Aggregator instead) — kept as two independent tables/tabs rather than one
  mixed (Member, Aggregator) table, since they answer different questions.
  A separate **"General" tab** (`/dashboard/general/`) shows the same kind
  of Member/Aggregator/Issuer/Acquirer/amount-bucket breakdown but pulled
  live from the switch database for any date range, independent of
  whether anyone has uploaded or fetched a file for that period (see
  [Fetching data straight from the switch DB](#fetching-data-straight-from-the-switch-db)).
- "Manual reversal" vs. "system reversal": a `REVERSAL` row is a **manual**
  reversal if its Source Message says so (these are the ones written into
  the generated file); any other `REVERSAL` row is a **system** reversal
  (the switch already reversed it automatically) — not written to the
  file, but still counted for reconciliation. **Exception:** a system
  reversal that is either **On-Us** (Global-to-Global or Prabhu-to-Prabhu)
  or **NCHL-routed** is written out to its own sheet in the generated file
  (`Onus Checked-System Reversal`) instead of only being counted, so it
  can still be reviewed.

### On-Us verification (Prabhu + Global)

"On-Us" means the Debtor Bank and Creditor Bank are the *same* one of our
own two banks — **Global-to-Global or Prabhu-to-Prabhu** (previously this
only covered Global-to-Global). On-Us rows get an extra "is this actually
already successful?" check, matching how NCHL rows are verified:

- On the **failed** sheet, an On-Us row whose Network Reference Id shows up
  as a **duplicate DR** in the bank statement (both legs of the transfer
  actually completed) is green-flagged and moved to "Already Success
  (OnUs)" instead of being treated as needing a reversal.
- On the **manual reversal sheets** (coop / imeremit / cityremit / prabhu),
  the same duplicate-DR check now also runs — catching an On-Us row that
  ended up there **by mistake** even though it was already successful —
  and red-flags it as already-reversed so nobody reverses it a second
  time. This mirrors the existing NCHL ref-id check on the same sheets.

### File naming

- Every uploaded source file is renamed/stored as `ibft_txn_data_<date>.xlsx`
  (date parsed from the original filename if it has one, otherwise today's
  date) — whatever it was originally called.
- Every generated file is `need_to_reversal_<date>.xlsx`. Downloads always
  use this clean name (via a dedicated download view), even though the
  file actually stored on disk may have a suffix Django added to avoid a
  naming collision.

### Notes / things worth knowing

- The app reads the source workbook's **"Transactions"** sheet (or the first
  sheet if that name isn't found) and auto-detects the header row, so a stray
  "Period: ..." banner row above the header (as in the sample file) is
  handled automatically.
- If the uploaded file is missing an expected column, the app shows a clear
  error on the upload page and records the failure in the audit log — it
  won't silently produce a bad file.
- `DATA_UPLOAD_MAX_MEMORY_SIZE` / `FILE_UPLOAD_MAX_MEMORY_SIZE` are raised to
  50 MB in `settings.py` since these exports can be large (the sample file
  was ~4.5 MB / ~29,000 rows). Raise further if needed.
- This ships with `DEBUG = True` and a placeholder `SECRET_KEY` — fine for
  running locally / on a trusted internal network, but change both (and set
  `DEBUG = False`, configure `ALLOWED_HOSTS` properly, rotate the two
  hardcoded passwords, and put uploaded/media files somewhere
  access-controlled) before exposing this beyond your own machine, since
  transaction files are sensitive.
- The "Prabhu Bank" check matches the **Debtor Bank** column case-
  insensitively containing "PRABHU", so "Prabhu Bank", "Prabhu Bank
  Limited", etc. all match.
- The double-reversal check only looks at the single most recent prior
  successful file, matching the "one step behind" comparison that was
  asked for. If you skip a day (or need to check further back), the
  `extract_reversal_network_reference_ids()` helper in `core/services.py`
  can be pointed at any older generated file too.

---

## Reconcile (`reconcile` app)

Takes the same kind of `TransactionReport` / `ibft-transaction_*.xlsx`
export and reconciles **every** transaction in it — SUCCESS, FAILED, and
REVERSAL alike — against your actual bank statements, producing a full
audit rather than a to-do list. No separate reversal file is needed: FAILED
rows, manual-reversal rows, and system-reversal rows are all identified
directly from the transaction file, and the expected refund narration is
synthesized the same way the reversal file itself builds it.

### What it checks

- **SCT Network** (cross-bank, Debtor != Creditor): CR on the debtor bank's
  statement + DR on the creditor bank's statement, same Network Reference
  Id, each at the transaction's own amount. **On-Us** (Debtor == Creditor):
  a CR *and* DR both present on that one bank's statement, each at the
  right amount, is enough — most on-us transfers never touch the statement
  at all and are reconciled by default.
- **Duplicate debit/credit detection**: presence alone isn't the whole
  check — a legitimate transfer posts **exactly one** matching CR and
  **exactly one** matching DR for its reference id and amount. If the
  statement shows **two or more** (a settlement retried or double-posted
  on the bank's side — the switch's own ACQ_SETTL on-us settlement rows
  have shown this in practice), it's flagged as a **possible duplicate
  debit/credit** for manual review instead of being silently marked
  reconciled. Applies to SCT on-us, SCT cross-bank, and the NCHL/Khalti
  direct reference-id-tagged settlement path alike; any count above one is
  caught (double, triple, or more), with the exact counts named in the
  flag reason.
- **NCHL / Khalti**: these settle through the issuing bank's own statement,
  but the CR/DR legs don't share a Network Reference Id — matched instead
  by beneficiary name + masked settlement account anchors (or a direct
  reference-id-tagged settlement line, where present, which is also
  subject to the duplicate-leg check above). A transaction that settled
  and was **later reversed** is labeled "Settled then Reversed" and still
  counted as reconciled — it's a legitimate, complete outcome, not a
  problem.
- **FAILED rows**: checked against the statement in case they actually
  went through despite the FAILED status (flagged for review if so). If
  there's no CR at all, it's a clean ordinary failure — nothing to do. If
  there's a CR but the row is FAILED, it's treated exactly like a manual
  reversal candidate (see below).
- **Manual reversal rows** (and FAILED-but-credited rows): the debtor's
  statement is searched for a DR whose remarks carry the expected refund
  narration. Zero matches falls back to the same reversal/NCHL/Khalti
  anchor checks used elsewhere, then "Pending" if still nothing; exactly
  one match is reconciled; two or more is flagged as a **possible double
  reversal**.
- **System reversal rows**: checked with the same "did this actually
  succeed anyway" logic as FAILED rows — flagged if the statement shows
  the transaction had already completed successfully before the system
  reversed it.
- **Timeout rows**: never resolved to SUCCESS/FAILED/REVERSAL by the
  switch, so there's nothing to check them against — listed on their own
  sheet rather than silently dropped.

Every SUCCESS row is also bucketed by amount range (Up to Rs. 5,000 /
5k-10k / 10k-25k / 25k-50k / 50k-100k / above 100k), split On-Us/Off-Us —
the same ranges the reversal app's own dashboard uses, so a monthly
submission built from either app lines up with the other.

### Bank statements — 18 banks, several export shapes

The upload form accepts a statement per bank, from the 18-bank SCT network
universe (`reconcile/banks.py`) — Global IME Bank and Prabhu Bank (our own
issuer banks) plus 16 member banks. You can select more than one file per
bank in one run (e.g. the 10 Aug **and** 11 Aug statement), since a
transaction near midnight often only posts on the bank's next EOD run.

Statement exports aren't all the same shape, and this is handled
automatically per bank (`reconcile/statements.py`):

- The common shape (S.N / ENTRY TYPE / REMARKS / AMOUNT / DATE) — read via
  the same reader `core` already uses.
- Rastriya Banijya Bank's WITHDRAW/DEPOSIT/DESCRIPTION column layout.
- Garima Bikas Bank's MainCode/TranDate/Desc1-5/Type/Amount/TranId layout.
- Agricultural Development Bank's (ADBL) raw core-banking ledger export —
  as `.xlsx`, legacy `.xls` (pre-2007 Excel Binary — ADBL sends this as
  often as a real `.xlsx`, read via `xlrd` since `openpyxl` can't open
  it), `.csv`, **or `.pdf`** (its tables are extracted page-by-page).

A statement that fails to parse (including an unsupported/unreadable file
format, e.g. uploading a `.pdf` where a bank's parser only understands
`.xlsx`/`.csv`), comes back with fewer than 5 rows, or doesn't reference a
single one of this run's own transactions anywhere in its remarks is
treated as **unavailable for this run** rather than risking every one of
its transactions being wrongly flagged — it lands on the "No Statement"
sheet instead. Every such exclusion is recorded as a **warning**, shown
right on the result page (and as a "⚠ N warnings" badge on the run's "My
activity" card) — not just buried in the generated report's own Warnings
sheet — so an excluded statement is never silently invisible on the site
itself.

### Report & dashboard

Each run produces:
- A downloadable **`.xlsx` report** (`report.py`) with a sheet per
  category above, plus a combined **Need To Reversal** sheet listing every
  reconciled/pending/flagged reversal candidate as one audit trail.
- A **`.zip` bundle** of the transaction file plus every statement
  uploaded alongside it, so the whole source-document set behind a report
  is one download.

The Reconcile dashboard is embedded on the same page as the Reversal one
(`/dashboard/#reconcile`), filterable by date range, with:
- Volume and by-network (SCT/NCHL/Khalti) totals
- A Failed On-Us/Off-Us reason breakdown
- A day-by-day breakdown (click a day for the full instant-result view,
  same as a single run's own result page)
- The success amount-range bucket report
- Four Excel exports: bucket report, failed On-Us/Off-Us, day breakdown,
  and a headline summary

The Reconcile audit log is likewise embedded on the shared Audit Log page
(`/audit-log/#reconcile-log`).

### Notes / things worth knowing

- Every matching decision delegates to `core.services` — the same,
  already-tested functions the `core` app uses to check a
  `need_to_reversal` file against a bank statement. `reconcile/engine.py`
  is the *dispatch* layer (which check applies to which row), not a
  reimplementation.
- Internal settlement bookkeeping rows the switch logs for its own audit
  trail (e.g. `KHALTI_SETTL/00000000PLR6`) are excluded from SCT stats —
  they're not an independent transfer, just the payout leg of an
  already-reconciled NCHL/Khalti transaction.
- Network Reference Ids are isolated out of statement remarks using this
  run's own known reference ids rather than a hardcoded id-length regex,
  since different banks embed them differently (smooshed against account
  numbers, joined with `|` instead of `/` or `:`) and the switch's id
  format isn't guaranteed to stay the same length forever.
- An unreadable/wrong-format upload (transaction file *or* any bank
  statement) no longer crashes the page with a raw Django error — every
  `openpyxl`/`xlrd` open is caught and turned into the same clean
  in-page error (transaction file) or per-bank warning (statement) the
  rest of this section describes, so a bad upload never surfaces as a
  500.
