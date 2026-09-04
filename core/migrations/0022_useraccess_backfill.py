from django.db import migrations


def backfill_user_access(apps, schema_editor):
    """Gives every existing user a UserAccess row that preserves exactly
    the access they already had before per-feature gating existed:
    Reversal/Reconcile/Verification format/Check statements were open to
    any logged-in user, while Audit log/Bank contacts/Issuer bank accounts
    required is_staff and Make users required is_superuser (superusers
    don't need a row set to True since they always bypass the checks —
    see core.permissions.has_feature — but one is still created so every
    user has exactly one)."""
    User = apps.get_model("auth", "User")
    UserAccess = apps.get_model("core", "UserAccess")

    rows = []
    for user in User.objects.all():
        rows.append(
            UserAccess(
                user=user,
                can_reversal=True,
                can_reconcile=True,
                can_verification_format=True,
                can_check_statements=True,
                can_audit_log=user.is_staff,
                can_bank_contacts=user.is_staff,
                can_issuer_bank_accounts=user.is_staff,
                can_make_users=user.is_superuser,
            )
        )
    UserAccess.objects.bulk_create(rows, ignore_conflicts=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_useraccess"),
    ]

    operations = [
        migrations.RunPython(backfill_user_access, noop),
    ]
