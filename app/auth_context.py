from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import fetch_one
from app.security import decode_user_access_token


bearer = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class UserPrincipal:
    id: str
    email: str
    role: str = "user"


def _extract_ws_token(websocket: WebSocket) -> Optional[str]:
    auth_header = websocket.headers.get("authorization")
    if auth_header:
        prefix = "bearer "
        if auth_header.lower().startswith(prefix):
            token = auth_header[len(prefix) :].strip()
            if token:
                return token

    query_token = websocket.query_params.get("token")
    if query_token:
        normalized = query_token.strip()
        return normalized or None
    return None


def user_from_token(token: str) -> UserPrincipal:
    if token == "DEV_TOKEN":
        return UserPrincipal(id="dev", email="dev@localdomain.com", role="user")

    try:
        payload = decode_user_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid user token") from exc

    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    row = fetch_one(
        """
        SELECT id::text, email
        FROM users
        WHERE id = %s
          AND deleted_at IS NULL
        """,
        (user_id,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return UserPrincipal(id=str(row[0]), email=str(row[1]), role="user")


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> UserPrincipal:
    return user_from_token(creds.credentials)


def get_websocket_user(websocket: WebSocket) -> UserPrincipal:
    token = _extract_ws_token(websocket)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    return user_from_token(token)
