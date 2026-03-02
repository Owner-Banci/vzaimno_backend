from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.db import execute, fetch_all, fetch_one
from app.ops import create_notification


STAFF_ROLES = ("admin", "moderator", "support")


def _message_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "thread_id": row[1],
        "sender_id": row[2],
        "type": row[3],
        "text": row[4],
        "is_blocked": bool(row[5]),
        "blocked_reason": row[6],
        "created_at": row[7],
        "edited_at": row[8],
        "deleted_at": row[9],
    }


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _pick_default_staff_user_id() -> Optional[str]:
    row = fetch_one(
        """
        SELECT id
        FROM users
        WHERE role IN ('support', 'admin', 'moderator')
        ORDER BY CASE role
            WHEN 'support' THEN 0
            WHEN 'admin' THEN 1
            ELSE 2
        END, created_at ASC
        LIMIT 1
        """
    )
    return row[0] if row else None


def ensure_support_participant(thread_id: str, user_id: str, role: str) -> None:
    execute(
        """
        INSERT INTO chat_participants (thread_id, user_id, role)
        VALUES (%s, %s, %s)
        ON CONFLICT (thread_id, user_id)
        DO UPDATE SET role = EXCLUDED.role, left_at = NULL
        """,
        (thread_id, user_id, role),
    )


def get_or_create_support_thread(user_id: str) -> str:
    existing = fetch_one(
        """
        SELECT ct.id
        FROM chat_threads ct
        JOIN chat_participants cp
          ON cp.thread_id = ct.id
        WHERE ct.kind = 'support'
          AND cp.user_id = %s
          AND cp.role = 'user'
          AND cp.left_at IS NULL
        ORDER BY ct.created_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if existing:
        return existing[0]

    thread_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at)
        VALUES (%s, 'support', NULL, NULL, NULL)
        """,
        (thread_id,),
    )
    ensure_support_participant(thread_id, user_id, "user")

    staff_id = _pick_default_staff_user_id()
    if staff_id and staff_id != user_id:
        ensure_support_participant(thread_id, staff_id, "support")

    return thread_id


def assert_support_access(thread_id: str, user_id: str) -> None:
    row = fetch_one(
        """
        SELECT 1
        FROM chat_threads ct
        JOIN chat_participants cp
          ON cp.thread_id = ct.id
        WHERE ct.id = %s
          AND ct.kind = 'support'
          AND cp.user_id = %s
          AND cp.left_at IS NULL
        """,
        (thread_id, user_id),
    )
    if not row:
        raise HTTPException(status_code=403, detail="Thread access denied")


def list_support_messages(
    thread_id: str,
    user_id: str,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    assert_support_access(thread_id, user_id)

    lim = max(1, min(int(limit), 100))
    params: List[Any] = [thread_id]
    sql = """
        SELECT id, thread_id, sender_id, type, text, is_blocked, blocked_reason, created_at, edited_at, deleted_at
        FROM chat_messages
        WHERE thread_id = %s
          AND deleted_at IS NULL
    """
    if before is not None:
        sql += " AND created_at < %s"
        params.append(before)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(lim)

    rows = fetch_all(sql, tuple(params))
    ordered = list(reversed(rows))
    if ordered:
        execute(
            """
            UPDATE chat_participants
            SET last_read_message_id = %s
            WHERE thread_id = %s AND user_id = %s
            """,
            (ordered[-1][0], thread_id, user_id),
        )
    return [_message_row_to_dict(row) for row in ordered]


def post_support_message(
    thread_id: str,
    sender_id: str,
    text: str,
    sender_role: str,
) -> Dict[str, Any]:
    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    assert_support_access(thread_id, sender_id)

    message_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_messages (id, thread_id, sender_id, type, text)
        VALUES (%s, %s, %s, 'text', %s)
        """,
        (message_id, thread_id, sender_id, clean_text),
    )
    execute(
        "UPDATE chat_threads SET last_message_at = now() WHERE id = %s",
        (thread_id,),
    )
    execute(
        """
        UPDATE chat_participants
        SET last_read_message_id = %s
        WHERE thread_id = %s AND user_id = %s
        """,
        (message_id, thread_id, sender_id),
    )

    if sender_role in STAFF_ROLES:
        user_participant = fetch_one(
            """
            SELECT user_id
            FROM chat_participants
            WHERE thread_id = %s
              AND role = 'user'
              AND left_at IS NULL
            LIMIT 1
            """,
            (thread_id,),
        )
        if user_participant:
            create_notification(
                user_id=user_participant[0],
                notif_type="support",
                body=clean_text,
                payload={"thread_id": thread_id, "message_id": message_id},
            )

    row = fetch_one(
        """
        SELECT id, thread_id, sender_id, type, text, is_blocked, blocked_reason, created_at, edited_at, deleted_at
        FROM chat_messages
        WHERE id = %s
        """,
        (message_id,),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Message was not saved")
    return _message_row_to_dict(row)
