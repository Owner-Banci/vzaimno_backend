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


def create_access_token(payload: Dict[str, Any], expires_minutes: int | None = None) -> str:
    data = _json_safe(dict(payload))

    exp_minutes = expires_minutes if expires_minutes is not None else JWT_EXPIRE_MINUTES
    exp = datetime.now(timezone.utc) + timedelta(minutes=exp_minutes)

    data["exp"] = exp
    data["iat"] = datetime.now(timezone.utc)

    return jwt.encode(data, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except JWTError as e:
        raise ValueError("Invalid token") from e
