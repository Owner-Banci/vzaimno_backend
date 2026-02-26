# app/security.py

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from dotenv import load_dotenv
from jose import jwt, JWTError
from passlib.context import CryptContext

load_dotenv()

# ---- Password hashing (bcrypt) ----

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd.verify(password, password_hash)


# ---- JWT ----

JWT_SECRET = os.getenv("JWT_SECRET", "DEV_JWT_SECRET_CHANGE_ME")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))  # 7 days by default


def create_access_token(payload: Dict[str, Any], expires_minutes: int | None = None) -> str:
    data = dict(payload)

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
