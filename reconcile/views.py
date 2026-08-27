import tempfile
import time
import zipfile
from pathlib import Path

from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.files import File
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.services import ProcessingError, build_bank_statement_index, normalize_reference_id

from .banks import BANKS
from .engine import reconcile
from .forms import ReconcileUploadForm
from .models import ReconcileRun
from .report import save_workbook
from .statements import StatementError, write_combined_statement_csv
from .transactions import TransactionFileError, load_transactions

PAGE_SIZE = 10


def is_admin(user):
    return user.is_authenticated and user.is_staff


def can_toggle_passed(user, run):
    return user.is_staff or user.username == run.uploaded_by


def _save_upload_to(tmp_dir: Path, uploaded_file, prefix: str) -> Path:
    dest = tmp_dir / f"{prefix}_{uploaded_file.name}"
    with open(dest, "wb") as out:
        for chunk in uploaded_file.chunks():
            out.write(chunk)
    return dest


def _panel_context(request):
    """Context for the two activity panels on the Reconcile page — same
    idea as core's own _panel_context(): a central "passed" list visible
    to everyone, and the current user's own activity (every run,
    regardless of passed status) with an inline Mark Passed toggle, so a
    run you haven't reviewed yet doesn't just vanish after you leave its
    result page — you can always find it again here."""
    shared_runs = ReconcileRun.objects.filter(passed=True)
    shared_paginator = Paginator(shared_runs, PAGE_SIZE)
    shared_page = shared_paginator.get_page(request.GET.get("page"))

    mine_runs = ReconcileRun.objects.filter(uploaded_by=request.user.username)
    mine_paginator = Paginator(mine_runs, PAGE_SIZE)
    mine_page = mine_paginator.get_page(request.GET.get("mypage"))

    return {"shared_page": shared_page, "mine_page": mine_page}


@login_required
def reconcile_view(request):
    if request.method == "POST":
        form = ReconcileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            with tempfile.TemporaryDirectory(prefix="reconcile_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)

                txn_upload = form.cleaned_data["transaction_file"]
                txn_path = _save_upload_to(tmp_dir, txn_upload, "txn")

                try:
                    transactions = load_transactions(txn_path)
                except TransactionFileError as exc:
                    return render(request, "reconcile/reconcile.html", {"form": form, "error": str(exc), **_panel_context(request)})

                bank_files: dict[str, list[Path]] = {}
                # Keep (arcname, saved path) for every uploaded file so we
                # can bundle everything — transaction data + every bank
                # statement, under their original names — into one .zip
                # further down, without re-reading the uploads.
                bundle_entries: list[tuple[str, Path]] = [(txn_upload.name, txn_path)]
                for bank_key, uploaded_files in form.statement_files().items():
                    bank_label = next((b.display_name for b in BANKS if b.key == bank_key), bank_key)
                    paths = []
                    for i, uf in enumerate(uploaded_files):
                        saved = _save_upload_to(tmp_dir, uf, f"stmt_{bank_key}_{i}")
                        paths.append(saved)
                        bundle_entries.append((f"statements/{bank_label}/{uf.name}", saved))
                    bank_files[bank_key] = paths

                known_ref_ids = {
                    normalize_reference_id(t.get("Network Reference Id"))
                    for t in transactions
                    if t.get("Network Reference Id")
                }

                try:
                    combined_csv, usable_bank_keys, statement_warnings = write_combined_statement_csv(
                        bank_files, tmp_dir / "combined_statements.csv", known_ref_ids
                    )
                    index = build_bank_statement_index(combined_csv)
                except ProcessingError as exc:
                    return render(
                        request,
                        "reconcile/reconcile.html",
                        {"form": form, "error": str(exc), **_panel_context(request)},
                    )

                t0 = time.perf_counter()
                result = reconcile(transactions, index, usable_bank_keys)
                result.warnings.extend(statement_warnings)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                output_filename = f"reconcile_report_{txn_upload.name.rsplit('.', 1)[0]}.xlsx"
                tmp_output_path = tmp_dir / output_filename
                save_workbook(result, txn_upload.name, tmp_output_path)

                # One .zip with the transaction file plus every statement
                # uploaded alongside it, so the whole source-document set
                # behind this report can be downloaded in a single click
                # instead of one file at a time.
                bundle_filename = f"reconcile_bundle_{txn_upload.name.rsplit('.', 1)[0]}.zip"
                tmp_bundle_path = tmp_dir / bundle_filename
                with zipfile.ZipFile(tmp_bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for arcname, path in bundle_entries:
                        zf.write(path, arcname=arcname)

                run = ReconcileRun(
                    transaction_filename=txn_upload.name,
                    uploaded_by=request.user.username if request.user.is_authenticated else "",
                    statements_uploaded=result.statements_uploaded,
                    statements_missing=result.statements_missing,
                    warnings=result.warnings,
                    total_transactions=result.transaction_count,
                    sct_total=result.sct_total,
                    sct_reconciled=result.sct_reconciled,
                    sct_flagged=result.sct_flagged_count,
                    sct_no_statement=result.sct_no_statement_count,
                    nchl_total=result.nchl_stat.total,
                    nchl_reconciled=result.nchl_stat.reconciled,
                    nchl_flagged=result.nchl_stat.flagged,
                    khalti_total=result.khalti_stat.total,
                    khalti_reconciled=result.khalti_stat.reconciled,
                    khalti_flagged=result.khalti_stat.flagged,
                    failed_total=result.failed_stat.total,
                    failed_reconciled=result.failed_stat.reconciled,
                    failed_pending=result.failed_stat.pending,
                    failed_flagged=result.failed_stat.flagged,
                    manual_reversal_total=result.manual_reversal_stat.total,
                    manual_reversal_reconciled=result.manual_reversal_stat.reconciled,
                    manual_reversal_pending=result.manual_reversal_stat.pending,
                    manual_reversal_flagged=result.manual_reversal_stat.flagged,
                    system_reversal_total=result.system_reversal_stat.total,
                    system_reversal_reconciled=result.system_reversal_stat.reconciled,
                    system_reversal_flagged=result.system_reversal_stat.flagged,
                    need_reversal_count=len(result.need_reversal),
                    grand_total_reconciled=result.grand_total_reconciled,
                    grand_total_outstanding=result.grand_total_outstanding,
                    failed_onus_count=result.failed_onus_count,
                    failed_onus_amount=result.failed_onus_amount,
                    failed_offus_count=result.failed_offus_count,
                    failed_offus_amount=result.failed_offus_amount,
                    failed_reason_breakdown_onus=result.failed_reason_breakdown_onus,
                    failed_reason_breakdown_offus=result.failed_reason_breakdown_offus,
                    success_buckets_onus=result.success_buckets_onus,
                    success_buckets_offus=result.success_buckets_offus,
                )
                with open(tmp_output_path, "rb") as f:
                    run.report_file.save(output_filename, File(f), save=False)
                # Keep the originally uploaded Transaction Data file too —
                # not just its name — so it can be re-downloaded later from
                # the audit log the same way the generated report can (see
                # download_file_view() below).
                with open(txn_path, "rb") as f:
                    run.transaction_file.save(txn_upload.name, File(f), save=False)
                with open(tmp_bundle_path, "rb") as f:
                    run.bundle_zip.save(bundle_filename, File(f), save=False)
                run.save()

                return redirect(reverse("reconcile:result", args=[run.id]))
    else:
        form = ReconcileUploadForm()

    banks_available = [b for b in BANKS if b.available_by_default]
    banks_pending = [b for b in BANKS if not b.available_by_default]
    ctx = {"form": form, "banks_available": banks_available, "banks_pending": banks_pending}
    ctx.update(_panel_context(request))
    return render(request, "reconcile/reconcile.html", ctx)


@login_required
def result_view(request, run_id):
    run = get_object_or_404(ReconcileRun, id=run_id)
    bank_labels = {b.key: b.display_name for b in BANKS}
    return render(
        request,
        "reconcile/result.html",
        {
            "run": run,
            "can_toggle": can_toggle_passed(request.user, run),
            "buckets": _run_bucket_rows(run),
            "statements_uploaded_labels": [bank_labels.get(k, k) for k in run.statements_uploaded],
            "statements_missing_labels": [bank_labels.get(k, k) for k in run.statements_missing],
        },
    )


@login_required
@require_POST
def toggle_passed_view(request, run_id):
    run = get_object_or_404(ReconcileRun, id=run_id)

    if not can_toggle_passed(request.user, run):
        return redirect(request.META.get("HTTP_REFERER") or reverse("reconcile:reconcile"))

    run.passed = not run.passed
    if run.passed:
        run.passed_by = request.user.username
        run.passed_at = timezone.now()
    else:
        run.passed_by = ""
        run.passed_at = None
    run.save(update_fields=["passed", "passed_by", "passed_at"])

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("reconcile:reconcile")
    return redirect(next_url)


@login_required
def download_file_view(request, run_id, kind):
    run = get_object_or_404(ReconcileRun, id=run_id)

    if kind == "uploaded":
        field, filename = run.transaction_file, run.transaction_filename
    elif kind == "generated":
        field, filename = run.report_file, Path(run.report_file.name).name if run.report_file else ""
    elif kind == "bundle":
        field, filename = run.bundle_zip, Path(run.bundle_zip.name).name if run.bundle_zip else ""
    else:
        raise Http404("Unknown file kind")

    if not field:
        raise Http404("File not available")

    return FileResponse(field.open("rb"), as_attachment=True, filename=filename or field.name)


@login_required
@user_passes_test(is_admin, login_url="reconcile:reconcile")
def audit_log_view(request):
    # The Reconcile audit log now lives on the same page as the Reversal
    # one (core:audit_log, "Reconcile" section below "Reversal") instead
    # of being its own nav item — this URL just forwards there so any
    # old bookmarks/links still land somewhere useful.
    return redirect(reverse("core:audit_log") + "#reconcile-log")


@login_required
def day_detail_view(request, date_str):
    """The "click a day, get the exact same instant-result view" page —
    every passed run created on that calendar day, rolled up into one
    set of totals using the same sections as the immediate post-upload
    result page (see result.html), plus a list of the individual runs
    that made up the day so each can still be opened/downloaded on its
    own."""
    from datetime import datetime as _datetime

    try:
        day = _datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise Http404("Invalid date")

    runs = ReconcileRun.objects.filter(passed=True, created_at__date=day).order_by("created_at")
    if not runs.exists():
        raise Http404("No passed reconcile runs for this day")

    totals = _aggregate_day_totals(runs)
    buckets = _day_bucket_rows(runs)
    return render(
        request,
        "reconcile/day_detail.html",
        {"day": day, "runs": runs, "totals": totals, "buckets": buckets},
    )


_SUM_FIELDS = [
    "total_transactions",
    "sct_total", "sct_reconciled", "sct_flagged", "sct_no_statement",
    "nchl_total", "nchl_reconciled", "nchl_flagged",
    "khalti_total", "khalti_reconciled", "khalti_flagged",
    "failed_total", "failed_reconciled", "failed_pending", "failed_flagged",
    "manual_reversal_total", "manual_reversal_reconciled", "manual_reversal_pending", "manual_reversal_flagged",
    "system_reversal_total", "system_reversal_reconciled", "system_reversal_flagged",
    "need_reversal_count", "grand_total_reconciled", "grand_total_outstanding",
    "failed_onus_count", "failed_offus_count",
]
_SUM_AMOUNT_FIELDS = ["failed_onus_amount", "failed_offus_amount"]


def _aggregate_day_totals(runs) -> dict:
    """Sums every ReconcileRun field result.html displays, across every
    run in `runs` — so a day with several runs shows one combined set of
    totals instead of forcing you to add them up by hand."""
    totals = {f: 0 for f in _SUM_FIELDS}
    totals.update({f: 0.0 for f in _SUM_AMOUNT_FIELDS})
    reason_onus: dict = {}
    reason_offus: dict = {}
    statements_uploaded: set = set()
    statements_missing: set = set()

    for run in runs:
        for f in _SUM_FIELDS:
            totals[f] += getattr(run, f) or 0
        for f in _SUM_AMOUNT_FIELDS:
            totals[f] += getattr(run, f) or 0.0
        for reason, count in (run.failed_reason_breakdown_onus or {}).items():
            reason_onus[reason] = reason_onus.get(reason, 0) + count
        for reason, count in (run.failed_reason_breakdown_offus or {}).items():
            reason_offus[reason] = reason_offus.get(reason, 0) + count
        statements_uploaded.update(run.statements_uploaded or [])
        statements_missing.update(run.statements_missing or [])

    totals["failed_reason_breakdown_onus"] = reason_onus
    totals["failed_reason_breakdown_offus"] = reason_offus
    totals["statements_uploaded"] = sorted(statements_uploaded)
    totals["statements_missing"] = sorted(statements_missing - statements_uploaded)
    return totals


def _day_bucket_rows(runs) -> list[dict]:
    from .engine import AMOUNT_BUCKETS

    onus_totals = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
    offus_totals = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
    for run in runs:
        for key, entry in (run.success_buckets_onus or {}).items():
            if key in onus_totals:
                onus_totals[key]["count"] += entry.get("count", 0)
                onus_totals[key]["amount"] += entry.get("amount", 0.0)
        for key, entry in (run.success_buckets_offus or {}).items():
            if key in offus_totals:
                offus_totals[key]["count"] += entry.get("count", 0)
                offus_totals[key]["amount"] += entry.get("amount", 0.0)

    rows = []
    for key, label, _upper in AMOUNT_BUCKETS:
        onus = onus_totals[key]
        offus = offus_totals[key]
        rows.append(
            {
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


def _run_bucket_rows(run) -> list[dict]:
    """Amount-range bucket rows for a single run's result page — same
    shape as the dashboard's aggregate version (see _aggregate_buckets())
    but sourced from one ReconcileRun instead of summed across many."""
    from .engine import AMOUNT_BUCKETS

    rows = []
    for key, label, _upper in AMOUNT_BUCKETS:
        onus = (run.success_buckets_onus or {}).get(key, {"count": 0, "amount": 0.0})
        offus = (run.success_buckets_offus or {}).get(key, {"count": 0, "amount": 0.0})
        rows.append(
            {
                "label": label,
                "onus_count": onus.get("count", 0),
                "onus_amount": round(onus.get("amount", 0.0), 2),
                "offus_count": offus.get("count", 0),
                "offus_amount": round(offus.get("amount", 0.0), 2),
                "total_count": onus.get("count", 0) + offus.get("count", 0),
                "total_amount": round(onus.get("amount", 0.0) + offus.get("amount", 0.0), 2),
            }
        )
    return rows
