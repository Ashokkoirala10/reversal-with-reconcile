from .permissions import user_feature_flags


def feature_flags(request):
    """Exposes each core.permissions.FEATURES flag as `feat_<name>` on
    every template context, so base.html's nav bar (and any other
    template) can hide links to features the logged-in user can't reach —
    without every view having to pass them in by hand."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {f"feat_{name}": value for name, value in user_feature_flags(user).items()}
