# app/security.py

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import UUID

import bcrypt
from dotenv import load_dotenv
from jose import JWTError, jwt

load_dotenv()


_BCRYPT_SHA256_PREFIX = "bcrypt_sha256$"


def _password_bytes(password: str) -> bytes:
    return (password or "").encode("utf-8")


def _bcrypt_sha256_input(password: str) -> bytes:
    return hashlib.sha256(_password_bytes(password)).hexdigest().encode("ascii")


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def hash_password(password: str) -> str:
    digest = _bcrypt_sha256_input(password)
    hashed = bcrypt.hashpw(digest, bcrypt.gensalt())
    return f"{_BCRYPT_SHA256_PREFIX}{hashed.decode('utf-8')}"


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False

    encoded_hash = password_hash.encode("utf-8")

    if password_hash.startswith(_BCRYPT_SHA256_PREFIX):
        raw_hash = password_hash[len(_BCRYPT_SHA256_PREFIX) :].encode("utf-8")
        return bcrypt.checkpw(_bcrypt_sha256_input(password), raw_hash)

    try:
        return bcrypt.checkpw(_password_bytes(password), encoded_hash)
    except ValueError:
        # Legacy bcrypt hashes may have been created by libraries that silently
        # truncated passwords to 72 bytes. Keep verification compatible.
        return bcrypt.checkpw(_password_bytes(password)[:72], encoded_hash)


JWT_SECRET = os.getenv("JWT_SECRET", "DEV_JWT_SECRET_CHANGE_ME")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", JWT_SECRET)
ADMIN_JWT_ALG = os.getenv("ADMIN_JWT_ALG", JWT_ALG)
ADMIN_JWT_EXPIRE_MINUTES = int(os.getenv("ADMIN_JWT_EXPIRE_MINUTES", str(JWT_EXPIRE_MINUTES)))
USER_TOKEN_AUDIENCE = os.getenv("USER_TOKEN_AUDIENCE", "user-api")
ADMIN_TOKEN_AUDIENCE = os.getenv("ADMIN_TOKEN_AUDIENCE", "admin-api")


def create_access_token(
    payload: Dict[str, Any],
    expires_minutes: int | None = None,
    *,
    secret: str | None = None,
    algorithm: str | None = None,
) -> str:
    data = _json_safe(dict(payload))

    exp_minutes = expires_minutes if expires_minutes is not None else JWT_EXPIRE_MINUTES
    exp = datetime.now(timezone.utc) + timedelta(minutes=exp_minutes)

    data["exp"] = exp
    data["iat"] = datetime.now(timezone.utc)

    return jwt.encode(data, secret or JWT_SECRET, algorithm=algorithm or JWT_ALG)


def create_user_access_token(user_id: str, *, role: str = "user", expires_minutes: int | None = None) -> str:
    return create_access_token(
        {
            "sub": str(user_id),
            "principal_type": "user",
            "token_kind": "user_access",
            "role": str(role or "user"),
            "aud": USER_TOKEN_AUDIENCE,
        },
        expires_minutes=expires_minutes,
        secret=JWT_SECRET,
        algorithm=JWT_ALG,
    )


def create_admin_access_token(
    admin_account_id: str,
    *,
    role: str,
    session_id: str,
    expires_minutes: int | None = None,
) -> str:
    return create_access_token(
        {
            "sub": str(admin_account_id),
            "principal_type": "admin",
            "token_kind": "admin_access",
            "role": str(role or "support"),
            "sid": str(session_id),
            "aud": ADMIN_TOKEN_AUDIENCE,
        },
        expires_minutes=expires_minutes if expires_minutes is not None else ADMIN_JWT_EXPIRE_MINUTES,
        secret=ADMIN_JWT_SECRET,
        algorithm=ADMIN_JWT_ALG,
    )


def decode_token(
    token: str,
    *,
    secret: str | None = None,
    algorithms: list[str] | None = None,
    audience: str | None = None,
) -> Dict[str, Any]:
    try:
        kwargs: Dict[str, Any] = {}
        if audience is not None:
            kwargs["audience"] = audience
        return jwt.decode(token, secret or JWT_SECRET, algorithms=algorithms or [JWT_ALG], **kwargs)
    except JWTError as e:
        raise ValueError("Invalid token") from e


def decode_user_access_token(token: str) -> Dict[str, Any]:
    payload = decode_token(token, secret=JWT_SECRET, algorithms=[JWT_ALG], audience=USER_TOKEN_AUDIENCE)
    if payload.get("principal_type") != "user" or payload.get("token_kind") != "user_access":
        raise ValueError("Invalid user token")
    return payload


def decode_admin_access_token(token: str) -> Dict[str, Any]:
    payload = decode_token(
        token,
        secret=ADMIN_JWT_SECRET,
        algorithms=[ADMIN_JWT_ALG],
        audience=ADMIN_TOKEN_AUDIENCE,
    )
    if payload.get("principal_type") != "admin" or payload.get("token_kind") != "admin_access":
        raise ValueError("Invalid admin token")
    return payload
