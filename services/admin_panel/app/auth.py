from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqladmin.authentication import AuthenticationBackend

from app.security import create_access_token, decode_token, verify_password

from .db import SessionLocal
from .models_sqlalchemy import User


STAFF_ROLES = {"admin", "moderator", "support"}


@dataclass
class StaffUser:
    id: str
    email: str
    role: str


def _as_str(value: object) -> str:
    return "" if value is None else str(value)


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


def _load_staff_user(token: str) -> Optional[StaffUser]:
    try:
        payload = decode_token(token)
    except Exception:
        return None

    user_id = payload.get("sub")
    role = payload.get("role")
    if not user_id or role not in STAFF_ROLES:
        return None

    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT id::text, email, role
                FROM users
                WHERE id::text = :user_id
                LIMIT 1
                """
            ),
            {"user_id": str(user_id)},
        ).first()
        if not row or row[2] not in STAFF_ROLES:
            return None
        return StaffUser(id=str(row[0]), email=str(row[1]), role=str(row[2]))


class AdminAuth(AuthenticationBackend):
    def __init__(self, secret_key: str) -> None:
        super().__init__(secret_key=secret_key)

    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = str(form.get("email") or form.get("username") or "").strip().lower()
        password = str(form.get("password", ""))
        if not email or not password:
            return False

        with SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == email))
            if not user or user.role not in STAFF_ROLES:
                return False
            if not verify_password(password, user.password_hash):
                return False

            user_id = _as_str(user.id)
            user_role = _as_str(user.role)
            user_email = _as_str(user.email)
            token = create_access_token({"sub": user_id, "role": user_role})

        request.session.update(
            {
                "admin_token": token,
                "admin_user_id": user_id,
                "admin_email": user_email,
                "admin_role": user_role,
            }
        )
        return True

    async def logout(self, request: Request) -> bool:
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
        request.session.update(
            {
                "admin_token": token,
                "admin_user_id": user.id,
                "admin_email": user.email,
                "admin_role": user.role,
            }
        )
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
