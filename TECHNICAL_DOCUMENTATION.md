# Technical Documentation

This is the deep-reference document for the codebase — how the two Django
apps (`core` and `reconcile`) actually work internally, their data models,
and the business rules baked into the reconciliation logic. For "how do I
run this" see [README.md](README.md); this document is "how does it work
and why."

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Project layout](#2-project-layout)
3. [Data models](#3-data-models)
4. [`core` app — Reversal generation](#4-core-app--reversal-generation)
5. [`core` app — Bank statement cross-check](#5-core-app--bank-statement-cross-check)
6. [`core` app — Extra page (admin utilities)](#6-core-app--extra-page-admin-utilities)
7. [`core` app — Dashboard & reporting](#7-core-app--dashboard--reporting)
8. [`reconcile` app — architecture](#8-reconcile-app--architecture)
9. [`reconcile` app — the reconciliation engine](#9-reconcile-app--the-reconciliation-engine)
10. [`reconcile` app — reporting & dashboard](#10-reconcile-app--reporting--dashboard)
11. [Auth & permissions model](#11-auth--permissions-model)
12. [`core.switch_db` — read-only "Fetch from DB" + General report](#12-coreswitch_db--read-only-fetch-from-db--general-report)
13. [`core.scheduler` — background alert & report jobs](#13-corescheduler--background-alert--report-jobs)
14. [Known limitations / operational notes](#14-known-limitations--operational-notes)

---

## 1. Architecture overview

A Django 5 project (`reversal_project/`) with two independent-but-related
apps sharing one login system and one "Extra" utility page:

- **`core`** — takes the daily `ibft-transaction_*.xlsx` export from the
  switch and produces a `need_to_reversal_*.xlsx` file: which transactions
  need a manual reversal, grouped and formatted exactly like the manual
  process it replaces. Optionally cross-checked against an actual bank
  statement afterward to catch double-reversals and failed-but-actually-
  credited transactions.
- **`reconcile`** — a broader, from-scratch reconciliation of the *same*
  kind of switch export against **every** bank statement you have (up to
  18 banks), producing a full SUCCESS/FAILED/REVERSAL audit rather than
  just a reversal to-do list.

Both apps are plain server-rendered Django (no SPA/API layer, no JS
framework) — views build context dicts, templates render HTML, and the
"download" affordance is either a `FileResponse` off a stored
`FileField`, or (for the one in-memory-only feature — Verification
format) a base64 data URI embedded directly in the page.

Both apps' upload flows also accept **"Fetch from DB"** as an alternative
to a manual file upload — pulling the same shape of data straight out of
the switch's own Postgres database for a chosen date range (§12) — and a
shared **in-process scheduler** (§13) runs two background jobs (a
per-minute dispute/timeout alert, a 09:00 daily report) independent of any
user action.

Storage: SQLite (`db.sqlite3`), file uploads/outputs under `media/`.
No task queue, no caching layer beyond a single `lru_cache` for the
bank-account lookup table (see §11).

---

## 2. Project layout

```
reversal_project/
├── manage.py
├── requirements.txt
├── db.sqlite3
├── media/                      # uploads, generated outputs, bundles
├── reversal_project/           # Django project settings/urls
│   ├── settings.py
│   └── urls.py                 # mounts "/", "reconcile/", "admin/"
├── core/                       # the "reversal" app
│   ├── models.py                # ProcessingLog, SeenNetworkReferenceId,
│   │                             # MemberAggregatorStat, BankAccount,
│   │                             # VerificationBankContact, MailSignature,
│   │                             # UserAccess, ScheduledReportRecipient,
│   │                             # SchedulerJobState, AlertedTimeoutTransaction,
│   │                             # UserNotificationPreference, DisputeNotificationEvent
│   ├── forms.py                 # UploadForm (incl. DbFetchForm),
│   │                             # BankStatementUploadForm, BankAccountForm,
│   │                             # CreateUserForm, UpdateUserForm,
│   │                             # VerificationFormatUploadForm
│   ├── services.py               # ~2000 lines — ALL business logic
│   ├── switch_db.py              # read-only switch-DB connector ("Fetch from DB")
│   ├── general_report.py         # live analytics straight from the switch DB
│   ├── scheduler.py              # dispute/timeout alert + daily report jobs
│   ├── permissions.py            # UserAccess-backed per-feature gating
│   ├── apps.py                   # starts the scheduler once per real server process
│   ├── views.py                  # ~1600 lines
│   ├── admin.py                  # custom UserAdmin + BankAccount/ProcessingLog
│   ├── urls.py
│   ├── report_constants.py       # MONTH_NAMES, REASON_ORDER
│   └── templates/core/
└── reconcile/                   # the broader "reconcile" app
    ├── models.py                 # ReconcileRun
    ├── banks.py                   # the 18-bank SCT network registry
    ├── transactions.py            # reads the TransactionReport export
    ├── statements.py              # reads bank statements (csv/xlsx/pdf)
    ├── engine.py                  # ~970 lines — the reconciliation engine
    ├── report.py                   # builds the downloadable .xlsx report
    ├── dashboard.py                # dashboard aggregation + exports
    ├── forms.py
    ├── views.py
    └── templates/reconcile/
```

---

## 3. Data models

### `core` app (`core/models.py`)

**`ProcessingLog`** — one row per `need_to_reversal_*.xlsx` generation
run. This is the widest model in the project (~90 fields) because it's
the single source of truth for both the result page and the analytics
dashboard, and it's mutated twice: once by `process_ibft_file()` at
generation time, and optionally a second time by
`apply_bank_statement_to_reversal_file()` when someone runs the "Check
bank statement" step against it later (§5).

Key groups of fields:
- Identity/status: `uploaded_file`, `generated_file`, `status`, `error_message`
- **`passed` / `passed_by` / `passed_at`** — the review gate. A file only
  joins the shared list and the dashboard once someone explicitly marks
  it passed (§7, §11).
- Raw counts from generation: `success_count`, `failed_total`,
  `failed_kept`, `reversal_manual_kept`, `reversal_system_count`,
  `timeout_count`, plus On-Us/Off-Us splits and `{reason: count}`
  breakdowns (`failed_reason_breakdown*`).
- Dedup bookkeeping: `duplicate_skipped` (double-reversal prevention),
  `duplicate_source_skipped` (overlapping-upload-window prevention).
- Bank-statement-check fields (only populated once checked):
  `failed_credited_count/amount/charge`, `already_reversed_count` and
  its NCHL/Khalti/onus-success sub-breakdowns, `onus_already_success_*`,
  `onus_system_reversal_flagged_*`. See §5 for exactly what each means
  and where it lands on the dashboard.

**`SeenNetworkReferenceId`** — every Network Reference Id ever seen in
any upload, used to skip rows already covered by an overlapping prior
upload (`duplicate_source_skipped`).

**`MemberAggregatorStat`** — one row per (Member Name, Aggregator) per
upload; rolled up across every passed upload for the dashboard's
Member-wise / Aggregator-wise reports.

**`BankAccount`** — admin-editable Debtor-Bank → Debit-Account mapping
(§11). Global IME Bank and Prabhu Bank ship as the seeded defaults;
adding a row here is how a new bank gets a debit account without a code
change.

**`VerificationBankContact`** — Creditor-Bank → To/Cc email mapping used
by the Verification format tab's "Send mail" button (§6): a converted
dispute file's rows are grouped by Creditor Bank, and any group whose bank
matches an active row here (by `keyword`) can be emailed in one click. A
group with no matching contact still shows its count but the button stays
disabled — nothing is ever sent to a guessed address.

**`MailSignature`** — per-user email signature(s) (name, title, mobile,
company/address/toll-free/website, each individually overridable or left
blank to fall back to the `MAIL_SIGNATURE_*` settings/.env defaults) used
to sign outgoing verification and scheduler-adjacent notification emails.
`user` is nullable only to keep pre-per-user rows working as a fallback;
`resolve_mail_signature()` (`core/services.py`) picks the sending user's
own most-recently-updated active row first, then a legacy `user=None`
row, then the settings defaults. Not gated by `UserAccess` — every user
manages their own regardless of role.

**`UserAccess`** — one row per user, one boolean per gated feature area
(`can_reversal`, `can_reconcile`, `can_verification_format`,
`can_check_statements`, `can_audit_log`, `can_bank_contacts`,
`can_issuer_bank_accounts`, `can_make_users`, `can_scheduler`). A
superuser bypasses this entirely (§11); this is the actual access-control
model for everyone else, replacing what used to be a flat `is_staff`
admin/regular split.

**`ScheduledReportRecipient`** — who `core.scheduler`'s two background
jobs email, one row per recipient group per `report_type`
(`dispute_alert` / `daily_report`). Multiple active rows for the same
`report_type` are all merged into one send (their To/Cc lists combined) —
same shape as `VerificationBankContact.to_emails`/`cc_emails`, just not
keyed by bank.

**`SchedulerJobState`** — one row per background job: whether it's
currently enabled, and its last-run outcome/timestamp. The Extra page's
Scheduler tab toggle reads/writes this row rather than talking to the
scheduler process directly — see §13 for why that's what actually makes
Start/Stop work regardless of how many worker processes serve the app.

**`AlertedTimeoutTransaction`** — dedup ledger keyed by Network Reference
Id, one row per transaction that has already gone out in a dispute/
timeout alert email. Necessary because each check's query window
deliberately overlaps the previous one (§13) — this table, not the
window, is what guarantees a transaction is never alerted twice.

**`UserNotificationPreference`** — one row per user: whether they've
turned on the "Desktop alerts" checkbox (Extra page → Scheduler tab, next
to the dispute/timeout alert's Start/Stop row, §6). Self-managed like
`MailSignature` — no `UserAccess` gate, since it's a personal browser
preference rather than a role-based feature.

**`DisputeNotificationEvent`** — one row per `check_dispute_timeouts()`
run that found new timeouts: just `count` + `created_at`. Written
independently of whether the email actually had recipients configured
(§13), so the browser-side poll (below) still fires even with no
`ScheduledReportRecipient` rows set up. This is a separate, in-browser
signal from the email alert — not a replacement for it.

### `reconcile` app (`reconcile/models.py`)

**`ReconcileRun`** — one row per reconciliation run. Unlike
`ProcessingLog`, this model stores mostly **counts**, not amounts — see
§9/§10 for exactly why and where amounts *do* get tracked
(`failed_onus_amount`/`failed_offus_amount`, and the
`success_buckets_onus`/`success_buckets_offus` JSON fields).

Per-category `total` / `reconciled` / `flagged` / `pending` /
`no_statement` counters exist for: SCT, NCHL, Khalti, Failed, Manual
reversal, System reversal — plus `need_reversal_count`,
`grand_total_reconciled`, `grand_total_outstanding`. Same `passed`
review gate as `ProcessingLog`. Stores three files: the generated report
(`report_file`), the original transaction upload (`transaction_file`),
and a `.zip` bundling the transaction file + every statement uploaded
alongside it (`bundle_zip`) so the whole source-document set is a single
download.

**`warnings`** (`JSONField`, list of strings) — every non-fatal issue
`write_combined_statement_csv()` raised while reading this run's
statements (a bank's file failed to parse, came back suspiciously thin,
or matched none of this run's own reference ids — see §8). A bank listed
here still lands in `statements_missing` and every one of its
transactions on the "No Statement" sheet even though a file *was*
uploaded for it; this field is what actually explains why, and is
rendered on the result page, the "My activity" card (a "⚠ N warnings"
badge), and the Statements section (which names the actual banks used/
missing, not just counts) — previously this reasoning only existed
inside the generated `.xlsx`'s own Warnings sheet, invisible anywhere on
the site itself.

---

## 4. `core` app — Reversal generation

Entry point: `core/services.py::process_ibft_file(input_path, output_path,
previously_reversed_ids, seen_reference_ids)`, called from
`core/views.py::upload_view`.

**Input**: the switch's `ibft-transaction_*.xlsx` export, 23 fixed
columns (`REQUIRED_COLUMNS` in `services.py`) — `_find_header_row()`
auto-detects the real header row even if there's a "Period: ..." banner
row above it (as in the real export).

**Row classification** by `Overall Status`:
- `SUCCESS` → counted, not written anywhere.
- `FAILED` → split into `failed` (kept — needs no reversal, just review)
  vs. dropped entirely if the reason is "Insufficient funds" (already
  self-evidently a non-issue).
- `REVERSAL` → split into **manual** (Source Message says so — these get
  written to the output file) vs. **system** (the switch already
  reversed it automatically — counted but not written, *except*: an
  On-Us or NCHL-routed system reversal is written to its own
  `Onus Checked-System Reversal` sheet instead of only being counted, so
  it stays reviewable).
- `TIMEOUT` → written unchanged to its own `timeout` sheet. **Not**
  cross-checked against a bank statement at all (§5, §12 — a known gap).

**Manual-reversal rows** are further split across sheets by Aggregator/
Debtor Bank: `coop` (everything else, grouped by Member Name), `imeremit`
(Aggregator = IME REMIT), `cityremit` (Aggregator = CITY REMIT), `prabhu`
(Debtor Bank = Prabhu Bank, kept separate so it can be worked/checked on
its own).

**Per-row derived fields** on every written reversal row:
- **Debit Account Number** — resolved via `resolve_debit_account()`
  against the `BankAccount` table (§3/§11): the clearing account for
  whichever bank matches the row's Debtor Bank keyword, defaulting to
  the hardcoded Global/Prabhu constants if nothing's configured.
- **Credit Account Number** — the *original* row's Debit Account Number
  (money goes back where it was pulled from).
- **Narration** — `REV` + the Member Transaction Id with its
  prefix/leading zeros stripped + `-` + Session Id.
- All id-like columns (Member Transaction Id, Network Reference Id,
  Session Id, Debit/Credit Account Number) are force-written as **text**
  (`_apply_id_text_format()`), never numbers — long numeric ids silently
  lose trailing digits if Excel is allowed to store them as a number.

**Double-reversal prevention** (`extract_reversal_network_reference_ids()`):
before writing a new file, the app reads every Network Reference Id out
of the *most recently generated file that's already been marked passed*
and skips any row that repeats one of those ids — counted as
`duplicate_skipped`.

**Overlapping-upload-window dedup** (`SeenNetworkReferenceId`, §3): any
row whose ref id was already processed in *any* earlier upload (any
status) is skipped before classification — counted as
`duplicate_source_skipped`. This is separate from double-reversal
prevention: it catches the "today's export re-covers part of yesterday's
time window" case, not just repeated reversal rows.

---

## 5. `core` app — Bank statement cross-check

Entry point: `core/services.py::apply_bank_statement_to_reversal_file()`,
called from `core/views.py::bank_statement_upload_view` (the "Check bank
statement" tab). This is a **second, optional pass** over an
already-generated file — it re-opens the saved workbook, cross-checks
row-by-row against an uploaded statement, colors matching rows in place,
and saves the workbook back. Nothing is deleted or reordered, only
highlighted.

### The statement index

`build_bank_statement_index()` parses the statement (CSV or XLSX;
columns `ENTRY TYPE` / `REMARKS` / `AMOUNT` / `DATE`, `SOURCE` optional)
into a `BankStatementIndex`:
- `by_token`: every `/`- or `:`-delimited token in REMARKS → list of
  statement rows containing it (this is how a Network Reference Id
  embedded in REMARKS gets found).
- `iso_entry_types` / `entries`: for the trailing "own switch id" chase
  (e.g. `.../S43161794`) used to detect a reversed-back CR.

`source` restricts a lookup to one bank's lines only (`"global"` /
`"prabhu"`), so a Prabhu-debtor row never matches a Global IME line that
happens to be part of the same combined upload — see
`_expected_statement_source()`.

### The checks, and what each one catches

| Function | What it detects | Applied to |
|---|---|---|
| `is_failed_but_credited()` | A `FAILED` row's ref id shows up *at all* in the statement — money moved despite the reported failure | `failed` sheet |
| `has_duplicate_dr()` | An On-Us ref id shows up as **DR twice** — both legs of the transfer actually completed | `failed` sheet + every manual-reversal sheet |
| `is_onus_already_success()` | Looser sibling of the above: ref id shows CR + a DR entry whose remarks carry a real (2+-word, alpha) beneficiary name — catches genuine success even without a literal duplicate DR | same |
| `is_already_reversed()` | A manual-reversal row's CR's own trailing ISO id shows up inside a DR elsewhere in the statement — already reversed once, don't do it again | manual-reversal sheets |
| `is_already_debited_nchl()` | NCHL-specific: CR and DR legs for the same payment share **no** ref id or ISO id at all, only the beneficiary name + masked settlement account id (`0{5,}[A-Z0-9]{2,}` pattern) | manual-reversal sheets, when Payment Processor contains "NCHL" |
| `is_already_debited_khalti()` | Khalti-specific sibling: DR leg carries the shared masked account id behind a literal `KHALTI_SETTL/` marker (no beneficiary name available) | manual-reversal sheets, when Aggregator/Payment Processor contains "KHALTI" |

Both NCHL and Khalti checks include a **reversal-of-reversal guard**: if
the matched "already debited" DR's own trailing ISO id shows up as a
*CR* elsewhere (i.e. NCHL/Khalti reversed their own settlement debit
back in), the row is **not** flagged — it goes through as a normal,
still-pending manual reversal instead of being wrongly marked resolved.
Verified by targeted tests against both the manual-reversal sheet and
the On-Us/NCHL system-reversal sheet (see §12 testing note).

A bare `"SCT"` token in statement REMARKS (a common settlement-code
artifact, not a name) is explicitly excluded from the beneficiary-name
match in `is_onus_already_success()` — the name check requires 2+ words,
all-alphabetic, which a lone `"SCT"` token never satisfies.

### Where flagged rows end up (bucket reclassification)

This only happens once a file has actually been through this check —
an unchecked file shows its raw generation-time split.

| Flag | Moves out of | Moves into |
|---|---|---|
| Red — failed-but-credited (`failed` sheet) | **Failed** | **Total reversal needed** (`total_reversal_count`), tracked as its own "Failed but credited" line |
| Red — already-reversed (any manual-reversal sheet: generic/NCHL/Khalti/on-us-dup) | **Manual reversal pending** | **System reversal** (already resolved, grouped with other already-handled rows) |
| Green — on-us duplicate-DR success (`failed` sheet) | **Failed** (count *and* amount) | **Success** (count *and* amount) — the only flag that reclassifies all the way to Success |
| Red — On-Us/NCHL system-reversal sheet re-flag | *(no bucket move)* | Stays counted under **System reversal** as before; the flag is a "go look at this specific one" annotation only |

See `core/views.py::_reclassify_log()` and `_compute_totals()` for the
exact arithmetic. Nothing flagged is ever dropped from the report — every
flagged row's amount is accounted for somewhere; it's reclassified into
the bucket that reflects its actual resolved state, not silently
discarded.

---

## 6. `core` app — Extra page (admin utilities)

Single page (`core/templates/core/bank_statement_upload.html`), tabbed,
served by `bank_statement_upload_view` / `verification_format_view` /
`create_user_view`, each rendering the **same** template with different
context (see `_extra_page_context()` in `views.py`):

- **Check bank statement** — the tab from §5. Available to any logged-in
  user (restricted to their own generated files unless staff).
- **Add bank account** — `is_staff`-gated. A quick form over the
  `BankAccount` model (§3); full CRUD (edit/delete) is still Django-
  admin-only, this is add-only.
- **Make user** — `is_superuser`-gated (stricter than the other tabs on
  purpose, since it can grant staff/admin rights). `CreateUserForm` /
  `UpdateUserForm` in `core/forms.py`: username, email, password,
  Active/Staff/Admin checkboxes only — deliberately skips Django's
  group/permission editor (mirrored in the Django admin itself, see
  §11). Includes a **Users** table below the form (list + per-row
  Edit/Delete), with guards against deleting or de-privileging your own
  account. Create/update render the *same* response with a
  green-success / red-error **popup** instead of a redirect+messages
  banner, so there's no page reload around the outcome.
- **Verification format** — upload a dispute-transaction export
  (`DisputeTransaction.xlsx`-shaped, same 23-column layout as the ibft
  export) and convert it, **entirely in memory**, to the 13-column
  format sent to the bank for verification
  (`build_verification_format()` in `services.py`). Nothing is written
  to disk or the database at any point — the converted workbook is
  base64-encoded straight into the HTML response and offered as a
  download popup (a `<a download href="data:...;base64,...">` link). If
  the user never clicks download, nothing of that run persists anywhere.
  The converted rows are also grouped by **Creditor Bank** right on the
  page; a group whose bank matches an active `VerificationBankContact`
  gets a **"Send mail"** button (`verification_send_mail_view`) that
  builds and sends the email straight from the server (signed with the
  sending user's `MailSignature`, `_attach_signature_image()` inlining the
  banner image by Content-ID) — a group with no matching contact shows its
  count with the button disabled. Bank contacts themselves are managed
  from an "Add/update contact" form on the same tab
  (`add_verification_bank_contact_view` / `update_verification_bank_contact_view`).
- **Mail signature** — every logged-in user (regardless of `UserAccess`,
  see §11) manages their own `MailSignature` row(s) here
  (`add_mail_signature_view` / `update_mail_signature_view`): name, title,
  mobile, and optional company/address/toll-free/website overrides. The
  most recently updated active row is what `resolve_mail_signature()`
  picks for that user's outgoing verification/notification emails (§3).
- **Scheduler** — `can_scheduler`-gated. Toggle either background job
  on/off (`scheduler_toggle_view`, flips `SchedulerJobState.is_enabled` —
  see §13 for why a DB flag rather than actually starting/stopping the
  APScheduler job is what makes this work across worker processes) and
  manage each job's `ScheduledReportRecipient` rows
  (`add_scheduled_recipient_view` / `update_scheduled_recipient_view` /
  `delete_scheduled_recipient_view`) — comma-separated To/Cc per group,
  every active group for a `report_type` merged into one send. Also hosts
  the **"Desktop alerts"** checkbox, next to the dispute/timeout alert's
  Start/Stop row — see below.

### Desktop alerts (in-browser Notification + spoken TTS)

A self-service checkbox (any logged-in user, no `UserAccess` gate — same
model as Mail signature), toggled via `toggle_desktop_notifications_view`
(flips `UserNotificationPreference.desktop_notifications_enabled`). Its
JS lives inline in `bank_statement_upload.html` (not `base.html` — it's
scoped to this page, so it only runs while this tab is open somewhere):

- Turning it on calls the browser's `Notification.requestPermission()`
  first; the preference is only persisted server-side once permission is
  actually granted.
- While on, it polls `poll_dispute_notifications_view` every 25 seconds
  with `after_id` (the highest `DisputeNotificationEvent.id` this browser
  has already alerted on, tracked in `localStorage` keyed by username).
  The very first poll omits `after_id` entirely so the browser syncs its
  cursor to "now" instead of alerting on every historical dispute.
- On a new count, it shows a browser `Notification` **and** speaks the
  same sentence via `SpeechSynthesisUtterance` — queued 3 times in a row
  (`SPEAK_REPEAT_COUNT`), since `speechSynthesis.speak()` queues rather
  than overlaps calls.
- Works as long as this tab stays open (including minimized/backgrounded
  or behind a locked screen) — stops once it's actually closed. This is
  a deliberate scope decision: a true always-on background alert (working
  even with every browser closed) would need a native OS-level helper
  outside the browser entirely, which was tried and then explicitly
  dropped as unnecessary complexity for this project.

### Extra page layout

Above the 780px breakpoint, `bank_statement_upload.html` lays the tabs
out as a **left sidebar** (`.extra-sidebar`, vertical list, sticky while
scrolling) next to a wider `.extra-content` panel — `main` renders with
`{% block main_class %}wide{% endblock %}` (1360px cap instead of the
site's usual 960px) to give the two columns room. Below 780px it
collapses to a wrapping row of small pill buttons instead of a sidebar (a
horizontal scroll-only strip was tried first and rejected — it hid every
tab past the first without an obvious way to reach them).
`showExtraTab()`'s show/hide-by-id logic is unchanged either way; only
the CSS layout of the tab buttons differs.

---

## 7. `core` app — Dashboard & reporting

`dashboard_view` (admin-only) only ever looks at
`ProcessingLog.objects.filter(status=SUCCESS, passed=True)` — an
unreviewed file never skews the numbers. Filtered by an inclusive
From/To calendar-date range (`_apply_year_month_filter()`).

Sections: headline totals (`_compute_totals()`), On-Us/Off-Us failed
breakdown with reasons, day-by-day breakdown (`_build_daily_stats()` —
shared verbatim between the on-screen view and its Excel export, so they
can never drift apart), monthly report, Member-wise report, Aggregator-
wise report (two independent tables, not one mixed table, since they
answer different questions), plus the `reconcile` app's own dashboard
sections folded into the same page (§10) sharing the same date filter.

Every export (`export_*_view` functions) builds an `openpyxl.Workbook`
in memory via shared style helpers (`_new_sheet`, `_write_table_header`,
`_autosize`, `_finalize_xlsx`) and returns it as a direct
`HttpResponse` attachment — a different, simpler pattern than the
Verification-format tab's popup (§6), since these already have
persisted `ProcessingLog` data behind them; there's no "in-memory only,
never saved" requirement here.

---

## 8. `reconcile` app — architecture

Upload flow (`reconcile_view` in `reconcile/views.py`):

1. Upload the day's `TransactionReport`/`ibft-transaction` export
   (`reconcile/transactions.py::load_transactions()` — same 14-column
   subset, same header-autodetect logic as `core`'s reader, but a
   separate implementation).
2. Upload statements for as many of the **18 known banks**
   (`reconcile/banks.py::BANKS`) as you have — each field accepts
   multiple files (a transaction near midnight often only posts on the
   bank's *next* day's EOD run).
3. `reconcile/statements.py::write_combined_statement_csv()` reads every
   uploaded statement (auto-detecting format — see below) and writes
   one combined CSV, tagging each row with which bank it came from.
4. `core.services.build_bank_statement_index()` (the **same** indexer
   §5 uses) builds the lookup index over that combined CSV.
5. `reconcile/engine.py::reconcile()` runs the actual matching (§9).
6. `reconcile/report.py::save_workbook()` writes the downloadable
   `.xlsx`; a `.zip` bundle of every source document is also built.
7. A `ReconcileRun` row is created (unreviewed — same `passed` gate as
   `core`) and everything is saved to `media/`.

### The 18-bank model (`reconcile/banks.py`)

Two banks (Global IME, Prabhu) are **our own issuer banks** — almost
every transaction's Debtor Bank is one of these two. The other 16 are
member banks money moves *to* over the SCT network. `identify_bank()`
matches a raw bank-name string against each bank's `keywords` tuple.
`available_by_default` marks which banks we currently receive a daily
statement export for at all (13 of 18 as of writing) — purely a UI hint
for the upload form; what actually counts as "missing" for a given run
is whatever file wasn't actually uploaded that time.

NCHL/Khalti-routed transactions land on banks **outside** this 18-bank
list entirely (Nabil, NIC Asia, Siddhartha, ...) — no statement access
to those, so NCHL/Khalti are verified purely against our own issuer
bank's statement instead (the same CR/DR-pair logic §5 uses).

### Multi-format statement parsing (`reconcile/statements.py`)

`read_statement_rows()` auto-detects the shape of an uploaded statement
and normalizes it to the same `{entry_type, remarks, amount, date}`
shape `core` expects:
- Plain CSV/XLSX with `ENTRY TYPE`/`REMARKS`/`AMOUNT`/`DATE` columns
  (the common case).
- A "withdraw/deposit" two-column shape (`_is_withdraw_deposit_shape()`)
  some banks export instead of a single signed entry-type column.
- Garima Bikas Bank's own layout (`_garima_statement_rows()`).
- **ADBL** (`_adbl_statement_rows()`) accepts all four of `.xlsx`, legacy
  `.xls`, `.csv`, or `.pdf` — in practice ADBL sends a genuine `.xlsx` as
  often as a pre-2007 Excel Binary `.xls` (same OLE2/BIFF format
  `openpyxl` cannot open at all — `xlrd` is used instead, the only
  maintained library that still reads it), or occasionally only a PDF.
  For the PDF case, `_pdf_table_rows()` extracts per-page tables via
  `pdfplumber`; ADBL's own PDF export doesn't repeat the header on later
  pages, so the header from page 1 is reused for every subsequent page's
  rows. All four formats converge on the same row list before the
  header-detection/parsing logic below ever runs, so nothing downstream
  needs to know which format the file actually was.

**Excel-reading is split across two loaders that both handle `.xls`,
implemented independently** (see §12's "two independent statement
readers" note): `reconcile/statements.py::_load_excel_rows()` (used by
`_adbl_statement_rows()` and `_is_withdraw_deposit_shape()`) and
`core/services.py::_load_excel_rows()` (used by the generic
`ENTRY TYPE`/`REMARKS` reader `_read_bank_statement_rows()` falls back
to). Both pick `openpyxl` for `.xlsx`/`.xlsm` or `xlrd` for `.xls` based
on the file's suffix, and both wrap the actual open in a `try/except`
that turns any failure (wrong/corrupt format, a `.pdf` or plain-text
file wearing an Excel-looking name, etc.) into a `ProcessingError`/`None`
instead of letting `openpyxl`'s/`xlrd`'s own exception propagate — this
is what stops a bad upload from surfacing as a raw Django 500 (see the
transaction-file reader `reconcile/transactions.py::load_transactions()`
for the same pattern, which raises `TransactionFileError` instead).

---

## 9. `reconcile` app — the reconciliation engine

`reconcile/engine.py::reconcile(transactions, index, uploaded_bank_keys)`
walks every transaction row once and classifies it by `Overall Status`:

- **`TIMEOUT`** — no CR/DR pattern to check (never resolved either way
  by the switch) — surfaced on its own sheet, neither reconciled nor
  flagged.
- **`FAILED`** — checked in this order: (1) `_already_succeeded()` — the
  statement shows it actually completed despite the FAILED status →
  **flagged**, needs review; (2) no CR at all in the statement → a
  clean ordinary failure, **reconciled** (nothing to do); (3) credited
  but FAILED → treated like a manual-reversal candidate via
  `_check_refund()` (reconciled if exactly one confirming DR found,
  flagged as a possible double-reversal if two or more, pending if
  zero).
- **`REVERSAL`**, split the same way `core` does (Source Message says
  "manual" vs. system-auto):
  - **Manual** — `_reversal_already_confirmed()` (a later NCHL/Khalti
    reversal, or a DR matched by narration prefix) wins over
    "already succeeded" evidence and is reported **Reconciled
    (Reversed)** rather than flagged — a deliberate correction (see the
    "settled then reversed" note in `engine.py`'s own top-of-file
    docstring): once something has genuinely settled, *either* staying
    settled *or* being reversed afterward is a complete, legitimate
    outcome, not an anomaly. Only flagged if the statement shows success
    **and there's no evidence of a subsequent reversal** — i.e. the
    reversal was plausibly unnecessary.
  - **System** — same "already succeeded despite the auto-reversal"
    check; flagged if so (needs review, matches `core`'s
    `onus_system_reversal_flagged_count` idea), reconciled otherwise.
- **`SUCCESS`** — this is where the SCT/NCHL/Khalti network-specific
  checks run. Presence of a matching CR/DR is necessary but **not**
  sufficient in any of the three cases below — the entry's own AMOUNT
  must also match what's expected (`_expected_statement_amount()`), and
  (see "Duplicate debit/credit detection" further down) there must be
  **exactly one** matching entry per leg, not more:
  - **SCT, on-us** (debtor bank == creditor bank): needs a statement for
    that one bank; no CR/DR at all is treated as reconciled (per
    instruction: most on-us transfers never touch the statement — see
    the top-of-file docstring for the full reasoning); CR **and** DR
    both present, each exactly once at the transaction's own amount →
    reconciled; only one of CR/DR present, or both present but neither
    at the right amount, or **more than one** matching CR or DR → flagged.
  - **SCT, cross-bank**: needs statements for *both* the debtor's and
    creditor's bank; reconciled only if exactly one CR at the right
    amount shows on the debtor's statement **and** exactly one DR at the
    right amount shows on the creditor's; a missing leg, a
    present-but-wrong-amount leg, or more than one matching CR/DR on
    either side is flagged, with a reason describing exactly which case
    it was.
  - **NCHL / Khalti**: fast path is the same reference id tagged
    directly on both a CR and DR leg at the network's own expected
    settlement amount (`_expected_statement_amount()` — Khalti nets its
    Rs. 10 charge out, NCHL backs out its own Rs. 10 then applies a
    tiered real-time charge on top); falls back to the anchor-matching
    `is_already_debited_nchl()` / `is_already_debited_khalti()` §5 logic
    (imported straight from `core.services`, matching on beneficiary
    name + masked settlement account instead of amount — the duplicate-
    count check below does not apply to this fallback path, since it has
    no reliable per-entry amount to count against) against our own
    issuer bank's statement.
  - **Duplicate debit/credit detection** (`_count_matching()`, all three
    cases above): a legitimate transfer posts each leg exactly once, so
    a reference id showing **two or more** CR entries or **two or more**
    DR entries at the exact expected amount — any count above one, not
    just exactly two — is flagged as a possible duplicate debit/credit
    instead of being marked reconciled, with the exact counts named in
    the reason (e.g. "Found 1 CR and 2 DR entries ... expected exactly
    one of each"). Added after a real ACQ_SETTL on-us settlement row was
    found silently reconciled despite the bank's statement showing the
    DR leg posted twice for the same reference id and amount — a
    presence-only check ("is there *a* CR and *a* DR") can't see this,
    only a per-leg count can.
  - Every SCT pair without an uploaded statement for one of its two
    sides is bucketed **no_statement**, not flagged — a missing
    statement isn't evidence of a problem.
  - A "settlement artifact" row (`_SETTLEMENT_ARTIFACT_RE` — internal
    bookkeeping for an already-reconciled NCHL/Khalti transaction, not
    a real independent transfer) is skipped entirely.

### What gets summed into an amount, vs. what's count-only

This is the exact answer to "where does a flagged transaction's amount
go":

- **Success amount buckets** (`success_buckets_onus`/`offus`, bucketed
  by `AMOUNT_BUCKETS` ranges) — computed for **every** SUCCESS row
  *before* any of the SCT/NCHL/Khalti checks above even run (see the
  comment right above `_add_to_bucket()` calls in `engine.py`). A
  transaction that later gets flagged is still sitting in this bucket
  total — it is never excluded.
- **Failed On-Us/Off-Us amount** (`failed_onus_amount`/`offus_amount`)
  — same pattern: accumulated for every FAILED row unconditionally,
  before the reconciled/flagged/pending branching runs.
- **Everything else** (SCT/NCHL/Khalti/manual-reversal/system-reversal
  `total`/`reconciled`/`flagged`/`pending`/`no_statement`) is **count-
  only** at the aggregate/summary level — no rolled-up "total flagged
  amount" figure exists for these. Nothing is hidden, though: every
  individual flagged/no-statement/timeout/need-reversal row carries its
  own `amount` field and is written out per-row on the corresponding
  detail sheet in the exported report (§10) — you just have to sum
  those yourself if you want a total, rather than reading one off the
  summary.

---

## 10. `reconcile` app — reporting & dashboard

`reconcile/report.py::save_workbook()` builds the per-run downloadable
`.xlsx` with one sheet per category: SCT/NCHL/Khalti Flagged, No
Statement, Timeout, the combined **Need To Reversal** sheet (every
reconciled/pending/flagged row together with its status, amount, and a
plain-English detail string), the Failed On-Us/Off-Us summary block, and
the **Success Amount Buckets** sheet (the primary amount-summary report
— range × On-Us/Off-Us × count/amount, with grand totals).

`reconcile/dashboard.py` mirrors `core`'s dashboard shape (same
`passed`-only filter, same date-range filter, same day-by-day /
bucket-report exports) but rolled up across `ReconcileRun` rows instead
of `ProcessingLog` rows — and is embedded **into `core`'s own dashboard
page** (`core/views.py::dashboard_view` imports straight from
`reconcile.dashboard`) so there's one Dashboard, not two separate ones.
`reconcile/views.py::day_detail_view` gives the "click a day" drill-down
(every passed run that day, summed into one set of totals via
`_aggregate_day_totals()` / `_day_bucket_rows()`) — the reconcile
equivalent of `core`'s dashboard day-click modal.

---

## 11. Auth & permissions model

No self-registration; Django's built-in `auth.User`, standard
session-based login (`core/views.py::BrandedLoginView`).

Access control is **per-feature**, driven by `core/permissions.py` and the
`UserAccess` model (§3) — not a blanket admin/regular split:

- **`is_superuser`** bypasses everything unconditionally
  (`permissions.has_feature()`'s first check) — a superuser always has
  every feature regardless of their `UserAccess` row's flags.
- Everyone else is gated feature-by-feature against their own
  `UserAccess` row: `can_reversal`, `can_reconcile`,
  `can_verification_format`, `can_check_statements`, `can_audit_log`,
  `can_bank_contacts`, `can_issuer_bank_accounts`, `can_make_users`,
  `can_scheduler` (the `FEATURES` list in `permissions.py`, paired with a
  human label used both in the "Make user" tab's checkboxes and in the
  "you don't have access to X" flash message).
- `permissions.get_user_access(user)` does a `get_or_create()` so a login
  created before this model existed (e.g. straight via
  `manage.py createsuperuser`) still gets a (all-`False`) row instead of
  erroring — real accounts are expected to already have one via the
  `0022_useraccess_backfill` migration and `create_user_view`/
  `update_user_view`.
- `permissions.require_feature(feature)` is the view decorator used
  throughout `core/views.py` and `reconcile/views.py`: an unauthenticated
  request is sent to login; an authenticated-but-unauthorized request gets
  a flash message and is redirected to the **Dashboard** specifically —
  deliberately a page that is never itself feature-gated, so a denied user
  can't land in a redirect loop by being bounced to another gated page.
- `permissions.user_feature_flags(user)` returns the whole
  `{feature: bool}` dict in one call — used by the nav bar and the Extra
  page to decide which links/tabs to even render.
- `MailSignature` (§3) is deliberately **not** in `FEATURES` — every
  logged-in user manages their own signature regardless of role.

The **Make user** tab (superuser-only, since it can hand out
`is_staff`/`is_superuser`/any `UserAccess` flag to a brand-new account)
is where these `UserAccess` checkboxes are actually set, alongside the
standard Django `is_active`/`is_staff`/`is_superuser` flags
(`CreateUserForm`/`UpdateUserForm` in `core/forms.py`). Django's own admin
site (`/admin/`) has a customized `UserAdmin` (`core/admin.py`) that
strips the **Groups** / **User permissions** fields from the change form
— this project doesn't use Django's own group/permission system anywhere,
so exposing that editor (there or on the Make-user tab) would just invite
confusion.

`BankAccount` lookups (`core/services.py::_bank_accounts()`) are cached
per-process via `lru_cache`, invalidated by a `post_save`/`post_delete`
signal on the `BankAccount` model — an admin's edit takes effect
immediately, no restart needed.

---

## 12. `core.switch_db` — read-only "Fetch from DB" + General report

`core/switch_db.py` is a connector to the switch's **own** production
Postgres database — deliberately **not** registered as a Django
`DATABASES` alias, so `manage.py migrate`/`test` can never touch a schema
this project doesn't own. Configuration comes from `SWITCH_DB_*` in
`.env` (`settings.SWITCH_DB`).

- **`_connect()`** opens a plain `psycopg` connection and immediately sets
  `conn.read_only = True` — a Postgres session-level `READ ONLY`
  transaction, so any write is rejected by Postgres itself, not just
  omitted by convention. This module issues exactly one query (`_QUERY`,
  a single `SELECT` joining `transaction_entry` against
  `member_configuration`/`bank_details`/`transaction_payment_status`/
  `transaction_status`); there is no write path into the switch DB
  anywhere in this codebase.
- **`fetch_transactions(start, end)`** runs that query for a `[start,
  end)` naive-datetime window and returns rows shaped like an uploaded
  `ibft-transaction` export (same keys as `core.services.REQUIRED_COLUMNS`),
  including synthesizing **Network Reference Id** from the row's own
  `transaction_id` (base36-encoded, zero-padded to 12 chars — the switch
  schema has no stored column for this).
- **`build_transactions_workbook(rows)`** turns those rows into an
  in-memory `.xlsx` in the same shape a manual export would have been —
  id-like columns forced to text at write time (column-level
  `number_format = "@"` rather than per-cell, for performance — see the
  docstring's ~29k-row timing note) — so every downstream step (reversal
  generation, reconciliation) runs completely unchanged whether the file
  came from an upload or a DB fetch.
- **`date_window(from_date, to_date, as_of=None)`** builds that
  `[start, end)` pair: a `to_date` before today is pulled through its own
  full midnight-to-midnight; a `to_date` that includes today is capped at
  "right now" so a same-day fetch only sees what's actually posted so
  far. `transaction_entry.created` is stored as a **naive** timestamp
  holding Asia/Kathmandu wall-clock time (confirmed against production,
  not assumed), so this compares naive-to-naive throughout.
- **`fetch_ibft_export(from_date, to_date, as_of=None)`** is the
  high-level entry point both `core/views.py::upload_view` and
  `reconcile/views.py::reconcile_view` call for their `mode="db_fetch"`
  path (`DbFetchForm` in `core/forms.py`) — raises `SwitchDBError` (shown
  as a normal in-page error, same as a bad upload) if the window has zero
  transactions, rather than producing an empty file.

**`core/general_report.py::compute_general_report(rows)`** is a separate
consumer of `fetch_transactions()` — not tied to any `ProcessingLog`/
`ReconcileRun` at all — behind the Dashboard's **"General" tab**
(`general_report_view`, `/dashboard/general/`). It reuses the exact same
classification helpers the rest of the app already applies to file-based
rows (`core.services.is_on_us`, `normalize_failure_reason`,
`reconcile.engine.bucket_key_for_amount`) so its numbers agree with the
file-based dashboards whenever both cover the same period — only the data
source differs. A row counts as "success" purely by `Overall Status ==
SUCCESS`; everything else counts as "failed" (including `REVERSAL` — a
transaction later reversed is not a successful one for this report), with
any other status still landing in a separate `other` bucket rather than
being silently dropped, so `total_count` always matches a plain
`SELECT COUNT(*)` for the window. Output feeds both the on-screen General
tab and its own family of Excel exports (`export_general_*_view`).

---

## 13. `core.scheduler` — background alert & report jobs

`core/scheduler.py` runs two jobs via `apscheduler.schedulers.background.BackgroundScheduler`,
started once per real server process from `core/apps.py::CoreConfig.ready()`
(§ below) — chosen over an OS cron job specifically because (a) the
Explore page's Start/Stop toggle needs to control these jobs from inside
the web app itself, without shelling out to an OS scheduler, and (b) this
runs on Windows, which has no cron at all.

- **`check_dispute_timeouts()`** — registered as an `interval` job, every
  1 minute (`max_instances=1`, `coalesce=True`, `misfire_grace_time=30`).
  Queries `switch_db.fetch_transactions()` for the window since the last
  check (widened by a 5-minute lookback buffer, since a transaction's
  `transaction_entry` row can exist before its
  `transaction_payment_status` is written — the buffer re-sees it on the
  next check; `AlertedTimeoutTransaction`, not the window, is what
  actually stops a duplicate alert), filters to `Overall Status ==
  TIMEOUT`, and — for any not already in the `AlertedTimeoutTransaction`
  dedup ledger — emails a summary (grouped by Aggregator / Payment
  Processor, as an HTML table) to that job's active
  `ScheduledReportRecipient` rows. The ledger is only written **after** a
  successful send, so a failed send leaves those ids un-alerted for the
  next check to retry, rather than silently swallowing them. Independent
  of the email step, a `DisputeNotificationEvent(count=len(new_timeouts))`
  row is created for every batch of new timeouts found regardless of
  whether the email send succeeds or even has recipients configured —
  that's what the Extra page's "Desktop alerts" checkbox (§6) polls.
- **`send_daily_report()`** — registered as a `cron` job, daily at 09:00
  server-local time (`misfire_grace_time=300`). Pulls the previous day's
  full window via `switch_db.fetch_transactions()` +
  `general_report.compute_general_report()`, and emails the same
  Issuer-wise/Acquirer-wise/Aggregator-wise breakdown + totals the
  Dashboard's General tab shows, with the same workbook
  (`core.views._build_general_report_workbook`) attached as `.xlsx`.
- Both jobs check their own `SchedulerJobState.is_enabled` at the top and
  return immediately if off — the scheduler itself always ticks once
  started; it's this per-job DB flag, not actually starting/stopping the
  APScheduler job, that makes the Extra page's Start/Stop toggle work
  correctly no matter how many worker processes are serving the app (each
  process's own `BackgroundScheduler` ticks, but only does real work when
  the shared flag says to).
- Both jobs share `_attach_signature_image()` /
  `resolve_mail_signature()` / `build_signature_blocks()` with the
  Verification format "Send mail" feature (§6) — same inline sct-signature
  banner, same per-user-signature resolution logic, `user=None` since a
  scheduled job has no logged-in user of its own.

**`core/apps.py::CoreConfig.ready()`** calls `scheduler.start_scheduler()`
guarded by `_should_start_scheduler()`: skipped entirely for one-shot
`manage.py` commands (`migrate`, `test`, `shell`, `collectstatic`, etc. —
`ready()` runs for *every* invocation, not just "serving the app", and
those commands shouldn't open a switch-DB connection or start emailing);
under `runserver`'s autoreloader, only the reloaded child process
(`RUN_MAIN=true`) starts it, not the parent watcher, so it doesn't start
twice. Production (gunicorn/waitress serving `wsgi.py` directly, no
`manage.py` argv at all) always starts it. `start_scheduler()` itself is
also idempotent (a module-level `_scheduler` guard) as a second layer of
protection against double-starting within one process.

**Shutdown behavior / why the dev server could seem to hang on Ctrl+C:**
APScheduler's `BackgroundScheduler` runs jobs in a plain
`concurrent.futures.ThreadPoolExecutor`. That executor's own `atexit`
hook (standard-library behavior, not something this project configures)
blocks interpreter shutdown until every worker thread finishes — Ctrl+C
only raises `KeyboardInterrupt` on the *main* thread, so a job stuck
inside a blocking network call keeps the whole process alive regardless
of how many times it's pressed. Two mitigations are in place:
- `EMAIL_TIMEOUT = 15` in `settings.py` bounds `email.send()` (Django's
  SMTP backend otherwise defaults to no socket timeout at all) so a slow/
  unreachable mail server fails fast instead of hanging indefinitely.
- `start_scheduler()` registers `atexit.register(lambda: scheduler.shutdown(wait=False))`
  so the scheduler itself stops dispatching new job runs immediately on
  shutdown instead of waiting for its next tick. This does **not**
  forcibly cancel a job already mid-run (Python cannot kill a thread) —
  it only prevents a *new* one from starting during shutdown.
- Net effect: worst case on Ctrl+C is now bounded by `EMAIL_TIMEOUT`
  (~15-20s), not unbounded. `switch_db.py`'s own DB connection already
  had a 10s `connect_timeout`, so that side wasn't the risk.

---

## 14. Known limitations / operational notes

- **Scheduler jobs can double-send under multiple worker processes.**
  `CoreConfig.ready()` starts one `BackgroundScheduler` per real server
  process (§13) — fine for a single `runserver`/single-worker deployment,
  but if the app is ever served by more than one gunicorn/waitress worker
  process, each process starts its own scheduler, and both would tick the
  same "every minute" / "09:00" jobs independently. `SchedulerJobState`
  only gates *whether* a job does work, not mutual exclusion between
  processes, so two workers can both see the same not-yet-alerted
  transactions and both send before either commits its
  `AlertedTimeoutTransaction` write — a real (if narrow) duplicate-email
  window. Not an issue at the current single-worker deployment; worth a
  proper lock (e.g. a DB-level advisory lock or a "claim this tick" row)
  before scaling to multiple workers.
- **No automated test suite.** `core/` has no `tests.py` at all;
  `reconcile/tests.py` is empty Django boilerplate — this also covers the
  newer `switch_db.py`/`scheduler.py`/`general_report.py`/`permissions.py`
  modules, none of which have dedicated tests either. All verification of
  the reconciliation logic in this codebase to date has been done via
  targeted manual scripts exercising the real functions against
  constructed and real bank-statement data (see conversation history /
  commit context) — nothing runs in CI. Anyone changing
  `apply_bank_statement_to_reversal_file()` or `reconcile/engine.py`
  should re-verify by hand: no regression safety net exists yet.
- **The `timeout` sheet is never cross-checked against a bank
  statement** in `core`'s `apply_bank_statement_to_reversal_file()` —
  unlike `failed`, a TIMEOUT row that actually landed at the destination
  bank currently gets no red flag. (`reconcile`'s engine has the same
  gap — TIMEOUT rows are just surfaced on their own sheet, not
  statement-checked.) Flagged as a possible enhancement, not yet built.
- **Two independent statement readers.** `core/services.py` and
  `reconcile/statements.py` each parse bank statements with their own
  code (the latter is the more capable one — multi-format, PDF
  support). They share `build_bank_statement_index()` and the
  NCHL/Khalti detection functions (imported from `core.services` into
  `reconcile/engine.py`), but the parsing-into-that-shape step is
  duplicated — including, now, a near-identical `_load_excel_rows()`
  helper in *both* files for `.xlsx`/`.xls` support (see §8). Worth
  keeping in mind if you fix a parsing bug in one — check whether the
  other needs the same fix.
- **`DEBUG = True` and a placeholder `SECRET_KEY`** in
  `reversal_project/settings.py` — fine for local/trusted-network use,
  not for anything internet-facing. See `README.md`'s Setup section for
  the full pre-deployment checklist (rotate `SECRET_KEY`, set
  `DEBUG = False`, configure `ALLOWED_HOSTS`, rotate the seeded
  passwords, lock down `media/`).
- **SQLite, no queue.** Fine at current scale (one-file-at-a-time
  uploads, synchronous processing in the request/response cycle). A
  large upload (~29,000 rows, ~4.5 MB observed) processes inline within
  one HTTP request — `DATA_UPLOAD_MAX_MEMORY_SIZE`/
  `FILE_UPLOAD_MAX_MEMORY_SIZE` are raised to 50 MB in `settings.py` to
  accommodate that.
