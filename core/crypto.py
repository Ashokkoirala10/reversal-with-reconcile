"""Encryption for secrets stored in the database — currently only
core.models.MailServerConfig.password.

This is encryption, not hashing, on purpose: unlike a login password, this
app needs the plaintext back out again to actually authenticate with an
SMTP server, so a one-way hash (as Django uses for User.password) would be
the wrong tool here — there'd be nothing to decrypt it back into.

The Fernet key is derived deterministically from settings.SECRET_KEY
(SHA-256, base64-urlsafe-encoded) rather than a separate
FIELD_ENCRYPTION_KEY .env value, so there's nothing new to configure,
generate, or lose track of. The trade-off: rotating SECRET_KEY makes every
already-encrypted password undecryptable (they'd need re-entering on the
Mail settings tab) — acceptable here since this app's SECRET_KEY is
essentially never rotated in practice.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.db import models


def _fernet() -> Fernet:
    from django.conf import settings  # lazy: importable before Django's apps are ready

    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Not a Fernet token — most likely a plaintext value saved before
        # encryption was added here. Return it as-is instead of raising, so
        # nothing already in the DB breaks; the next save re-encrypts it.
        return ciphertext


class EncryptedCharField(models.CharField):
    """A CharField that's encrypted at rest and transparent everywhere
    else — Python code (forms, views, resolve_mail_connection_config())
    always sees/sets the plaintext value; only the DB column holds the
    Fernet ciphertext. `max_length` here is the *plaintext* limit users
    see on the form; the column itself needs real headroom for the
    ciphertext, which is why MailServerConfig.password's own max_length is
    set well above what a human password ever needs (see that field's
    definition)."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_secret(value) if value else value

    def from_db_value(self, value, expression, connection):
        return decrypt_secret(value) if value else value
