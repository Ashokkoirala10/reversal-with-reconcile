"""System-wide activity log — a single plain-text file
(MEDIA_ROOT/audit_log.txt), not a database table. Every action anywhere in
the app is appended to it as one line ("when | user | ip | action") by
calling log_action(request, "..."); login/logout are appended automatically
via Django's auth signals. core.views.audit_log_view reads this same file
for the on-screen table, and export_audit_log_view serves it verbatim as
the downloadable audit_log.txt — there is no separate database copy to
drift out of sync with it."""

import threading
from pathlib import Path

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.utils import timezone

AUDIT_LOG_PATH: Path = Path(settings.MEDIA_ROOT) / "audit_log.txt"

# One process-wide lock around the append — cheap insurance against two
# requests interleaving their writes; each write is a single small line so
# the window is tiny, but there's no reason to leave it open.
_write_lock = threading.Lock()


def _client_ip(request) -> str:
    # Behind a reverse proxy X-Forwarded-For carries the real client first;
    # falls back to the direct connection otherwise.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or "?"


def _write(username: str, ip: str, action: str) -> None:
    when = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{when} | {username or 'anonymous'} | {ip or '?'} | {action}\n"
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)


def log_action(request, action: str) -> None:
    """Appends one line to audit_log.txt for the current request's user
    (or "anonymous" if not logged in) and client IP."""
    username = request.user.username if request.user.is_authenticated else ""
    _write(username, _client_ip(request), action)


def read_events(limit: int | None = None) -> list[str]:
    """Returns raw log lines, most recent first (each already formatted as
    "when | user | ip | action"). Used by audit_log_view for display."""
    if not AUDIT_LOG_PATH.exists():
        return []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]
    lines.reverse()
    return lines[:limit] if limit else lines


@receiver(user_logged_in)
def _log_login(sender, request, user, **kwargs):
    # Read the user straight off the signal's own `user` kwarg rather than
    # request.user — Django's login() only back-fills request.user when the
    # request already had that attribute (i.e. went through
    # AuthenticationMiddleware), which isn't guaranteed for every caller
    # (e.g. Client.force_login() in tests uses a bare HttpRequest).
    _write(user.username, _client_ip(request), "Logged in")


@receiver(user_logged_out)
def _log_logout(sender, request, user, **kwargs):
    if user is not None:
        _write(user.username, _client_ip(request), "Logged out")
