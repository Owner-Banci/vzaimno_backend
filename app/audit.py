from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from app.db import execute
from app.pii import hash_ip


def log_audit_event(
    *,
    actor_type: str,
    action: str,
    target_type: str,
    target_id: str,
    actor_user_account_id: Optional[str] = None,
    actor_admin_account_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    result: str = "success",
    created_at: Any = None,
) -> str:
    audit_id = str(uuid.uuid4())
    payload_details = dict(details or {})
    if "ip_address" in payload_details:
        raw_ip = str(payload_details.get("ip_address") or "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", raw_ip):
            payload_details["ip_address"] = raw_ip
        else:
            payload_details["ip_address"] = hash_ip(raw_ip) or None

    execute(
        """
        INSERT INTO audit_logs (
            id,
            actor_type,
            actor_user_account_id,
            actor_admin_account_id,
            action,
            target_type,
            target_id,
            result,
            details,
            created_at
        )
        VALUES (
            %s::uuid,
            %s,
            %s::uuid,
            %s::uuid,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            COALESCE(%s, now())
        )
        """,
        (
            audit_id,
            actor_type,
            actor_user_account_id,
            actor_admin_account_id,
            action,
            target_type,
            target_id,
            result,
            json.dumps(payload_details, ensure_ascii=False, default=str),
            created_at,
        ),
    )
    return audit_id
