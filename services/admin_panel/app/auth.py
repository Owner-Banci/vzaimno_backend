from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqladmin.authentication import AuthenticationBackend
from starlette.concurrency import run_in_threadpool

from app.audit import log_audit_event
from app.config import get_int
from app.pii import hash_ip
from app.rate_limit import LimitRule, enforce_rate_limit
from app.security import (
    ADMIN_ACCESS_EXPIRE_MINUTES,
    ADMIN_JWT_EXPIRE_MINUTES,
    create_admin_access_token,
    decode_admin_access_token,
    hash_password,
    hash_token,
    verify_password,
)

from .db import SessionLocal


STAFF_ROLES = {"admin", "moderator", "support"}
LOGIN_LOCK_THRESHOLD = max(1, get_int("LOGIN_LOCK_THRESHOLD", 5))
LOGIN_LOCK_DURATION_MINUTES = max(1, get_int("LOGIN_LOCK_DURATION_MINUTES", 15))
ADMIN_REFRESH_EXPIRE_DAYS = max(1, get_int("ADMIN_REFRESH_EXPIRE_DAYS", get_int("REFRESH_EXPIRE_DAYS", 30)))
_DUMMY_ADMIN_HASH = hash_password("dummy-admin-password")


@dataclass
class StaffUser:
    id: str
    login_identifier: str
    email: Optional[str]
    role: str
    display_name: str
    linked_user_account_id: Optional[str]
    session_id: Optional[str] = None


def _extract_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    if hasattr(request, "session"):
        token = request.session.get("admin_token")
        if token:
            return str(token)
    token = request.cookies.get("admin_token")
    return token or None


def _request_ip(request: Request) -> Optional[str]:
    if request.client and request.client.host:
        return str(request.client.host)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return None


def _normalize_login_identifier(value: object) -> str:
    return str(value or "").strip().lower()


def _row_to_staff_user(row, session_id: Optional[str] = None) -> StaffUser:
    return StaffUser(
        id=str(row[0]),
        login_identifier=str(row[1] or ""),
        email=str(row[2]) if row[2] is not None else None,
        role=str(row[3] or "support"),
        display_name=str(row[4] or row[2] or row[1] or "Команда Vzaimno"),
        linked_user_account_id=str(row[5]) if row[5] is not None else None,
        session_id=session_id,
    )


def _load_admin_account_by_login(login_identifier: str):
    with SessionLocal() as session:
        return session.execute(
            text(
                """
                SELECT
                    aa.id::text,
                    aa.login_identifier,
                    aa.email,
                    aa.role,
                    aa.display_name,
                    aa.linked_user_account_id::text,
                    aa.password_hash,
                    aa.failed_login_attempts,
                    aa.locked_until
                FROM admin_accounts aa
                WHERE lower(aa.login_identifier) = lower(:login_identifier)
                  AND aa.status = 'active'
                  AND aa.disabled_at IS NULL
                LIMIT 1
                """
            ),
            {"login_identifier": login_identifier},
        ).first()


def _store_session(request: Request, token: str, user: StaffUser) -> None:
    request.session.update(
        {
            "admin_token": token,
            "admin_account_id": user.id,
            "admin_login_identifier": user.login_identifier,
            "admin_email": user.email,
            "admin_role": user.role,
            "admin_display_name": user.display_name,
            "admin_session_id": user.session_id,
        }
    )


def _record_login_attempt(login_identifier: str, request: Request, *, success: bool, failure_reason: str | None) -> None:
    with SessionLocal() as session:
        session.execute(
            text(
                """
                INSERT INTO login_attempts (email, ip_address, success, user_agent, failure_reason, attempted_at)
                VALUES (:email, :ip_address, :success, :user_agent, :failure_reason, now())
                """
            ),
            {
                "email": _normalize_login_identifier(login_identifier),
                "ip_address": hash_ip(_request_ip(request)),
                "success": bool(success),
                "user_agent": request.headers.get("user-agent"),
                "failure_reason": failure_reason,
            },
        )
        session.commit()


@lru_cache(maxsize=1)
def _admin_sessions_has_refresh_hash() -> bool:
    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'admin_sessions'
                  AND column_name = 'refresh_token_hash'
                """
            )
        ).first()
        return bool(row)


def _issue_admin_session(user: StaffUser, request: Request) -> tuple[str, str]:
    session_id = str(uuid.uuid4())
    token_id = str(uuid.uuid4())
    refresh_token = secrets.token_urlsafe(48)
    refresh_hash = hash_token(refresh_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=ADMIN_REFRESH_EXPIRE_DAYS)

    with SessionLocal() as session:
        if _admin_sessions_has_refresh_hash():
            session.execute(
                text(
                    """
                    INSERT INTO admin_sessions (
                        id,
                        admin_account_id,
                        token_id,
                        refresh_token_hash,
                        created_at,
                        updated_at,
                        last_seen_at,
                        expires_at,
                        revoked_at,
                        user_agent,
                        ip_address
                    )
                    VALUES (
                        CAST(:session_id AS uuid),
                        CAST(:admin_account_id AS uuid),
                        CAST(:token_id AS uuid),
                        :refresh_token_hash,
                        :now,
                        :now,
                        :now,
                        :expires_at,
                        NULL,
                        :user_agent,
                        :ip_address
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "admin_account_id": user.id,
                    "token_id": token_id,
                    "refresh_token_hash": refresh_hash,
                    "now": now,
                    "expires_at": expires_at,
                    "user_agent": request.headers.get("user-agent"),
                    "ip_address": hash_ip(_request_ip(request)),
                },
            )
        else:
            # Backward-compatible fallback if refresh column hasn't been migrated yet.
            session.execute(
                text(
                    """
                    INSERT INTO admin_sessions (
                        id,
                        admin_account_id,
                        token_id,
                        created_at,
                        updated_at,
                        last_seen_at,
                        expires_at,
                        revoked_at,
                        user_agent,
                        ip_address
                    )
                    VALUES (
                        CAST(:session_id AS uuid),
                        CAST(:admin_account_id AS uuid),
                        CAST(:token_id AS uuid),
                        :now,
                        :now,
                        :now,
                        :expires_at,
                        NULL,
                        :user_agent,
                        :ip_address
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "admin_account_id": user.id,
                    "token_id": token_id,
                    "now": now,
                    "expires_at": now + timedelta(minutes=ADMIN_ACCESS_EXPIRE_MINUTES),
                    "user_agent": request.headers.get("user-agent"),
                    "ip_address": hash_ip(_request_ip(request)),
                },
            )
        session.commit()
    return session_id, refresh_token


def _authenticate_admin_credentials_sync(login_identifier: str, password: str, request: Request) -> tuple[StaffUser, str, str]:
    normalized_login = _normalize_login_identifier(login_identifier)
    if not normalized_login or not password:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    row = _load_admin_account_by_login(normalized_login)
    candidate_hash = str(row[6] or "") if row else _DUMMY_ADMIN_HASH
    password_ok = verify_password(password, candidate_hash)
    now = datetime.now(timezone.utc)

    if row and row[8] is not None and row[8] > now:
        _record_login_attempt(normalized_login, request, success=False, failure_reason="locked")
        raise HTTPException(status_code=423, detail="Admin account is locked. Try again later.")

    if not row or not password_ok:
        _record_login_attempt(normalized_login, request, success=False, failure_reason="invalid_credentials")
        if row:
            with SessionLocal() as session:
                session.execute(
                    text(
                        """
                        UPDATE admin_accounts
                        SET failed_login_attempts = failed_login_attempts + 1,
                            locked_until = CASE
                                WHEN failed_login_attempts + 1 >= :threshold
                                    THEN now() + make_interval(mins => :lock_minutes)
                                ELSE locked_until
                            END,
                            updated_at = now()
                        WHERE id::text = :admin_account_id
                        """
                    ),
                    {
                        "threshold": LOGIN_LOCK_THRESHOLD,
                        "lock_minutes": LOGIN_LOCK_DURATION_MINUTES,
                        "admin_account_id": str(row[0]),
                    },
                )
                session.commit()
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    _record_login_attempt(normalized_login, request, success=True, failure_reason=None)
    user = _row_to_staff_user(row)
    session_id, refresh_token = _issue_admin_session(user, request)
    user = _row_to_staff_user(row, session_id=session_id)
    token = create_admin_access_token(
        user.id,
        role=user.role,
        session_id=session_id,
        expires_minutes=ADMIN_JWT_EXPIRE_MINUTES,
    )

    with SessionLocal() as session:
        session.execute(
            text(
                """
                UPDATE admin_accounts
                SET last_login_at = :now,
                    failed_login_attempts = 0,
                    locked_until = NULL,
                    updated_at = :now
                WHERE id::text = :admin_account_id
                """
            ),
            {"now": now, "admin_account_id": user.id},
        )
        session.commit()

    log_audit_event(
        actor_type="admin",
        actor_admin_account_id=user.id,
        action="admin_login",
        target_type="admin_account",
        target_id=user.id,
        details={
            "session_id": session_id,
            "login_identifier": user.login_identifier,
            "ip_address": hash_ip(_request_ip(request)),
            "user_agent": request.headers.get("user-agent"),
        },
    )
    _store_session(request, token, user)
    return user, token, refresh_token


def revoke_admin_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with SessionLocal() as session:
        session.execute(
            text(
                """
                UPDATE admin_sessions
                SET revoked_at = now(),
                    updated_at = now()
                WHERE id::text = :session_id
                  AND revoked_at IS NULL
                """
            ),
            {"session_id": session_id},
        )
        session.commit()


def revoke_all_admin_sessions(admin_account_id: str) -> None:
    with SessionLocal() as session:
        session.execute(
            text(
                """
                UPDATE admin_sessions
                SET revoked_at = now(),
                    updated_at = now()
                WHERE admin_account_id::text = :admin_account_id
                  AND revoked_at IS NULL
                """
            ),
            {"admin_account_id": admin_account_id},
        )
        session.commit()


def _refresh_admin_credentials_sync(refresh_token: str, request: Request) -> tuple[StaffUser, str, str]:
    if not _admin_sessions_has_refresh_hash():
        raise HTTPException(status_code=501, detail="Admin refresh is not available before DB migration")

    token_hash = hash_token(refresh_token)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT
                    aa.id::text,
                    aa.login_identifier,
                    aa.email,
                    aa.role,
                    aa.display_name,
                    aa.linked_user_account_id::text,
                    s.id::text
                FROM admin_sessions s
                JOIN admin_accounts aa
                  ON aa.id = s.admin_account_id
                WHERE s.refresh_token_hash = :refresh_token_hash
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND aa.status = 'active'
                  AND aa.disabled_at IS NULL
                LIMIT 1
                """
            ),
            {"refresh_token_hash": token_hash},
        ).first()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        old_session_id = str(row[6])
        session.execute(
            text(
                """
                UPDATE admin_sessions
                SET revoked_at = now(),
                    updated_at = now()
                WHERE id::text = :session_id
                  AND revoked_at IS NULL
                """
            ),
            {"session_id": old_session_id},
        )
        session.commit()

    user = _row_to_staff_user(row[:6])
    session_id, new_refresh_token = _issue_admin_session(user, request)
    user = _row_to_staff_user(row[:6], session_id=session_id)
    access_token = create_admin_access_token(
        user.id,
        role=user.role,
        session_id=session_id,
        expires_minutes=ADMIN_JWT_EXPIRE_MINUTES,
    )
    _store_session(request, access_token, user)
    return user, access_token, new_refresh_token


async def authenticate_admin_credentials(login_identifier: str, password: str, request: Request) -> tuple[StaffUser, str, str]:
    await enforce_rate_limit(
        "admin_login_ip",
        _request_ip(request) or "unknown",
        (LimitRule(5, 60), LimitRule(20, 3600)),
    )
    return await run_in_threadpool(_authenticate_admin_credentials_sync, login_identifier, password, request)


async def refresh_admin_credentials(refresh_token: str, request: Request) -> tuple[StaffUser, str, str]:
    await enforce_rate_limit(
        "admin_refresh_ip",
        _request_ip(request) or "unknown",
        (LimitRule(60, 60),),
    )
    return await run_in_threadpool(_refresh_admin_credentials_sync, refresh_token, request)


def _load_staff_user(token: str) -> Optional[StaffUser]:
    try:
        payload = decode_admin_access_token(token)
    except Exception:
        return None

    admin_account_id = str(payload.get("sub") or "").strip()
    role = str(payload.get("role") or "").strip().lower()
    session_id = str(payload.get("sid") or "").strip()
    if not admin_account_id or not session_id or role not in STAFF_ROLES:
        return None

    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT
                    aa.id::text,
                    aa.login_identifier,
                    aa.email,
                    aa.role,
                    aa.display_name,
                    aa.linked_user_account_id::text
                FROM admin_accounts aa
                JOIN admin_sessions s
                  ON s.admin_account_id = aa.id
                WHERE aa.id::text = :admin_account_id
                  AND s.id::text = :session_id
                  AND aa.status = 'active'
                  AND aa.disabled_at IS NULL
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                LIMIT 1
                """
            ),
            {"admin_account_id": admin_account_id, "session_id": session_id},
        ).first()
        if not row or str(row[3] or "").strip().lower() not in STAFF_ROLES:
            return None
        session.execute(
            text(
                """
                UPDATE admin_sessions
                SET last_seen_at = now(),
                    updated_at = now()
                WHERE id::text = :session_id
                """
            ),
            {"session_id": session_id},
        )
        session.commit()
    return _row_to_staff_user(row, session_id=session_id)


class AdminAuth(AuthenticationBackend):
    def __init__(self, secret_key: str) -> None:
        super().__init__(secret_key=secret_key)

    async def login(self, request: Request) -> bool:
        form = await request.form()
        login_identifier = str(form.get("login_identifier") or form.get("email") or form.get("username") or "")
        password = str(form.get("password", ""))
        try:
            await authenticate_admin_credentials(login_identifier, password, request)
        except HTTPException:
            return False
        return True

    async def logout(self, request: Request) -> bool:
        revoke_admin_session(request.session.get("admin_session_id"))
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = _extract_token(request)
        if not token:
            return False

        user = _load_staff_user(token)
        if not user:
            request.session.clear()
            return False

        request.state.admin_user = user
        _store_session(request, token, user)
        return True


def get_staff_user(request: Request) -> StaffUser:
    user = getattr(request.state, "admin_user", None)
    if isinstance(user, StaffUser):
        return user

    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Admin auth required")

    loaded = _load_staff_user(token)
    if not loaded:
        raise HTTPException(status_code=401, detail="Admin auth required")
    request.state.admin_user = loaded
    return loaded


def require_staff_user(request: Request) -> StaffUser:
    return get_staff_user(request)


def require_admin_user(request: Request) -> StaffUser:
    user = get_staff_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
