"""
Django settings for reversal_project.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# --- Security ---------------------------------------------------------
# Change this before deploying anywhere outside your own machine.
SECRET_KEY = "django-insecure-change-me-before-deploying"

DEBUG = True

ALLOWED_HOSTS = ["*"]

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
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
# Simple sqlite db is enough for the audit log.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.postgresql",
#         "NAME": os.environ.get("POSTGRES_DB", "sct_reversal_db"),
#         "USER": os.environ.get("POSTGRES_USER", "postgres"),
#         "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
#         "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
#         "PORT": os.environ.get("POSTGRES_PORT", "5432"),
#     }
# }
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
