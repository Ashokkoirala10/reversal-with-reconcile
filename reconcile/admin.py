from django.contrib import admin

from .models import ReconcileRun


@admin.register(ReconcileRun)
class ReconcileRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "transaction_filename",
        "total_transactions",
        "grand_total_reconciled",
        "grand_total_outstanding",
        "need_reversal_count",
    )
    readonly_fields = [f.name for f in ReconcileRun._meta.fields]
