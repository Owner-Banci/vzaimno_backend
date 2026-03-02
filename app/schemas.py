# app/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str


# ----------------------------
# Announcements (Ads)
# ----------------------------
class CreateAnnouncementIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    status: str = Field(default="active", max_length=32)
    data: Dict[str, Any] = Field(default_factory=dict)


class AnnouncementOut(BaseModel):
    id: str
    user_id: str
    category: str
    title: str
    status: str
    data: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Moderation ---
class AppealIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class ReportCreateIn(BaseModel):
    target_type: str = Field(..., min_length=1, max_length=32)
    target_id: str = Field(..., min_length=1, max_length=128)
    reason_code: str = Field(..., min_length=1, max_length=64)
    reason_text: Optional[str] = Field(default=None, max_length=2000)


class ReportOut(BaseModel):
    id: str
    reporter_id: str
    target_type: str
    target_id: str
    reason_code: str
    reason_text: Optional[str] = None
    status: str
    resolution: Optional[str] = None
    resolved_by: Optional[str] = None
    moderator_comment: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None


class SupportThreadOut(BaseModel):
    thread_id: str


class SupportMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class SupportMessageOut(BaseModel):
    id: str
    thread_id: str
    sender_id: str
    type: str
    text: str
    is_blocked: bool = False
    blocked_reason: Optional[str] = None
    created_at: datetime
    edited_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class TextCheckIn(BaseModel):
    text: str

class TextCheckOut(BaseModel):
    label: str
    reason: str = ""
    t: float | None = None

class MediaUploadOut(BaseModel):
    announcement: AnnouncementOut
    max_nsfw: float
    decision: str  # "active" | "draft"
    can_appeal: bool
    message: str
