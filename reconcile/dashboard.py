"""Reconcile dashboard: aggregate analytics across every passed
ReconcileRun.

This mirrors core/views.py's own reversal dashboard (year/month/day
filter, day-wise breakdown, On-Us/Off-Us failed-reason breakdown) so the
two apps look and behave the same way, and reuses the exact same
amount-range buckets (see engine.AMOUNT_BUCKETS) for the "success by
amount range" report — this whole module exists because, until now, the
reconcile app had no persistent, filterable, exportable report the way
core's reversal dashboard already did; every run's numbers just lived on
that one run's own result page with nothing tying days/months together.

Only PASSED runs count here — same "review it, then mark it passed"
workflow as core.ProcessingLog, so a run nobody has checked yet can't
skew the monthly numbers (see ReconcileRun.passed / views.toggle_passed_view).
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.audit import log_action
from core.report_constants import MONTH_NAMES as _MONTH_NAMES
from core.report_constants import REASON_ORDER as _REASON_ORDER

from .engine import AMOUNT_BUCKETS
from .models import ReconcileRun


def _apply_year_month_filter(request):
    """Same shape/behavior as core.views._apply_year_month_filter(), for
    ReconcileRun instead of ProcessingLog — filters PASSED runs down to
    the selected From/To date range and returns everything needed to
    render the filter controls too, so the monthly report can be pulled
    and submitted the same way from either app.

    The dashboard's single filter bar sends `from`/`to` (see
    core.views._apply_year_month_filter) — year/month/day are kept here
    only for backward compatibility with old bookmarked/linked URLs."""
    from datetime import datetime as _datetime

    runs = ReconcileRun.objects.filter(passed=True)

    available_years = sorted(
        {timezone.localtime(dt).year for dt in runs.values_list("created_at", flat=True) if dt},
        reverse=True,
    )

    def _parse_date(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return _datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None

    selected_from = _parse_date(request.GET.get("from"))
    selected_to = _parse_date(request.GET.get("to"))
    if selected_from:
        runs = runs.filter(created_at__date__gte=selected_from)
    if selected_to:
        runs = runs.filter(created_at__date__lte=selected_to)

    selected_year_raw = (request.GET.get("year") or "").strip()
    selected_year = int(selected_year_raw) if selected_year_raw.isdigit() else None
    if selected_year:
        runs = runs.filter(created_at__year=selected_year)

    selected_month_raw = (request.GET.get("month") or "").strip()
    selected_month = int(selected_month_raw) if selected_month_raw.isdigit() and 1 <= int(selected_month_raw) <= 12 else None
    selected_month_name = dict(_MONTH_NAMES).get(selected_month) if selected_month else None
    if selected_month:
        runs = runs.filter(created_at__month=selected_month)

    selected_day_raw = (request.GET.get("day") or "").strip()
    selected_day = int(selected_day_raw) if selected_day_raw.isdigit() and 1 <= int(selected_day_raw) <= 31 else None
    if selected_day:
        runs = runs.filter(created_at__day=selected_day)

    return {
        "runs": runs,
        "available_years": available_years,
        "selected_year": selected_year,
        "selected_month": selected_month,
        "selected_month_name": selected_month_name,
        "selected_day": selected_day,
        "selected_from": selected_from,
        "selected_to": selected_to,
        "month_names": _MONTH_NAMES,
        "day_numbers": list(range(1, 32)),
    }


def _ordered_reasons(bucket: dict, total: int) -> list:
    rows = []
    for reason in _REASON_ORDER:
        count = bucket.get(reason, 0)
        rows.append({"reason": reason, "count": count, "pct": round(100 * count / total) if total else 0})
    for reason, count in bucket.items():
        if reason not in _REASON_ORDER:
            rows.append({"reason": reason, "count": count, "pct": round(100 * count / total) if total else 0})
    return rows


def _compute_totals(runs):
    totals = {
        "run_count": runs.count(),
        "total_transactions": runs.aggregate(v=Sum("total_transactions"))["v"] or 0,
        "grand_total_reconciled": runs.aggregate(v=Sum("grand_total_reconciled"))["v"] or 0,
        "grand_total_outstanding": runs.aggregate(v=Sum("grand_total_outstanding"))["v"] or 0,
        "need_reversal_count": runs.aggregate(v=Sum("need_reversal_count"))["v"] or 0,
        "sct_total": runs.aggregate(v=Sum("sct_total"))["v"] or 0,
        "sct_reconciled": runs.aggregate(v=Sum("sct_reconciled"))["v"] or 0,
        "sct_flagged": runs.aggregate(v=Sum("sct_flagged"))["v"] or 0,
        "nchl_total": runs.aggregate(v=Sum("nchl_total"))["v"] or 0,
        "nchl_reconciled": runs.aggregate(v=Sum("nchl_reconciled"))["v"] or 0,
        "nchl_flagged": runs.aggregate(v=Sum("nchl_flagged"))["v"] or 0,
        "khalti_total": runs.aggregate(v=Sum("khalti_total"))["v"] or 0,
        "khalti_reconciled": runs.aggregate(v=Sum("khalti_reconciled"))["v"] or 0,
        "khalti_flagged": runs.aggregate(v=Sum("khalti_flagged"))["v"] or 0,
        "failed_onus_count": runs.aggregate(v=Sum("failed_onus_count"))["v"] or 0,
        "failed_onus_amount": runs.aggregate(v=Sum("failed_onus_amount"))["v"] or 0.0,
        "failed_offus_count": runs.aggregate(v=Sum("failed_offus_count"))["v"] or 0,
        "failed_offus_amount": runs.aggregate(v=Sum("failed_offus_amount"))["v"] or 0.0,
    }
    return totals


def _compute_onus_offus(runs, totals):
    onus_reason_totals: dict = {}
    offus_reason_totals: dict = {}
    for run in runs.only("id", "failed_reason_breakdown_onus", "failed_reason_breakdown_offus"):
        for reason, count in (run.failed_reason_breakdown_onus or {}).items():
            onus_reason_totals[reason] = onus_reason_totals.get(reason, 0) + count
        for reason, count in (run.failed_reason_breakdown_offus or {}).items():
            offus_reason_totals[reason] = offus_reason_totals.get(reason, 0) + count

    return {
        "onus": {
            "count": totals["failed_onus_count"],
            "amount": round(totals["failed_onus_amount"], 2),
            "reasons": _ordered_reasons(onus_reason_totals, totals["failed_onus_count"]),
        },
        "offus": {
            "count": totals["failed_offus_count"],
            "amount": round(totals["failed_offus_amount"], 2),
            "reasons": _ordered_reasons(offus_reason_totals, totals["failed_offus_count"]),
        },
    }


def _aggregate_buckets(runs) -> list[dict]:
    """Sum every run's success_buckets_onus/offus JSON field into one
    On-Us/Off-Us amount-range table for the current filter window — the
    "success with range: upto 5000, 5k-10k, ... greater than 100k" report,
    filterable monthly the same way the rest of the dashboard is."""
    onus_totals = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
    offus_totals = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}

    for run in runs.only("id", "success_buckets_onus", "success_buckets_offus"):
        for key, entry in (run.success_buckets_onus or {}).items():
            if key not in onus_totals:
                continue
            onus_totals[key]["count"] += entry.get("count", 0)
            onus_totals[key]["amount"] += entry.get("amount", 0.0)
        for key, entry in (run.success_buckets_offus or {}).items():
            if key not in offus_totals:
                continue
            offus_totals[key]["count"] += entry.get("count", 0)
            offus_totals[key]["amount"] += entry.get("amount", 0.0)

    rows = []
    for key, label, _upper in AMOUNT_BUCKETS:
        onus = onus_totals[key]
        offus = offus_totals[key]
        rows.append(
            {
                "key": key,
                "label": label,
                "onus_count": onus["count"],
                "onus_amount": round(onus["amount"], 2),
                "offus_count": offus["count"],
                "offus_amount": round(offus["amount"], 2),
                "total_count": onus["count"] + offus["count"],
                "total_amount": round(onus["amount"] + offus["amount"], 2),
            }
        )
    return rows


def _build_daily_stats(runs):
    """One row per calendar day, with every run created on that day
    rolled up into it — same idea as core's own day-by-day dashboard
    breakdown, but computed in Python (rather than a DB-side aggregate)
    since each day's row also needs its bucket/reason JSON fields merged,
    which SQL can't do for us. Also builds `detail`, a JSON-ready payload
    per day, so clicking a day can show the exact same rich breakdown
    instantly (no extra page load) — the same pattern core's own day
    modal already uses."""
    from collections import defaultdict

    by_day = defaultdict(list)
    for run in runs.only(
        "id", "created_at", "total_transactions", "grand_total_reconciled", "grand_total_outstanding",
        "need_reversal_count", "sct_total", "sct_reconciled", "sct_flagged", "sct_no_statement",
        "nchl_total", "nchl_reconciled", "nchl_flagged",
        "khalti_total", "khalti_reconciled", "khalti_flagged",
        "failed_onus_count", "failed_onus_amount", "failed_offus_count", "failed_offus_amount",
        "failed_reason_breakdown_onus", "failed_reason_breakdown_offus",
        "success_buckets_onus", "success_buckets_offus",
    ):
        day = timezone.localtime(run.created_at).date()
        by_day[day].append(run)

    daily_stats = []
    chart_points = []
    for day in sorted(by_day.keys(), reverse=True):
        day_runs = by_day[day]
        totals = {
            "files": len(day_runs),
            "total_transactions": sum(r.total_transactions for r in day_runs),
            "grand_total_reconciled": sum(r.grand_total_reconciled for r in day_runs),
            "grand_total_outstanding": sum(r.grand_total_outstanding for r in day_runs),
            "need_reversal_count": sum(r.need_reversal_count for r in day_runs),
            "sct_total": sum(r.sct_total for r in day_runs),
            "sct_reconciled": sum(r.sct_reconciled for r in day_runs),
            "sct_flagged": sum(r.sct_flagged for r in day_runs),
            "sct_no_statement": sum(r.sct_no_statement for r in day_runs),
            "nchl_total": sum(r.nchl_total for r in day_runs),
            "nchl_reconciled": sum(r.nchl_reconciled for r in day_runs),
            "nchl_flagged": sum(r.nchl_flagged for r in day_runs),
            "khalti_total": sum(r.khalti_total for r in day_runs),
            "khalti_reconciled": sum(r.khalti_reconciled for r in day_runs),
            "khalti_flagged": sum(r.khalti_flagged for r in day_runs),
            "failed_onus_count": sum(r.failed_onus_count for r in day_runs),
            "failed_onus_amount": round(sum(r.failed_onus_amount for r in day_runs), 2),
            "failed_offus_count": sum(r.failed_offus_count for r in day_runs),
            "failed_offus_amount": round(sum(r.failed_offus_amount for r in day_runs), 2),
        }

        reason_onus: dict = {}
        reason_offus: dict = {}
        onus_buckets = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
        offus_buckets = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
        for r in day_runs:
            for reason, count in (r.failed_reason_breakdown_onus or {}).items():
                reason_onus[reason] = reason_onus.get(reason, 0) + count
            for reason, count in (r.failed_reason_breakdown_offus or {}).items():
                reason_offus[reason] = reason_offus.get(reason, 0) + count
            for key, entry in (r.success_buckets_onus or {}).items():
                if key in onus_buckets:
                    onus_buckets[key]["count"] += entry.get("count", 0)
                    onus_buckets[key]["amount"] += entry.get("amount", 0.0)
            for key, entry in (r.success_buckets_offus or {}).items():
                if key in offus_buckets:
                    offus_buckets[key]["count"] += entry.get("count", 0)
                    offus_buckets[key]["amount"] += entry.get("amount", 0.0)

        bucket_rows = []
        for key, label, _upper in AMOUNT_BUCKETS:
            onus = onus_buckets[key]
            offus = offus_buckets[key]
            bucket_rows.append(
                {
                    "label": label,
                    "onus_count": onus["count"], "onus_amount": round(onus["amount"], 2),
                    "offus_count": offus["count"], "offus_amount": round(offus["amount"], 2),
                    "total_count": onus["count"] + offus["count"],
                    "total_amount": round(onus["amount"] + offus["amount"], 2),
                }
            )

        day_id = f"rc-day-{day.isoformat()}"
        # Percent of (reconciled + outstanding) specifically, not of
        # total_transactions scanned — those two are the bar's only two
        # segments, so this is what makes the bar fill edge-to-edge
        # instead of leaving a large uncolored remainder for every scanned
        # row that isn't part of either bucket (e.g. rows outside the
        # SCT/NCHL/Khalti networks this reconciliation covers).
        accounted_for = totals["grand_total_reconciled"] + totals["grand_total_outstanding"]
        row = dict(totals)
        row["day"] = day
        row["day_id"] = day_id
        row["day_iso"] = day.isoformat()
        row["reconciled_pct"] = round(100 * totals["grand_total_reconciled"] / accounted_for) if accounted_for else 0
        row["outstanding_pct"] = round(100 * totals["grand_total_outstanding"] / accounted_for) if accounted_for else 0
        row["detail"] = {
            "day": day.strftime("%A, %d %B %Y"),
            "day_iso": day.isoformat(),
            **totals,
            "onus_reasons": _ordered_reasons(reason_onus, totals["failed_onus_count"]),
            "offus_reasons": _ordered_reasons(reason_offus, totals["failed_offus_count"]),
            "buckets": bucket_rows,
        }
        daily_stats.append(row)
        chart_points.append(
            {
                "day": day.strftime("%d %b"),
                "reconciled": totals["grand_total_reconciled"],
                "outstanding": totals["grand_total_outstanding"],
            }
        )

    chart_points.reverse()
    return daily_stats, chart_points


@login_required
def dashboard_view(request):
    # The Reconcile dashboard now lives embedded directly below the
    # Reversal dashboard on one combined page (core:dashboard) instead
    # of being a separate nav item — see core/views.py's dashboard_view,
    # which calls _apply_year_month_filter() etc. from this module to
    # build that section. This URL just forwards there.
    from django.shortcuts import redirect
    from django.urls import reverse

    query = request.META.get("QUERY_STRING", "")
    target = reverse("core:dashboard") + (f"?{query}" if query else "") + "#reconcile"
    return redirect(target)


# ---------------------------------------------------------------------------
# Excel exports
# ---------------------------------------------------------------------------

_HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def _new_sheet(title):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    return wb, ws


def _write_table_header(ws, row_idx, headers):
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    return row_idx + 1


def _autosize(ws, max_width=50):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            widths[cell.column] = min(max_width, max(widths.get(cell.column, 10), len(str(cell.value)) + 2))
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _finalize_xlsx(request, wb, filename):
    from io import BytesIO

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    response = HttpResponse(
        buf.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    log_action(request, f"Downloaded reconcile dashboard report: {filename}")
    return response


@login_required
def export_bucket_report_view(request):
    """The monthly "success by amount range, On-Us/Off-Us" report — filter
    to a year/month/day with the same query params the dashboard uses,
    then download this for submission (per the request: "with time filter
    on monthly basis you can submit the report")."""
    ctx = _apply_year_month_filter(request)
    runs = ctx["runs"]
    buckets = _aggregate_buckets(runs)

    label_bits = []
    if ctx["selected_from"] or ctx["selected_to"]:
        left = ctx["selected_from"].isoformat() if ctx["selected_from"] else "start"
        right = ctx["selected_to"].isoformat() if ctx["selected_to"] else "end"
        label_bits.append(f"{left} to {right}")
    if ctx["selected_year"]:
        label_bits.append(str(ctx["selected_year"]))
    if ctx["selected_month_name"]:
        label_bits.append(ctx["selected_month_name"])
    if ctx["selected_day"]:
        label_bits.append(f"day {ctx['selected_day']}")
    period_label = " - ".join(label_bits) if label_bits else "All time"

    wb, ws = _new_sheet("Success Amount Buckets")
    ws.cell(row=1, column=1, value=f"Success transactions by amount range \u2014 {period_label}").font = Font(bold=True, size=14)
    row = 3
    row = _write_table_header(
        ws, row, ["Amount Range", "On-Us Count", "On-Us Amount", "Off-Us Count", "Off-Us Amount", "Total Count", "Total Amount"]
    )
    total_onus_c = total_onus_a = total_offus_c = total_offus_a = 0
    for b in buckets:
        ws.append(
            [b["label"], b["onus_count"], b["onus_amount"], b["offus_count"], b["offus_amount"], b["total_count"], b["total_amount"]]
        )
        total_onus_c += b["onus_count"]
        total_onus_a += b["onus_amount"]
        total_offus_c += b["offus_count"]
        total_offus_a += b["offus_amount"]
        row += 1
    ws.append(
        [
            "Total",
            total_onus_c,
            round(total_onus_a, 2),
            total_offus_c,
            round(total_offus_a, 2),
            total_onus_c + total_offus_c,
            round(total_onus_a + total_offus_a, 2),
        ]
    )
    for c in range(1, 8):
        ws.cell(row=ws.max_row, column=c).font = Font(bold=True)
    _autosize(ws)
    return _finalize_xlsx(request, wb, f"success_amount_buckets_{period_label.replace(' ', '_')}.xlsx")


@login_required
def export_failed_onoffus_view(request):
    ctx = _apply_year_month_filter(request)
    runs = ctx["runs"]
    totals = _compute_totals(runs)
    onus_offus = _compute_onus_offus(runs, totals)

    wb, ws = _new_sheet("Failed On-Us")
    row = _write_table_header(ws, 1, ["Reason", "Count", "% of On-Us total"])
    for r in onus_offus["onus"]["reasons"]:
        ws.append([r["reason"], r["count"], f"{r['pct']}%"])
    _autosize(ws)

    ws2 = wb.create_sheet("Failed Off-Us")
    row2 = _write_table_header(ws2, 1, ["Reason", "Count", "% of Off-Us total"])
    for r in onus_offus["offus"]["reasons"]:
        ws2.append([r["reason"], r["count"], f"{r['pct']}%"])
    _autosize(ws2)

    return _finalize_xlsx(request, wb, "reconcile_failed_onus_offus.xlsx")


@login_required
def export_day_breakdown_view(request):
    ctx = _apply_year_month_filter(request)
    runs = ctx["runs"]
    daily_stats, _chart = _build_daily_stats(runs)

    wb, ws = _new_sheet("Day-wise Reconcile Report")
    headers = [
        "Day", "Runs", "Total Transactions", "Grand Reconciled", "Grand Outstanding",
        "Need Reversal", "SCT Total", "SCT Reconciled", "SCT Flagged",
        "NCHL Total", "NCHL Reconciled", "NCHL Flagged",
        "Khalti Total", "Khalti Reconciled", "Khalti Flagged",
        "Failed On-Us Count", "Failed On-Us Amount", "Failed Off-Us Count", "Failed Off-Us Amount",
    ]
    row = _write_table_header(ws, 1, headers)
    for d in daily_stats:
        ws.append(
            [
                d["day"].strftime("%Y-%m-%d"), d["files"], d["total_transactions"],
                d["grand_total_reconciled"], d["grand_total_outstanding"], d["need_reversal_count"],
                d["sct_total"], d["sct_reconciled"], d["sct_flagged"],
                d["nchl_total"], d["nchl_reconciled"], d["nchl_flagged"],
                d["khalti_total"], d["khalti_reconciled"], d["khalti_flagged"],
                d["failed_onus_count"], d["failed_onus_amount"], d["failed_offus_count"], d["failed_offus_amount"],
            ]
        )
    _autosize(ws)
    return _finalize_xlsx(request, wb, "reconcile_day_breakdown.xlsx")


_SUMMARY_TITLE_FONT = Font(bold=True, size=14)
_SUMMARY_SECTION_FONT = Font(bold=True, size=12, color="1E3A8A")
_SUMMARY_LABEL_FONT = Font(bold=True)


def _write_summary_section(ws, row, title):
    ws.cell(row=row, column=1, value=title).font = _SUMMARY_SECTION_FONT
    return row + 1


def _write_summary_kv(ws, row, label, value):
    ws.cell(row=row, column=1, value=label).font = _SUMMARY_LABEL_FONT
    ws.cell(row=row, column=2, value=value)
    return row + 1


@login_required
def export_summary_view(request):
    """Reconcile-side counterpart to core's "Download summary CSV" — the
    same headline totals shown on the Reconcile — volume / By network
    cards, for the currently applied From/To filter."""
    ctx = _apply_year_month_filter(request)
    runs = ctx["runs"]
    totals = _compute_totals(runs)
    onus_offus = _compute_onus_offus(runs, totals)

    if ctx["selected_from"] or ctx["selected_to"]:
        left = ctx["selected_from"].isoformat() if ctx["selected_from"] else "start"
        right = ctx["selected_to"].isoformat() if ctx["selected_to"] else "end"
        period = f"{left} to {right}"
    else:
        period = "All time"

    wb, ws = _new_sheet("Reconcile Summary")
    ws.cell(row=1, column=1, value="Reconcile dashboard summary").font = _SUMMARY_TITLE_FONT
    ws.cell(row=2, column=1, value=f"Period: {period}")
    row = 4

    row = _write_summary_section(ws, row, "Volume")
    row = _write_summary_kv(ws, row, "Passed reconcile runs", totals["run_count"])
    row = _write_summary_kv(ws, row, "Total transactions scanned", totals["total_transactions"])
    row = _write_summary_kv(ws, row, "Grand total reconciled", totals["grand_total_reconciled"])
    row = _write_summary_kv(ws, row, "Grand total outstanding", totals["grand_total_outstanding"])
    row = _write_summary_kv(ws, row, "Need To Reversal rows", totals["need_reversal_count"])
    row += 1

    row = _write_summary_section(ws, row, "By network")
    row = _write_summary_kv(ws, row, "SCT total", totals["sct_total"])
    row = _write_summary_kv(ws, row, "SCT reconciled", totals["sct_reconciled"])
    row = _write_summary_kv(ws, row, "SCT flagged", totals["sct_flagged"])
    row = _write_summary_kv(ws, row, "NCHL total", totals["nchl_total"])
    row = _write_summary_kv(ws, row, "NCHL reconciled", totals["nchl_reconciled"])
    row = _write_summary_kv(ws, row, "NCHL flagged", totals["nchl_flagged"])
    row = _write_summary_kv(ws, row, "Khalti total", totals["khalti_total"])
    row = _write_summary_kv(ws, row, "Khalti reconciled", totals["khalti_reconciled"])
    row = _write_summary_kv(ws, row, "Khalti flagged", totals["khalti_flagged"])
    row += 1

    row = _write_summary_section(ws, row, "Failed On-Us / Off-Us")
    row = _write_summary_kv(ws, row, "Failed On-Us count", onus_offus["onus"]["count"])
    row = _write_summary_kv(ws, row, "Failed On-Us amount (Rs.)", onus_offus["onus"]["amount"])
    row = _write_summary_kv(ws, row, "Failed Off-Us count", onus_offus["offus"]["count"])
    row = _write_summary_kv(ws, row, "Failed Off-Us amount (Rs.)", onus_offus["offus"]["amount"])

    _autosize(ws)
    filename = f"reconcile_summary_{period.replace(' ', '_').replace('/', '-')}.xlsx"
    return _finalize_xlsx(request, wb, filename)
