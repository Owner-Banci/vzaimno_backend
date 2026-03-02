from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class AnnouncementDecisionReason(BaseModel):
    field: str = Field(..., min_length=1, max_length=64)
    code: str = Field(..., min_length=1, max_length=64)
    details: str = Field(..., min_length=1, max_length=2000)
    can_appeal: bool = True


class AnnouncementDecisionIn(BaseModel):
    decision: Literal["approve", "needs_fix", "reject", "archive", "delete"]
    message: str = Field(..., min_length=1, max_length=2000)
    reasons: list[AnnouncementDecisionReason] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class SupportReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class ReportResolutionIn(BaseModel):
    resolution: Literal["valid", "invalid"]
    moderator_comment: Optional[str] = Field(default=None, max_length=2000)


class RestrictionCreateIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    type: Literal["warning", "ban", "shadowban"]
    ends_at: Optional[datetime] = None
    comment: Optional[str] = Field(default=None, max_length=2000)


class RestrictionRevokeIn(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


class RoleUpdateIn(BaseModel):
    role: Literal["user", "support", "moderator", "admin"]


class StaffUserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
