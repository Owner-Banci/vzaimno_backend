from __future__ import annotations

import json
import os
import uuid
import itsdangerous
import anyio
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth_context import UserPrincipal, get_current_user, get_websocket_user
from app.bootstrap import ensure_all_tables
from app.chat import (
    assert_thread_access,
    broadcast_chat_event,
    broadcast_chat_message,
    broadcast_thread_preview_to_user,
    connect_user_chat_socket,
    connect_chat_socket,
    disconnect_user_chat_socket,
    disconnect_chat_socket,
    get_or_create_offer_thread,
    list_thread_messages,
    list_user_threads,
    post_system_thread_message,
    post_thread_message,
)
from app.db import execute, fetch_all, fetch_one
from app.geocoding import geocode_address
from app.moderation_image import get_nsfw_detector
from app.moderation_text import classify_text
from app.ops import create_notification, create_report, ensure_appeal_report, report_status_select_sql
from app.routes_module.router import router as routes_router
from app.schema_compat import table_has_column
from app.schemas import (
    AcceptOfferOut,
    AnnouncementOut,
    AppealIn,
    ChatMessageIn,
    ChatMessageOut,
    ChatThreadOut,
    CreateAnnouncementIn,
    CreateOfferIn,
    DeviceRegisterIn,
    DeviceUnregisterIn,
    ExecutionStageUpdateIn,
    GeoPointOut,
    LoginIn,
    MeProfileOut,
    OKOut,
    RegisterIn,
    ReportCreateIn,
    ReportOut,
    ReportReasonOptionOut,
    SupportMessageIn,
    SupportMessageOut,
    SupportThreadOut,
    TokenOut,
    UpdateMyProfileIn,
    OfferOut,
    OfferOutExpanded,
    UserProfileOut,
    UserReviewListOut,
    UserReviewOut,
    UserStatsOut,
    UserOut,
)
from app.security import create_user_access_token, hash_password, verify_password
from app.support import get_or_create_support_thread, list_support_messages, post_support_message
from app.task_compat import (
    announcement_status_to_task_fields,
    builder_category_slug,
    canonical_execution_status,
    canonical_offer_status_to_legacy,
    derive_budget_bounds,
    derive_quick_offer_price,
    derive_reward_amount,
    destination_point,
    ensure_task_payload,
    is_uuid_like,
    legacy_offer_status_to_canonical,
    primary_destination_address,
    primary_map_point,
    primary_source_address,
    route_points_from_payload,
    route_visibility_for_execution,
    task_offer_row_to_legacy_dict,
    task_row_to_announcement_dict,
    task_to_announcement_status,
)

app = FastAPI(title="Slayma Backend (MVP)")
app.include_router(routes_router)

# ----------------------------
# Statuses (string enum in DB)
# ----------------------------
STATUS_PENDING = "pending_review"
STATUS_NEEDS_FIX = "needs_fix"
STATUS_REJECTED = "rejected"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

REVIEW_ROLE_ALL = "all"
REVIEW_ROLE_CUSTOMER = "customer"
REVIEW_ROLE_PERFORMER = "performer"
VALID_REVIEW_ROLES = {REVIEW_ROLE_ALL, REVIEW_ROLE_CUSTOMER, REVIEW_ROLE_PERFORMER}
REPORT_REASON_OPTIONS: List[Dict[str, Any]] = [
    {
        "code": "abuse_insults",
        "title": "Оскорбления и абьюз",
        "description": "Грубость, унижения, токсичное общение.",
        "allowed_target_types": ["message", "user"],
    },
    {
        "code": "spam",
        "title": "Спам",
        "description": "Повторяющиеся навязчивые сообщения или нерелевантные предложения.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
    {
        "code": "scam_suspicious_behavior",
        "title": "Подозрение на мошенничество",
        "description": "Подозрительные схемы, выманивание денег или данных.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
    {
        "code": "fake_information",
        "title": "Ложная информация",
        "description": "Неверные сведения о задаче, пользователе или сообщении.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
    {
        "code": "harassment_threats",
        "title": "Угрозы и преследование",
        "description": "Запугивание, угрозы, навязчивое преследование.",
        "allowed_target_types": ["message", "user"],
    },
    {
        "code": "prohibited_content",
        "title": "Запрещённый контент",
        "description": "Нелегальный или запрещённый контент в карточке или сообщении.",
        "allowed_target_types": ["message", "task", "announcement"],
    },
    {
        "code": "off_platform_coercion",
        "title": "Увод с платформы",
        "description": "Принуждение продолжить сделку или общение вне приложения.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
    {
        "code": "unsafe_behavior",
        "title": "Небезопасное поведение",
        "description": "Действия, создающие риск для безопасности участников.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
    {
        "code": "other",
        "title": "Другое",
        "description": "Иная причина, которую пользователь опишет в комментарии.",
        "allowed_target_types": ["message", "user", "task", "announcement"],
    },
]


class ReviewFeedItemOut(BaseModel):
    id: str
    from_user_display_name: str
    stars: int = Field(..., ge=1, le=5)
    text: Optional[str] = None
    created_at: datetime
    target_role: Optional[str] = None


class ReviewFeedSummaryOut(BaseModel):
    average: float = 0.0
    count: int = 0


class ReviewFeedOut(BaseModel):
    items: list[ReviewFeedItemOut] = Field(default_factory=list)
    selected_role: str = REVIEW_ROLE_ALL
    summary: ReviewFeedSummaryOut = Field(default_factory=ReviewFeedSummaryOut)


class ReviewEligibilityOut(BaseModel):
    can_submit: bool = False
    already_submitted: bool = False
    announcement_id: str
    announcement_title: Optional[str] = None
    thread_id: Optional[str] = None
    counterpart_user_id: Optional[str] = None
    counterpart_display_name: Optional[str] = None
    counterpart_role: Optional[str] = None
    message: Optional[str] = None


class SubmitReviewIn(BaseModel):
    stars: int = Field(..., ge=1, le=5)
    text: Optional[str] = Field(default=None, max_length=2000)

# ----------------------------
# Upload config
# ----------------------------
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "uploads"))
NSFW_REVIEW = float(os.getenv("NSFW_REVIEW", "0.30"))
NSFW_HARD_BLOCK = float(os.getenv("NSFW_HARD_BLOCK", "0.85"))

@app.get("/uploads/{ann_id}/{filename}")
def download_announcement_media(
    ann_id: str,
    filename: str,
    user: UserPrincipal = Depends(get_current_user),
) -> FileResponse:
    """Serve an uploaded file only to users who have access to the announcement.

    Replaces the previous ``app.mount("/uploads", StaticFiles(...))`` which
    served any file to anyone who knew the URL. Access reuses
    ``_user_can_fetch_announcement`` (owner / active performer / public-listed
    task). Path traversal is blocked by rejecting separators and '..' in both
    path parts and by resolving the path inside ``UPLOADS_DIR``.
    """
    for part_name, part_value in (("ann_id", ann_id), ("filename", filename)):
        if "/" in part_value or "\\" in part_value or ".." in part_value:
            raise HTTPException(status_code=400, detail=f"Invalid {part_name}")

    uploads_root = UPLOADS_DIR.resolve()
    file_path = (uploads_root / ann_id / filename).resolve()
    try:
        file_path.relative_to(uploads_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ann = _fetch_announcement_or_404(ann_id)
    if not _user_can_fetch_announcement(ann_id, user.id, ann.user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    return FileResponse(str(file_path))


@app.on_event("startup")
def ensure_tables() -> None:
    ensure_all_tables()


# ----------------------------
# Auth
# ----------------------------
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
    _ensure_profile_and_stats(user_id)

    token = create_user_access_token(user_id, role="user")
    return TokenOut(access_token=token)


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn) -> TokenOut:
    row = fetch_one("SELECT id::text, password_hash FROM users WHERE email=%s", (data.email,))
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id, pwd_hash = row
    if not verify_password(data.password, pwd_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_user_access_token(str(user_id), role="user")
    return TokenOut(access_token=token)


@app.get("/me", response_model=UserOut)
def me(user: UserPrincipal = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role)


# ----------------------------
# Profile / devices
# ----------------------------
def _model_dump(model: Any) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return {}


def _normalize_optional_text(value: Optional[str], collapse_spaces: bool = False) -> Optional[str]:
    if value is None:
        return None

    normalized = value.strip()
    if collapse_spaces:
        normalized = " ".join(normalized.split())

    return normalized or None


def _normalize_home_location(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None

    raw_value = value
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except Exception:
            return None

    if not isinstance(raw_value, dict):
        return None

    try:
        lat = float(raw_value.get("lat"))
        lon = float(raw_value.get("lon"))
    except (TypeError, ValueError):
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    return {"lat": lat, "lon": lon}


def _point_out(value: Any) -> Optional[GeoPointOut]:
    point = _normalize_home_location(value)
    if not point:
        return None
    return GeoPointOut(**point)


def _normalize_json_object(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}

    raw_value = value
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except Exception:
            return {}

    if isinstance(raw_value, dict):
        return dict(raw_value)

    return {}


def _preferred_address_from_extra(value: Any) -> Optional[str]:
    extra = _normalize_json_object(value)
    return _normalize_optional_text(extra.get("preferred_address"), collapse_spaces=True)


@lru_cache(maxsize=1)
def _profile_has_extra_column() -> bool:
    return table_has_column("user_profiles", "extra")


@lru_cache(maxsize=1)
def _profile_home_location_udt_name() -> Optional[str]:
    row = fetch_one(
        """
        SELECT udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'user_profiles'
          AND column_name = 'home_location'
        """,
    )
    if not row or not row[0]:
        return None
    return str(row[0]).lower()


def _profile_extra_select_sql(alias: str) -> str:
    if not _profile_has_extra_column():
        return "NULL::jsonb AS profile_extra"

    prefix = f"{alias}." if alias else ""
    return f"{prefix}extra AS profile_extra"


def _profile_home_location_select_sql(alias: str) -> str:
    prefix = f"{alias}." if alias else ""
    column_type = _profile_home_location_udt_name()

    if not column_type:
        return "NULL AS home_lat, NULL AS home_lon"

    if column_type in {"geography", "geometry"}:
        return (
            f"CASE WHEN {prefix}home_location IS NULL THEN NULL ELSE ST_Y({prefix}home_location::geometry) END AS home_lat, "
            f"CASE WHEN {prefix}home_location IS NULL THEN NULL ELSE ST_X({prefix}home_location::geometry) END AS home_lon"
        )

    if column_type in {"jsonb", "json"}:
        return (
            f"CASE WHEN {prefix}home_location IS NULL THEN NULL ELSE NULLIF({prefix}home_location->>'lat', '')::double precision END AS home_lat, "
            f"CASE WHEN {prefix}home_location IS NULL THEN NULL ELSE NULLIF({prefix}home_location->>'lon', '')::double precision END AS home_lon"
        )

    return "NULL AS home_lat, NULL AS home_lon"


def _fetch_profile_extra(user_id: str) -> Dict[str, Any]:
    if not _profile_has_extra_column():
        return {}

    row = fetch_one(
        """
        SELECT extra
        FROM user_profiles
        WHERE user_id = %s
        """,
        (user_id,),
    )
    if not row:
        return {}

    return _normalize_json_object(row[0])


def _ensure_profile_and_stats(user_id: str) -> None:
    if user_id == "dev":
        return

    execute(
        """
        INSERT INTO user_profiles (user_id, display_name, created_at, updated_at)
        SELECT
            u.id,
            COALESCE(NULLIF(BTRIM(u.phone), ''), NULLIF(BTRIM(u.email), ''), 'Пользователь'),
            now(),
            now()
        FROM users u
        WHERE u.id = %s
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )
    execute(
        """
        INSERT INTO user_stats (user_id, created_at, updated_at)
        VALUES (%s, now(), now())
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )


def _dev_me_profile() -> MeProfileOut:
    return MeProfileOut(
        user={
            "id": "dev",
            "email": "dev@localdomain.com",
            "phone": None,
            "created_at": datetime.now(timezone.utc),
        },
        profile={
            "display_name": "Dev User",
            "bio": None,
            "city": None,
            "preferred_address": None,
            "home_location": None,
        },
        stats={
            "rating_avg": 0.0,
            "rating_count": 0,
            "completed_count": 0,
            "cancelled_count": 0,
        },
    )


def _fetch_me_profile(user_id: str) -> MeProfileOut:
    if user_id == "dev":
        return _dev_me_profile()

    _ensure_profile_and_stats(user_id)

    row = fetch_one(
        f"""
        SELECT
            u.id::text,
            u.email,
            u.phone,
            u.created_at,
            up.display_name,
            up.bio,
            up.city,
            {_profile_extra_select_sql("up")},
            {_profile_home_location_select_sql("up")},
            COALESCE(us.rating_avg, 0),
            COALESCE(us.rating_count, 0),
            COALESCE(us.completed_count, 0),
            COALESCE(us.cancelled_count, 0)
        FROM users u
        LEFT JOIN user_profiles up ON up.user_id = u.id
        LEFT JOIN user_stats us ON us.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return MeProfileOut(
        user={
            "id": row[0],
            "email": row[1],
            "phone": row[2],
            "created_at": row[3],
        },
        profile={
            "display_name": _normalize_optional_text(row[4], collapse_spaces=True),
            "bio": _normalize_optional_text(row[5]),
            "city": _normalize_optional_text(row[6], collapse_spaces=True),
            "preferred_address": _preferred_address_from_extra(row[7]),
            "home_location": _point_out({"lat": row[8], "lon": row[9]}) if row[8] is not None and row[9] is not None else None,
        },
        stats=UserStatsOut(
            rating_avg=float(row[10] or 0),
            rating_count=int(row[11] or 0),
            completed_count=int(row[12] or 0),
            cancelled_count=int(row[13] or 0),
        ),
    )


def _fetch_profile_section(user_id: str) -> UserProfileOut:
    if user_id == "dev":
        return UserProfileOut(display_name="Dev User", preferred_address=None)

    _ensure_profile_and_stats(user_id)

    row = fetch_one(
        f"""
        SELECT
            display_name,
            bio,
            city,
            {_profile_extra_select_sql("")},
            {_profile_home_location_select_sql("")}
        FROM user_profiles
        WHERE user_id = %s
        """,
        (user_id,),
    )

    if not row:
        return UserProfileOut()

    return UserProfileOut(
        display_name=_normalize_optional_text(row[0], collapse_spaces=True),
        bio=_normalize_optional_text(row[1]),
        city=_normalize_optional_text(row[2], collapse_spaces=True),
        preferred_address=_preferred_address_from_extra(row[3]),
        home_location=_point_out({"lat": row[4], "lon": row[5]}) if row[4] is not None and row[5] is not None else None,
    )


@app.get("/users/me", response_model=MeProfileOut)
def users_me(user: UserOut = Depends(get_current_user)) -> MeProfileOut:
    return _fetch_me_profile(user.id)


@app.patch("/users/me/profile", response_model=UserProfileOut)
def update_my_profile(
    payload: UpdateMyProfileIn,
    user: UserOut = Depends(get_current_user),
) -> UserProfileOut:
    display_name = _normalize_optional_text(payload.display_name, collapse_spaces=True)
    bio = _normalize_optional_text(payload.bio)
    city = _normalize_optional_text(payload.city, collapse_spaces=True)
    preferred_address = _normalize_optional_text(payload.preferred_address, collapse_spaces=True)
    if not display_name or len(display_name) < 2:
        raise HTTPException(status_code=422, detail="Display name must contain at least 2 characters")

    if user.id == "dev":
        return UserProfileOut(
            display_name=display_name,
            bio=bio,
            city=city,
            preferred_address=preferred_address,
            home_location=payload.home_location,
        )

    _ensure_profile_and_stats(user.id)

    current_profile = _fetch_profile_section(user.id)

    resolved_home_location = payload.home_location
    if resolved_home_location is None:
        if preferred_address:
            geocoded = geocode_address(preferred_address)
            if geocoded:
                resolved_home_location = GeoPointOut(lat=geocoded[0], lon=geocoded[1])
            else:
                resolved_home_location = current_profile.home_location
        else:
            resolved_home_location = None

    set_clauses = [
        "display_name = %s",
        "bio = %s",
        "city = %s",
    ]
    params: List[Any] = [display_name, bio, city]

    if _profile_has_extra_column():
        extra_payload = _fetch_profile_extra(user.id)
        if preferred_address:
            extra_payload["preferred_address"] = preferred_address
        else:
            extra_payload.pop("preferred_address", None)

        set_clauses.append("extra = %s::jsonb")
        params.append(json.dumps(extra_payload or {}, ensure_ascii=False))

    home_location_type = _profile_home_location_udt_name()
    if home_location_type in {"geography", "geometry"}:
        if resolved_home_location is not None:
            geometry_expression = "ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326)"
            if home_location_type == "geography":
                geometry_expression = f"{geometry_expression}::geography"

            set_clauses.append(f"home_location = {geometry_expression}")
            params.extend([resolved_home_location.lon, resolved_home_location.lat])
        else:
            set_clauses.append("home_location = NULL")
    elif home_location_type in {"jsonb", "json"}:
        if resolved_home_location is not None:
            set_clauses.append("home_location = %s::jsonb")
            params.append(
                json.dumps(
                    {"lat": resolved_home_location.lat, "lon": resolved_home_location.lon},
                    ensure_ascii=False,
                )
            )
        else:
            set_clauses.append("home_location = NULL")

    set_clauses.append("updated_at = now()")
    params.append(user.id)

    execute(
        f"""
        UPDATE user_profiles
        SET {", ".join(set_clauses)}
        WHERE user_id = %s
        """,
        tuple(params),
    )

    return _fetch_profile_section(user.id)


def _normalize_review_role(role: Optional[str]) -> str:
    normalized = (role or "").strip().lower()
    if normalized in VALID_REVIEW_ROLES:
        return normalized
    return REVIEW_ROLE_ALL


def _review_feed_summary(user_id: str, role: str) -> ReviewFeedSummaryOut:
    params: List[Any] = [user_id]
    sql = """
        SELECT COALESCE(AVG(r.stars), 0), COUNT(*)
        FROM reviews r
        WHERE r.to_user_id = %s
    """
    if role != REVIEW_ROLE_ALL:
        sql += " AND COALESCE(r.target_role, '') = %s"
        params.append(role)

    row = fetch_one(sql, tuple(params))
    return ReviewFeedSummaryOut(
        average=round(float(row[0] or 0), 1) if row else 0.0,
        count=int(row[1] or 0) if row else 0,
    )


def _review_context_for_user(task_id: str, user_id: str) -> Dict[str, Any]:
    row = fetch_one(
        """
        SELECT
            ta.task_id::text,
            ta.assignment_status,
            ta.execution_stage,
            ta.chat_thread_id::text,
            ta.customer_id::text,
            ta.performer_id::text,
            COALESCE(NULLIF(BTRIM(cup.display_name), ''), NULLIF(BTRIM(cu.phone), ''), NULLIF(BTRIM(cu.email), ''), 'Заказчик') AS customer_name,
            COALESCE(NULLIF(BTRIM(pup.display_name), ''), NULLIF(BTRIM(pu.phone), ''), NULLIF(BTRIM(pu.email), ''), 'Исполнитель') AS performer_name,
            a.title
        FROM task_assignments ta
        LEFT JOIN announcements a ON a.id::text = ta.task_id::text
        LEFT JOIN user_profiles cup ON cup.user_id = ta.customer_id
        LEFT JOIN users cu ON cu.id = ta.customer_id
        LEFT JOIN user_profiles pup ON pup.user_id = ta.performer_id
        LEFT JOIN users pu ON pu.id = ta.performer_id
        WHERE ta.task_id::text = %s
        ORDER BY
            CASE ta.assignment_status
                WHEN 'completed' THEN 0
                WHEN 'in_progress' THEN 1
                WHEN 'assigned' THEN 2
                ELSE 3
            END,
            ta.updated_at DESC,
            ta.created_at DESC
        LIMIT 1
        """,
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Контекст отзыва для задания не найден")

    assignment_status = str(row[1] or "")
    execution_stage = str(row[2] or "")
    thread_id = str(row[3]) if row[3] else None
    customer_id = str(row[4] or "")
    performer_id = str(row[5] or "")
    customer_name = str(row[6] or "Заказчик")
    performer_name = str(row[7] or "Исполнитель")
    announcement_title = _normalize_optional_text(row[8], collapse_spaces=True)

    if user_id == customer_id:
        counterpart_user_id = performer_id
        counterpart_display_name = performer_name
        counterpart_role = REVIEW_ROLE_PERFORMER
        author_role = REVIEW_ROLE_CUSTOMER
    elif user_id == performer_id:
        counterpart_user_id = customer_id
        counterpart_display_name = customer_name
        counterpart_role = REVIEW_ROLE_CUSTOMER
        author_role = REVIEW_ROLE_PERFORMER
    else:
        raise HTTPException(status_code=403, detail="Недостаточно прав для работы с отзывом")

    already_submitted = bool(
        fetch_one(
            """
            SELECT 1
            FROM reviews
            WHERE task_id = %s
              AND from_user_id = %s
            LIMIT 1
            """,
            (task_id, user_id),
        )
    )
    is_completed = assignment_status == "completed" or execution_stage == "completed"
    can_submit = is_completed and not already_submitted and bool(counterpart_user_id)

    message = None
    if already_submitted:
        message = "Ваш отзыв уже сохранён."
    elif not is_completed:
        message = "Оставить отзыв можно после завершения задания."

    return {
        "announcement_id": task_id,
        "announcement_title": announcement_title,
        "thread_id": thread_id,
        "counterpart_user_id": counterpart_user_id,
        "counterpart_display_name": counterpart_display_name,
        "counterpart_role": counterpart_role,
        "author_role": author_role,
        "already_submitted": already_submitted,
        "can_submit": can_submit,
        "message": message,
    }


@app.get("/users/me/reviews", response_model=ReviewFeedOut)
def my_reviews(
    limit: int = 2,
    offset: int = 0,
    role: str = REVIEW_ROLE_ALL,
    user: UserOut = Depends(get_current_user),
) -> ReviewFeedOut:
    selected_role = _normalize_review_role(role)
    if user.id == "dev":
        return ReviewFeedOut(items=[], selected_role=selected_role, summary=ReviewFeedSummaryOut())

    lim = max(1, min(int(limit), 100))
    off = max(0, int(offset))
    summary = _review_feed_summary(user.id, selected_role)

    params: List[Any] = [user.id]
    filter_sql = ""
    if selected_role != REVIEW_ROLE_ALL:
        filter_sql = " AND COALESCE(r.target_role, '') = %s"
        params.append(selected_role)
    params.extend([lim, off])

    rows = fetch_all(
        f"""
        SELECT
            COALESCE(r.id::text, r.from_user_id::text || '|' || COALESCE(r.created_at::text, '') || '|' || COALESCE(r.text, '')),
            COALESCE(NULLIF(up.display_name, ''), u.email, 'Пользователь') AS from_user_display_name,
            r.stars,
            r.text,
            r.created_at,
            r.target_role
        FROM reviews r
        LEFT JOIN user_profiles up ON up.user_id = r.from_user_id
        LEFT JOIN users u ON u.id = r.from_user_id
        WHERE r.to_user_id = %s
        {filter_sql}
        ORDER BY r.created_at DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params),
    )

    return ReviewFeedOut(
        items=[
            ReviewFeedItemOut(
                id=str(row[0]),
                from_user_display_name=row[1],
                stars=int(row[2]),
                text=row[3],
                created_at=row[4],
                target_role=row[5],
            )
            for row in rows
        ],
        selected_role=selected_role,
        summary=summary,
    )


@app.get("/announcements/{ann_id}/review-context", response_model=ReviewEligibilityOut)
def announcement_review_context(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> ReviewEligibilityOut:
    if user.id == "dev":
        return ReviewEligibilityOut(announcement_id=ann_id, message="В dev-режиме отзывы отключены.")

    return ReviewEligibilityOut(**_review_context_for_user(ann_id, user.id))


@app.post("/announcements/{ann_id}/review", response_model=OKOut)
def submit_announcement_review(
    ann_id: str,
    payload: SubmitReviewIn,
    user: UserOut = Depends(get_current_user),
) -> OKOut:
    if user.id == "dev":
        return OKOut(ok=True)

    context = _review_context_for_user(ann_id, user.id)
    if context["already_submitted"]:
        raise HTTPException(status_code=409, detail="Отзыв уже отправлен")
    if not context["can_submit"]:
        raise HTTPException(status_code=409, detail=context["message"] or "Оставить отзыв пока нельзя")

    clean_text = _normalize_optional_text(payload.text)
    review_id = str(uuid.uuid4())
    inserted = fetch_one(
        """
        INSERT INTO reviews (id, task_id, from_user_id, to_user_id, stars, text, author_role, target_role, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (task_id, from_user_id) DO NOTHING
        RETURNING id
        """,
        (
            review_id,
            ann_id,
            user.id,
            context["counterpart_user_id"],
            int(payload.stars),
            clean_text,
            context["author_role"],
            context["counterpart_role"],
        ),
    )
    if not inserted:
        raise HTTPException(status_code=409, detail="Отзыв уже отправлен")

    counterpart_user_id = str(context["counterpart_user_id"])
    _ensure_profile_and_stats(counterpart_user_id)
    overall_summary = _review_feed_summary(counterpart_user_id, REVIEW_ROLE_ALL)
    execute(
        """
        UPDATE user_stats
        SET rating_avg = %s,
            rating_count = %s,
            updated_at = now()
        WHERE user_id = %s
        """,
        (overall_summary.average, overall_summary.count, counterpart_user_id),
    )
    create_notification(
        user_id=counterpart_user_id,
        notif_type="review_received",
        body="Вам оставили отзыв по завершённому заданию.",
        payload={"announcement_id": ann_id, "from_user_id": user.id},
    )
    return OKOut(ok=True)


@app.post("/devices/register", response_model=OKOut)
def register_device(
    payload: DeviceRegisterIn,
    user: UserOut = Depends(get_current_user),
) -> OKOut:
    if user.id == "dev":
        return OKOut(ok=True)

    device_id = _normalize_optional_text(payload.device_id)
    if not device_id:
        raise HTTPException(status_code=422, detail="device_id is required")

    existing = fetch_one(
        """
        SELECT id
        FROM user_devices
        WHERE device_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (device_id,),
    )

    if existing:
        execute(
            """
            UPDATE user_devices
            SET user_id = %s,
                platform = %s,
                push_token = %s,
                locale = %s,
                timezone = %s,
                device_name = %s,
                last_seen_at = now(),
                deleted_at = NULL
            WHERE id = %s
            """,
            (
                user.id,
                payload.platform,
                payload.push_token,
                payload.locale,
                payload.timezone,
                payload.device_name,
                existing[0],
            ),
        )
    else:
        execute(
            """
            INSERT INTO user_devices (
                id, user_id, platform, device_id, push_token, locale, timezone, device_name, created_at, last_seen_at, deleted_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), NULL)
            """,
            (
                str(uuid.uuid4()),
                user.id,
                payload.platform,
                device_id,
                payload.push_token,
                payload.locale,
                payload.timezone,
                payload.device_name,
            ),
        )
    return OKOut(ok=True)


@app.delete("/devices/me", response_model=OKOut)
def unregister_device(
    payload: DeviceUnregisterIn,
    user: UserOut = Depends(get_current_user),
) -> OKOut:
    if user.id == "dev":
        return OKOut(ok=True)

    device_id = _normalize_optional_text(payload.device_id)
    if not device_id:
        raise HTTPException(status_code=422, detail="device_id is required")

    execute(
        """
        UPDATE user_devices
        SET deleted_at = now(),
            last_seen_at = now(),
            push_token = NULL
        WHERE user_id = %s
          AND deleted_at IS NULL
          AND (
              device_id = %s
              OR (%s IS NOT NULL AND push_token = %s)
          )
        """,
        (user.id, device_id, payload.push_token, payload.push_token),
    )
    return OKOut(ok=True)


# ----------------------------
# Helpers (announcements / moderation)
# ----------------------------
TASK_ANNOUNCEMENT_SELECT = """
    SELECT
        t.id::text,
        t.customer_id::text,
        COALESCE(c.slug, COALESCE(t.extra->>'category', t.extra->>'main_group', 'help')) AS category_slug,
        t.title,
        t.description,
        t.extra,
        t.created_at,
        t.status::text,
        t.moderation_status::text,
        t.deleted_at,
        t.responses_count,
        t.address_text,
        CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_Y(t.location_point::geometry) END AS location_lat,
        CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_X(t.location_point::geometry) END AS location_lon,
        ta.id::text AS assignment_id,
        ta.assignment_status,
        ta.execution_stage,
        ta.performer_id::text AS assignment_performer_id,
        ta.chat_thread_id::text AS assignment_chat_thread_id,
        ta.route_visibility
    FROM tasks t
    LEFT JOIN categories c
      ON c.id = t.category_id
    LEFT JOIN task_assignments ta
      ON ta.task_id = t.id
     AND ta.assignment_status IN ('assigned', 'in_progress')
"""


def _default_category_slug(extra: Dict[str, Any]) -> str:
    return builder_category_slug(
        _normalize_optional_text(extra.get("category"))
        or _normalize_optional_text(extra.get("main_group"))
        or "help"
    )


def _task_row_to_announcement(row) -> AnnouncementOut:
    extra = _normalize_json_object(row[5])
    payload = {
        "id": row[0],
        "customer_id": row[1],
        "category_slug": row[2] or _default_category_slug(extra),
        "title": row[3],
        "description": row[4],
        "extra": extra,
        "created_at": row[6],
        "task_status": row[7],
        "moderation_status": row[8],
        "deleted_at": row[9],
        "responses_count": row[10],
        "address_text": row[11],
        "location_lat": row[12],
        "location_lon": row[13],
        "assignment_id": row[14],
        "assignment_status": row[15],
        "execution_stage": row[16],
        "assignment_performer_id": row[17],
        "assignment_chat_thread_id": row[18],
        "route_visibility": row[19],
    }
    return AnnouncementOut(**task_row_to_announcement_dict(payload))


def _fetch_task_row(task_id: str):
    return fetch_one(
        f"""
        {TASK_ANNOUNCEMENT_SELECT}
        WHERE t.id::text = %s
        """,
        (task_id,),
    )


def _repair_announcement_row_if_needed(row):
    if not row:
        return row
    if row[12] is not None and row[13] is not None:
        return row

    extra = _normalize_json_object(row[5])
    category = (
        _normalize_optional_text(extra.get("category"), collapse_spaces=True)
        or row[2]
        or _default_category_slug(extra)
    )
    announcement_status = task_to_announcement_status(
        row[7],
        row[8],
        row[9],
        assignment_status=row[15],
        execution_stage=row[16],
    )
    data = ensure_task_payload(
        extra,
        title=row[3] or "",
        announcement_status=announcement_status,
        deleted_at=row[9],
        assignment={
            "id": row[14],
            "assignment_status": row[15],
            "execution_stage": row[16],
            "performer_id": row[17],
            "chat_thread_id": row[18],
            "route_visibility": row[19],
        },
    )
    if not _persist_repaired_task_point(str(row[0]), category, data):
        return row

    return _fetch_task_row(str(row[0])) or row


def _row_to_report(row) -> ReportOut:
    return ReportOut(
        id=str(row[0]) if row[0] is not None else "",
        reporter_id=str(row[1]) if row[1] is not None else "",
        target_type=str(row[2]) if row[2] is not None else "",
        target_id=str(row[3]) if row[3] is not None else "",
        reason_code=str(row[4]) if row[4] is not None else "",
        reason_text=row[5],
        status=str(row[6]) if row[6] is not None else "open",
        resolution=str(row[7]) if row[7] is not None else None,
        resolved_by=str(row[8]) if row[8] is not None else None,
        moderator_comment=row[9],
        created_at=row[10],
        resolved_at=row[11],
    )


def _prepare_report_target(target_type: str, target_id: str, reporter_id: str) -> tuple[str, str, Dict[str, Any]]:
    normalized_type = (target_type or "").strip().lower()
    clean_target_id = str(target_id or "").strip()
    if normalized_type not in {"announcement", "message", "user", "task"}:
        raise HTTPException(status_code=400, detail="Unsupported target type")
    if not is_uuid_like(clean_target_id):
        raise HTTPException(status_code=400, detail="Invalid target id")

    meta: Dict[str, Any] = {}

    if normalized_type in {"announcement", "task"}:
        task_row = fetch_one(
            """
            SELECT t.id::text,
                   t.customer_id::text,
                   t.title
            FROM tasks t
            WHERE t.id::text = %s
            LIMIT 1
            """,
            (clean_target_id,),
        )
        legacy_row = fetch_one(
            """
            SELECT a.id::text
            FROM announcements a
            WHERE a.id::text = %s
              AND a.deleted_at IS NULL
            LIMIT 1
            """,
            (clean_target_id,),
        )
        if not task_row and not legacy_row:
            raise HTTPException(status_code=404, detail="Report target not found")
        if task_row:
            meta["source_task_id"] = str(task_row[0])
            meta["target_user_id"] = str(task_row[1])
            meta["task_title"] = str(task_row[2] or "")
        return normalized_type, clean_target_id, meta

    if normalized_type == "user":
        user_row = fetch_one(
            """
            SELECT id::text
            FROM users
            WHERE id::text = %s
            LIMIT 1
            """,
            (clean_target_id,),
        )
        if not user_row:
            raise HTTPException(status_code=404, detail="Report target not found")
        meta["target_user_id"] = str(user_row[0])
        return normalized_type, clean_target_id, meta

    message_row = fetch_one(
        """
        SELECT m.id::text,
               ct.id::text AS thread_id,
               COALESCE(ct.task_id::text, ta.task_id::text, tf.task_id::text) AS task_id,
               m.sender_id::text
        FROM chat_messages m
        JOIN chat_threads ct
          ON ct.id = m.thread_id
        LEFT JOIN task_assignments ta
          ON ta.id = ct.assignment_id
        LEFT JOIN task_offers tf
          ON tf.id = COALESCE(ct.offer_id, ta.offer_id)
        JOIN chat_participants cp
          ON cp.thread_id = ct.id
         AND cp.user_id::text = %s
         AND cp.left_at IS NULL
        WHERE m.id::text = %s
          AND m.deleted_at IS NULL
        LIMIT 1
        """,
        (reporter_id, clean_target_id),
    )
    if not message_row:
        raise HTTPException(status_code=404, detail="Report target not found")
    meta["source_message_id"] = str(message_row[0])
    meta["source_thread_id"] = str(message_row[1])
    if message_row[2]:
        meta["source_task_id"] = str(message_row[2])
    meta["target_user_id"] = str(message_row[3])
    return normalized_type, clean_target_id, meta


def _ensure_obj(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _ensure_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _get_mod(data: Dict[str, Any]) -> Dict[str, Any]:
    return _ensure_obj(data.get("moderation"))


def _set_mod(data: Dict[str, Any], mod: Dict[str, Any]) -> None:
    data["moderation"] = mod


def _set_decision(mod: Dict[str, Any], status: str, message: str) -> None:
    mod["decision"] = {"status": status, "message": message}


def _remove_reasons_for_field(mod: Dict[str, Any], field: str) -> None:
    reasons = _ensure_list(mod.get("reasons"))
    mod["reasons"] = [r for r in reasons if not (isinstance(r, dict) and r.get("field") == field)]


def _add_reason(mod: Dict[str, Any], field: str, code: str, details: str, can_appeal: bool) -> None:
    reasons = _ensure_list(mod.get("reasons"))
    reasons.append(
        {
            "field": field,
            "code": code,
            "details": details,
            "can_appeal": bool(can_appeal),
        }
    )
    mod["reasons"] = reasons


def _set_suggestions(mod: Dict[str, Any], suggestions: List[str]) -> None:
    mod["suggestions"] = [s for s in suggestions if isinstance(s, str) and s.strip()]


def _is_temporary_text_moderation_issue(reason: str) -> bool:
    normalized = (reason or "").strip().lower()
    if not normalized:
        return False

    technical_markers = (
        "ollama error",
        "timed out",
        "connection refused",
        "connection reset",
        "remote end closed connection",
        "non-json",
        "не-json",
    )
    return any(marker in normalized for marker in technical_markers)


def _status_priority(s: str) -> int:
    return {
        STATUS_ACTIVE: 0,
        STATUS_PENDING: 1,
        STATUS_NEEDS_FIX: 2,
        STATUS_REJECTED: 3,
        STATUS_ARCHIVED: 4,
        "draft": 1,
    }.get(s or "", 1)


def _keep_stricter(current: str, candidate: str) -> str:
    return current if _status_priority(current) >= _status_priority(candidate) else candidate


def _has_media_attachments(data: Dict[str, Any]) -> bool:
    for key in ("media", "images", "photos", "media_local_identifiers"):
        raw = data.get(key)

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    return True
                if isinstance(item, dict) and item:
                    return True
            continue

        if isinstance(raw, str) and raw.strip():
            return True

        if isinstance(raw, dict) and raw:
            return True

    return False


def _normalize_address(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _normalize_message(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _user_timezone_name(user_id: str, data: Dict[str, Any]) -> Optional[str]:
    for key in ("timezone", "schedule_timezone", "time_zone"):
        raw = _normalize_optional_text(data.get(key))
        if not raw:
            continue
        try:
            ZoneInfo(raw)
            return raw
        except Exception:
            continue

    if not table_has_column("user_devices", "timezone"):
        return None

    row = fetch_one(
        """
        SELECT timezone
        FROM user_devices
        WHERE user_id = %s
          AND deleted_at IS NULL
          AND timezone IS NOT NULL
          AND BTRIM(timezone) <> ''
        ORDER BY last_seen_at DESC, created_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if not row or not row[0]:
        return None
    raw = _normalize_optional_text(row[0])
    if not raw:
        return None
    try:
        ZoneInfo(raw)
    except Exception:
        return None
    return raw


def _normalize_schedule_timestamp(raw_value: Any, timezone_name: Optional[str]) -> Optional[str]:
    value = _normalize_optional_text(raw_value)
    if not value:
        return None
    if "T" not in value and " " not in value:
        return value

    normalized = value.replace("Z", "+00:00")
    try:
        dt_value = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    if dt_value.tzinfo is None:
        if not timezone_name:
            return dt_value.replace(microsecond=0).isoformat()
        try:
            dt_value = dt_value.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            return dt_value.replace(microsecond=0).isoformat()
        return dt_value.replace(microsecond=0).isoformat()

    if not timezone_name:
        return dt_value.replace(microsecond=0).isoformat()

    try:
        return dt_value.astimezone(ZoneInfo(timezone_name)).replace(microsecond=0).isoformat()
    except Exception:
        return dt_value.replace(microsecond=0).isoformat()


def _normalize_schedule_fields_for_user(*, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    timezone_name = _user_timezone_name(user_id, data)
    task = data.get("task") if isinstance(data.get("task"), dict) else None
    route = task.get("route") if isinstance(task, dict) and isinstance(task.get("route"), dict) else None
    if timezone_name:
        data["timezone"] = timezone_name
        data.setdefault("schedule_timezone", timezone_name)
        if route is not None:
            route["timezone"] = timezone_name

    for key in ("start_at", "end_at"):
        source_value = data.get(key)
        if source_value is None and route is not None:
            source_value = route.get(key)
        normalized = _normalize_schedule_timestamp(source_value, timezone_name)
        if normalized:
            data[key] = normalized
            if route is not None:
                route[key] = normalized
    return data


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


def _task_route_node(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    task = data.get("task")
    if not isinstance(task, dict):
        task = {}
        data["task"] = task

    route = task.get("route")
    if not isinstance(route, dict):
        route = {}
        task["route"] = route

    node = route.get(key)
    if not isinstance(node, dict):
        node = {}
        route[key] = node
    return node


def _source_point_from_payload(category: str, data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    normalized_category = (category or "").strip().lower()
    route_source = _task_route_node(data, "source")

    if normalized_category == "delivery":
        return (
            _extract_point(data.get("pickup_point"))
            or _extract_point(data.get("source_point"))
            or _extract_point(data.get("start_point"))
            or _extract_point(route_source.get("point"))
            or _extract_point(data.get("point"))
        )

    if normalized_category == "help":
        return (
            _extract_point(data.get("help_point"))
            or _extract_point(data.get("source_point"))
            or _extract_point(data.get("start_point"))
            or _extract_point(route_source.get("point"))
            or _extract_point(data.get("point"))
        )

    return (
        _extract_point(data.get("point"))
        or _extract_point(data.get("source_point"))
        or _extract_point(data.get("start_point"))
        or _extract_point(route_source.get("point"))
    )


def _destination_point_from_payload(data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    route_destination = _task_route_node(data, "destination")
    return destination_point(data) or _extract_point(route_destination.get("point"))


def _store_source_point(category: str, data: Dict[str, Any], point: Tuple[float, float]) -> None:
    normalized_category = (category or "").strip().lower()
    point_obj = _point_obj(point)

    if normalized_category == "delivery":
        data["pickup_point"] = point_obj
    elif normalized_category == "help":
        data["help_point"] = point_obj
    else:
        data["point"] = point_obj

    data.setdefault("point", point_obj)
    _task_route_node(data, "source")["point"] = point_obj


def _store_destination_point(category: str, data: Dict[str, Any], point: Tuple[float, float]) -> None:
    normalized_category = (category or "").strip().lower()
    point_obj = _point_obj(point)

    if normalized_category == "delivery":
        data["dropoff_point"] = point_obj
    else:
        data["destination_point"] = point_obj

    _task_route_node(data, "destination")["point"] = point_obj


def _ensure_payload_points(category: str, data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    source_point = _source_point_from_payload(category, data)
    if source_point is None:
        source_address = primary_source_address(data)
        if source_address:
            source_point = geocode_address(source_address)
    if source_point is not None:
        _store_source_point(category, data, source_point)

    destination_fallback_point = _destination_point_from_payload(data)
    if destination_fallback_point is None:
        destination_address = primary_destination_address(data)
        if destination_address:
            destination_fallback_point = geocode_address(destination_address)
    if destination_fallback_point is not None:
        _store_destination_point(category, data, destination_fallback_point)

    return source_point or primary_map_point(data) or destination_fallback_point


def _normalize_budget_fields(data: Dict[str, Any]) -> None:
    for key in ("budget", "budget_min", "budget_max"):
        if key not in data:
            continue

        parsed = _parse_float(data.get(key))
        if parsed is None:
            if data.get(key) in ("", None):
                data[key] = None
            continue

        data[key] = int(parsed)


def _extract_primary_address(category: str, data: Dict[str, Any]) -> Optional[str]:
    cat = (category or "").strip().lower()
    if cat == "delivery":
        return _normalize_address(data.get("pickup_address"))
    if cat == "help":
        return _normalize_address(data.get("address"))
    return _normalize_address(data.get("address"))


def _resolve_point(
    data: Dict[str, Any],
    preferred_key: str,
    fallback_key: Optional[str],
    address: Optional[str],
) -> Optional[Tuple[float, float]]:
    point = _extract_point(data.get(preferred_key))
    if point:
        return point

    if fallback_key:
        point = _extract_point(data.get(fallback_key))
        if point:
            return point

    if address:
        return geocode_address(address)

    return None


def _announcement_point_for_storage(category: str, data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    normalized_category = (category or "").strip().lower()
    if normalized_category == "delivery":
        return _extract_point(data.get("pickup_point")) or _extract_point(data.get("point"))
    if normalized_category == "help":
        return _extract_point(data.get("help_point")) or _extract_point(data.get("point"))
    return _extract_point(data.get("point"))


def _persist_repaired_task_point(task_id: str, category: str, data: Dict[str, Any]) -> bool:
    point = _ensure_payload_points(category, data)
    if point is None:
        return False

    payload_json = json.dumps(data, ensure_ascii=False)
    address_text = _normalize_optional_text(data.get("address_text"), collapse_spaces=True) or primary_source_address(data)

    execute(
        """
        UPDATE tasks
        SET extra = %s::jsonb,
            address_text = COALESCE(%s, address_text),
            location_point = CASE
                WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN location_point
                ELSE ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326)::geography
            END,
            updated_at = now()
        WHERE id::text = %s
        """,
        (
            payload_json,
            address_text,
            point[1],
            point[0],
            point[1],
            point[0],
            task_id,
        ),
    )
    execute(
        """
        UPDATE announcements
        SET data = %s::jsonb,
            location_point = CASE
                WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN location_point
                ELSE ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326)::geography
            END,
            updated_at = now()
        WHERE id::text = %s
        """,
        (
            payload_json,
            point[1],
            point[0],
            point[1],
            point[0],
            task_id,
        ),
    )
    return True


def _save_upload(ann_id: str, file: UploadFile, content: bytes) -> str:
    safe_name = (file.filename or "image").replace("/", "_").replace("\\", "_")
    folder = UPLOADS_DIR / ann_id
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{uuid.uuid4().hex}_{safe_name}"
    out.write_bytes(content)
    return "/" + out.as_posix().lstrip("/")


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_legacy_announcement_projection(
    task_id: str,
    *,
    user_id: str,
    category: str,
    title: str,
    task_status: str,
    moderation_status: str,
    data: Dict[str, Any],
    deleted_at: Any = None,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
) -> None:
    legacy_status = task_to_announcement_status(task_status, moderation_status, deleted_at)
    point = primary_map_point(data)
    normalized_category = (
        _normalize_optional_text(data.get("category"), collapse_spaces=True)
        or _normalize_optional_text(category, collapse_spaces=True)
        or "help"
    )
    created_value = created_at or datetime.now(timezone.utc)
    updated_value = updated_at or created_value
    payload_json = json.dumps(data, ensure_ascii=False)

    existing = fetch_one("SELECT 1 FROM announcements WHERE id::text = %s", (task_id,))
    params = (
        user_id,
        normalized_category.lower(),
        title,
        legacy_status,
        payload_json,
        created_value,
        updated_value,
        deleted_at,
        point[1] if point else None,
        point[0] if point else None,
        point[1] if point else None,
        point[0] if point else None,
        task_id,
    )
    if existing:
        execute(
            """
            UPDATE announcements
            SET user_id = %s,
                category = %s,
                title = %s,
                status = %s,
                data = %s::jsonb,
                created_at = COALESCE(created_at, %s),
                updated_at = %s,
                deleted_at = %s,
                location_point = CASE
                    WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                    ELSE ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326)::geography
                END
            WHERE id::text = %s
            """,
            params,
        )
        return

    execute(
        """
        INSERT INTO announcements (
            user_id, category, title, status, data,
            created_at, updated_at, deleted_at, location_point, id
        )
        VALUES (
            %s, %s, %s, %s, %s::jsonb,
            %s, %s, %s,
            CASE
                WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                ELSE ST_SetSRID(ST_MakePoint(%s::double precision, %s::double precision), 4326)::geography
            END,
            %s
        )
        """,
        params,
    )


def _offer_avatar_select_sql() -> str:
    if _profile_has_extra_column():
        return "up.extra->>'avatar_url' AS avatar_url"
    return "NULL AS avatar_url"


def _category_id_for_slug(slug: str) -> str:
    row = fetch_one(
        """
        SELECT id::text
        FROM categories
        WHERE slug = %s
        LIMIT 1
        """,
        (slug,),
    )
    if row and row[0]:
        return str(row[0])
    raise HTTPException(status_code=500, detail=f"Category slug '{slug}' is not configured")


def _offer_from_row(row) -> OfferOut:
    return OfferOut(**task_offer_row_to_legacy_dict(
        {
            "id": row[0],
            "task_id": row[1],
            "performer_id": row[2],
            "message": row[3],
            "proposed_price": row[4],
            "agreed_price": row[5],
            "pricing_mode": row[6],
            "minimum_price_accepted": row[7],
            "can_reoffer": row[8],
            "status": row[9],
            "created_at": row[10],
        }
    ))


def _offer_expanded_from_row(row) -> OfferOutExpanded:
    offer = _offer_from_row(row)
    return OfferOutExpanded(
        id=offer.id,
        announcement_id=offer.announcement_id,
        performer_id=offer.performer_id,
        message=offer.message,
        proposed_price=offer.proposed_price,
        agreed_price=offer.agreed_price,
        pricing_mode=offer.pricing_mode,
        minimum_price_accepted=offer.minimum_price_accepted,
        can_reoffer=offer.can_reoffer,
        status=offer.status,
        created_at=offer.created_at,
        performer_profile={
            "user_id": row[11],
            "display_name": row[12],
            "city": _normalize_optional_text(row[13], collapse_spaces=True),
            "contact": _normalize_optional_text(row[14], collapse_spaces=True),
            "avatar_url": row[15],
        },
        performer_stats={
            "rating_avg": float(row[16] or 0),
            "rating_count": int(row[17] or 0),
            "completed_count": int(row[18] or 0),
            "cancelled_count": int(row[19] or 0),
        },
    )


def _task_exists(task_id: str) -> bool:
    return bool(fetch_one("SELECT 1 FROM tasks WHERE id::text = %s", (task_id,)))


def _announcement_exists(task_id: str) -> bool:
    return _task_exists(task_id)


def _fetch_task_row_or_404(task_id: str):
    row = _repair_announcement_row_if_needed(_fetch_task_row(task_id))
    if not row:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return row


def _fetch_announcement_or_404(ann_id: str) -> AnnouncementOut:
    return _task_row_to_announcement(_fetch_task_row_or_404(ann_id))


def _sync_legacy_announcement_from_current_task(task_id: str) -> None:
    row = _fetch_task_row_or_404(task_id)
    announcement = _task_row_to_announcement(row)
    _sync_legacy_announcement_projection(
        task_id,
        user_id=announcement.user_id,
        category=announcement.category,
        title=announcement.title,
        task_status=str(row[7] or ""),
        moderation_status=str(row[8] or ""),
        data=dict(announcement.data or {}),
        deleted_at=row[9],
        created_at=row[6],
    )


def _count_pending_offers(task_id: str) -> int:
    row = fetch_one(
        """
        SELECT COUNT(*)
        FROM task_offers
        WHERE task_id::text = %s
          AND status IN ('sent', 'accepted_by_customer')
        """,
        (task_id,),
    )
    return int(row[0] or 0) if row else 0


def _sync_announcement_offers_count(task_id: str) -> int:
    offers_count = _count_pending_offers(task_id)
    execute(
        """
        UPDATE tasks
        SET responses_count = %s,
            status = CASE
                WHEN accepted_offer_id IS NOT NULL THEN status
                WHEN %s > 0 AND status = 'published' THEN 'in_responses'
                WHEN %s = 0 AND status = 'in_responses' THEN 'published'
                ELSE status
            END,
            updated_at = now()
        WHERE id::text = %s
        """,
        (offers_count, offers_count, offers_count, task_id),
    )
    _sync_legacy_announcement_from_current_task(task_id)
    return offers_count


def _sync_task_route_points(task_id: str, data: Dict[str, Any]) -> None:
    execute("DELETE FROM task_route_points WHERE task_id::text = %s", (task_id,))
    for point in route_points_from_payload(task_id, data):
        point_value = point.get("point")
        if not isinstance(point_value, tuple):
            continue
        execute(
            """
            INSERT INTO task_route_points (
                id, task_id, point_order, title, address_text, point, point_kind, created_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s, now()
            )
            """,
            (
                str(uuid.uuid4()),
                task_id,
                int(point["point_order"]),
                point.get("title"),
                point.get("address_text"),
                point_value[1],
                point_value[0],
                point.get("point_kind"),
            ),
        )


def _fetch_expanded_offer(ann_id: str, offer_id: str) -> Optional[OfferOutExpanded]:
    row = fetch_one(
        f"""
        SELECT
            tf.id::text,
            tf.task_id::text,
            tf.performer_id::text,
            tf.message,
            tf.proposed_price,
            tf.agreed_price,
            tf.pricing_mode,
            tf.minimum_price_accepted,
            tf.can_reoffer,
            tf.status::text,
            tf.created_at,
            tf.performer_id::text,
            COALESCE(
                NULLIF(BTRIM(up.display_name), ''),
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), ''),
                'Пользователь'
            ) AS performer_display_name,
            up.city,
            COALESCE(
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), '')
            ) AS performer_contact,
            {_offer_avatar_select_sql()},
            COALESCE(us.rating_avg, 0),
            COALESCE(us.rating_count, 0),
            COALESCE(us.completed_count, 0),
            COALESCE(us.cancelled_count, 0)
        FROM task_offers tf
        JOIN users u
          ON u.id::text = tf.performer_id::text
        LEFT JOIN user_profiles up
          ON up.user_id::text = tf.performer_id::text
        LEFT JOIN user_stats us
          ON us.user_id::text = tf.performer_id::text
        WHERE tf.task_id::text = %s
          AND tf.id::text = %s
        """,
        (ann_id, offer_id),
    )
    if not row:
        return None
    return _offer_expanded_from_row(row)


def _task_columns_from_payload(
    *,
    user_id: str,
    category: str,
    title: str,
    legacy_status: str,
    data: Dict[str, Any],
    deleted_at: Any = None,
    has_accepted_offer: bool = False,
) -> Dict[str, Any]:
    normalized_data = ensure_task_payload(
        data,
        title=title,
        announcement_status=legacy_status,
        deleted_at=deleted_at,
    )
    normalized_data = _normalize_schedule_fields_for_user(user_id=user_id, data=normalized_data)
    task_status, moderation_status = announcement_status_to_task_fields(
        legacy_status,
        deleted=deleted_at is not None,
        has_accepted_offer=has_accepted_offer,
    )
    budget_min, budget_max = derive_budget_bounds(normalized_data)
    quick_offer_price = derive_quick_offer_price(normalized_data)
    reward_amount = derive_reward_amount(normalized_data)
    point = _ensure_payload_points(category, normalized_data)
    address_text = _normalize_optional_text(normalized_data.get("address_text"), collapse_spaces=True) or primary_source_address(normalized_data)
    published_at = datetime.now(timezone.utc) if moderation_status == "published" and task_status in {"published", "in_responses"} else None
    closed_at = datetime.now(timezone.utc) if task_status in {"closed", "completed", "cancelled"} or deleted_at is not None else None
    return {
        "category_id": _category_id_for_slug(builder_category_slug(category)),
        "data": normalized_data,
        "task_status": task_status,
        "moderation_status": moderation_status,
        "reward_amount": reward_amount,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "quick_offer_price": quick_offer_price,
        "point": point,
        "address_text": address_text,
        "published_at": published_at,
        "closed_at": closed_at,
        "reoffer_policy": _normalize_optional_text(normalized_data.get("offer_policy", {}).get("reoffer_policy")) or "blocked_after_reject",
        "customer_comment": _normalize_optional_text(normalized_data.get("notes")),
        "description": _normalize_optional_text(normalized_data.get("description")) or _normalize_optional_text(normalized_data.get("generated_description")) or _normalize_optional_text(normalized_data.get("notes")) or title,
    }


def _insert_task(task_id: str, user_id: str, category: str, title: str, legacy_status: str, data: Dict[str, Any]) -> None:
    fields = _task_columns_from_payload(
        user_id=user_id,
        category=category,
        title=title,
        legacy_status=legacy_status,
        data=data,
    )
    point = fields["point"]
    created_at = datetime.now(timezone.utc)
    execute(
        """
        INSERT INTO tasks (
            id, customer_id, title, description, category_id, reward_amount, currency, price_type,
            deadline_at, location_point, address_text, customer_comment, performer_preferences,
            status, moderation_status, views_count, favorites_count, responses_count, accepted_offer_id,
            extra, created_at, updated_at, published_at, closed_at, deleted_at,
            budget_min, budget_max, quick_offer_price, reoffer_policy
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, 'RUB',
            %s,
            NULL,
            CASE
                WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                ELSE ST_SetSRID(
                    ST_MakePoint(%s::double precision, %s::double precision),
                    4326
                )::geography
            END,
            %s, %s, NULL,
            %s, %s,
            0, 0, 0, NULL,
            %s::jsonb, %s, %s, %s, %s, NULL,
            %s, %s, %s, %s
        )
        """,
        (
            task_id,
            user_id,
            title,
            fields["description"],
            fields["category_id"],
            fields["reward_amount"],
            "negotiable" if fields["budget_min"] is not None or fields["budget_max"] is not None else ("free" if fields["reward_amount"] == 0 else "fixed"),
            point[1] if point else None,
            point[0] if point else None,
            point[1] if point else None,
            point[0] if point else None,
            fields["address_text"],
            fields["customer_comment"],
            fields["task_status"],
            fields["moderation_status"],
            json.dumps(fields["data"], ensure_ascii=False),
            created_at,
            created_at,
            fields["published_at"],
            fields["closed_at"],
            fields["budget_min"],
            fields["budget_max"],
            fields["quick_offer_price"],
            fields["reoffer_policy"],
        ),
    )
    _sync_legacy_announcement_projection(
        task_id,
        user_id=user_id,
        category=category,
        title=title,
        task_status=fields["task_status"],
        moderation_status=fields["moderation_status"],
        data=fields["data"],
        created_at=created_at,
        updated_at=created_at,
    )
    _sync_task_route_points(task_id, fields["data"])
    execute(
        """
        INSERT INTO task_status_events (id, task_id, from_status, to_status, changed_by, reason, created_at)
        VALUES (%s, %s, NULL, %s, %s, %s, now())
        """,
        (str(uuid.uuid4()), task_id, fields["task_status"], user_id, "created_from_announcement_api"),
    )


def _update_task_content(
    task_id: str,
    *,
    user_id: str,
    category: str,
    title: str,
    legacy_status: str,
    data: Dict[str, Any],
    deleted_at: Any = None,
) -> None:
    current = _fetch_task_row_or_404(task_id)
    fields = _task_columns_from_payload(
        user_id=user_id,
        category=category,
        title=title,
        legacy_status=legacy_status,
        data=data,
        deleted_at=deleted_at,
        has_accepted_offer=bool(current[14]),
    )
    point = fields["point"]
    updated_at = datetime.now(timezone.utc)
    execute(
        """
        UPDATE tasks
        SET title = %s,
            description = %s,
            category_id = %s,
            reward_amount = %s,
            price_type = %s,
            location_point = CASE
                WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                ELSE ST_SetSRID(
                    ST_MakePoint(%s::double precision, %s::double precision),
                    4326
                )::geography
            END,
            address_text = %s,
            customer_comment = %s,
            status = %s,
            moderation_status = %s,
            extra = %s::jsonb,
            published_at = COALESCE(%s, published_at),
            closed_at = %s,
            deleted_at = %s,
            budget_min = %s,
            budget_max = %s,
            quick_offer_price = %s,
            reoffer_policy = %s,
            updated_at = %s
        WHERE id::text = %s
        """,
        (
            title,
            fields["description"],
            fields["category_id"],
            fields["reward_amount"],
            "negotiable" if fields["budget_min"] is not None or fields["budget_max"] is not None else ("free" if fields["reward_amount"] == 0 else "fixed"),
            point[1] if point else None,
            point[0] if point else None,
            point[1] if point else None,
            point[0] if point else None,
            fields["address_text"],
            fields["customer_comment"],
            fields["task_status"],
            fields["moderation_status"],
            json.dumps(fields["data"], ensure_ascii=False),
            fields["published_at"],
            fields["closed_at"],
            deleted_at,
            fields["budget_min"],
            fields["budget_max"],
            fields["quick_offer_price"],
            fields["reoffer_policy"],
            updated_at,
            task_id,
        ),
    )
    _sync_legacy_announcement_projection(
        task_id,
        user_id=user_id,
        category=category,
        title=title,
        task_status=fields["task_status"],
        moderation_status=fields["moderation_status"],
        data=fields["data"],
        deleted_at=deleted_at,
        updated_at=updated_at,
    )
    _sync_task_route_points(task_id, fields["data"])


def _active_assignment_for_task(task_id: str):
    return fetch_one(
        """
        SELECT id::text, offer_id::text, performer_id::text, assignment_status, execution_stage, chat_thread_id::text
        FROM task_assignments
        WHERE task_id::text = %s
          AND assignment_status IN ('assigned', 'in_progress')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id,),
    )


def _create_or_update_assignment(task_id: str, offer_id: str, customer_id: str, performer_id: str) -> str:
    existing = _active_assignment_for_task(task_id)
    if existing and str(existing[1]) == offer_id:
        return str(existing[0])
    if existing and str(existing[1]) != offer_id:
        raise HTTPException(status_code=409, detail="По объявлению уже выбран исполнитель")

    assignment_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO task_assignments (
            id, task_id, offer_id, customer_id, performer_id,
            assignment_status, execution_stage, route_visibility,
            created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s,
            'assigned', 'accepted', 'performer_only',
            now(), now()
        )
        """,
        (assignment_id, task_id, offer_id, customer_id, performer_id),
    )
    execute(
        """
        INSERT INTO task_assignment_events (
            id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload
        )
        VALUES (%s, %s, %s, 'assignment_status', NULL, 'assigned', %s, '{}'::jsonb)
        """,
        (str(uuid.uuid4()), assignment_id, task_id, customer_id),
    )
    execute(
        """
        INSERT INTO task_assignment_events (
            id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload
        )
        VALUES (%s, %s, %s, 'execution_stage', NULL, 'accepted', %s, '{}'::jsonb)
        """,
        (str(uuid.uuid4()), assignment_id, task_id, customer_id),
    )
    execute(
        """
        UPDATE tasks
        SET accepted_offer_id = %s,
            status = 'agreed',
            updated_at = now()
        WHERE id::text = %s
        """,
        (offer_id, task_id),
    )
    execute(
        """
        INSERT INTO task_status_events (id, task_id, from_status, to_status, changed_by, reason, created_at)
        VALUES (%s, %s, 'in_responses', 'agreed', %s, %s, now())
        """,
        (str(uuid.uuid4()), task_id, customer_id, "offer_accepted"),
    )
    _sync_legacy_announcement_from_current_task(task_id)
    return assignment_id


# ----------------------------
@app.post("/announcements", response_model=AnnouncementOut, status_code=201)
def create_announcement(
    payload: CreateAnnouncementIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann_id = str(uuid.uuid4())

    data: Dict[str, Any] = dict(payload.data or {})
    category = (payload.category or "").strip().lower()
    _normalize_budget_fields(data)
    data["offers_count"] = int(_parse_float(data.get("offers_count")) or 0)

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
            data["help_point"] = _point_obj(help_point)
            data["point"] = _point_obj(help_point)
        if help_address:
            data["address_text"] = help_address

    else:
        addr = _extract_primary_address(payload.category, data)
        point = _resolve_point(data=data, preferred_key="point", fallback_key=None, address=addr)
        if point:
            data["point"] = _point_obj(point)
        if addr:
            data["address_text"] = addr

    mod = _get_mod(data)

    title = (payload.title or "").strip()
    notes = data.get("notes")

    parts = [title]
    if isinstance(notes, str) and notes.strip():
        parts.append(notes.strip())

    text_for_check = "\n\n".join([p for p in parts if p])

    text_mod = classify_text(text_for_check) if text_for_check else {"label": "LEGAL", "reason": ""}
    label = str(text_mod.get("label", "UNKNOWN")).upper()
    reason = str(text_mod.get("reason", "")).strip()

    _remove_reasons_for_field(mod, "title")
    _remove_reasons_for_field(mod, "notes")

    status = STATUS_PENDING
    suggestions: List[str] = []
    has_media = _has_media_attachments(data)
    text_moderation_temporarily_unavailable = label == "UNKNOWN" and _is_temporary_text_moderation_issue(reason)

    mod["text"] = {
        "label": label,
        "reason": reason,
        "can_appeal": True if label in ("ILLEGAL", "UNKNOWN") else False,
    }

    if label == "ILLEGAL":
        status = STATUS_REJECTED
        _set_decision(mod, status, "Отказано: текст похож на запрещённый контент. Измените формулировку.")
        _add_reason(mod, "title", "TEXT_ILLEGAL", reason or "Запрещённый текст", True)
        if isinstance(notes, str) and notes.strip():
            _add_reason(mod, "notes", "TEXT_ILLEGAL", reason or "Запрещённый текст", True)
        suggestions = [
            "Переформулируйте название и описание.",
            "Уберите неоднозначные или запрещённые формулировки.",
        ]
    elif label != "LEGAL":
        if text_moderation_temporarily_unavailable:
            if has_media:
                status = STATUS_PENDING
                _set_decision(mod, status, "На проверке: сначала проверим фото, затем объявление появится на карте.")
            else:
                status = STATUS_ACTIVE
                _set_decision(mod, status, "Одобрено: объявление без фото активно и отображается на карте.")
        else:
            status = STATUS_NEEDS_FIX
            _set_decision(mod, status, "Нужно исправить: текст выглядит спорным. Уточните формулировку.")
            _add_reason(mod, "title", "TEXT_UNKNOWN", reason or "Спорный текст", True)
            if isinstance(notes, str) and notes.strip():
                _add_reason(mod, "notes", "TEXT_UNKNOWN", reason or "Спорный текст", True)
            suggestions = [
                "Сделайте описание более нейтральным и конкретным.",
                "Уберите фразы, которые можно трактовать неоднозначно.",
            ]
    else:
        if has_media:
            status = STATUS_PENDING
            _set_decision(mod, status, "На проверке: сначала проверим фото, затем объявление появится на карте.")
        else:
            status = STATUS_ACTIVE
            _set_decision(mod, status, "Одобрено: объявление без фото активно и отображается на карте.")

    mod["image"] = {
        "max_nsfw": None,
        "items": [],
        "can_appeal": None,
        "has_media": has_media,
        "review_thr": NSFW_REVIEW,
        "hard_block_thr": NSFW_HARD_BLOCK,
    }

    if suggestions:
        _set_suggestions(mod, suggestions)

    _set_mod(data, mod)

    _insert_task(
        task_id=ann_id,
        user_id=user.id,
        category=payload.category,
        title=title,
        legacy_status=status,
        data=data,
    )
    return _fetch_announcement_or_404(ann_id)


# ----------------------------
# Announcements (Media)
# ----------------------------
@app.post("/announcements/{ann_id}/media", response_model=AnnouncementOut)
def upload_announcement_media(
    ann_id: str,
    files: List[UploadFile] = File(...),
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    data_obj: Dict[str, Any] = dict(ann.data or {})
    mod = _get_mod(data_obj)

    det = get_nsfw_detector()

    items: List[Dict[str, Any]] = []
    max_nsfw = 0.0

    for f in files:
        content = f.file.read()
        if not content:
            continue

        r = det.predict_bytes(content)
        max_nsfw = max(max_nsfw, float(r.nsfw))

        path = _save_upload(ann_id, f, content)
        items.append(
            {
                "filename": f.filename,
                "path": path,
                "nsfw": float(r.nsfw),
                "sfw": float(r.sfw),
                "top_label": r.top_label,
                "top_prob": float(r.top_prob),
                "infer_s": float(r.infer_seconds),
            }
        )

    data_obj["media"] = items
    _remove_reasons_for_field(mod, "media")

    current_status = ann.status or STATUS_PENDING
    new_status = current_status

    if max_nsfw >= NSFW_HARD_BLOCK:
        new_status = _keep_stricter(current_status, STATUS_REJECTED)
        _add_reason(mod, "media", "NSFW_HARD", f"NSFW {max_nsfw:.2f} ≥ {NSFW_HARD_BLOCK:.2f}", False)
        _set_decision(mod, new_status, "Отказано: фото содержит запрещённый/неприемлемый контент.")
        mod["penalty_stub"] = {"type": "warning", "points": 1, "applied_at": None}
        mod["image"] = {
            "max_nsfw": max_nsfw,
            "items": items,
            "can_appeal": False,
            "review_thr": NSFW_REVIEW,
            "hard_block_thr": NSFW_HARD_BLOCK,
        }
    elif max_nsfw >= NSFW_REVIEW:
        new_status = _keep_stricter(current_status, STATUS_NEEDS_FIX)
        _add_reason(mod, "media", "NSFW_REVIEW", f"NSFW {max_nsfw:.2f}", True)
        _set_decision(mod, new_status, "Нужно исправить: фото выглядит спорным. Замените фото и отправьте заново.")
        mod["image"] = {
            "max_nsfw": max_nsfw,
            "items": items,
            "can_appeal": True,
            "review_thr": NSFW_REVIEW,
            "hard_block_thr": NSFW_HARD_BLOCK,
        }
    else:
        if _status_priority(current_status) <= _status_priority(STATUS_PENDING):
            new_status = STATUS_ACTIVE
            _set_decision(mod, new_status, "Одобрено: объявление активно и отображается на карте.")
        else:
            new_status = current_status
        mod["image"] = {
            "max_nsfw": max_nsfw,
            "items": items,
            "can_appeal": None,
            "review_thr": NSFW_REVIEW,
            "hard_block_thr": NSFW_HARD_BLOCK,
        }

    _set_mod(data_obj, mod)
    _update_task_content(
        ann_id,
        user_id=user.id,
        category=ann.category,
        title=ann.title,
        legacy_status=new_status,
        data=data_obj,
    )
    return _fetch_announcement_or_404(ann_id)


# ----------------------------
# Announcements (Appeal / Archive / Delete)
# ----------------------------
@app.post("/announcements/{ann_id}/appeal", response_model=AnnouncementOut)
def appeal_announcement(
    ann_id: str,
    payload: AppealIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    data_obj: Dict[str, Any] = dict(ann.data or {})
    mod = _get_mod(data_obj)
    appeal = _ensure_obj(mod.get("appeal"))
    appeal["requested"] = True
    appeal["reason"] = (payload.reason or "").strip() or None
    appeal["requested_at"] = _utc_iso_now()
    appeal["resolved_at"] = None
    mod["appeal"] = appeal

    _set_decision(mod, STATUS_PENDING, "Апелляция принята и отправлена на повторную проверку.")
    _set_mod(data_obj, mod)
    _update_task_content(
        ann_id,
        user_id=user.id,
        category=ann.category,
        title=ann.title,
        legacy_status=STATUS_PENDING,
        data=data_obj,
    )
    ensure_appeal_report(user.id, ann_id, payload.reason)
    return _fetch_announcement_or_404(ann_id)


@app.patch("/announcements/{ann_id}/archive", response_model=AnnouncementOut)
def archive_announcement(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")
    _update_task_content(
        ann_id,
        user_id=user.id,
        category=ann.category,
        title=ann.title,
        legacy_status=STATUS_ARCHIVED,
        data=dict(ann.data or {}),
    )
    return _fetch_announcement_or_404(ann_id)


@app.delete("/announcements/{ann_id}")
def delete_announcement(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> Dict[str, bool]:
    if not _task_exists(ann_id):
        return {"ok": True}
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")
    now = datetime.now(timezone.utc)
    execute(
        """
        UPDATE task_assignments
        SET assignment_status = 'cancelled',
            execution_stage = 'cancelled',
            route_visibility = 'hidden',
            cancelled_at = now(),
            updated_at = now()
        WHERE task_id::text = %s
          AND assignment_status IN ('assigned', 'in_progress')
        """,
        (ann_id,),
    )
    execute(
        """
        UPDATE chat_threads
        SET archived_at = now()
        WHERE task_id::text = %s
        """,
        (ann_id,),
    )
    execute(
        """
        UPDATE chat_participants
        SET left_at = now()
        WHERE thread_id IN (
            SELECT id FROM chat_threads WHERE task_id::text = %s
        )
        """,
        (ann_id,),
    )
    _update_task_content(
        ann_id,
        user_id=user.id,
        category=ann.category,
        title=ann.title,
        legacy_status="deleted",
        data=dict(ann.data or {}),
        deleted_at=now,
    )
    return {"ok": True}


# ----------------------------
# Offers
# ----------------------------
@app.post("/announcements/{ann_id}/offers", response_model=OfferOut, status_code=201)
def create_offer(
    ann_id: str,
    payload: CreateOfferIn,
    user: UserOut = Depends(get_current_user),
) -> OfferOut:
    ann = _fetch_announcement_or_404(ann_id)

    if ann.user_id == user.id:
        raise HTTPException(status_code=403, detail="Нельзя откликаться на своё объявление")

    if ann.status != STATUS_ACTIVE:
        raise HTTPException(status_code=400, detail="Отклики доступны только для активных объявлений")

    if _active_assignment_for_task(ann_id):
        raise HTTPException(status_code=409, detail="По объявлению уже выбран исполнитель")

    message = _normalize_message(payload.message)
    pricing_mode = (payload.pricing_mode or "").strip().lower() or ("quick_min_price" if payload.proposed_price is None else "counter_price")
    quick_offer_price = derive_quick_offer_price(dict(ann.data or {}))
    minimum_price_accepted = bool(payload.minimum_price_accepted) or pricing_mode == "quick_min_price"
    proposed_price = payload.proposed_price
    agreed_price = payload.agreed_price

    if pricing_mode == "quick_min_price":
        minimum_price_accepted = True
        proposed_price = None
        agreed_price = quick_offer_price
    elif agreed_price is None and proposed_price is not None:
        agreed_price = proposed_price

    existing = fetch_one(
        """
        SELECT id::text, status::text, can_reoffer
        FROM task_offers
        WHERE task_id::text = %s
          AND performer_id::text = %s
        LIMIT 1
        """,
        (ann_id, user.id),
    )

    if existing:
        offer_id = str(existing[0])
        existing_status = str(existing[1] or "")
        can_reoffer = bool(existing[2])
        if existing_status == "accepted_by_customer":
            raise HTTPException(status_code=409, detail="Ваш отклик уже принят")
        if not can_reoffer and existing_status in {"rejected_by_customer", "withdrawn_by_sender"}:
            raise HTTPException(status_code=409, detail="Повторный отклик по этому заданию запрещён")
        execute(
            """
            UPDATE task_offers
            SET message = %s,
                proposed_price = %s,
                agreed_price = %s,
                pricing_mode = %s,
                minimum_price_accepted = %s,
                can_reoffer = TRUE,
                reoffer_block_reason = NULL,
                status = 'sent',
                rejected_at = NULL,
                withdrawn_at = NULL,
                updated_at = now()
            WHERE id::text = %s
            """,
            (message, proposed_price, agreed_price, pricing_mode, minimum_price_accepted, offer_id),
        )
    else:
        offer_id = str(uuid.uuid4())
        execute(
            """
            INSERT INTO task_offers (
                id, task_id, performer_id, message, proposed_price, currency, status, created_at, updated_at,
                pricing_mode, agreed_price, minimum_price_accepted, can_reoffer, reoffer_block_reason
            )
            VALUES (%s, %s, %s, %s, %s, 'RUB', 'sent', now(), now(), %s, %s, %s, TRUE, NULL)
            """,
            (offer_id, ann_id, user.id, message, proposed_price, pricing_mode, agreed_price, minimum_price_accepted),
        )

    _sync_announcement_offers_count(ann_id)
    create_notification(
        user_id=ann.user_id,
        notif_type="offer",
        body="Новый отклик на ваше объявление",
        payload={"announcement_id": ann_id, "offer_id": offer_id},
    )

    row = fetch_one(
        """
        SELECT
            id::text,
            task_id::text,
            performer_id::text,
            message,
            proposed_price,
            agreed_price,
            pricing_mode,
            minimum_price_accepted,
            can_reoffer,
            status::text,
            created_at
        FROM task_offers
        WHERE id::text = %s
        """,
        (offer_id,),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create offer")

    return _offer_from_row(row)


@app.get("/announcements/{ann_id}/offers", response_model=List[OfferOutExpanded])
def list_announcement_offers(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> List[OfferOutExpanded]:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    rows = fetch_all(
        f"""
        SELECT
            tf.id::text,
            tf.task_id::text,
            tf.performer_id::text,
            tf.message,
            tf.proposed_price,
            tf.agreed_price,
            tf.pricing_mode,
            tf.minimum_price_accepted,
            tf.can_reoffer,
            tf.status::text,
            tf.created_at,
            tf.performer_id::text,
            COALESCE(
                NULLIF(BTRIM(up.display_name), ''),
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), ''),
                'Пользователь'
            ) AS performer_display_name,
            up.city,
            COALESCE(
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), '')
            ) AS performer_contact,
            {_offer_avatar_select_sql()},
            COALESCE(us.rating_avg, 0),
            COALESCE(us.rating_count, 0),
            COALESCE(us.completed_count, 0),
            COALESCE(us.cancelled_count, 0)
        FROM task_offers tf
        JOIN users u
          ON u.id::text = tf.performer_id::text
        LEFT JOIN user_profiles up
          ON up.user_id::text = tf.performer_id::text
        LEFT JOIN user_stats us
          ON us.user_id::text = tf.performer_id::text
        WHERE tf.task_id::text = %s
          AND tf.status IN ('sent', 'accepted_by_customer', 'rejected_by_customer')
        ORDER BY CASE WHEN tf.status = 'accepted_by_customer' THEN 0 ELSE 1 END,
                 tf.created_at DESC
        """,
        (ann_id,),
    )

    return [_offer_expanded_from_row(row) for row in rows]


@app.post("/announcements/{ann_id}/offers/{offer_id}/accept", response_model=AcceptOfferOut)
def accept_announcement_offer(
    ann_id: str,
    offer_id: str,
    user: UserOut = Depends(get_current_user),
) -> AcceptOfferOut:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    row = fetch_one(
        """
        SELECT performer_id::text, status::text
        FROM task_offers
        WHERE id::text = %s
          AND task_id::text = %s
        """,
        (offer_id, ann_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Offer not found")

    performer_id, status = row
    if status == "rejected_by_customer":
        raise HTTPException(status_code=409, detail="Отклик уже отклонён")

    did_accept_now = False
    assignment_id = None

    if status != "accepted_by_customer":
        if status != "sent":
            raise HTTPException(status_code=409, detail="Отклик нельзя принять в текущем статусе")

        execute(
            """
            UPDATE task_offers
            SET status = 'accepted_by_customer',
                can_reoffer = FALSE,
                accepted_at = now(),
                agreed_price = COALESCE(agreed_price, proposed_price, %s)
            WHERE id::text = %s
            """,
            (derive_quick_offer_price(dict(ann.data or {})), offer_id),
        )
        execute(
            """
            UPDATE task_offers
            SET status = 'rejected_by_customer',
                can_reoffer = FALSE,
                reoffer_block_reason = 'task_already_assigned',
                rejected_at = now(),
                updated_at = now()
            WHERE task_id::text = %s
              AND id::text <> %s
              AND status = 'sent'
            """,
            (ann_id, offer_id),
        )
        _sync_announcement_offers_count(ann_id)
        assignment_id = _create_or_update_assignment(ann_id, offer_id, ann.user_id, performer_id)
        did_accept_now = True
    else:
        existing_assignment = _active_assignment_for_task(ann_id)
        assignment_id = str(existing_assignment[0]) if existing_assignment else None

    thread_id = get_or_create_offer_thread(
        task_id=ann_id,
        offer_id=offer_id,
        assignment_id=assignment_id,
        owner_id=ann.user_id,
        performer_id=performer_id,
    )

    if did_accept_now:
        create_notification(
            user_id=performer_id,
            notif_type="offer_accepted",
            body="Ваш отклик принят. Открыт чат.",
            payload={"thread_id": thread_id, "announcement_id": ann_id, "offer_id": offer_id},
        )

    expanded = _fetch_expanded_offer(ann_id, offer_id)
    if expanded is None:
        raise HTTPException(status_code=500, detail="Failed to load accepted offer")

    return AcceptOfferOut(thread_id=thread_id, offer=expanded)


@app.post("/announcements/{ann_id}/offers/{offer_id}/reject", response_model=OKOut)
def reject_announcement_offer(
    ann_id: str,
    offer_id: str,
    user: UserOut = Depends(get_current_user),
) -> OKOut:
    ann = _fetch_announcement_or_404(ann_id)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    row = fetch_one(
        """
        SELECT performer_id::text, status::text
        FROM task_offers
        WHERE id::text = %s
          AND task_id::text = %s
        """,
        (offer_id, ann_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Offer not found")

    performer_id, status = row
    if status == "accepted_by_customer":
        raise HTTPException(status_code=409, detail="Нельзя отклонить уже принятый отклик")

    if status != "rejected_by_customer":
        execute(
            """
            UPDATE task_offers
            SET status = 'rejected_by_customer',
                can_reoffer = FALSE,
                reoffer_block_reason = 'blocked_after_reject',
                rejected_at = now(),
                updated_at = now()
            WHERE id::text = %s
            """,
            (offer_id,),
        )
        _sync_announcement_offers_count(ann_id)
        create_notification(
            user_id=performer_id,
            notif_type="offer_rejected",
            body="Ваш отклик отклонён.",
            payload={"announcement_id": ann_id, "offer_id": offer_id},
        )

    return OKOut(ok=True)


def _canonical_execution_stage(stage: str) -> str:
    normalized = (stage or "").strip().lower()
    mapping = {
        "accepted": "accepted",
        "heading": "en_route",
        "en_route": "en_route",
        "onsite": "on_site",
        "on_site": "on_site",
        "doing": "in_progress",
        "in_progress": "in_progress",
        "finishing": "handoff",
        "handoff": "handoff",
        "completed": "completed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return mapping.get(normalized, normalized)


@app.post("/announcements/{ann_id}/execution-stage", response_model=AnnouncementOut)
def update_announcement_execution_stage(
    ann_id: str,
    payload: ExecutionStageUpdateIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann = _fetch_announcement_or_404(ann_id)
    assignment = _active_assignment_for_task(ann_id)
    if not assignment:
        raise HTTPException(status_code=409, detail="Для задания пока нет активного исполнителя")

    assignment_id, accepted_offer_id, performer_id, assignment_status, current_stage, chat_thread_id = assignment
    if user.id != performer_id and user.id != ann.user_id:
        raise HTTPException(status_code=403, detail="Недостаточно прав для смены этапа")

    new_stage = _canonical_execution_stage(payload.stage)
    allowed_stages = {"accepted", "en_route", "on_site", "in_progress", "handoff", "completed", "cancelled"}
    if new_stage not in allowed_stages:
        raise HTTPException(status_code=422, detail="Неизвестный execution-stage")

    stage_order = {
        "accepted": 0,
        "en_route": 1,
        "on_site": 2,
        "in_progress": 3,
        "handoff": 4,
        "completed": 5,
        "cancelled": 99,
    }
    current_stage = canonical_execution_status(
        execution_stage=current_stage,
        assignment_status=assignment_status,
        current_value="accepted",
    )
    if new_stage != "cancelled" and stage_order[new_stage] < stage_order[current_stage]:
        raise HTTPException(status_code=409, detail="Нельзя откатить execution-stage назад")
    if new_stage != "cancelled" and stage_order[new_stage] > stage_order[current_stage] + 1:
        raise HTTPException(status_code=409, detail="Нужно отмечать этапы последовательно")

    next_assignment_status = "assigned"
    next_task_status = "agreed"
    if new_stage in {"en_route", "on_site", "in_progress", "handoff"}:
        next_assignment_status = "in_progress"
        next_task_status = "in_progress"
    elif new_stage == "completed":
        next_assignment_status = "completed"
        next_task_status = "completed"
    elif new_stage == "cancelled":
        next_assignment_status = "cancelled"
        next_task_status = "cancelled"

    accepted_confirmed = new_stage != "cancelled"
    data_obj: Dict[str, Any] = dict(ann.data or {})
    task_obj = _ensure_obj(data_obj.get("task"))
    task_execution = _ensure_obj(task_obj.get("execution"))
    task_assignment = _ensure_obj(task_obj.get("assignment"))
    legacy_execution = _ensure_obj(data_obj.get("execution"))

    task_execution["status"] = new_stage
    task_execution["accepted_confirmed"] = accepted_confirmed
    task_assignment["execution_status"] = new_stage
    task_assignment["accepted_confirmed"] = accepted_confirmed
    task_obj["execution"] = task_execution
    task_obj["assignment"] = task_assignment
    data_obj["task"] = task_obj

    legacy_execution["status"] = new_stage
    legacy_execution["accepted_confirmed"] = accepted_confirmed
    data_obj["execution"] = legacy_execution
    data_obj["execution_status"] = new_stage
    data_obj["execution_status_confirmed"] = accepted_confirmed

    next_route_visibility = route_visibility_for_execution(new_stage)
    execute(
        """
        UPDATE task_assignments
        SET assignment_status = %s,
            execution_stage = %s,
            route_visibility = %s,
            started_at = CASE
                WHEN started_at IS NULL AND %s = 'in_progress' THEN now()
                ELSE started_at
            END,
            completed_at = CASE WHEN %s = 'completed' THEN now() ELSE completed_at END,
            cancelled_at = CASE WHEN %s = 'cancelled' THEN now() ELSE cancelled_at END,
            updated_at = now()
        WHERE id::text = %s
        """,
        (
            next_assignment_status,
            new_stage,
            next_route_visibility,
            next_assignment_status,
            next_assignment_status,
            next_assignment_status,
            assignment_id,
        ),
    )
    execute(
        """
        INSERT INTO task_assignment_events (id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload)
        VALUES (%s, %s, %s, 'execution_stage', %s, %s, %s, '{}'::jsonb)
        """,
        (str(uuid.uuid4()), assignment_id, ann_id, current_stage, new_stage, user.id),
    )
    if assignment_status != next_assignment_status:
        execute(
            """
            INSERT INTO task_assignment_events (id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload)
            VALUES (%s, %s, %s, 'assignment_status', %s, %s, %s, '{}'::jsonb)
            """,
            (str(uuid.uuid4()), assignment_id, ann_id, assignment_status, next_assignment_status, user.id),
        )

    current_task = fetch_one("SELECT status::text FROM tasks WHERE id::text = %s", (ann_id,))
    previous_task_status = str(current_task[0]) if current_task and current_task[0] else "agreed"
    execute(
        """
        UPDATE tasks
        SET status = %s,
            extra = %s::jsonb,
            closed_at = CASE WHEN %s IN ('completed', 'cancelled') THEN now() ELSE NULL END,
            updated_at = now()
        WHERE id::text = %s
        """,
        (next_task_status, json.dumps(data_obj, ensure_ascii=False), next_task_status, ann_id),
    )
    _sync_legacy_announcement_projection(
        ann_id,
        user_id=ann.user_id,
        category=ann.category,
        title=ann.title,
        task_status=next_task_status,
        moderation_status="published",
        data=data_obj,
    )
    if previous_task_status != next_task_status:
        execute(
            """
            INSERT INTO task_status_events (id, task_id, from_status, to_status, changed_by, reason, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            """,
            (str(uuid.uuid4()), ann_id, previous_task_status, next_task_status, user.id, "execution_stage_updated"),
        )

    if chat_thread_id:
        if new_stage == "completed":
            post_system_thread_message(
                chat_thread_id,
                "Заказ выполнен. Если возникли проблемы, позже здесь можно будет открыть спор. Если всё прошло хорошо, оставьте отзыв друг о друге."
            )
        anyio.from_thread.run(broadcast_thread_preview_to_user, chat_thread_id, ann.user_id)
        anyio.from_thread.run(broadcast_thread_preview_to_user, chat_thread_id, performer_id)
    return _fetch_announcement_or_404(ann_id)


# ----------------------------
# Chat (offer threads)
# ----------------------------
@app.get("/chats", response_model=List[ChatThreadOut])
def get_chats(user: UserOut = Depends(get_current_user)) -> List[ChatThreadOut]:
    if user.id == "dev":
        return []

    threads = list_user_threads(user.id)
    return [ChatThreadOut(**thread) for thread in threads]


@app.get("/chats/{thread_id}/messages", response_model=List[ChatMessageOut])
def get_chat_messages(
    thread_id: str,
    limit: int = 50,
    before: Optional[datetime] = None,
    user: UserOut = Depends(get_current_user),
) -> List[ChatMessageOut]:
    if user.id == "dev":
        return []

    messages = list_thread_messages(thread_id=thread_id, user_id=user.id, limit=limit, before=before)
    return [ChatMessageOut(**message) for message in messages]


@app.post("/chats/{thread_id}/messages", response_model=ChatMessageOut, status_code=201)
async def send_chat_message(
    thread_id: str,
    payload: ChatMessageIn,
    user: UserOut = Depends(get_current_user),
) -> ChatMessageOut:
    if user.id == "dev":
        raise HTTPException(status_code=400, detail="DEV chat is not available")

    await run_in_threadpool(assert_thread_access, thread_id, user.id)
    message = await run_in_threadpool(post_thread_message, thread_id, user.id, payload.text)
    await broadcast_chat_message(thread_id=thread_id, message=message)
    return ChatMessageOut(**message)


def _parse_ws_chat_text(raw_message: str) -> Optional[str]:
    raw = (raw_message or "").strip()
    if not raw:
        return None

    if not raw.startswith("{"):
        return raw

    try:
        payload = json.loads(raw)
    except Exception:
        return raw

    if not isinstance(payload, dict):
        return None

    message_type = str(payload.get("type") or "").lower()
    if message_type == "ping":
        return None

    text_value = payload.get("text")
    if isinstance(text_value, str):
        normalized = text_value.strip()
        return normalized or None

    return None


@app.websocket("/ws/chats/{thread_id}")
async def chat_websocket(thread_id: str, websocket: WebSocket) -> None:
    try:
        user = get_websocket_user(websocket)
    except HTTPException:
        await websocket.close(code=4401)
        return

    if user.id == "dev":
        await websocket.close(code=4403)
        return

    try:
        await run_in_threadpool(assert_thread_access, thread_id, user.id)
    except HTTPException:
        await websocket.close(code=4403)
        return

    await connect_chat_socket(thread_id, websocket)
    try:
        await websocket.send_json({"type": "ready", "thread_id": thread_id})
        while True:
            incoming = await websocket.receive_text()
            text = _parse_ws_chat_text(incoming)
            if text is None:
                await websocket.send_json({"type": "pong"})
                continue

            message = await run_in_threadpool(post_thread_message, thread_id, user.id, text)
            await broadcast_chat_event(
                thread_id=thread_id,
                payload={"type": "message", "payload": message},
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.send_json({"type": "error", "message": "Ошибка чата"})
        except Exception:
            pass
    finally:
        await disconnect_chat_socket(thread_id, websocket)


# ----------------------------
# Reports
# ----------------------------
@app.get("/reports/reason-codes", response_model=List[ReportReasonOptionOut])
def list_report_reason_codes() -> List[ReportReasonOptionOut]:
    return [ReportReasonOptionOut(**item) for item in REPORT_REASON_OPTIONS]


@app.post("/reports", response_model=ReportOut, status_code=201)
def submit_report(
    payload: ReportCreateIn,
    user: UserOut = Depends(get_current_user),
) -> ReportOut:
    canonical_target_type, canonical_target_id, report_meta = _prepare_report_target(
        payload.target_type,
        payload.target_id,
        user.id,
    )

    report_id = create_report(
        reporter_id=user.id,
        target_type=canonical_target_type,
        target_id=canonical_target_id,
        reason_code=(payload.reason_code or "").strip(),
        reason_text=payload.reason_text,
        meta=report_meta,
    )
    row = fetch_one(
        f"""
        SELECT id, reporter_id, target_type, target_id, reason_code, reason_text, status,
               resolution, resolved_by, moderator_comment, created_at, resolved_at
        FROM reports
        WHERE id = %s
        """.replace("status", f"{report_status_select_sql('reports')} AS status", 1),
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create report")
    return _row_to_report(row)


# ----------------------------
# Support chat (public API)
# ----------------------------
@app.get("/support/thread", response_model=SupportThreadOut)
def get_support_thread(user: UserOut = Depends(get_current_user)) -> SupportThreadOut:
    return SupportThreadOut(thread_id=str(get_or_create_support_thread(user.id)))


@app.get("/support/thread/{thread_id}/messages", response_model=List[SupportMessageOut])
def get_support_thread_messages(
    thread_id: str,
    limit: int = 50,
    before: Optional[datetime] = None,
    user: UserOut = Depends(get_current_user),
) -> List[SupportMessageOut]:
    messages = list_support_messages(thread_id=thread_id, user_id=user.id, limit=limit, before=before)
    return [SupportMessageOut(**message) for message in messages]


@app.post("/support/thread/{thread_id}/messages", response_model=SupportMessageOut, status_code=201)
def send_support_thread_message(
    thread_id: str,
    payload: SupportMessageIn,
    user: UserOut = Depends(get_current_user),
) -> SupportMessageOut:
    message = post_support_message(
        thread_id=thread_id,
        sender_id=user.id,
        text=payload.text,
        sender_role=user.role,
    )
    return SupportMessageOut(**message)


# ----------------------------
# Lists
# ----------------------------
@app.get("/announcements/me", response_model=List[AnnouncementOut])
def my_announcements(user: UserOut = Depends(get_current_user)) -> List[AnnouncementOut]:
    rows = fetch_all(
        f"""
        {TASK_ANNOUNCEMENT_SELECT}
        WHERE t.customer_id::text = %s
          AND t.deleted_at IS NULL
        ORDER BY t.created_at DESC
        """,
        (user.id,),
    )
    rows = [_repair_announcement_row_if_needed(row) for row in rows]
    return [_task_row_to_announcement(r) for r in rows]


@app.get("/announcements/public", response_model=List[AnnouncementOut])
def public_announcements(limit: int = 200) -> List[AnnouncementOut]:
    lim = max(1, min(int(limit), 500))
    rows = fetch_all(
        f"""
        {TASK_ANNOUNCEMENT_SELECT}
        WHERE t.deleted_at IS NULL
          AND t.moderation_status = 'published'
          AND t.status IN ('published', 'in_responses')
          AND t.customer_id::text <> 'dev'
          AND NOT EXISTS (
              SELECT 1
              FROM task_assignments ta
              WHERE ta.task_id = t.id
                AND ta.assignment_status IN ('assigned', 'in_progress')
          )
        ORDER BY t.created_at DESC
        LIMIT %s
        """,
        (lim,),
    )
    rows = [_repair_announcement_row_if_needed(row) for row in rows]
    return [_task_row_to_announcement(r) for r in rows]


def _user_can_fetch_announcement(ann_id: str, user_id: str, owner_id: str) -> bool:
    if owner_id == user_id:
        return True

    assignment = fetch_one(
        """
        SELECT 1
        FROM task_assignments
        WHERE task_id::text = %s
          AND performer_id::text = %s
          AND assignment_status IN ('assigned', 'in_progress', 'completed')
        LIMIT 1
        """,
        (ann_id, user_id),
    )
    if assignment:
        return True

    public_row = fetch_one(
        """
        SELECT 1
        FROM tasks t
        WHERE t.id::text = %s
          AND t.deleted_at IS NULL
          AND t.moderation_status = 'published'
          AND t.status IN ('published', 'in_responses')
          AND t.customer_id::text <> 'dev'
          AND NOT EXISTS (
              SELECT 1
              FROM task_assignments ta
              WHERE ta.task_id = t.id
                AND ta.assignment_status IN ('assigned', 'in_progress')
          )
        LIMIT 1
        """,
        (ann_id,),
    )
    return public_row is not None


@app.get("/announcements/{ann_id}", response_model=AnnouncementOut)
def get_announcement(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann = _fetch_announcement_or_404(ann_id)
    if not _user_can_fetch_announcement(ann_id, user.id, ann.user_id):
        raise HTTPException(status_code=404, detail="Announcement not found")
    return ann
