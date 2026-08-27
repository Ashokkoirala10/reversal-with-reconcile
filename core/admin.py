from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User

from .models import BankAccount, MailSignature, ProcessingLog, VerificationBankContact


class UserAdmin(DjangoUserAdmin):
    """Stock Django UserAdmin with the Groups / User permissions editor
    stripped out of the change form — this app doesn't use Django's
    group/permission system (same reasoning as core/forms.py's
    CreateUserForm on the "Extra" page's own Make user tab, which leaves
    those out too), so exposing it here just invites confusion. is_active
    / is_staff / is_superuser stay, since those are what this app actually
    checks."""

    fieldsets = tuple(
        (title, {**opts, "fields": tuple(f for f in opts["fields"] if f not in ("groups", "user_permissions"))})
        for title, opts in DjangoUserAdmin.fieldsets
    )
    filter_horizontal = ()


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id", "bank_name", "keyword", "debit_account_number",
        "is_own_bank", "is_active", "updated_at",
    )
    list_filter = ("is_own_bank", "is_active")
    search_fields = ("bank_name", "keyword", "debit_account_number")


@admin.register(VerificationBankContact)
class VerificationBankContactAdmin(admin.ModelAdmin):
    list_display = ("id", "bank_name", "keyword", "to_emails", "cc_emails", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("bank_name", "keyword", "to_emails", "cc_emails")


@admin.register(MailSignature)
class MailSignatureAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "title", "mobile", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "title", "mobile")


@admin.register(ProcessingLog)
class ProcessingLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "uploaded_filename",
        "generated_filename",
        "status",
        "passed",
        "uploaded_by",
        "reversal_manual_kept",
        "failed_kept",
        "timeout_count",
        "prabhu_rerouted",
        "prabhu_reversal_count",
        "unrecognized_debtor_bank_rows",
        "duplicate_skipped",
        "created_at",
    )
    list_filter = ("status", "passed", "created_at")
    search_fields = ("uploaded_filename", "generated_filename", "uploaded_by")
    readonly_fields = [f.name for f in ProcessingLog._meta.fields]

# MemberAggregatorStat is intentionally NOT registered here — it's a
# per-upload, per-(member, aggregator) row (hundreds/thousands of rows per
# file) that isn't meaningful to browse/manage one-by-one in Django admin.
# It's already fully exposed, rolled up and filterable, on the dashboard's
# Member-wise / Aggregator-wise report tabs (see core/views.py) and via
# their Excel exports, so a Django admin listing for it is unnecessary.
