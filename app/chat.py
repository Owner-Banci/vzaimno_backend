from __future__ import annotations

import anyio
import asyncio
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, WebSocket
from fastapi.encoders import jsonable_encoder

from app.db import execute, fetch_all, fetch_one
from app.ops import create_notification
from app.schema_compat import table_has_column
from app.task_compat import normalize_optional_text


class ChatWebSocketHub:
    def __init__(self) -> None:
        self._connections: Dict[str, List[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, thread_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            thread_sockets = self._connections.setdefault(thread_id, [])
            thread_sockets.append(websocket)

    async def disconnect(self, thread_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            thread_sockets = self._connections.get(thread_id)
            if not thread_sockets:
                return

            self._connections[thread_id] = [item for item in thread_sockets if item is not websocket]
            if not self._connections[thread_id]:
                self._connections.pop(thread_id, None)

    async def broadcast(self, thread_id: str, payload: Dict[str, Any]) -> None:
        async with self._lock:
            thread_sockets = list(self._connections.get(thread_id, []))

        if not thread_sockets:
            return

        try:
            serialized_payload = jsonable_encoder(payload)
        except Exception:
            return

        stale: List[WebSocket] = []
        for websocket in thread_sockets:
            try:
                await websocket.send_json(serialized_payload)
            except Exception:
                stale.append(websocket)

        if stale:
            async with self._lock:
                existing = self._connections.get(thread_id, [])
                self._connections[thread_id] = [item for item in existing if item not in stale]
                if not self._connections[thread_id]:
                    self._connections.pop(thread_id, None)


_chat_ws_hub = ChatWebSocketHub()
_chat_user_ws_hub = ChatWebSocketHub()
SYSTEM_CHAT_SENDER_ID = "system"


async def connect_chat_socket(thread_id: str, websocket: WebSocket) -> None:
    await _chat_ws_hub.connect(thread_id, websocket)


async def disconnect_chat_socket(thread_id: str, websocket: WebSocket) -> None:
    await _chat_ws_hub.disconnect(thread_id, websocket)


async def broadcast_chat_event(thread_id: str, payload: Dict[str, Any]) -> None:
    await _chat_ws_hub.broadcast(thread_id, payload)


async def broadcast_chat_message(thread_id: str, message: Dict[str, Any]) -> None:
    await broadcast_chat_event(
        thread_id=thread_id,
        payload={"type": "message", "payload": message},
    )


async def connect_user_chat_socket(user_id: str, websocket: WebSocket) -> None:
    await _chat_user_ws_hub.connect(user_id, websocket)


async def disconnect_user_chat_socket(user_id: str, websocket: WebSocket) -> None:
    await _chat_user_ws_hub.disconnect(user_id, websocket)


async def broadcast_user_chat_event(user_id: str, payload: Dict[str, Any]) -> None:
    await _chat_user_ws_hub.broadcast(user_id, payload)


def publish_chat_message_sync(thread_id: str, message: Dict[str, Any]) -> None:
    try:
        anyio.from_thread.run(broadcast_chat_message, thread_id, message)
    except Exception:
        # websocket-уведомление не должно валить REST-отправку
        return


def publish_thread_preview_sync(thread_id: str, user_id: str | None = None) -> None:
    try:
        if user_id:
            anyio.from_thread.run(broadcast_thread_preview_to_user, thread_id, user_id)
        else:
            anyio.from_thread.run(broadcast_thread_preview_update, thread_id)
    except Exception:
        return


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _user_sender_identity(user_id: str) -> tuple[str, str]:
    row = fetch_one(
        """
        SELECT
            COALESCE(
                NULLIF(BTRIM(up.display_name), ''),
                NULLIF(BTRIM(u.phone), ''),
                NULLIF(BTRIM(u.email), ''),
                'Пользователь'
            ) AS display_name,
            'Пользователь' AS sender_label
        FROM users u
        LEFT JOIN user_profiles up
          ON up.user_id = u.id
        WHERE u.id = %s
        LIMIT 1
        """
        ,
        (user_id,),
    )
    if not row:
        return ("Пользователь", "Пользователь")
    return (str(row[0] or "Пользователь"), str(row[1] or "Пользователь"))


def _message_row_to_dict(row) -> Dict[str, Any]:
    message_type = str(row[5]) if len(row) > 5 and row[5] is not None else "text"
    sender_type = str(row[6]) if len(row) > 6 and row[6] is not None else ("system" if message_type == "system" else "user")
    sender_user_account_id = str(row[7]) if len(row) > 7 and row[7] is not None else None
    sender_admin_account_id = str(row[8]) if len(row) > 8 and row[8] is not None else None
    sender_display_name = str(row[9]) if len(row) > 9 and row[9] is not None else None
    sender_label = str(row[10]) if len(row) > 10 and row[10] is not None else None
    sender_value = SYSTEM_CHAT_SENDER_ID
    if sender_type == "user":
        sender_value = sender_user_account_id or (str(row[2]) if row[2] is not None else None)
    elif sender_type == "admin":
        sender_value = sender_admin_account_id or (str(row[2]) if row[2] is not None else None)
    return {
        "id": str(row[0]),
        "thread_id": str(row[1]),
        "sender_id": sender_value,
        "sender_type": sender_type,
        "sender_user_account_id": sender_user_account_id,
        "sender_admin_account_id": sender_admin_account_id,
        "sender_display_name": sender_display_name,
        "sender_label": sender_label,
        "text": row[3],
        "created_at": row[4],
        "type": message_type,
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


def get_or_create_offer_thread(
    *,
    task_id: str,
    offer_id: str,
    assignment_id: str | None,
    owner_id: str,
    performer_id: str,
) -> str:
    existing = fetch_one(
        """
        SELECT id::text
        FROM chat_threads
        WHERE (%s::uuid IS NOT NULL AND assignment_id = %s::uuid)
           OR offer_id::text = %s
           OR (task_id::text = %s AND kind = %s)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (assignment_id, assignment_id, offer_id, task_id, _offer_thread_kind_value()),
    )
    if existing:
        thread_id = str(existing[0])
        ensure_chat_participant(thread_id, owner_id, "owner")
        ensure_chat_participant(thread_id, performer_id, "performer")
        execute(
            """
            UPDATE chat_threads
            SET task_id = %s,
                offer_id = %s,
                assignment_id = %s
            WHERE id::text = %s
            """,
            (task_id, offer_id, assignment_id, thread_id),
        )
        if table_has_column("task_offers", "chat_thread_id"):
            execute("UPDATE task_offers SET chat_thread_id = %s WHERE id::text = %s", (thread_id, offer_id))
        if assignment_id:
            execute("UPDATE task_assignments SET chat_thread_id = %s WHERE id::text = %s", (thread_id, assignment_id))
        publish_thread_preview_sync(thread_id)
        return thread_id

    thread_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at, assignment_id)
        VALUES (%s, %s, %s, %s, NULL, %s)
        """,
        (thread_id, _offer_thread_kind_value(), task_id, offer_id, assignment_id),
    )
    ensure_chat_participant(thread_id, owner_id, "owner")
    ensure_chat_participant(thread_id, performer_id, "performer")
    if table_has_column("task_offers", "chat_thread_id"):
        execute("UPDATE task_offers SET chat_thread_id = %s WHERE id::text = %s", (thread_id, offer_id))
    if assignment_id:
        execute(
            """
            UPDATE task_assignments
            SET chat_thread_id = %s,
                updated_at = now()
            WHERE id::text = %s
            """,
            (thread_id, assignment_id),
        )
    publish_thread_preview_sync(thread_id)
    return thread_id


def _thread_preview_rows(user_id: str, thread_id: str | None = None) -> List[Dict[str, Any]]:
    params: List[Any] = [user_id, user_id, user_id]
    extra_filter = ""
    if thread_id is not None:
        extra_filter = " AND ct.id::text = %s"
        params.append(thread_id)

    rows = fetch_all(
        f"""
        SELECT
            ct.id::text,
            ct.kind::text,
            partner.partner_id,
            CASE
                WHEN ct.kind = 'support' THEN 'Поддержка Vzaimno'
                ELSE COALESCE(partner.partner_display_name, 'Собеседник')
            END AS partner_display_name,
            partner.partner_avatar_url,
            COALESCE(
                lm.text,
                CASE WHEN ct.kind = 'support' THEN 'Чат с поддержкой открыт' ELSE 'Чат открыт' END
            ) AS last_message_text,
            COALESCE(ct.last_message_at, lm.created_at, ct.created_at) AS last_message_at,
            COALESCE(unread.unread_count, 0) AS unread_count,
            t.id::text AS task_id,
            t.title AS task_title,
            CASE WHEN ct.kind = 'support' THEN TRUE ELSE FALSE END AS is_pinned
        FROM chat_threads ct
        JOIN chat_participants me
          ON me.thread_id = ct.id
         AND me.user_id = %s
         AND me.left_at IS NULL
        LEFT JOIN support_threads st
          ON st.id = ct.id
         AND st.user_account_id = me.user_id
        LEFT JOIN chat_messages read_msg
          ON read_msg.id = me.last_read_message_id
        LEFT JOIN LATERAL (
            SELECT
                cp.user_id::text AS partner_id,
                COALESCE(
                    NULLIF(BTRIM(pup.display_name), ''),
                    NULLIF(BTRIM(pu.phone), ''),
                    NULLIF(BTRIM(pu.email), ''),
                    CASE WHEN ct.kind = 'support' THEN 'Поддержка Vzaimno' ELSE 'Собеседник' END
                ) AS partner_display_name,
                pup.extra->>'avatar_url' AS partner_avatar_url,
                COALESCE(pu.role::text, 'user') AS partner_role
            FROM chat_participants cp
            LEFT JOIN users pu
              ON pu.id = cp.user_id
            LEFT JOIN user_profiles pup
              ON pup.user_id = cp.user_id
            WHERE cp.thread_id = ct.id
              AND cp.user_id <> %s
              AND cp.left_at IS NULL
            ORDER BY
                CASE
                    WHEN ct.kind = 'support'
                     AND COALESCE(pu.role::text, '') IN ('support', 'moderator', 'admin')
                    THEN 0
                    WHEN ct.kind = 'support' THEN 1
                    ELSE 0
                END,
                cp.joined_at ASC,
                cp.user_id ASC
            LIMIT 1
        ) partner ON TRUE
        LEFT JOIN task_assignments ta
          ON ta.id = ct.assignment_id
        LEFT JOIN task_offers tf
          ON tf.id = COALESCE(ct.offer_id, ta.offer_id)
        LEFT JOIN tasks t
          ON t.id = COALESCE(ct.task_id, ta.task_id, tf.task_id)
        LEFT JOIN LATERAL (
            SELECT text, created_at
            FROM chat_messages
            WHERE thread_id = ct.id
              AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) lm ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS unread_count
            FROM chat_messages m
            WHERE m.thread_id = ct.id
              AND m.deleted_at IS NULL
              AND (
                    COALESCE(m.sender_type, CASE WHEN COALESCE(m.type::text, 'text') = 'system' THEN 'system' ELSE 'user' END) = 'system'
                    OR COALESCE(m.sender_type, 'user') = 'admin'
                    OR COALESCE(m.sender_user_account_id::text, m.sender_id::text, '') <> %s
              )
              AND (
                    me.last_read_message_id IS NULL
                    OR m.created_at > COALESCE(read_msg.created_at, 'epoch'::timestamptz)
                    OR (
                        m.created_at = COALESCE(read_msg.created_at, 'epoch'::timestamptz)
                        AND m.id::text > COALESCE(me.last_read_message_id::text, '')
                    )
                  )
        ) unread ON TRUE
        WHERE ct.archived_at IS NULL
          AND (t.id IS NULL OR t.deleted_at IS NULL)
          AND (
                ct.kind <> 'support'
                OR (
                    st.id IS NOT NULL
                    AND st.closed_at IS NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM support_threads st2
                        JOIN chat_threads ct2
                          ON ct2.id = st2.id
                        WHERE st2.user_account_id = me.user_id
                          AND st2.closed_at IS NULL
                          AND ct2.archived_at IS NULL
                          AND (
                                COALESCE(ct2.last_message_at, st2.updated_at, ct2.created_at)
                                    > COALESCE(ct.last_message_at, st.updated_at, ct.created_at)
                                OR (
                                    COALESCE(ct2.last_message_at, st2.updated_at, ct2.created_at)
                                        = COALESCE(ct.last_message_at, st.updated_at, ct.created_at)
                                    AND ct2.id::text > ct.id::text
                                )
                              )
                    )
                )
              )
          {extra_filter}
        ORDER BY
            CASE WHEN ct.kind = 'support' THEN 0 ELSE 1 END,
            COALESCE(ct.last_message_at, lm.created_at, ct.created_at) DESC,
            ct.created_at DESC
        """,
        tuple(params),
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
            "is_pinned": bool(row[10]),
        }
        for row in rows
    ]


async def broadcast_thread_preview_update(thread_id: str) -> None:
    participants = fetch_all(
        """
        SELECT user_id::text
        FROM chat_participants
        WHERE thread_id::text = %s
          AND left_at IS NULL
        """,
        (thread_id,),
    )
    for row in participants:
        user_id = str(row[0])
        preview = _thread_preview_rows(user_id=user_id, thread_id=thread_id)
        if not preview:
            continue
        await broadcast_user_chat_event(user_id=user_id, payload={"type": "thread_upsert", "payload": preview[0]})


async def broadcast_thread_preview_to_user(thread_id: str, user_id: str) -> None:
    preview = _thread_preview_rows(user_id=user_id, thread_id=thread_id)
    if not preview:
        return
    await broadcast_user_chat_event(user_id=user_id, payload={"type": "thread_upsert", "payload": preview[0]})


def list_user_threads(user_id: str) -> List[Dict[str, Any]]:
    return _thread_preview_rows(user_id=user_id)


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
        SELECT
            id,
            thread_id,
            sender_id,
            text,
            created_at,
            type,
            sender_type,
            sender_user_account_id,
            sender_admin_account_id,
            sender_display_name,
            sender_label
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
        publish_thread_preview_sync(thread_id, user_id=user_id)

    return [_message_row_to_dict(row) for row in ordered]


def _fetch_chat_message_row(message_id: str):
    return fetch_one(
        """
        SELECT
            id,
            thread_id,
            sender_id,
            text,
            created_at,
            type,
            sender_type,
            sender_user_account_id,
            sender_admin_account_id,
            sender_display_name,
            sender_label
        FROM chat_messages
        WHERE id = %s
        """,
        (message_id,),
    )


def post_thread_message(thread_id: str, sender_id: str, text: str) -> Dict[str, Any]:
    assert_thread_access(thread_id, sender_id)

    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    sender_display_name, sender_label = _user_sender_identity(sender_id)
    message_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_messages (
            id,
            thread_id,
            sender_id,
            type,
            text,
            sender_type,
            sender_user_account_id,
            sender_admin_account_id,
            sender_display_name,
            sender_label
        )
        VALUES (%s, %s, %s, 'text', %s, 'user', %s, NULL, %s, %s)
        """,
        (message_id, thread_id, sender_id, clean_text, sender_id, sender_display_name, sender_label),
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

    row = _fetch_chat_message_row(message_id)
    if not row:
        raise HTTPException(status_code=500, detail="Message was not saved")

    message = _message_row_to_dict(row)
    publish_thread_preview_sync(thread_id)
    return message


def post_system_thread_message(thread_id: str, text: str) -> Dict[str, Any]:
    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    message_id = str(uuid.uuid4())
    execute(
        """
        INSERT INTO chat_messages (
            id,
            thread_id,
            sender_id,
            type,
            text,
            sender_type,
            sender_user_account_id,
            sender_admin_account_id,
            sender_display_name,
            sender_label
        )
        VALUES (%s, %s, NULL, 'system', %s, 'system', NULL, NULL, 'Система', 'Система')
        """,
        (message_id, thread_id, clean_text),
    )
    execute(
        """
        UPDATE chat_threads
        SET last_message_at = now()
        WHERE id = %s
        """,
        (thread_id,),
    )

    recipients = fetch_all(
        """
        SELECT user_id
        FROM chat_participants
        WHERE thread_id = %s
          AND left_at IS NULL
        """,
        (thread_id,),
    )
    for row in recipients:
        create_notification(
            user_id=row[0],
            notif_type="chat_system",
            body=clean_text,
            payload={"thread_id": thread_id, "message_id": message_id, "sender_id": SYSTEM_CHAT_SENDER_ID},
        )

    row = _fetch_chat_message_row(message_id)
    if not row:
        raise HTTPException(status_code=500, detail="System message was not saved")

    message = _message_row_to_dict(row)
    publish_chat_message_sync(thread_id, message)
    publish_thread_preview_sync(thread_id)
    return message
