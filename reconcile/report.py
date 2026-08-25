"""Builds the multi-sheet reconciliation output workbook from a
ReconcileResult (see engine.py)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .banks import BANKS_BY_KEY
from .engine import AMOUNT_BUCKETS, ReconcileResult

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
_TITLE_FONT = Font(bold=True, size=14)
_SECTION_FONT = Font(bold=True, size=12, color="2F5496")
_RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
_GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
_YELLOW_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
_STRIPE_FILL = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")
_THIN = Side(style="thin", color="B7C3D6")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _write_header(ws: Worksheet, row_idx: int, headers: list[str]) -> None:
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER
    ws.row_dimensions[row_idx].height = 28


def _freeze_below(ws: Worksheet, row_idx: int) -> None:
    """Freeze everything from row 1 down through row_idx (typically a
    single table's own header row) so it stays visible while scrolling
    the (often long) data below it. Call this AT MOST ONCE per sheet —
    calling it more than once, or freezing a row deep into the sheet,
    is what caused the "sheet won't scroll properly" bug: nearly the
    whole sheet ends up pinned, leaving almost nothing left to scroll
    into below it. Sheets with more than one independent table (Summary,
    SCT Reconciliation) intentionally never call this at all."""
    ws.freeze_panes = ws.cell(row=row_idx + 1, column=1).coordinate


def _write_section_title(ws: Worksheet, row_idx: int, title: str, span: int) -> None:
    cell = ws.cell(row=row_idx, column=1, value=title)
    cell.font = _SECTION_FONT
    if span > 1:
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=span)


def _border_row(ws: Worksheet, row_idx: int, ncols: int, stripe: bool = False) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.border = _BORDER
        if stripe and cell.fill.patternType is None:
            cell.fill = _STRIPE_FILL


def _autofit(ws: Worksheet, max_width: int = 60) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            length = len(str(cell.value))
            widths[cell.column] = min(max_width, max(widths.get(cell.column, 10), length + 2))
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _autofilter(ws: Worksheet, header_row: int, last_row: int, ncols: int) -> None:
    if last_row <= header_row:
        return
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ncols)}{last_row}"


def build_summary_sheet(wb: Workbook, result: ReconcileResult, txn_filename: str) -> None:
    ws = wb.active
    ws.title = "Summary"

    row = 1
    ws.cell(row=row, column=1, value="Reconciliation Summary").font = _TITLE_FONT
    row += 2

    ws.cell(row=row, column=1, value="Transaction file:").font = Font(bold=True)
    ws.cell(row=row, column=2, value=txn_filename)
    row += 1
    ws.cell(row=row, column=1, value="Total transactions:").font = Font(bold=True)
    ws.cell(row=row, column=2, value=result.transaction_count)
    row += 2

    ws.cell(row=row, column=1, value="Status breakdown").font = _SECTION_FONT
    row += 1
    _write_header(ws, row, ["Overall Status", "Count"])
    row += 1
    for status, count in sorted(result.status_counts.items(), key=lambda kv: -kv[1]):
        ws.cell(row=row, column=1, value=status)
        ws.cell(row=row, column=2, value=count)
        row += 1
    row += 1

    ws.cell(row=row, column=1, value="Network breakdown").font = _SECTION_FONT
    row += 1
    _write_header(ws, row, ["Payment Processor", "Count"])
    row += 1
    for network, count in sorted(result.network_counts.items(), key=lambda kv: -kv[1]):
        ws.cell(row=row, column=1, value=network)
        ws.cell(row=row, column=2, value=count)
        row += 1
    row += 1

    ws.cell(row=row, column=1, value="Bank statements").font = _SECTION_FONT
    row += 1
    _write_header(ws, row, ["Bank", "Statement uploaded this run?"])
    row += 1
    missing_count = 0
    for key, bank in BANKS_BY_KEY.items():
        uploaded = key in result.statements_uploaded
        if not uploaded:
            missing_count += 1
        c1 = ws.cell(row=row, column=1, value=bank.display_name)
        c2 = ws.cell(row=row, column=2, value="Yes" if uploaded else "No")
        if not uploaded:
            c1.fill = _RED_FILL
            c2.fill = _RED_FILL
        row += 1
    row += 1
    ws.cell(row=row, column=1, value="No-statement banks this run:").font = Font(bold=True)
    ws.cell(row=row, column=2, value=f"{missing_count} of {len(BANKS_BY_KEY)}")
    row += 2

    ws.cell(row=row, column=1, value="Reconciliation results").font = _SECTION_FONT
    row += 1
    _write_header(ws, row, ["Category", "Total", "Reconciled", "Flagged / Pending", "No Statement"])
    row += 1
    lines = [
        ("SCT Network", result.sct_total, result.sct_reconciled, result.sct_flagged_count, result.sct_no_statement_count),
        ("NCHL Network", result.nchl_stat.total, result.nchl_stat.reconciled, result.nchl_stat.flagged, 0),
        ("Khalti Network", result.khalti_stat.total, result.khalti_stat.reconciled, result.khalti_stat.flagged, 0),
        ("Failed \u2192 Reversal Check", result.failed_stat.total, result.failed_stat.reconciled, result.failed_stat.pending + result.failed_stat.flagged, 0),
        ("Manual Reversal Check", result.manual_reversal_stat.total, result.manual_reversal_stat.reconciled, result.manual_reversal_stat.pending + result.manual_reversal_stat.flagged, 0),
        ("System Reversal Check", result.system_reversal_stat.total, result.system_reversal_stat.reconciled, result.system_reversal_stat.flagged, 0),
    ]
    for label, total, ok, flag, ns in lines:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=total)
        ws.cell(row=row, column=3, value=ok).fill = _GREEN_FILL
        if flag:
            ws.cell(row=row, column=4, value=flag).fill = _RED_FILL
        else:
            ws.cell(row=row, column=4, value=flag)
        if ns:
            ws.cell(row=row, column=5, value=ns).fill = _YELLOW_FILL
        else:
            ws.cell(row=row, column=5, value=ns)
        row += 1
    row += 1

    ws.cell(row=row, column=1, value="Grand total reconciled:").font = Font(bold=True, size=12)
    ws.cell(row=row, column=2, value=result.grand_total_reconciled).fill = _GREEN_FILL
    row += 1
    ws.cell(row=row, column=1, value="Grand total outstanding (needs review/action):").font = Font(bold=True, size=12)
    c = ws.cell(row=row, column=2, value=result.grand_total_outstanding)
    if result.grand_total_outstanding:
        c.fill = _RED_FILL
    row += 1
    ws.cell(row=row, column=1, value="Need To Reversal rows:").font = Font(bold=True)
    ws.cell(row=row, column=2, value=len(result.need_reversal))
    row += 1
    ws.cell(row=row, column=1, value="Timeout rows (see 'Timeout' sheet):").font = Font(bold=True)
    c = ws.cell(row=row, column=2, value=len(result.timeout_rows))
    if result.timeout_rows:
        c.fill = _YELLOW_FILL
    row += 1

    if result.warnings:
        row += 1
        ws.cell(row=row, column=1, value="Warnings").font = _SECTION_FONT
        row += 1
        for w in result.warnings:
            ws.cell(row=row, column=1, value=w).fill = _YELLOW_FILL
            row += 1

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["E"].width = 16


def _write_pair_and_flagged_sheet(wb: Workbook, title: str, pairs, flagged_rows) -> None:
    ws = wb.create_sheet(title=title)
    row = 1

    _write_section_title(ws, row, "Bank-to-Bank Success Counts", 6)
    row += 2
    _write_header(ws, row, ["Debtor Bank (from)", "Creditor Bank (to)", "Total Success", "Reconciled", "Flagged", "No Statement"])
    header_row = row
    row += 1
    pair_start = row
    for pair in sorted(pairs, key=lambda p: -p.total):
        ws.cell(row=row, column=1, value=pair.debtor)
        ws.cell(row=row, column=2, value=pair.creditor)
        ws.cell(row=row, column=3, value=pair.total)
        ws.cell(row=row, column=4, value=pair.reconciled)
        c5 = ws.cell(row=row, column=5, value=pair.flagged)
        c6 = ws.cell(row=row, column=6, value=pair.no_statement)
        if pair.flagged:
            c5.fill = _RED_FILL
        if pair.no_statement:
            c6.fill = _YELLOW_FILL
        _border_row(ws, row, 6, stripe=(row - pair_start) % 2 == 1)
        row += 1
    if row == pair_start:
        ws.cell(row=row, column=1, value="(no data)")
        row += 1
    row += 2

    _write_section_title(ws, row, "Flagged Transactions (need review)", 7)
    row += 2
    _write_header(ws, row, ["S.N", "Debtor Bank", "Creditor Bank", "Member Transaction Id", "Network Reference Id", "Amount", "Reason"])
    header_row = row
    row += 1
    flag_start = row
    for sn, f in enumerate(flagged_rows, start=1):
        ws.cell(row=row, column=1, value=sn)
        ws.cell(row=row, column=2, value=f.debtor)
        ws.cell(row=row, column=3, value=f.creditor)
        c4 = ws.cell(row=row, column=4, value=f.member_txn_id)
        c5 = ws.cell(row=row, column=5, value=f.ref_id)
        c4.number_format = "@"
        c5.number_format = "@"
        ws.cell(row=row, column=6, value=f.amount).number_format = "#,##0.00"
        ws.cell(row=row, column=7, value=f.reason)
        for col in range(1, 8):
            ws.cell(row=row, column=col).fill = _RED_FILL
        _border_row(ws, row, 7)
        row += 1
    if row == flag_start:
        ws.cell(row=row, column=1, value="(none \u2014 all reconciled)").fill = _GREEN_FILL
        row += 1
    else:
        _autofilter(ws, header_row, row - 1, 7)

    _autofit(ws)


def build_sct_sheet(wb: Workbook, result: ReconcileResult) -> None:
    _write_pair_and_flagged_sheet(wb, "SCT Reconciliation", result.sct_pairs.values(), result.sct_flagged)


def build_network_sheet(wb: Workbook, title: str, stat, flagged_rows, network_label: str) -> None:
    ws = wb.create_sheet(title=title)
    row = 1
    _write_section_title(
        ws,
        row,
        f"{network_label} \u2014 verified against the issuing bank's own statement (CR + matching settlement DR)",
        7,
    )
    row += 2
    _write_header(ws, row, ["Metric", "Count"])
    row += 1
    for label, val in [("Total success", stat.total), ("Reconciled (CR + settlement DR matched)", stat.reconciled), ("Flagged / pending", stat.flagged)]:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=val)
        if label.startswith("Flagged") and val:
            c.fill = _RED_FILL
        elif label.startswith("Reconciled"):
            c.fill = _GREEN_FILL
        _border_row(ws, row, 2)
        row += 1
    row += 1

    _write_section_title(ws, row, "Flagged Transactions (need review)", 7)
    row += 2
    _write_header(ws, row, ["S.N", "Debtor Bank", "Creditor Bank (recorded)", "Member Transaction Id", "Network Reference Id", "Amount", "Reason"])
    header_row = row
    row += 1
    flag_start = row
    for sn, f in enumerate(flagged_rows, start=1):
        ws.cell(row=row, column=1, value=sn)
        ws.cell(row=row, column=2, value=f.debtor)
        ws.cell(row=row, column=3, value=f.creditor)
        c4 = ws.cell(row=row, column=4, value=f.member_txn_id)
        c5 = ws.cell(row=row, column=5, value=f.ref_id)
        c4.number_format = "@"
        c5.number_format = "@"
        ws.cell(row=row, column=6, value=f.amount).number_format = "#,##0.00"
        ws.cell(row=row, column=7, value=f.reason)
        for col in range(1, 8):
            ws.cell(row=row, column=col).fill = _RED_FILL
        _border_row(ws, row, 7)
        row += 1
    if row == flag_start:
        ws.cell(row=row, column=1, value="(none \u2014 all reconciled)").fill = _GREEN_FILL
        row += 1
    else:
        _freeze_below(ws, header_row)
        _autofilter(ws, header_row, row - 1, 7)

    _autofit(ws)


def build_no_statement_sheet(wb: Workbook, result: ReconcileResult) -> None:
    ws = wb.create_sheet(title="No Statement (Unreconciled)")
    row = 1

    missing = [BANKS_BY_KEY[k].display_name for k in result.statements_missing]
    ws.cell(row=row, column=1, value=f"{len(missing)} of {len(BANKS_BY_KEY)} bank statement(s) not available for this run:").font = Font(bold=True, size=12)
    row += 1
    if missing:
        ws.cell(row=row, column=1, value=", ".join(missing))
    else:
        ws.cell(row=row, column=1, value="(none \u2014 every bank's statement was uploaded this run)")
    row += 2

    ws.cell(row=row, column=1, value="These transactions could not be checked either way because a statement wasn't available:").font = Font(bold=True)
    row += 2
    _write_header(ws, row, ["S.N", "Network", "Debtor Bank", "Creditor Bank", "Missing Statement For", "Member Transaction Id", "Network Reference Id", "Amount"])
    header_row = row
    row += 1
    start = row
    for sn, r in enumerate(result.sct_no_statement, start=1):
        ws.cell(row=row, column=1, value=sn)
        ws.cell(row=row, column=2, value=r.network)
        ws.cell(row=row, column=3, value=r.debtor)
        ws.cell(row=row, column=4, value=r.creditor)
        ws.cell(row=row, column=5, value=r.missing_bank).fill = _YELLOW_FILL
        c6 = ws.cell(row=row, column=6, value=r.member_txn_id)
        c7 = ws.cell(row=row, column=7, value=r.ref_id)
        c6.number_format = "@"
        c7.number_format = "@"
        ws.cell(row=row, column=8, value=r.amount).number_format = "#,##0.00"
        _border_row(ws, row, 8)
        row += 1
    if row == start:
        ws.cell(row=row, column=1, value="(none)")
        row += 1
    else:
        _freeze_below(ws, header_row)
        _autofilter(ws, header_row, row - 1, 8)

    _autofit(ws)


def build_timeout_sheet(wb: Workbook, result: ReconcileResult) -> None:
    """TIMEOUT transactions never resolved to SUCCESS/FAILED/REVERSAL, so
    there's no CR/DR pattern to check them against — they're neither
    reconciled nor flagged, just listed here on their own sheet for
    manual review, separate from every other status."""
    ws = wb.create_sheet(title="Timeout")
    row = 1
    _write_section_title(
        ws,
        row,
        "Timeout transactions — kept for manual reversal.",
        8,
    )
    row += 2
    _write_header(
        ws,
        row,
        ["S.N", "Network", "Debtor Bank", "Creditor Bank", "Member Transaction Id", "Network Reference Id", "Amount", "Source Message"],
    )
    header_row = row
    row += 1
    start = row
    for sn, r in enumerate(result.timeout_rows, start=1):
        ws.cell(row=row, column=1, value=sn)
        ws.cell(row=row, column=2, value=r.network)
        ws.cell(row=row, column=3, value=r.debtor)
        ws.cell(row=row, column=4, value=r.creditor)
        c5 = ws.cell(row=row, column=5, value=r.member_txn_id)
        c6 = ws.cell(row=row, column=6, value=r.ref_id)
        c5.number_format = "@"
        c6.number_format = "@"
        ws.cell(row=row, column=7, value=r.amount).number_format = "#,##0.00"
        ws.cell(row=row, column=8, value=r.source_message)
        for col in range(1, 9):
            ws.cell(row=row, column=col).fill = _YELLOW_FILL
        _border_row(ws, row, 8)
        row += 1
    if row == start:
        ws.cell(row=row, column=1, value="(none — no timeout transactions this run)").fill = _GREEN_FILL
        row += 1
    else:
        _freeze_below(ws, header_row)
        _autofilter(ws, header_row, row - 1, 8)

    _autofit(ws)


def build_need_reversal_sheet(wb: Workbook, result: ReconcileResult) -> None:
    ws = wb.create_sheet(title="Need To Reversal")
    row = 1
    _write_section_title(
        ws,
        row,
        "Full audit trail for every Failed / Manual Reversal / System Reversal check: reconciled "
        "(green), pending (yellow), and flagged anomalies like double reversals or a reversal issued "
        "against an already-successful transaction (red). Use the Status filter to narrow to just one.",
        9,
    )
    row += 2
    _write_header(
        ws,
        row,
        ["S.N", "Status", "Source", "Debtor Bank", "Creditor Bank", "Member Transaction Id", "Network Reference Id", "Amount", "Detail"],
    )
    header_row = row
    row += 1
    start = row
    status_fill = {"Reconciled": _GREEN_FILL, "Pending": _YELLOW_FILL, "Flagged": _RED_FILL}
    for sn, r in enumerate(result.need_reversal, start=1):
        fill = status_fill.get(r.status, _YELLOW_FILL)
        ws.cell(row=row, column=1, value=sn)
        ws.cell(row=row, column=2, value=r.status)
        ws.cell(row=row, column=3, value=r.source)
        ws.cell(row=row, column=4, value=r.debtor)
        ws.cell(row=row, column=5, value=r.creditor)
        c6 = ws.cell(row=row, column=6, value=r.member_txn_id)
        c7 = ws.cell(row=row, column=7, value=r.ref_id)
        c6.number_format = "@"
        c7.number_format = "@"
        ws.cell(row=row, column=8, value=r.amount).number_format = "#,##0.00"
        ws.cell(row=row, column=9, value=r.detail)
        for col in range(1, 10):
            ws.cell(row=row, column=col).fill = fill
        _border_row(ws, row, 9)
        row += 1
    if row == start:
        ws.cell(row=row, column=1, value="(none \u2014 nothing to check)").fill = _GREEN_FILL
        row += 1
    else:
        _freeze_below(ws, header_row)
        _autofilter(ws, header_row, row - 1, 9)

    _autofit(ws)


def build_failed_onoffus_sheet(wb: Workbook, result: ReconcileResult) -> None:
    """On-Us / Off-Us failed breakdown, with a reason table for each —
    the reconcile-side equivalent of core's dashboard "Failed On-Us /
    Off-Us" export, so a single run's own report already shows where its
    failures came from without needing the aggregate dashboard."""
    ws = wb.create_sheet(title="Failed On-Us Off-Us")
    row = 1
    ws.cell(row=row, column=1, value="Failed transactions \u2014 On-Us vs Off-Us, with reason breakdown").font = _TITLE_FONT
    row += 2

    for label, count, amount, reasons in (
        ("On-Us", result.failed_onus_count, result.failed_onus_amount, result.failed_reason_breakdown_onus),
        ("Off-Us", result.failed_offus_count, result.failed_offus_amount, result.failed_reason_breakdown_offus),
    ):
        ws.cell(row=row, column=1, value=f"{label} \u2014 Total failed").font = _SECTION_FONT
        row += 1
        ws.cell(row=row, column=1, value="Count:").font = Font(bold=True)
        ws.cell(row=row, column=2, value=count)
        row += 1
        ws.cell(row=row, column=1, value="Amount (Rs.):").font = Font(bold=True)
        ws.cell(row=row, column=2, value=round(amount, 2)).number_format = "#,##0.00"
        row += 1
        _write_header(ws, row, ["Reason", "Count", "% of total"])
        row += 1
        start = row
        for reason, reason_count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            ws.cell(row=row, column=1, value=reason)
            ws.cell(row=row, column=2, value=reason_count)
            pct = round(100 * reason_count / count) if count else 0
            ws.cell(row=row, column=3, value=f"{pct}%")
            _border_row(ws, row, 3, stripe=True)
            row += 1
        if row == start:
            ws.cell(row=row, column=1, value="(none)")
            row += 1
        row += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 14


def build_amount_buckets_sheet(wb: Workbook, result: ReconcileResult) -> None:
    """Success-transaction amount-range buckets, split On-Us/Off-Us \u2014
    the same up-to-5k / 5k-10k / 10k-25k / 25k-50k / 50k-100k / above-100k
    bands used on the reversal dashboard's own bucket report, so a
    monthly submission can be built from either app's export and the
    numbers line up."""
    ws = wb.create_sheet(title="Success Amount Buckets")
    row = 1
    ws.cell(row=row, column=1, value="Successful transactions by amount range \u2014 On-Us vs Off-Us").font = _TITLE_FONT
    row += 2

    _write_header(ws, row, ["Amount Range", "On-Us Count", "On-Us Amount", "Off-Us Count", "Off-Us Amount", "Total Count", "Total Amount"])
    row += 1
    start = row
    total_count = total_amount = 0
    total_onus_count = total_onus_amount = total_offus_count = total_offus_amount = 0
    for key, label, _upper in AMOUNT_BUCKETS:
        onus = result.success_buckets_onus.get(key, {"count": 0, "amount": 0.0})
        offus = result.success_buckets_offus.get(key, {"count": 0, "amount": 0.0})
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=onus["count"])
        ws.cell(row=row, column=3, value=round(onus["amount"], 2)).number_format = "#,##0.00"
        ws.cell(row=row, column=4, value=offus["count"])
        ws.cell(row=row, column=5, value=round(offus["amount"], 2)).number_format = "#,##0.00"
        ws.cell(row=row, column=6, value=onus["count"] + offus["count"])
        ws.cell(row=row, column=7, value=round(onus["amount"] + offus["amount"], 2)).number_format = "#,##0.00"
        _border_row(ws, row, 7, stripe=True)
        total_onus_count += onus["count"]
        total_onus_amount += onus["amount"]
        total_offus_count += offus["count"]
        total_offus_amount += offus["amount"]
        row += 1
    total_count = total_onus_count + total_offus_count
    total_amount = total_onus_amount + total_offus_amount
    ws.cell(row=row, column=1, value="Total").font = Font(bold=True)
    ws.cell(row=row, column=2, value=total_onus_count).font = Font(bold=True)
    ws.cell(row=row, column=3, value=round(total_onus_amount, 2)).font = Font(bold=True)
    ws.cell(row=row, column=3).number_format = "#,##0.00"
    ws.cell(row=row, column=4, value=total_offus_count).font = Font(bold=True)
    ws.cell(row=row, column=5, value=round(total_offus_amount, 2)).font = Font(bold=True)
    ws.cell(row=row, column=5).number_format = "#,##0.00"
    ws.cell(row=row, column=6, value=total_count).font = Font(bold=True)
    ws.cell(row=row, column=7, value=round(total_amount, 2)).font = Font(bold=True)
    ws.cell(row=row, column=7).number_format = "#,##0.00"
    _border_row(ws, row, 7)
    _freeze_below(ws, start - 1)
    _autofit(ws)


def build_workbook(result: ReconcileResult, txn_filename: str) -> Workbook:
    wb = Workbook()
    build_summary_sheet(wb, result, txn_filename)
    build_sct_sheet(wb, result)
    build_no_statement_sheet(wb, result)
    build_network_sheet(wb, "NCHL Reconciliation", result.nchl_stat, result.nchl_flagged, "NCHL_NETWORK")
    build_network_sheet(wb, "Khalti Reconciliation", result.khalti_stat, result.khalti_flagged, "KHALTI_NETWORK")
    build_failed_onoffus_sheet(wb, result)
    build_amount_buckets_sheet(wb, result)
    build_timeout_sheet(wb, result)
    build_need_reversal_sheet(wb, result)
    return wb


def save_workbook(result: ReconcileResult, txn_filename: str, output_path: str | Path) -> Path:
    wb = build_workbook(result, txn_filename)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path
