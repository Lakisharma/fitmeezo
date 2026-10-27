"""
Fitmeezo Banking Details Cryptographic Engine & Encryption at Rest.

Provides:
1. Authenticated encryption (Fernet AES-128-CBC + HMAC-SHA256) for sensitive merchant bank account numbers.
2. Zero-downtime key rotation via MultiFernet.
3. Strict failure on missing or malformed keys (never silently falls back to plaintext).
4. Custom Django model field `EncryptedBankAccountField` ensuring transparent ORM persistence and loading.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

logger = logging.getLogger(__name__)


class BankEncryptionError(Exception):
    """Base exception for banking details cryptographic operations."""
    pass


class BankEncryptionConfigurationError(BankEncryptionError, ImproperlyConfigured):
    """Raised when encryption key configuration is missing, empty, or malformed."""
    pass


class BankDecryptionError(BankEncryptionError):
    """Raised when ciphertext is corrupted, tampered with, or cannot be authenticated."""
    pass


def get_bank_fernet() -> MultiFernet:
    """
    Constructs and returns a MultiFernet instance from environment configuration.
    Raises BankEncryptionConfigurationError if key is missing or invalid.
    Never falls back to plaintext or dummy keys.
    """
    raw_key = getattr(settings, "FITMEEZO_BANK_ENCRYPTION_KEY", "") or ""
    if isinstance(raw_key, bytes):
        raw_key = raw_key.decode("ascii")
    raw_key = raw_key.strip()

    if not raw_key:
        raise BankEncryptionConfigurationError(
            "FITMEEZO_BANK_ENCRYPTION_KEY is not configured in settings or environment. "
            "A valid 32-byte URL-safe base64 Fernet key is required for sensitive banking encryption at rest."
        )

    keys = [raw_key]
    rotation_keys = getattr(settings, "FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION", []) or []
    if isinstance(rotation_keys, str):
        rotation_keys = [k.strip() for k in rotation_keys.split(",") if k.strip()]
    for rk in rotation_keys:
        if rk and rk not in keys:
            keys.append(rk)

    fernet_instances: list[Fernet] = []
    for k in keys:
        try:
            decoded = base64.urlsafe_b64decode(k.encode("ascii"))
            if len(decoded) != 32:
                raise ValueError(f"Fernet key must decode to exactly 32 bytes (got {len(decoded)} bytes).")
            fernet_instances.append(Fernet(k.encode("ascii")))
        except Exception as err:
            raise BankEncryptionConfigurationError(
                f"Invalid banking encryption key: {err}. Key must be 32 URL-safe base64-encoded bytes."
            ) from err

    return MultiFernet(fernet_instances)


def encrypt_bank_account_number(plaintext: str | None) -> str:
    """
    Encrypts a bank account number into an authenticated URL-safe base64 Fernet token.
    Returns empty string for falsy/empty values.
    """
    if not plaintext:
        return ""
    clean = str(plaintext).strip()
    if not clean:
        return ""

    fernet = get_bank_fernet()

    # Prevent double-encryption if already valid ciphertext under active keys
    if clean.startswith("gAAAAA"):
        try:
            fernet.decrypt(clean.encode("ascii"))
            return clean
        except InvalidToken:
            pass

    token_bytes = fernet.encrypt(clean.encode("utf-8"))
    return token_bytes.decode("ascii")


def decrypt_bank_account_number(ciphertext: str | None) -> str:
    """
    Decrypts an authenticated Fernet token back into plaintext bank account number.
    Raises BankDecryptionError if the token was tampered with or corrupted.
    Handles legacy plaintext records gracefully if unmigrated, but requires a valid key configured.
    """
    if not ciphertext:
        return ""
    clean = str(ciphertext).strip()
    if not clean:
        return ""

    # Always ensure valid encryption key is configured first
    fernet = get_bank_fernet()

    # Authenticated Fernet ciphertext token identification
    if clean.startswith("gAAAAA"):
        try:
            decrypted_bytes = fernet.decrypt(clean.encode("ascii"))
            return decrypted_bytes.decode("utf-8")
        except InvalidToken as it:
            raise BankDecryptionError(
                "Decryption failed: Bank account ciphertext is corrupted, tampered with, or encrypted with an unrecognized key."
            ) from it
        except Exception as exc:
            raise BankDecryptionError(f"Unexpected decryption failure: {exc}") from exc

    # Unmigrated legacy plaintext record fallback (read-only tolerance prior to running data migration)
    return clean


class EncryptedBankAccountField(models.CharField):
    """
    Django Model Field for storing merchant bank account numbers encrypted at rest.
    - Transparently encrypts plaintext to authenticated Fernet ciphertext on database write.
    - Transparently decrypts ciphertext to plaintext on model instance load.
    - Prevents sensitive financial account exposure in database backups, logs, and dumps.
    """

    description = "Authenticated Fernet-encrypted bank account number stored as ciphertext."

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 255)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if kwargs.get("max_length") == 255:
            del kwargs["max_length"]
        return name, path, args, kwargs

    def get_internal_type(self) -> str:
        return "CharField"

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str:
        if value is None or value == "":
            return ""
        return decrypt_bank_account_number(value)

    def to_python(self, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, str) and value.startswith("gAAAAA"):
            try:
                return decrypt_bank_account_number(value)
            except BankDecryptionError:
                return value
        return str(value)

    def get_prep_value(self, value: Any) -> str:
        value = super().get_prep_value(value)
        if value is None or value == "":
            return ""
        return encrypt_bank_account_number(value)
