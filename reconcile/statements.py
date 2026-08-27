"""Reading bank statement exports (.csv / .xlsx) — normalizes every known
export shape down to the same {entry_type, remarks, amount, date} row dict
that core.services._read_bank_statement_rows() itself produces, so the
combined output can be fed straight into
core.services.combine_bank_statement_files() / build_bank_statement_index()
and every one of core's own (already-correct, already-tested) matching
functions can be reused as-is — see engine.py.

Different banks hand back their exports in genuinely different shapes:

  - The common shape core already understands: a header row with S.N /
    ENTRY TYPE / REMARKS / AMOUNT / DATE.
  - Rastriya Banijya Bank's shape: a banner + account-info block, then a
    header row of ID / TRANSACTION DATE / DESCRIPTION / WITHDRAW /
    DEPOSIT / BALANCE. There's no explicit ENTRY TYPE — a non-empty
    WITHDRAW means DR, a non-empty DEPOSIT means CR. DESCRIPTION plays
    the same role REMARKS does elsewhere.

Both shapes are normalized down to the same row dict here; the common
shape is actually just delegated straight to core's own reader.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import openpyxl
import pdfplumber
import xlrd

from core.services import ProcessingError, _read_bank_statement_rows  # noqa: F401 (re-exported for convenience)

# Several banks' exports embed the Network Reference Id in ways core's
# [/:] tokenizer can't cleanly isolate — smooshed against an account
# number with only a space between them (e.g. Machhapuchhre:
# "...0080996233400015 00000000PWM7:exp/...", Rastriya Banijya:
# "...236833 236833 00000000PNTD:100224/..."), or joined with "|" rather
# than "/" or ":" (Jyoti, Sangrila, Nepal Finance: "...s |
# 00000000PN8Y:SCT:GIBL").
#
# The reference id currently happens to always be 8 zeros + 4 characters,
# but that's just what the switch's counter looks like *today* — as it
# grows, the leading-zero count will shrink, and a regex hardcoded to
# "exactly 8 zeros" will silently stop matching new ids over time without
# any obvious error. Rather than guess at a shape, this isolates ids the
# only way that's actually future-proof: it works from the *real*
# reference ids in this run's own uploaded Transaction Data (whatever
# length or format they happen to be) and scans each statement row's
# remarks for those specific strings, appending a clean, unambiguously
# isolated copy of any match — so whatever tokenizer runs downstream
# always finds it as its own token regardless of what surrounds it.
def _isolate_known_reference_ids(remarks: str, known_ids: set[str], id_lengths: tuple[int, ...]) -> tuple[str, bool]:
    if not remarks or not known_ids or not id_lengths:
        return remarks, False
    upper = remarks.upper()
    found: set[str] = set()
    for length in id_lengths:
        for i in range(len(upper) - length + 1):
            window = upper[i : i + length]
            if window in known_ids:
                found.add(window)
    if not found:
        return remarks, False
    return remarks + "".join(f"/{m}" for m in found), True
    return remarks + "".join(f"/{m}" for m in matches)


class StatementError(Exception):
    pass


def _clean_amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text == "-":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _normalize_headers(raw_headers) -> list[str]:
    return [str(h).strip().upper() if h is not None else "" for h in raw_headers]


def _pdf_table_rows(path: Path) -> list[list] | None:
    """Extracts every page's table out of a PDF bank statement export and
    concatenates them into one list of rows — the same shape
    openpyxl/csv.reader already hand back elsewhere in this module, so a
    PDF export can be fed into the exact same header-detection and
    column-parsing logic as its CSV/XLSX equivalent.

    Only the first page is expected to carry a header row (confirmed
    against ADBL's own PDF export, which doesn't repeat it on later
    pages) — every row from every page is just appended in order, and
    whatever function calls this treats row 0 as the header the same way
    it already does for CSV/XLSX."""
    try:
        with pdfplumber.open(path) as pdf:
            rows: list[list] = []
            for page in pdf.pages:
                table = page.extract_table()
                if table:
                    rows.extend(table)
            return rows or None
    except Exception:
        return None


def _rows_from_withdraw_deposit_table(header: list[str], data_rows) -> list[dict]:
    desc_idx = header.index("DESCRIPTION")
    withdraw_idx = header.index("WITHDRAW") if "WITHDRAW" in header else None
    deposit_idx = header.index("DEPOSIT") if "DEPOSIT" in header else None
    date_idx = header.index("TRANSACTION DATE") if "TRANSACTION DATE" in header else None

    rows = []
    for raw in data_rows:
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue
        desc = raw[desc_idx] if desc_idx < len(raw) else ""
        if desc is None or str(desc).strip() == "":
            continue
        withdraw = _clean_amount(raw[withdraw_idx]) if withdraw_idx is not None and withdraw_idx < len(raw) else 0.0
        deposit = _clean_amount(raw[deposit_idx]) if deposit_idx is not None and deposit_idx < len(raw) else 0.0
        if withdraw:
            entry_type, amount = "DR", withdraw
        elif deposit:
            entry_type, amount = "CR", deposit
        else:
            continue
        rows.append(
            {
                "entry_type": entry_type,
                "remarks": str(desc).strip(),
                "amount": amount,
                "date": raw[date_idx] if date_idx is not None and date_idx < len(raw) else None,
            }
        )
    return rows


def _load_excel_rows(path: Path) -> list[list] | None:
    """Read an Excel file's first sheet into a list of row-value lists —
    the modern OOXML .xlsx/.xlsm format via openpyxl, or a legacy
    pre-2007 Excel Binary .xls export via xlrd (openpyxl can only read
    the former; some banks, e.g. ADBL, still send the latter). Returns
    None for any other suffix, or if the file can't be opened at all."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except Exception:
            return None
        try:
            ws = wb[wb.sheetnames[0]]
            return [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    elif suffix == ".xls":
        try:
            wb = xlrd.open_workbook(str(path))
        except Exception:
            return None
        ws = wb.sheet_by_index(0)
        return [
            [ws.cell_value(r, c) if ws.cell_value(r, c) != "" else None for c in range(ws.ncols)]
            for r in range(ws.nrows)
        ]
    return None


def _is_withdraw_deposit_shape(path: Path) -> list[dict] | None:
    """Returns parsed rows if `path` is the WITHDRAW/DEPOSIT/DESCRIPTION
    shape (e.g. Rastriya Banijya Bank), else None (meaning: try core's own
    ENTRY TYPE/REMARKS reader instead)."""
    all_rows = _load_excel_rows(path)
    if all_rows is None:
        return None

    for i, raw in enumerate(all_rows):
        if raw is None:
            continue
        names = _normalize_headers(raw)
        names_set = set(n for n in names if n)
        if {"DESCRIPTION", "WITHDRAW", "DEPOSIT"} <= names_set:
            return _rows_from_withdraw_deposit_table(names, all_rows[i + 1 :])
    return None



def _garima_statement_rows(path: Path) -> list[dict] | None:
    """Parse Garima Bikas Bank's statement export.

    Garima's export uses MainCode / TranDate / Desc1..Desc5 / Type /
    Amount / TranId / Balance rather than the common ENTRY TYPE / REMARKS
    layout. Type explicitly carries DR/CR and Amount may be signed, so the
    normalized amount is always positive.
    """
    if path.suffix.lower() != ".csv":
        return None

    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = [str(h or "").strip().upper() for h in (reader.fieldnames or [])]
            required = {"MAINCODE", "TRANDATE", "TYPE", "AMOUNT", "TRANID"}
            if not required.issubset(set(headers)):
                return None

            # Re-map original headers to normalized names so exports with
            # harmless whitespace/BOM differences still work.
            header_map = {str(h or "").strip().upper(): h for h in (reader.fieldnames or [])}
            desc_headers = [
                header_map.get(f"DESC{i}") for i in range(1, 6)
                if header_map.get(f"DESC{i}") is not None
            ]
            rows = []
            for raw in reader:
                entry_type = str(raw.get(header_map.get("TYPE"), "") or "").strip().upper()
                if entry_type not in {"DR", "CR"}:
                    # Opening/closing balance and other non-transaction rows.
                    continue

                amount_raw = raw.get(header_map.get("AMOUNT"), "")
                amount = _clean_amount(amount_raw)
                if not amount:
                    continue

                remarks_parts = [
                    str(raw.get(h, "") or "").strip()
                    for h in desc_headers
                    if str(raw.get(h, "") or "").strip()
                ]
                tran_id = str(raw.get(header_map.get("TRANID"), "") or "").strip()
                if tran_id:
                    remarks_parts.append(tran_id)

                rows.append({
                    "entry_type": entry_type,
                    "remarks": " | ".join(remarks_parts),
                    "amount": abs(amount),
                    "date": raw.get(header_map.get("TRANDATE")),
                })
            return rows
    except (OSError, UnicodeError, csv.Error):
        return None


def _normalize_adbl_headers(raw_headers) -> list[str]:
    """Like _normalize_headers(), but also folds underscore-separated
    headers (ADBL's actual export uses "NValue_Date", "Debit_Account",
    etc.) down to the same space-separated form so a single set of column
    names can match either style."""
    return [str(h).strip().upper().replace("_", " ") if h is not None else "" for h in raw_headers]


def _adbl_statement_rows(path: Path) -> list[dict] | None:
    """Parse Agricultural Development Bank's (ADBL) own settlement-account
    statement export — as .xlsx/.xls, .csv, or .pdf (ADBL exports a legacy
    pre-2007 Excel Binary .xls as often as a real .xlsx, and sometimes only
    hands back a PDF rather than a spreadsheet at all; the PDF's own per-page tables
    are extracted via _pdf_table_rows() into the exact same row shape and
    fed through the same header-detection/parsing logic below, so nothing
    past that point needs to know which format it came from).

    Unlike every other bank's export (a header row of S.N / ENTRY TYPE /
    REMARKS / AMOUNT / DATE), ADBL hands back its raw core-banking ledger
    for the settlement account: SN / NValue_Date / EValue_Date /
    NBooking_Date / Ref / FT_Ref / Narration_Details / Dr / Cr / Balance /
    Turn / Category / FDate / TDate / Account_Number / Payment_Details /
    Debit_Account / Credit_Account / Payment_Type / From_Info / To_Info.

    There's no explicit ENTRY TYPE column — a non-zero Dr means DR, a
    non-zero Cr means CR. Every row observed in practice is a DR (money
    paid out of ADBL's settlement account to fund an inbound IBFT credit
    to a customer) — the only CR rows are periodic "Transfer IPS Credit"
    bulk settlement-funding lines, which carry a settlement batch
    reference rather than any individual transaction's reference id, so
    they're never lookup candidates for statement_entries_for_reference()
    regardless of whether they parse cleanly.

    Crucially, the switch's own Network Reference Id (e.g. "00000000JAUQ"
    — same "8 zeros + 4 characters" shape as the Transaction Data file's
    own Network Reference Id column) lives in the Payment_Details column,
    not in Narration_Details — confirmed by cross-checking real rows
    against a real Transaction Data export: the same id and the same
    amount show up in both. So every account-number/reference-carrying
    column is folded into REMARKS (slash-joined, so each piece becomes
    its own matchable token the same way core's tokenizer already splits
    on "/" and ":") rather than Narration_Details alone.

    Some Dr/Cr/Balance/Turn cells come through as "#"/"##"/"###" —
    Excel's own column-too-narrow-to-display overflow marker, baked into
    the export as literal text rather than the real number. Observed only
    on the irrelevant "Transfer IPS Credit" rows in practice; such a row
    is simply skipped (nothing to reconcile against) rather than recorded
    with a bogus zero amount.
    """
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        all_rows = _load_excel_rows(path)
        if all_rows is None:
            return None
    elif suffix == ".csv":
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as fh:
                all_rows = [row for row in csv.reader(fh)]
        except (OSError, UnicodeError, csv.Error):
            return None
    elif suffix == ".pdf":
        all_rows = _pdf_table_rows(path)
        if not all_rows:
            return None
    else:
        return None

    # ADBL's own exports aren't consistent about this column's name across
    # batches — some call it "Narration_Details", others (e.g. its
    # auto-generated "STMT auto" exports) just "Narration" — so either
    # satisfies the requirement below.
    required = {"DR", "CR", "BALANCE", "DEBIT ACCOUNT", "CREDIT ACCOUNT"}
    narration_names = ("NARRATION DETAILS", "NARRATION")
    header = None
    header_idx = None
    for i, raw in enumerate(all_rows):
        if raw is None:
            continue
        names = _normalize_adbl_headers(raw)
        names_set = set(n for n in names if n)
        if required <= names_set and names_set & set(narration_names):
            header, header_idx = names, i
            break
    if header is None:
        return None

    def col(name: str) -> int | None:
        return header.index(name) if name in header else None

    ref_idx = col("REF")
    ft_ref_idx = col("FT REF")
    narration_idx = col("NARRATION DETAILS")
    if narration_idx is None:
        narration_idx = col("NARRATION")
    dr_idx = col("DR")
    cr_idx = col("CR")
    evalue_date_idx = col("EVALUE DATE")
    fdate_idx = col("FDATE")
    payment_details_idx = col("PAYMENT DETAILS")
    debit_account_idx = col("DEBIT ACCOUNT")
    credit_account_idx = col("CREDIT ACCOUNT")
    payment_type_idx = col("PAYMENT TYPE")
    from_info_idx = col("FROM INFO")
    to_info_idx = col("TO INFO")

    def cell(raw, idx) -> str:
        if idx is None or idx >= len(raw):
            return ""
        value = raw[idx]
        return "" if value is None else str(value).strip()

    rows: list[dict] = []
    for raw in all_rows[header_idx + 1 :]:
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue

        dr = _clean_amount(raw[dr_idx]) if dr_idx is not None and dr_idx < len(raw) else 0.0
        cr = _clean_amount(raw[cr_idx]) if cr_idx is not None and cr_idx < len(raw) else 0.0
        if dr:
            entry_type, amount = "DR", dr
        elif cr:
            entry_type, amount = "CR", cr
        else:
            # Zero/blank (or an unparseable "#"-overflow cell) on both
            # sides — nothing to reconcile against, so skip it rather
            # than record a bogus zero-amount entry.
            continue

        parts = [
            cell(raw, ref_idx),
            cell(raw, ft_ref_idx),
            cell(raw, narration_idx),
            cell(raw, payment_details_idx),
            cell(raw, debit_account_idx),
            cell(raw, credit_account_idx),
            cell(raw, payment_type_idx),
            cell(raw, from_info_idx),
            cell(raw, to_info_idx),
        ]
        remarks = "/".join(p for p in parts if p)

        date = None
        for idx in (evalue_date_idx, fdate_idx):
            if idx is not None and idx < len(raw) and raw[idx] not in (None, ""):
                date = raw[idx]
                break

        rows.append(
            {
                "entry_type": entry_type,
                "remarks": remarks,
                "amount": amount,
                "date": date,
            }
        )
    return rows


def read_statement_rows(path: str | Path, display_name: str = "") -> list[dict]:
    """Read one bank statement export (.csv or .xlsx) into a list of
    {entry_type, remarks, amount, date} row dicts — auto-detecting which
    of the known shapes it uses."""
    path = Path(path)

    garima_rows = _garima_statement_rows(path)
    if garima_rows is not None:
        return garima_rows

    adbl_rows = _adbl_statement_rows(path)
    if adbl_rows is not None:
        return adbl_rows

    if path.suffix.lower() == ".pdf":
        # PDF is only understood via the ADBL table-extraction path above.
        # If that returned None, either pdfplumber couldn't find a table on
        # any page (e.g. a scanned/image PDF, or one with no visible cell
        # borders) or the extracted table's headers didn't match ADBL's
        # known columns — either way, falling through to the generic
        # openpyxl/csv reader below would always fail (it can't open a PDF
        # at all), so raise a clear, specific error instead.
        raise StatementError(
            f"'{display_name or path.name}': could not extract a recognizable table from this PDF. "
            "PDF statements are currently only supported in Agricultural Development Bank's export "
            "layout — check that the file isn't a scanned image and that it has visible table "
            "borders, or re-export it as .csv/.xlsx instead."
        )

    alt_rows = _is_withdraw_deposit_shape(path)
    if alt_rows is not None:
        return alt_rows

    try:
        return _read_bank_statement_rows(path)
    except ProcessingError as exc:
        raise StatementError(f"'{display_name or path.name}': {exc}") from exc


def write_combined_statement_csv(
    bank_files: dict[str, list[Path]],
    combined_csv_path: str | Path,
    known_ref_ids: set[str] | None = None,
) -> tuple[Path, set[str], list[str]]:
    """Reads every uploaded file for every bank (2-4 files per bank is
    expected/normal — e.g. the 10 Aug statement *and* the 11 Aug one,
    since a late-evening transaction's other leg often only posts on the
    bank's next EOD run) and writes them all into a single combined CSV
    in exactly the shape core.services.combine_bank_statement_files()
    itself produces (S.N / ENTRY TYPE / REMARKS / AMOUNT / DATE / SOURCE),
    tagging every row with its bank key as SOURCE so
    core.services.statement_entries_for_reference() can later restrict a
    lookup to the right bank.

    `known_ref_ids` should be every Network Reference Id from this run's
    own uploaded Transaction Data — passing it enables
    _isolate_known_reference_ids() (see above) so any of them embedded
    awkwardly in a statement's remarks still gets matched.

    A bank whose file(s) fail to parse, come back suspiciously thin
    (fewer than MIN_USABLE_ROWS total rows), or don't carry a single one
    of this run's own reference ids anywhere in their remarks (a strong
    sign of a broken/wrong-period export — seen in practice: a statement
    with real-looking rows but none of them referencing any of today's
    transactions at all) is excluded entirely rather than allowed to
    poison the run: every one of its transactions would otherwise get
    wrongly flagged instead of landing on the "No Statement" sheet where
    they belong. Returns (csv_path, usable_bank_keys, warnings).
    """
    MIN_USABLE_ROWS = 5
    known_ref_ids = known_ref_ids or set()
    id_lengths = tuple(sorted({len(rid) for rid in known_ref_ids}))

    combined_csv_path = Path(combined_csv_path)
    combined_csv_path.parent.mkdir(parents=True, exist_ok=True)

    usable_bank_keys: set[str] = set()
    warnings: list[str] = []
    rows_by_bank: dict[str, list[dict]] = {}

    for bank_key, paths in bank_files.items():
        bank_rows: list[dict] = []
        try:
            for path in paths:
                bank_rows.extend(read_statement_rows(path, bank_key))
        except StatementError as exc:
            warnings.append(
                f"Could not read the statement for '{bank_key}' ({exc}) — treated as unavailable this run."
            )
            continue

        if len(bank_rows) < MIN_USABLE_ROWS:
            warnings.append(
                f"The statement for '{bank_key}' only had {len(bank_rows)} row(s) — this looks like an "
                "incomplete or broken export, so it was treated as unavailable this run rather than risk "
                "flagging every one of its transactions incorrectly. Please re-check and re-upload it."
            )
            continue

        any_found = False
        for row in bank_rows:
            new_remarks, found = _isolate_known_reference_ids(row.get("remarks", ""), known_ref_ids, id_lengths)
            row["remarks"] = new_remarks
            any_found = any_found or found

        if known_ref_ids and not any_found:
            warnings.append(
                f"The statement for '{bank_key}' had {len(bank_rows)} row(s) but not one of them referenced "
                "any of this run's own transactions — this looks like a broken or wrong-period export, so "
                "it was treated as unavailable this run rather than risk flagging every one of its "
                "transactions incorrectly. Please re-check and re-upload it."
            )
            continue

        rows_by_bank[bank_key] = bank_rows
        usable_bank_keys.add(bank_key)

    with open(combined_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["S.N", "ENTRY TYPE", "REMARKS", "AMOUNT", "DATE", "SOURCE"])
        sn = 1
        for bank_key, rows in rows_by_bank.items():
            for row in rows:
                writer.writerow(
                    [sn, row.get("entry_type", ""), row.get("remarks", ""), row.get("amount", ""), row.get("date", ""), bank_key]
                )
                sn += 1

    return combined_csv_path, usable_bank_keys, warnings
