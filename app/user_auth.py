from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request

from app.config import app_env, get_bool, get_int
from app.db import execute, fetch_one, transaction
from app.pii import hash_ip
from app.security import create_user_access_token, hash_password, hash_token, verify_password


LOGIN_LOCK_THRESHOLD = max(1, get_int("LOGIN_LOCK_THRESHOLD", 5))
LOGIN_LOCK_DURATION_MINUTES = max(1, get_int("LOGIN_LOCK_DURATION_MINUTES", 15))
USER_REFRESH_EXPIRE_DAYS = max(1, get_int("USER_REFRESH_EXPIRE_DAYS", get_int("REFRESH_EXPIRE_DAYS", 30)))
PASSWORD_RESET_EXPIRE_MINUTES = max(5, get_int("PASSWORD_RESET_EXPIRE_MINUTES", 30))
_DUMMY_USER_HASH = hash_password("dummy-user-password-123")


@dataclass(frozen=True)
class IssuedUserCredentials:
    user_id: str
    role: str
    session_id: str
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class PasswordResetRequestResult:
    reset_token: Optional[str] = None


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def _request_ip(request: Request) -> Optional[str]:
    if request.client and request.client.host:
        return str(request.client.host)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return None


def _request_device_id(request: Request) -> Optional[str]:
    raw = request.headers.get("x-device-id") or request.headers.get("x-client-device-id")
    value = str(raw or "").strip()
    return value[:200] if value else None


def _record_login_attempt(email: str, request: Request, *, success: bool, failure_reason: str | None) -> None:
    execute(
        """
        INSERT INTO login_attempts (email, ip_address, success, user_agent, failure_reason, attempted_at)
        VALUES (%s, %s, %s, %s, %s, now())
        """,
        (
            _normalize_email(email),
            hash_ip(_request_ip(request)),
            bool(success),
            request.headers.get("user-agent"),
            failure_reason,
        ),
    )


def _issue_user_session(user_id: str, role: str, request: Request) -> IssuedUserCredentials:
    session_id = str(uuid.uuid4())
    refresh_token = secrets.token_urlsafe(48)
    execute(
        """
        INSERT INTO user_sessions (
            id, user_id, refresh_token_hash, device_id, user_agent, ip_address,
            created_at, last_used_at, expires_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            now(), now(), now() + (%s::int * interval '1 day')
        )
        """,
        (
            session_id,
            user_id,
            hash_token(refresh_token),
            _request_device_id(request),
            request.headers.get("user-agent"),
            hash_ip(_request_ip(request)),
            USER_REFRESH_EXPIRE_DAYS,
        ),
    )
    access_token = create_user_access_token(user_id, role=role or "user", session_id=session_id)
    return IssuedUserCredentials(
        user_id=user_id,
        role=role or "user",
        session_id=session_id,
        access_token=access_token,
        refresh_token=refresh_token,
    )


def register_user(email: str, password: str, request: Request) -> IssuedUserCredentials:
    normalized_email = _normalize_email(email)
    existing = fetch_one("SELECT 1 FROM users WHERE lower(email)=lower(%s) AND deleted_at IS NULL", (normalized_email,))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user_id = str(uuid.uuid4())
    pwd_hash = hash_password(password)
    with transaction():
        execute(
            """
            INSERT INTO users (id, email, password_hash, role, last_login_at)
            VALUES (%s, %s, %s, %s, now())
            """,
            (user_id, normalized_email, pwd_hash, "user"),
        )
        credentials = _issue_user_session(user_id, "user", request)
    return credentials


def authenticate_user(email: str, password: str, request: Request) -> IssuedUserCredentials:
    normalized_email = _normalize_email(email)
    row = fetch_one(
        """
        SELECT id::text, password_hash, role, failed_login_attempts, locked_until
        FROM users
        WHERE lower(email)=lower(%s)
          AND deleted_at IS NULL
        LIMIT 1
        """,
        (normalized_email,),
    )

    if not row:
        verify_password(password, _DUMMY_USER_HASH)
        _record_login_attempt(normalized_email, request, success=False, failure_reason="invalid_credentials")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id, pwd_hash, role, _failed_attempts, locked_until = row
    now = datetime.now(timezone.utc)
    if locked_until is not None and locked_until > now:
        verify_password(password, str(pwd_hash or _DUMMY_USER_HASH))
        _record_login_attempt(normalized_email, request, success=False, failure_reason="account_locked")
        raise HTTPException(status_code=423, detail="Account temporarily locked")

    if not verify_password(password, str(pwd_hash or "")):
        with transaction():
            execute(
                """
                UPDATE users
                SET failed_login_attempts = failed_login_attempts + 1,
                    locked_until = CASE
                        WHEN failed_login_attempts + 1 >= %s
                        THEN now() + (%s::int * interval '1 minute')
                        ELSE locked_until
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                (LOGIN_LOCK_THRESHOLD, LOGIN_LOCK_DURATION_MINUTES, user_id),
            )
            _record_login_attempt(normalized_email, request, success=False, failure_reason="invalid_credentials")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    with transaction():
        execute(
            """
            UPDATE users
            SET failed_login_attempts = 0,
                locked_until = NULL,
                last_login_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (user_id,),
        )
        _record_login_attempt(normalized_email, request, success=True, failure_reason=None)
        credentials = _issue_user_session(str(user_id), str(role or "user"), request)
    return credentials


def refresh_user_credentials(refresh_token: str, request: Request) -> IssuedUserCredentials:
    token_hash = hash_token(refresh_token)
    with transaction():
        row = fetch_one(
            """
            SELECT s.id::text, s.user_id::text, u.role
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.refresh_token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND u.deleted_at IS NULL
            FOR UPDATE OF s
            """,
            (token_hash,),
        )
        if not row:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        session_id, user_id, role = row
        new_refresh_token = secrets.token_urlsafe(48)
        execute(
            """
            UPDATE user_sessions
            SET refresh_token_hash = %s,
                last_used_at = now(),
                user_agent = COALESCE(%s, user_agent),
                ip_address = COALESCE(%s, ip_address)
            WHERE id = %s
            """,
            (
                hash_token(new_refresh_token),
                request.headers.get("user-agent"),
                hash_ip(_request_ip(request)),
                session_id,
            ),
        )
    return IssuedUserCredentials(
        user_id=str(user_id),
        role=str(role or "user"),
        session_id=str(session_id),
        access_token=create_user_access_token(str(user_id), role=str(role or "user"), session_id=str(session_id)),
        refresh_token=new_refresh_token,
    )


def revoke_user_refresh_token(refresh_token: str) -> None:
    execute(
        """
        UPDATE user_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, 'logout')
        WHERE refresh_token_hash = %s
        """,
        (hash_token(refresh_token),),
    )


def revoke_user_session(user_id: str, session_id: str, *, reason: str = "user_requested") -> None:
    execute(
        """
        UPDATE user_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, %s)
        WHERE id::text = %s
          AND user_id::text = %s
        """,
        (reason, session_id, user_id),
    )


def revoke_all_user_sessions(user_id: str, *, reason: str = "user_requested") -> None:
    execute(
        """
        UPDATE user_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revoke_reason = COALESCE(revoke_reason, %s)
        WHERE user_id::text = %s
          AND revoked_at IS NULL
        """,
        (reason, user_id),
    )


def request_password_reset(email: str, request: Request) -> PasswordResetRequestResult:
    normalized_email = _normalize_email(email)
    row = fetch_one(
        """
        SELECT id::text
        FROM users
        WHERE lower(email)=lower(%s)
          AND deleted_at IS NULL
        LIMIT 1
        """,
        (normalized_email,),
    )
    if not row:
        verify_password("dummy-password-reset-123", _DUMMY_USER_HASH)
        return PasswordResetRequestResult(reset_token=None)

    reset_token = secrets.token_urlsafe(48)
    execute(
        """
        INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at, ip_address)
        VALUES (%s, %s, %s, now(), now() + (%s::int * interval '1 minute'), %s)
        """,
        (
            str(uuid.uuid4()),
            str(row[0]),
            hash_token(reset_token),
            PASSWORD_RESET_EXPIRE_MINUTES,
            hash_ip(_request_ip(request)),
        ),
    )
    if app_env() == "dev" and get_bool("ALLOW_DEV_PASSWORD_RESET_TOKEN", False):
        return PasswordResetRequestResult(reset_token=reset_token)
    return PasswordResetRequestResult(reset_token=None)


def confirm_password_reset(token: str, new_password: str) -> None:
    token_hash = hash_token(token)
    with transaction():
        row = fetch_one(
            """
            SELECT prt.id::text, prt.user_id::text
            FROM password_reset_tokens prt
            JOIN users u ON u.id = prt.user_id
            WHERE prt.token_hash = %s
              AND prt.used_at IS NULL
              AND prt.expires_at > now()
              AND u.deleted_at IS NULL
            FOR UPDATE OF prt
            """,
            (token_hash,),
        )
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")

        reset_id, user_id = row
        execute(
            """
            UPDATE users
            SET password_hash = %s,
                failed_login_attempts = 0,
                locked_until = NULL,
                updated_at = now()
            WHERE id::text = %s
            """,
            (hash_password(new_password), user_id),
        )
        execute("UPDATE password_reset_tokens SET used_at = now() WHERE id::text = %s", (reset_id,))
        revoke_all_user_sessions(str(user_id), reason="password_reset")
