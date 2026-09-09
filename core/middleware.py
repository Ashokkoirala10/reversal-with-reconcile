"""Request-level logging — one INFO line when Django is about to call a
view (who, what, from where) and one INFO line when that request finishes
(status code, duration). Unhandled exceptions need no extra code here:
Django's own "django.request" logger already emits an ERROR line with the
full traceback for every 5xx response — see LOGGING in settings.py, which
routes that logger to logs/error.log.

This is deliberately separate from core.audit.log_action(), which is a
user-facing "who did what" business audit trail (viewable in-app, one
line per meaningful action). This middleware is operational logging for
developers/ops — every request, not just the ones a view chooses to
narrate."""

import logging
import time

logger = logging.getLogger("core.request")


def _username(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.username
    return "anonymous"


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or "?"


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        view_name = getattr(getattr(request, "resolver_match", None), "view_name", request.path)
        user, ip = _username(request), _client_ip(request)
        logger.info(
            "%s %s -> %s [%s] %.1fms user=%s ip=%s",
            request.method, request.path, response.status_code, view_name, duration_ms, user, ip,
            extra={
                "http_method": request.method,
                "path": request.path,
                "status_code": response.status_code,
                "view": view_name,
                "duration_ms": duration_ms,
                "user": user,
                "ip": ip,
            },
        )
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        view_name = getattr(view_func, "__name__", str(view_func))
        user, ip = _username(request), _client_ip(request)
        logger.info(
            "-> %s %s [%s] user=%s ip=%s",
            request.method, request.path, view_name, user, ip,
            extra={"http_method": request.method, "path": request.path, "view": view_name, "user": user, "ip": ip},
        )
        return None
