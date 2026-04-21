from __future__ import annotations

from functools import lru_cache
from typing import Tuple

from app.pii import decrypt_phone_expr
from app.schema_compat import table_has_column


@lru_cache(maxsize=1)
def _users_has_phone_column() -> bool:
    return table_has_column("users", "phone")


@lru_cache(maxsize=1)
def _users_has_phone_enc_column() -> bool:
    return table_has_column("users", "phone_enc")


def _phone_like_expr(user_alias: str) -> tuple[str | None, tuple]:
    """
    Build a SQL expression for user phone-like contact in a schema-safe way.

    Priority:
    1) users.phone (legacy schema)
    2) users.phone_enc via decrypt_phone_expr (encrypted schema)
    3) None
    """
    if _users_has_phone_column():
        return f"{user_alias}.phone", ()

    if _users_has_phone_enc_column():
        expr, params = decrypt_phone_expr(f"{user_alias}.phone_enc")
        return expr, tuple(params)

    return None, ()


def user_display_name_sql(
    *,
    user_alias: str,
    profile_alias: str | None,
    fallback: str = "Пользователь",
) -> tuple[str, tuple]:
    parts: list[str] = []
    params: list[object] = []

    if profile_alias:
        parts.append(f"NULLIF(BTRIM({profile_alias}.display_name), '')")

    phone_expr, phone_params = _phone_like_expr(user_alias)
    if phone_expr:
        parts.append(f"NULLIF(BTRIM({phone_expr}), '')")
        params.extend(phone_params)

    parts.append(f"NULLIF(BTRIM({user_alias}.email), '')")
    parts.append("%s")
    params.append(fallback)

    return f"COALESCE({', '.join(parts)})", tuple(params)


def user_contact_sql(*, user_alias: str) -> tuple[str, tuple]:
    parts: list[str] = []
    params: list[object] = []

    phone_expr, phone_params = _phone_like_expr(user_alias)
    if phone_expr:
        parts.append(f"NULLIF(BTRIM({phone_expr}), '')")
        params.extend(phone_params)

    parts.append(f"NULLIF(BTRIM({user_alias}.email), '')")
    return f"COALESCE({', '.join(parts)})", tuple(params)

