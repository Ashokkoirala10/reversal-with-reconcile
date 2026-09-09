"""
Django settings for reversal_project.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# --- Security ---------------------------------------------------------
# Env-overridable (see .env.example's DJANGO_* block / docker-compose.yml)
# so a container can run DEBUG=False with a real secret key without any
# code change — the literal defaults below are exactly what this app ran
# with before these existed, so nothing changes for anyone who hasn't set
# them. Still change SECRET_KEY before deploying anywhere but your own
# machine.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-change-me-before-deploying")

DEBUG = os.environ.get("DJANGO_DEBUG", "True").strip().lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

# --- Applications -------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "reconcile",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves STATIC_ROOT itself (compressed + far-future cache headers) so
    # the container doesn't need a separate nginx just for static files —
    # sits right after SecurityMiddleware per whitenoise's own setup docs.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # After AuthenticationMiddleware so request.user is already resolved —
    # see core/middleware.py for what it logs and why.
    "core.middleware.RequestLoggingMiddleware",
]

ROOT_URLCONF = "reversal_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.feature_flags",
            ],
        },
    },
]

WSGI_APPLICATION = "reversal_project.wsgi.application"

# --- Database -------------------------------------------------------------
# Postgres if POSTGRES_DB is set in the environment (this is what
# docker-compose.yml's "db" service sets — see its POSTGRES_* env block),
# otherwise the sqlite file this app has always used, so running it bare
# (no .env Postgres config, e.g. `python manage.py runserver` on a laptop)
# keeps working exactly as before with zero setup.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
# else:
#     DATABASES = {
#         "default": {
#             "ENGINE": "django.db.backends.sqlite3",
#             "NAME": BASE_DIR / "db.sqlite3",
#         }
#     }
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kathmandu"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# `collectstatic`'s destination — whitenoise (see MIDDLEWARE above) serves
# straight out of this directory, so the container needs no separate nginx
# just for CSS/JS/images.
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- Media (uploaded source files + generated reversal files) -------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth -------------------------------------------------------------
# This app only has two hardcoded accounts (seeded via a data migration,
# see core/migrations/0003_seed_users.py) — there is no self-registration.
LOGIN_URL = "core:login"
LOGIN_REDIRECT_URL = "core:upload"
LOGOUT_REDIRECT_URL = "core:login"

# Max upload size the ibft-transaction file can reasonably be (50 MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

# --- SMTP (Extra page > "Verification format" tab's per-bank "Send mail") -
# Loaded from .env (see .env.example) — never hardcode credentials here.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("SMTP_HOST", "")
EMAIL_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
_smtp_encryption = os.environ.get("SMTP_ENCRYPTION", "tls").strip().lower()
EMAIL_USE_TLS = _smtp_encryption == "tls"
EMAIL_USE_SSL = _smtp_encryption == "ssl"
DEFAULT_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", EMAIL_HOST_USER)
# Without this, Django's SMTP backend uses no socket timeout at all — a
# slow/unreachable mail server can hang a send forever. core.scheduler's
# every-1-minute dispute check runs email.send() inside an APScheduler
# worker thread, and Python won't let the process fully exit (even after
# Ctrl+C) while that thread is still blocked — this bounds the hang.
EMAIL_TIMEOUT = 15

# Signature block appended to a verification email's body (see
# core/services.py:build_verification_email). Department/company are
# app-wide defaults; leave MAIL_SIGNATURE_PHONE/ADDRESS unset to omit those
# lines rather than showing one person's contact details for every sender.
# MAIL_SIGNATURE_NAME overrides the logged-in user's name on the "Regards,"
# line — set it when one person's signature (not each sender's own name) is
# what banks expect to see on these verification requests; leave unset to
# fall back to request.user.get_full_name() / username instead.
MAIL_SIGNATURE_NAME = os.environ.get("MAIL_SIGNATURE_NAME", "")
MAIL_SIGNATURE_TITLE = os.environ.get("MAIL_SIGNATURE_TITLE", "Tech Operation Department")
MAIL_SIGNATURE_COMPANY = os.environ.get("MAIL_SIGNATURE_COMPANY", "Smart Choice Technologies Ltd. (SCT)")
MAIL_SIGNATURE_PHONE = os.environ.get("MAIL_SIGNATURE_PHONE", "")
MAIL_SIGNATURE_ADDRESS = os.environ.get("MAIL_SIGNATURE_ADDRESS", "")
MAIL_SIGNATURE_TOLL_FREE = os.environ.get("MAIL_SIGNATURE_TOLL_FREE", "")
MAIL_SIGNATURE_WEBSITE = os.environ.get("MAIL_SIGNATURE_WEBSITE", "")

# --- Switch DB (read-only source for "Fetch from DB", see core/switch_db.py)
# Not a Django DATABASES alias on purpose — see switch_db.py's module
# docstring for why.
SWITCH_DB = {
    "HOST": os.environ.get("SWITCH_DB_HOST", ""),
    "PORT": os.environ.get("SWITCH_DB_PORT", "5432"),
    "NAME": os.environ.get("SWITCH_DB_NAME", ""),
    "USER": os.environ.get("SWITCH_DB_USER", ""),
    "PASSWORD": os.environ.get("SWITCH_DB_PASSWORD", ""),
}

# --- Logging --------------------------------------------------------------
# Two rotating files under BASE_DIR/logs, written as JSON lines (see
# core/logging_utils.JsonFormatter) so a log shipper (Promtail / Grafana
# Agent tailing these files into Loki, etc.) can query on level/logger/any
# extra={} field without a grok pattern: app.log gets every INFO+ line
# (every request via core.middleware.RequestLoggingMiddleware, plus every
# logger.info()/.exception() call across core/reconcile), error.log gets
# ERROR+ only (uncaught exceptions — Django's own "django.request" logger
# already emits those with a full traceback for every 5xx response) so a
# problem can be found without scrolling past routine request lines. Not a
# replacement for core.audit's audit_log.txt, which is a user-facing "who
# did what" business trail, not an operational one.
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {module}.{funcName}:{lineno} — {message}",
            "style": "{",
        },
        "json": {
            "()": "core.logging_utils.JsonFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "DEBUG" if DEBUG else "INFO",
            "formatter": "verbose",
        },
        "app_file": {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": LOGS_DIR / "app.log",
            "when": "midnight",
            "backupCount": 14,
            "encoding": "utf-8",
            "level": "INFO",
            "formatter": "json",
        },
        "error_file": {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": LOGS_DIR / "error.log",
            "when": "midnight",
            "backupCount": 14,
            "encoding": "utf-8",
            "level": "ERROR",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console", "app_file", "error_file"],
        "level": "INFO",
    },
    "loggers": {
        # Django's own request/500 logger — routed here instead of left to
        # propagate only to the default console-only config, so an
        # uncaught exception in a view always lands in error.log too.
        "django.request": {
            "handlers": ["console", "app_file", "error_file"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
