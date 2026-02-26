# /Users/maftunamurtazaeva/Desktop/vzaimno_backend/app/main.py
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import execute, fetch_all, fetch_one
from app.geocoding import geocode_address
from app.schemas import (
    AnnouncementOut,
    CreateAnnouncementIn,
    LoginIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from app.security import create_access_token, decode_token, hash_password, verify_password

app = FastAPI(title="Slayma Backend (MVP)")
bearer = HTTPBearer(auto_error=True)


# ----------------------------
# DB bootstrap (MVP)
# ----------------------------
@app.on_event("startup")
def ensure_tables() -> None:
    # users (нужно, потому что /auth/register и /auth/login ссылаются на users)
    execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # announcements
    execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        );
        """
    )

    execute("CREATE INDEX IF NOT EXISTS idx_announcements_user_id ON announcements(user_id);")
    execute("CREATE INDEX IF NOT EXISTS idx_announcements_created_at ON announcements(created_at DESC);")
    execute("CREATE INDEX IF NOT EXISTS idx_announcements_status ON announcements(status);")


# ----------------------------
# Auth
# ----------------------------
def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> UserOut:
    token = creds.credentials

    # DEV режим из iOS (AppConfig.authEnabled == false)
    # if token == "DEV_TOKEN":
    #     return UserOut(id="dev", email="dev@local", role="user")

    if token == "DEV_TOKEN":
        return UserOut(id="dev", email="dev@localdomain.com", role="user")

    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    row = fetch_one("SELECT id::text, email, role FROM users WHERE id = %s", (user_id,))
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return UserOut(id=row[0], email=row[1], role=row[2])


@app.post("/auth/register", response_model=TokenOut)
def register(data: RegisterIn) -> TokenOut:
    existing = fetch_one("SELECT 1 FROM users WHERE email=%s", (data.email,))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user_id = str(uuid.uuid4())
    pwd_hash = hash_password(data.password)

    execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (%s,%s,%s,%s)",
        (user_id, data.email, pwd_hash, "user"),
    )

    token = create_access_token({"sub": user_id, "role": "user"})
    return TokenOut(access_token=token)


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn) -> TokenOut:
    row = fetch_one(
        "SELECT id::text, password_hash, role FROM users WHERE email=%s",
        (data.email,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id, pwd_hash, role = row
    if not verify_password(data.password, pwd_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user_id, "role": role})
    return TokenOut(access_token=token)


@app.get("/me", response_model=UserOut)
def me(user: UserOut = Depends(get_current_user)) -> UserOut:
    return user


# ----------------------------
# Announcements (Ads)
# ----------------------------
def _row_to_announcement(row) -> AnnouncementOut:
    raw_data = row[5]
    if raw_data is None:
        data_obj: Dict[str, Any] = {}
    elif isinstance(raw_data, str):
        try:
            data_obj = json.loads(raw_data)
        except Exception:
            data_obj = {}
    else:
        data_obj = raw_data

    return AnnouncementOut(
        id=row[0],
        user_id=row[1],
        category=row[2],
        title=row[3],
        status=row[4],
        data=data_obj,
        created_at=row[6],
    )


def _extract_primary_address(category: str, data: Dict[str, Any]) -> Optional[str]:
    """
    MVP-правило: точку строим по одному адресу.
    - delivery: pickup_address
    - help: address
    """
    cat = (category or "").strip().lower()
    if cat == "delivery":
        return _normalize_address(data.get("pickup_address"))
    if cat == "help":
        return _normalize_address(data.get("address"))

    # запасной вариант
    return _normalize_address(data.get("address"))


def _normalize_address(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _parse_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    return None


def _extract_point(value: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(value, dict):
        return None

    lat = _parse_float(value.get("lat"))
    lon = _parse_float(value.get("lon"))
    if lat is None or lon is None:
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    return lat, lon


def _point_obj(point: Tuple[float, float]) -> Dict[str, float]:
    return {"lat": point[0], "lon": point[1]}


def _resolve_point(
    data: Dict[str, Any],
    preferred_key: str,
    fallback_key: Optional[str],
    address: Optional[str],
) -> Optional[Tuple[float, float]]:
    # 1) точка уже пришла от клиента в нужном поле
    point = _extract_point(data.get(preferred_key))
    if point:
        return point

    # 2) fallback (например старое поле point)
    if fallback_key:
        point = _extract_point(data.get(fallback_key))
        if point:
            return point

    # 3) геокодим по адресу
    if address:
        return geocode_address(address)

    return None


@app.post("/announcements", response_model=AnnouncementOut, status_code=201)
def create_announcement(
    payload: CreateAnnouncementIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann_id = str(uuid.uuid4())

    # Берём data как dict и дополняем точками, сохраняя обратную совместимость
    data: Dict[str, Any] = dict(payload.data or {})
    category = (payload.category or "").strip().lower()

    if category == "delivery":
        pickup_address = _normalize_address(data.get("pickup_address"))
        dropoff_address = _normalize_address(data.get("dropoff_address"))

        if pickup_address:
            data["pickup_address"] = pickup_address
        if dropoff_address:
            data["dropoff_address"] = dropoff_address

        pickup_point = _resolve_point(
            data=data,
            preferred_key="pickup_point",
            fallback_key="point",
            address=pickup_address,
        )
        dropoff_point = _resolve_point(
            data=data,
            preferred_key="dropoff_point",
            fallback_key=None,
            address=dropoff_address,
        )

        if pickup_point:
            # Базовая точка для старых клиентов = pickup
            data["pickup_point"] = _point_obj(pickup_point)
            data["point"] = _point_obj(pickup_point)
            if pickup_address:
                data["address_text"] = pickup_address

        if dropoff_point:
            data["dropoff_point"] = _point_obj(dropoff_point)

    elif category == "help":
        help_address = _normalize_address(data.get("address"))
        if help_address:
            data["address"] = help_address

        help_point = _resolve_point(
            data=data,
            preferred_key="help_point",
            fallback_key="point",
            address=help_address,
        )

        if help_point:
            # Базовая точка для старых клиентов = help
            data["help_point"] = _point_obj(help_point)
            data["point"] = _point_obj(help_point)
            if help_address:
                data["address_text"] = help_address

    else:
        # Для остальных категорий сохраняем старую логику по одному адресу
        addr = _extract_primary_address(payload.category, data)
        point = _resolve_point(data=data, preferred_key="point", fallback_key=None, address=addr)
        if point:
            data["point"] = _point_obj(point)
            if addr:
                data["address_text"] = addr

    execute(
        """
        INSERT INTO announcements (id, user_id, category, title, status, data)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (ann_id, user.id, payload.category, payload.title, payload.status, json.dumps(data, ensure_ascii=False)),
    )

    row = fetch_one(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE id = %s
        """,
        (ann_id,),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create announcement")

    return _row_to_announcement(row)


@app.get("/announcements/me", response_model=List[AnnouncementOut])
def my_announcements(user: UserOut = Depends(get_current_user)) -> List[AnnouncementOut]:
    rows = fetch_all(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE user_id = %s AND deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (user.id,),
    )
    return [_row_to_announcement(r) for r in rows]


# Публичная лента для карты (другие пользователи видят точки)
@app.get("/announcements/public", response_model=List[AnnouncementOut])
def public_announcements(limit: int = 200) -> List[AnnouncementOut]:
    lim = max(1, min(int(limit), 500))
    rows = fetch_all(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE deleted_at IS NULL AND status = 'active'
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (lim,),
    )
    return [_row_to_announcement(r) for r in rows]
