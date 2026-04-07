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
    resolution: Literal[
        "no_action",
        "warning",
        "mute_chat",
        "restrict_posting",
        "restrict_offers",
        "temporary_ban",
        "permanent_ban",
        "custom_restriction",
        "report_rejected",
        "valid",
        "invalid",
    ]
    moderator_comment: Optional[str] = Field(default=None, max_length=2000)
    ends_at: Optional[datetime] = None
    custom_restriction_label: Optional[str] = Field(default=None, max_length=120)


class RestrictionCreateIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    type: Literal[
        "warning",
        "mute_chat",
        "restrict_posting",
        "restrict_offers",
        "temporary_ban",
        "permanent_ban",
        "custom",
        "shadowban",
        "publish_ban",
        "response_ban",
        "temp_ban",
        "perm_ban",
    ]
    ends_at: Optional[datetime] = None
    comment: Optional[str] = Field(default=None, max_length=2000)
    source_type: Optional[Literal["manual", "report", "moderation"]] = "manual"
    source_id: Optional[str] = Field(default=None, max_length=128)
    custom_label: Optional[str] = Field(default=None, max_length=120)


class RestrictionRevokeIn(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


class RestrictionExtendIn(BaseModel):
    ends_at: datetime
    comment: Optional[str] = Field(default=None, max_length=2000)


class AdminLoginIn(BaseModel):
    login_identifier: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


class AdminTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    principal_type: str = "admin"
    admin_account_id: str
    role: str
    display_name: str


class AdminAccessCreateIn(BaseModel):
    login_identifier: str = Field(..., min_length=3, max_length=255)
    display_name: str = Field(..., min_length=2, max_length=120)
    role: Literal["support", "moderator", "admin"] = "support"
    password: str = Field(..., min_length=8, max_length=255)
    email: Optional[EmailStr] = None


class AdminAccessResetIn(BaseModel):
    password: str = Field(..., min_length=8, max_length=255)


class SupportAssignmentIn(BaseModel):
    assigned_admin_account_id: str = Field(..., min_length=1, max_length=128)


class StaffUserOut(BaseModel):
    id: str
    login_identifier: str
    email: Optional[EmailStr] = None
    role: str
    display_name: str
    linked_user_account_id: Optional[str] = None
