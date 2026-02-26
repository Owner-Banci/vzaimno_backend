# app/schemas.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

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