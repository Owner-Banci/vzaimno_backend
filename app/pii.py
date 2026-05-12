from __future__ import annotations

import hashlib
import hmac
import os
from functools import lru_cache

from app.config import get_env
from app.logging_utils import logger


_warned_missing_ip_hash_key = False
_warned_missing_phone_key = False
_warned_missing_phone_hash_key = False


@lru_cache(maxsize=1)
def ip_hash_key() -> str:
    return (get_env("IP_HASH_KEY", "") or "").strip()


@lru_cache(maxsize=1)
def pii_encryption_key() -> str:
    return (os.getenv("PII_ENCRYPTION_KEY", "") or "").strip()


@lru_cache(maxsize=1)
def phone_hash_key() -> str:
    return (os.getenv("PHONE_HASH_KEY", "") or "").strip()


def hash_ip(ip_value: str | None) -> str | None:
    global _warned_missing_ip_hash_key

    raw = str(ip_value or "").strip()
    if not raw:
        return None

    key = ip_hash_key()
    if not key:
        if not _warned_missing_ip_hash_key:
            logger.warning(
                "ip_hash_key_missing",
                extra={"status_code": 0, "event": "ip_hash_key_missing"},
            )
            _warned_missing_ip_hash_key = True
        return raw

    digest = hmac.new(key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def hash_phone(phone_value: str | None) -> str | None:
    global _warned_missing_phone_hash_key

    raw = str(phone_value or "").strip()
    if not raw:
        return None

    key = phone_hash_key()
    if not key:
        if not _warned_missing_phone_hash_key:
            logger.warning(
                "phone_hash_key_missing",
                extra={"status_code": 0, "event": "phone_hash_key_missing"},
            )
            _warned_missing_phone_hash_key = True
        return None

    return hmac.new(key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def decrypt_phone_expr(column_expr: str) -> tuple[str, tuple]:
    """
    SQL expression builder for decrypting users.phone_enc.

    Migration 0004 writes phone_enc with pgp_sym_encrypt, so runtime readers
    must use pgp_sym_decrypt with a bound PII key.
    """
    global _warned_missing_phone_key

    key = pii_encryption_key()
    if not key:
        if not _warned_missing_phone_key:
            logger.warning(
                "pii_encryption_key_missing",
                extra={"status_code": 0, "event": "pii_encryption_key_missing"},
            )
            _warned_missing_phone_key = True
        return ("NULL::text", ())

    return (f"pgp_sym_decrypt({column_expr}, %s)::text", (key,))
