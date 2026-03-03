from __future__ import annotations

import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.db import execute, fetch_all, fetch_one
from app.ops import create_notification
from app.schema_compat import table_has_column


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _message_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "thread_id": str(row[1]),
        "sender_id": str(row[2]) if row[2] is not None else None,
        "text": row[3],
        "created_at": row[4],
    }


def _avatar_select_sql(alias: str) -> str:
    prefix = f"{alias}." if alias else ""
    if table_has_column("user_profiles", "extra"):
        return f"{prefix}extra->>'avatar_url' AS avatar_url"
    return "NULL AS avatar_url"


@lru_cache(maxsize=1)
def _chat_threads_offer_fk_target() -> Optional[str]:
    rows = fetch_all(
        """
        SELECT ccu.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = 'chat_threads'
          AND tc.constraint_type = 'FOREIGN KEY'
          AND kcu.column_name = 'offer_id'
        """
    )
    targets = [str(row[0]) for row in rows if row and row[0]]
    return targets[0] if targets else None


def _can_store_announcement_offer_id_in_chat_thread() -> bool:
    target = _chat_threads_offer_fk_target()
    return target in (None, "announcement_offers")


def _announcement_offer_chat_thread_id_sql() -> str:
    if table_has_column("announcement_offers", "chat_thread_id"):
        return "ao.chat_thread_id::text = ct.id::text OR "
    return ""


@lru_cache(maxsize=1)
def _offer_thread_kind_value() -> str:
    row = fetch_one(
        """
        SELECT data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_threads'
          AND column_name = 'kind'
        """,
    )
    if not row:
        return "offer"

    data_type = str(row[0] or "").lower()
    udt_name = str(row[1] or "").lower()
    if data_type != "user-defined" or udt_name != "chat_thread_kind":
        return "offer"

    labels = {
        str(item[0]).lower()
        for item in fetch_all(
            """
            SELECT enumlabel
            FROM pg_type t
            JOIN pg_enum e
              ON t.oid = e.enumtypid
            WHERE t.typname = 'chat_thread_kind'
            ORDER BY e.enumsortorder
            """
        )
    }

    if "offer" in labels:
        return "offer"
    if "task" in labels:
        return "task"
    if "system" in labels:
        return "system"
    return "offer"


def ensure_chat_participant(thread_id: str, user_id: str, role: str) -> None:
    if table_has_column("chat_participants", "role"):
        execute(
            """
            INSERT INTO chat_participants (thread_id, user_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (thread_id, user_id)
            DO UPDATE SET role = EXCLUDED.role, left_at = NULL
            """,
            (thread_id, user_id, role),
        )
        return

    execute(
        """
        INSERT INTO chat_participants (thread_id, user_id)
        VALUES (%s, %s)
        ON CONFLICT (thread_id, user_id)
        DO UPDATE SET left_at = NULL
        """,
        (thread_id, user_id),
    )


def assert_thread_access(thread_id: str, user_id: str) -> None:
    row = fetch_one(
        """
        SELECT 1
        FROM chat_participants
        WHERE thread_id = %s
          AND user_id = %s
          AND left_at IS NULL
        """,
        (thread_id, user_id),
    )
    if not row:
        raise HTTPException(status_code=403, detail="Thread access denied")


def get_or_create_offer_thread(offer_id: str, owner_id: str, performer_id: str) -> str:
    if table_has_column("announcement_offers", "chat_thread_id"):
        mapped = fetch_one(
            """
            SELECT chat_thread_id
            FROM announcement_offers
            WHERE id = %s
              AND chat_thread_id IS NOT NULL
            """,
            (offer_id,),
        )
        if mapped and mapped[0]:
            thread_id = str(mapped[0])
            ensure_chat_participant(thread_id, owner_id, "owner")
            ensure_chat_participant(thread_id, performer_id, "performer")
            return thread_id

    existing = fetch_one(
        """
        SELECT id
        FROM chat_threads
        WHERE offer_id::text = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (offer_id,),
    )
    if existing:
        thread_id = existing[0]
        ensure_chat_participant(thread_id, owner_id, "owner")
        ensure_chat_participant(thread_id, performer_id, "performer")
        if table_has_column("announcement_offers", "chat_thread_id"):
            execute(
                """
                UPDATE announcement_offers
                SET chat_thread_id = %s
                WHERE id = %s
                """,
                (thread_id, offer_id),
            )
        return thread_id

    thread_id = str(uuid.uuid4())
    kind = _offer_thread_kind_value()
    if _can_store_announcement_offer_id_in_chat_thread():
        execute(
            """
            INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at)
            VALUES (%s, %s, NULL, %s, NULL)
            """,
            (thread_id, kind, offer_id),
        )
    else:
        execute(
            """
            INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at)
            VALUES (%s, %s, NULL, NULL, NULL)
            """,
            (thread_id, kind),
        )

    ensure_chat_participant(thread_id, owner_id, "owner")
    ensure_chat_participant(thread_id, performer_id, "performer")
    if table_has_column("announcement_offers", "chat_thread_id"):
        execute(
            """
            UPDATE announcement_offers
            SET chat_thread_id = %s
            WHERE id = %s
            """,
            (thread_id, offer_id),
        )
    return thread_id


def list_user_threads(user_id: str) -> List[Dict[str, Any]]:
    rows = fetch_all(
        f"""
        SELECT
            ct.id,
            ct.kind,
            other.user_id::text,
            COALESCE(
                NULLIF(BTRIM(up.display_name), ''),
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), ''),
                'Собеседник'
            ) AS partner_display_name,
            {_avatar_select_sql("up")},
            COALESCE(lm.text, 'Чат открыт'),
            COALESCE(ct.last_message_at, lm.created_at, ct.created_at),
            0 AS unread_count,
            ann.id::text,
            ann.title
        FROM chat_threads ct
        JOIN chat_participants me
          ON me.thread_id = ct.id
         AND me.user_id = %s
         AND me.left_at IS NULL
        LEFT JOIN chat_participants other
          ON other.thread_id = ct.id
         AND other.user_id <> %s
         AND other.left_at IS NULL
        LEFT JOIN users u
          ON u.id = other.user_id
        LEFT JOIN user_profiles up
          ON up.user_id = other.user_id
        LEFT JOIN announcement_offers ao
          ON (
                {_announcement_offer_chat_thread_id_sql()}
                ao.id::text = ct.offer_id::text
             )
        LEFT JOIN announcements ann
          ON ann.id = ao.announcement_id
        LEFT JOIN LATERAL (
            SELECT text, created_at
            FROM chat_messages
            WHERE thread_id = ct.id
              AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) lm ON TRUE
        WHERE ct.kind <> 'support'
        ORDER BY COALESCE(ct.last_message_at, lm.created_at, ct.created_at) DESC, ct.created_at DESC
        """,
        (user_id, user_id),
    )

    return [
        {
            "thread_id": str(row[0]),
            "kind": str(row[1]),
            "partner_id": row[2],
            "partner_display_name": row[3],
            "partner_avatar_url": row[4],
            "last_message_text": row[5],
            "last_message_at": row[6],
            "unread_count": int(row[7] or 0),
            "announcement_id": row[8],
            "announcement_title": row[9],
        }
        for row in rows
    ]


def list_thread_messages(
    thread_id: str,
    user_id: str,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    assert_thread_access(thread_id, user_id)

    lim = max(1, min(int(limit), 100))
    params: List[Any] = [thread_id]
    sql = """
        SELECT id, thread_id, sender_id, text, created_at
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
            WHERE thread_id = %s
              AND user_id = %s
            """,
            (ordered[-1][0], thread_id, user_id),
        )

    return [_message_row_to_dict(row) for row in ordered]


def post_thread_message(thread_id: str, sender_id: str, text: str) -> Dict[str, Any]:
    assert_thread_access(thread_id, sender_id)

    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    message_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_messages (id, thread_id, sender_id, type, text)
        VALUES (%s, %s, %s, 'text', %s)
        """,
        (message_id, thread_id, sender_id, clean_text),
    )
    execute(
        """
        UPDATE chat_threads
        SET last_message_at = now()
        WHERE id = %s
        """,
        (thread_id,),
    )
    execute(
        """
        UPDATE chat_participants
        SET last_read_message_id = %s
        WHERE thread_id = %s
          AND user_id = %s
        """,
        (message_id, thread_id, sender_id),
    )

    recipients = fetch_all(
        """
        SELECT user_id
        FROM chat_participants
        WHERE thread_id = %s
          AND user_id <> %s
          AND left_at IS NULL
        """,
        (thread_id, sender_id),
    )
    for row in recipients:
        create_notification(
            user_id=row[0],
            notif_type="chat",
            body=clean_text,
            payload={"thread_id": thread_id, "message_id": message_id},
        )

    row = fetch_one(
        """
        SELECT id, thread_id, sender_id, text, created_at
        FROM chat_messages
        WHERE id = %s
        """,
        (message_id,),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Message was not saved")

    return _message_row_to_dict(row)
