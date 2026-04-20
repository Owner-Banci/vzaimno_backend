from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache

from app.config import get_env
from app.logging_utils import logger


_warned_missing_ip_hash_key = False
_warned_missing_phone_key = False


@lru_cache(maxsize=1)
def ip_hash_key() -> str:
    return (get_env("IP_HASH_KEY", "") or "").strip()


@lru_cache(maxsize=1)
def pii_encryption_key() -> str:
    return (get_env("PII_ENCRYPTION_KEY", "") or "").strip()


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


def decrypt_phone_expr(column_expr: str) -> tuple[str, tuple]:
    """
    Backward-compatible SQL expression builder for phone decryption.

    Current lightweight local build stores plain phone/email in text columns,
    so we safely cast to text and return empty bind params.
    """
    global _warned_missing_phone_key

    if not pii_encryption_key() and not _warned_missing_phone_key:
        logger.warning(
            "pii_encryption_key_missing",
            extra={"status_code": 0, "event": "pii_encryption_key_missing"},
        )
        _warned_missing_phone_key = True

    return (f"{column_expr}::text", ())

