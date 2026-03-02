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


class GeoPointOut(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class CurrentUserDetailsOut(BaseModel):
    id: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    created_at: datetime


class UserProfileOut(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    city: Optional[str] = None
    preferred_address: Optional[str] = None
    home_location: Optional[GeoPointOut] = None


class UserStatsOut(BaseModel):
    rating_avg: float = 0.0
    rating_count: int = 0
    completed_count: int = 0
    cancelled_count: int = 0


class MeProfileOut(BaseModel):
    user: CurrentUserDetailsOut
    profile: UserProfileOut
    stats: UserStatsOut


class UpdateMyProfileIn(BaseModel):
    display_name: str = Field(..., min_length=2, max_length=80)
    bio: Optional[str] = Field(default=None, max_length=300)
    city: Optional[str] = Field(default=None, max_length=120)
    preferred_address: Optional[str] = Field(default=None, max_length=180)
    home_location: Optional[GeoPointOut] = None


class UserReviewOut(BaseModel):
    from_user_display_name: str
    stars: int = Field(..., ge=1, le=5)
    text: Optional[str] = None
    created_at: datetime


class UserReviewListOut(BaseModel):
    items: list[UserReviewOut] = Field(default_factory=list)


class DeviceRegisterIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=200)
    platform: str = Field(..., min_length=1, max_length=32)
    push_token: Optional[str] = Field(default=None, max_length=500)
    locale: Optional[str] = Field(default=None, max_length=50)
    timezone: Optional[str] = Field(default=None, max_length=120)
    device_name: Optional[str] = Field(default=None, max_length=120)


class DeviceUnregisterIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=200)
    push_token: Optional[str] = Field(default=None, max_length=500)


class OKOut(BaseModel):
    ok: bool = True


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
    decision: str
    can_appeal: bool
    message: str
