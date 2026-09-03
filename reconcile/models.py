from django.db import models
from django.utils import timezone


def reconcile_upload_path(instance, filename):
    return f"uploads/{instance.id or 'tmp'}_{filename}"


class ReconcileRun(models.Model):
    """One reconciliation run: an audit trail of what was uploaded and a
    summary of the outcome, plus the generated report file."""

    created_at = models.DateTimeField(auto_now_add=True)
    transaction_filename = models.CharField(max_length=255)
    report_file = models.FileField(upload_to="reconcile_outputs/")

    # The uploaded Transaction Data file itself — stored (not just its
    # name) so it can be re-downloaded later the same way core's
    # ProcessingLog.uploaded_file works, instead of only the generated
    # report being retrievable.
    transaction_file = models.FileField(upload_to="uploads/", blank=True, null=True)

    # A single .zip containing the transaction file plus every bank
    # statement uploaded alongside it for this run — built once at
    # upload time (see reconcile/views.py) so the whole set of source
    # documents behind a report can be pulled down in one file instead
    # of one-by-one.
    bundle_zip = models.FileField(upload_to="reconcile_bundles/", blank=True, null=True)

    uploaded_by = models.CharField(max_length=150, blank=True, default="")

    # A run only counts toward the reconcile dashboard's analytics (and
    # only shows up in the shared "Reconcile audit log") once its owner
    # (or an admin) has reviewed it and explicitly marked it as passed —
    # exactly the same "passed" workflow core.models.ProcessingLog uses,
    # so a still-being-checked run can't skew the numbers.
    passed = models.BooleanField(default=False)
    passed_by = models.CharField(max_length=150, blank=True, default="")
    passed_at = models.DateTimeField(null=True, blank=True)

    statements_uploaded = models.JSONField(default=list, blank=True)
    statements_missing = models.JSONField(default=list, blank=True)

    # Non-fatal issues raised while reading the uploaded statements (e.g. a
    # bank's file failed to parse, came back suspiciously thin, or matched
    # none of this run's own reference ids) — see
    # reconcile.statements.write_combined_statement_csv(). A bank listed
    # here still lands in statements_missing/every one of its transactions
    # on the "No Statement" sheet even though a file *was* uploaded for it,
    # so this is the only place that actually explains why.
    warnings = models.JSONField(default=list, blank=True)

    total_transactions = models.PositiveIntegerField(default=0)

    sct_total = models.PositiveIntegerField(default=0)
    sct_reconciled = models.PositiveIntegerField(default=0)
    sct_flagged = models.PositiveIntegerField(default=0)
    sct_no_statement = models.PositiveIntegerField(default=0)

    nchl_total = models.PositiveIntegerField(default=0)
    nchl_reconciled = models.PositiveIntegerField(default=0)
    nchl_flagged = models.PositiveIntegerField(default=0)

    khalti_total = models.PositiveIntegerField(default=0)
    khalti_reconciled = models.PositiveIntegerField(default=0)
    khalti_flagged = models.PositiveIntegerField(default=0)

    failed_total = models.PositiveIntegerField(default=0)
    failed_reconciled = models.PositiveIntegerField(default=0)
    failed_pending = models.PositiveIntegerField(default=0)
    failed_flagged = models.PositiveIntegerField(default=0)

    manual_reversal_total = models.PositiveIntegerField(default=0)
    manual_reversal_reconciled = models.PositiveIntegerField(default=0)
    manual_reversal_pending = models.PositiveIntegerField(default=0)
    manual_reversal_flagged = models.PositiveIntegerField(default=0)

    system_reversal_total = models.PositiveIntegerField(default=0)
    system_reversal_reconciled = models.PositiveIntegerField(default=0)
    system_reversal_flagged = models.PositiveIntegerField(default=0)

    need_reversal_count = models.PositiveIntegerField(default=0)

    grand_total_reconciled = models.PositiveIntegerField(default=0)
    grand_total_outstanding = models.PositiveIntegerField(default=0)

    # --- On-Us / Off-Us failed breakdown (mirrors core.ProcessingLog) ---
    # On-Us = Debtor Bank and Creditor Bank are the same one of our own
    # banks (Global-to-Global / Prabhu-to-Prabhu); Off-Us = anything else.
    # Tracked separately, each with its own {reason: count} breakdown, so
    # the reconcile dashboard can show where failures actually originate
    # from — the same view core's reversal dashboard already gives you.
    failed_onus_count = models.PositiveIntegerField(default=0)
    failed_onus_amount = models.FloatField(default=0.0)
    failed_offus_count = models.PositiveIntegerField(default=0)
    failed_offus_amount = models.FloatField(default=0.0)
    failed_reason_breakdown_onus = models.JSONField(default=dict, blank=True)
    failed_reason_breakdown_offus = models.JSONField(default=dict, blank=True)

    # --- Success amount-range buckets, split On-Us / Off-Us ---
    # {bucket_key: {"count": int, "amount": float}} for every SUCCESS-
    # status transaction in this run, bucketed by Transaction Amount:
    # upto_5k / 5k_10k / 10k_25k / 25k_50k / 50k_100k / above_100k — see
    # reconcile/engine.py's AMOUNT_BUCKETS for the exact boundaries and
    # reconcile/report.py / views.py for where this is rendered/exported.
    success_buckets_onus = models.JSONField(default=dict, blank=True)
    success_buckets_offus = models.JSONField(default=dict, blank=True)

    # --- Issue note (shared dashboard) ---
    # One free-text note per run — editable only by the run's own
    # uploader (or staff), see reconcile.views.can_toggle_passed(); every
    # other viewer of the shared "passed" table sees it read-only. Lets
    # the person who actually ran the reconciliation flag something
    # unusual about that day, mark it resolved once it's sorted out, and
    # optionally email others for anything urgent. Deliberately a single
    # note edited in place (not a comment thread) — see update_issue_view.
    issue_description = models.TextField(blank=True, default="")
    issue_resolved = models.BooleanField(default=False)
    issue_updated_by = models.CharField(max_length=150, blank=True, default="")
    issue_updated_at = models.DateTimeField(null=True, blank=True)

    # Set only when someone actually sends the "notify others" email (not
    # just on every note edit) — issue_notified_to/_cc is whatever
    # address(es) they typed in at that moment, not a stored mailing list.
    issue_notified_at = models.DateTimeField(null=True, blank=True)
    issue_notified_by = models.CharField(max_length=150, blank=True, default="")
    issue_notified_to = models.CharField(max_length=255, blank=True, default="")
    issue_notified_cc = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Reconcile run {self.pk} ({self.created_at:%Y-%m-%d %H:%M})"
