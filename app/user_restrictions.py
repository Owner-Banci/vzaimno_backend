from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException

from app.db import fetch_all


GLOBAL_WRITE_RESTRICTIONS = {"temporary_ban", "permanent_ban", "custom", "custom_restriction", "shadowban"}

ACTION_RESTRICTIONS = {
    "posting": {"restrict_posting"},
    "offers": {"restrict_offers"},
    "chat": {"mute_chat"},
    "support": {"mute_chat"},
    "uploads": {"restrict_posting"},
    "reports": set(),
    "disputes": set(),
}

RESTRICTION_MESSAGES = {
    "mute_chat": "Chat messages are temporarily restricted",
    "restrict_posting": "Posting is temporarily restricted",
    "restrict_offers": "Offers are temporarily restricted",
    "temporary_ban": "Account is temporarily banned",
    "permanent_ban": "Account is permanently banned",
    "custom": "Account actions are restricted",
    "custom_restriction": "Account actions are restricted",
    "shadowban": "Account actions are restricted",
}


def _normalize_restriction_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "custom_restriction":
        return "custom"
    return normalized


def active_user_restriction_types(user_id: str) -> set[str]:
    if not user_id or user_id == "dev":
        return set()

    rows = fetch_all(
        """
        SELECT type
        FROM user_restrictions
        WHERE user_id::text = %s
          AND status = 'active'
          AND revoked_at IS NULL
          AND starts_at <= now()
          AND (ends_at IS NULL OR ends_at > now())
        """,
        (user_id,),
    )
    return {_normalize_restriction_type(row[0]) for row in rows if row and row[0]}


def assert_user_action_allowed(user_id: str, action: str) -> None:
    active_types = active_user_restriction_types(user_id)
    blocked_by = GLOBAL_WRITE_RESTRICTIONS | ACTION_RESTRICTIONS.get(action, set())
    matched = sorted(active_types & blocked_by)
    if not matched:
        return

    restriction_type = matched[0]
    raise HTTPException(
        status_code=403,
        detail=RESTRICTION_MESSAGES.get(restriction_type, "Account action is restricted"),
    )


def assert_user_actions_allowed(user_id: str, actions: Iterable[str]) -> None:
    for action in actions:
        assert_user_action_allowed(user_id, action)
