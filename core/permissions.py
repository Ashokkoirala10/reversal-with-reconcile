"""Central role/permission gate — a superuser has unconditional access to
everything; every other logged-in user is gated per feature by their own
core.models.UserAccess row. Used by both core/views.py and
reconcile/views.py (reconcile already imports plenty from core, so this
is just one more shared module rather than a duplicated helper in each
app).

Mail signatures are deliberately not in FEATURES — every logged-in user
manages their own regardless of role (see core.models.MailSignature),
so there's nothing to gate there."""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from .models import UserAccess

# (field name on UserAccess, human label) — the label is used in both the
# "Make user" tab's checkboxes and the "you don't have access" message.
FEATURES = [
    ("can_reversal", "Reversal"),
    ("can_reconcile", "Reconcile"),
    ("can_verification_format", "Verification format"),
    ("can_check_statements", "Check statements"),
    ("can_audit_log", "Audit log"),
    ("can_bank_contacts", "Bank contacts"),
    ("can_issuer_bank_accounts", "Issuer bank accounts"),
    ("can_make_users", "Make users"),
    ("can_scheduler", "Scheduler (dispute/timeout alerts, daily report)"),
]
FEATURE_LABELS = dict(FEATURES)


def get_user_access(user) -> UserAccess:
    """Every user is expected to have exactly one UserAccess row (see the
    0022_useraccess_backfill migration and create_user_view/
    update_user_view), but get_or_create() here is a cheap safety net for
    any login that predates it (e.g. created directly via manage.py
    createsuperuser or the Django admin)."""
    access, _ = UserAccess.objects.get_or_create(user=user)
    return access


def has_feature(user, feature: str) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return bool(getattr(get_user_access(user), feature, False))


def user_feature_flags(user) -> dict:
    """{feature_name: bool, ...} for every entry in FEATURES — used by the
    nav bar and the "Extra" page to decide what to show."""
    if not user.is_authenticated:
        return {name: False for name, _label in FEATURES}
    if user.is_superuser:
        return {name: True for name, _label in FEATURES}
    access = get_user_access(user)
    return {name: bool(getattr(access, name, False)) for name, _label in FEATURES}


def require_feature(feature: str, redirect_to: str = "core:dashboard"):
    """Like @user_passes_test, but for a specific FEATURES entry and with
    a flash message instead of a bare redirect — and critically, redirects
    to a page that's never itself feature-gated (the Dashboard), so a
    denied user can't get stuck in a redirect loop the way redirecting
    back to another gated page would."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            if not has_feature(request.user, feature):
                label = FEATURE_LABELS.get(feature, feature)
                messages.error(request, f"You don't have access to {label}.")
                return redirect(redirect_to)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
