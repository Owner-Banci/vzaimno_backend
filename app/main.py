from __future__ import annotations

import json
import os
import uuid
import itsdangerous
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.bootstrap import ensure_all_tables
from app.db import execute, fetch_all, fetch_one
from app.geocoding import geocode_address
from app.moderation_image import get_nsfw_detector
from app.moderation_text import classify_text
from app.ops import create_report, ensure_appeal_report, report_status_select_sql
from app.schemas import (
    AnnouncementOut,
    AppealIn,
    CreateAnnouncementIn,
    LoginIn,
    RegisterIn,
    ReportCreateIn,
    ReportOut,
    SupportMessageIn,
    SupportMessageOut,
    SupportThreadOut,
    TokenOut,
    UserOut,
)
from app.security import create_access_token, decode_token, hash_password, verify_password
from app.support import get_or_create_support_thread, list_support_messages, post_support_message

app = FastAPI(title="Slayma Backend (MVP)")
bearer = HTTPBearer(auto_error=True)

# ----------------------------
# Statuses (string enum in DB)
# ----------------------------
STATUS_PENDING = "pending_review"
STATUS_NEEDS_FIX = "needs_fix"
STATUS_REJECTED = "rejected"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

# ----------------------------
# Upload config
# ----------------------------
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "uploads"))
NSFW_REVIEW = float(os.getenv("NSFW_REVIEW", "0.30"))
NSFW_HARD_BLOCK = float(os.getenv("NSFW_HARD_BLOCK", "0.85"))

app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR), check_dir=False), name="uploads")


@app.on_event("startup")
def ensure_tables() -> None:
    ensure_all_tables()


# ----------------------------
# Auth
# ----------------------------
def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> UserOut:
    token = creds.credentials

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
# Helpers (announcements / moderation)
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


def _row_to_report(row) -> ReportOut:
    return ReportOut(
        id=row[0],
        reporter_id=row[1],
        target_type=row[2],
        target_id=row[3],
        reason_code=row[4],
        reason_text=row[5],
        status=row[6],
        resolution=row[7],
        resolved_by=row[8],
        moderator_comment=row[9],
        created_at=row[10],
        resolved_at=row[11],
    )


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


def _save_upload(ann_id: str, file: UploadFile, content: bytes) -> str:
    safe_name = (file.filename or "image").replace("/", "_").replace("\\", "_")
    folder = UPLOADS_DIR / ann_id
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{uuid.uuid4().hex}_{safe_name}"
    out.write_bytes(content)
    return str(out)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------
# Announcements (Create)
# ----------------------------
@app.post("/announcements", response_model=AnnouncementOut, status_code=201)
def create_announcement(
    payload: CreateAnnouncementIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    ann_id = str(uuid.uuid4())

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
        status = STATUS_PENDING
        _set_decision(mod, status, "На проверке: сначала проверим фото, затем объявление появится на карте.")

    mod["image"] = {
        "max_nsfw": None,
        "items": [],
        "can_appeal": None,
        "review_thr": NSFW_REVIEW,
        "hard_block_thr": NSFW_HARD_BLOCK,
    }

    if suggestions:
        _set_suggestions(mod, suggestions)

    _set_mod(data, mod)

    execute(
        """
        INSERT INTO announcements (id, user_id, category, title, status, data)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (ann_id, user.id, payload.category, title, status, json.dumps(data, ensure_ascii=False)),
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


# ----------------------------
# Announcements (Media)
# ----------------------------
@app.post("/announcements/{ann_id}/media", response_model=AnnouncementOut)
def upload_announcement_media(
    ann_id: str,
    files: List[UploadFile] = File(...),
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    row = fetch_one(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE id=%s AND deleted_at IS NULL
        """,
        (ann_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Announcement not found")

    ann = _row_to_announcement(row)
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

    execute(
        "UPDATE announcements SET status=%s, data=%s::jsonb, updated_at=now() WHERE id=%s",
        (new_status, json.dumps(data_obj, ensure_ascii=False), ann_id),
    )

    updated = fetch_one(
        "SELECT id, user_id, category, title, status, data, created_at FROM announcements WHERE id=%s",
        (ann_id,),
    )
    return _row_to_announcement(updated)


# ----------------------------
# Announcements (Appeal / Archive / Delete)
# ----------------------------
@app.post("/announcements/{ann_id}/appeal", response_model=AnnouncementOut)
def appeal_announcement(
    ann_id: str,
    payload: AppealIn,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    row = fetch_one(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE id = %s AND deleted_at IS NULL
        """,
        (ann_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Announcement not found")

    ann = _row_to_announcement(row)
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

    execute(
        """
        UPDATE announcements
        SET status = %s,
            data = %s::jsonb,
            updated_at = now()
        WHERE id = %s
        """,
        (STATUS_PENDING, json.dumps(data_obj, ensure_ascii=False), ann_id),
    )
    ensure_appeal_report(user.id, ann_id, payload.reason)

    updated = fetch_one(
        "SELECT id, user_id, category, title, status, data, created_at FROM announcements WHERE id = %s",
        (ann_id,),
    )
    return _row_to_announcement(updated)


@app.patch("/announcements/{ann_id}/archive", response_model=AnnouncementOut)
def archive_announcement(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> AnnouncementOut:
    row = fetch_one(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE id=%s AND deleted_at IS NULL
        """,
        (ann_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Announcement not found")

    ann = _row_to_announcement(row)
    if ann.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    execute("UPDATE announcements SET status=%s, updated_at=now() WHERE id=%s", (STATUS_ARCHIVED, ann_id))

    updated = fetch_one(
        "SELECT id, user_id, category, title, status, data, created_at FROM announcements WHERE id=%s",
        (ann_id,),
    )
    return _row_to_announcement(updated)


@app.delete("/announcements/{ann_id}")
def delete_announcement(
    ann_id: str,
    user: UserOut = Depends(get_current_user),
) -> Dict[str, bool]:
    row = fetch_one(
        "SELECT id, user_id FROM announcements WHERE id=%s AND deleted_at IS NULL",
        (ann_id,),
    )
    if not row:
        return {"ok": True}

    if row[1] != user.id:
        raise HTTPException(status_code=403, detail="Not your announcement")

    execute("UPDATE announcements SET deleted_at=now(), updated_at=now() WHERE id=%s", (ann_id,))
    return {"ok": True}


# ----------------------------
# Reports
# ----------------------------
@app.post("/reports", response_model=ReportOut, status_code=201)
def submit_report(
    payload: ReportCreateIn,
    user: UserOut = Depends(get_current_user),
) -> ReportOut:
    allowed_targets = {"announcement", "message", "user", "task"}
    if payload.target_type not in allowed_targets:
        raise HTTPException(status_code=400, detail="Unsupported target type")

    report_id = create_report(
        reporter_id=user.id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
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
    return SupportThreadOut(thread_id=get_or_create_support_thread(user.id))


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
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE user_id = %s AND deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (user.id,),
    )
    return [_row_to_announcement(r) for r in rows]


@app.get("/announcements/public", response_model=List[AnnouncementOut])
def public_announcements(limit: int = 200) -> List[AnnouncementOut]:
    lim = max(1, min(int(limit), 500))
    rows = fetch_all(
        """
        SELECT id, user_id, category, title, status, data, created_at
        FROM announcements
        WHERE deleted_at IS NULL AND status = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (STATUS_ACTIVE, lim),
    )
    return [_row_to_announcement(r) for r in rows]
