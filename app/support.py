from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.audit import log_audit_event
from app.chat import publish_chat_message_sync, publish_thread_preview_sync
from app.db import execute, fetch_all, fetch_one
from app.ops import create_notification
from app.pii import decrypt_phone_expr
from app.schema_compat import table_has_column


ADMIN_ACCESS_ROLES = ("support", "moderator", "admin")


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _admin_sender_label(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized == "admin":
        return "Администратор"
    if normalized == "moderator":
        return "Модератор"
    return "Поддержка"


def _message_row_to_dict(row) -> Dict[str, Any]:
    sender_type = str(row[10]) if row[10] is not None else "user"
    sender_user_account_id = str(row[11]) if row[11] is not None else None
    sender_admin_account_id = str(row[12]) if row[12] is not None else None
    sender_display_name = str(row[13]) if row[13] is not None else None
    sender_label = str(row[14]) if row[14] is not None else None
    sender_id = None
    if sender_type == "user":
        sender_id = sender_user_account_id or (str(row[2]) if row[2] is not None else None)
    elif sender_type == "admin":
        sender_id = sender_admin_account_id or (str(row[2]) if row[2] is not None else None)
    else:
        sender_id = "system"

    return {
        "id": str(row[0]) if row[0] is not None else "",
        "thread_id": str(row[1]) if row[1] is not None else "",
        "sender_id": sender_id,
        "sender_type": sender_type,
        "sender_user_account_id": sender_user_account_id,
        "sender_admin_account_id": sender_admin_account_id,
        "sender_display_name": sender_display_name,
        "sender_label": sender_label,
        "type": str(row[3]) if row[3] is not None else "text",
        "text": row[4],
        "is_blocked": bool(row[5]),
        "blocked_reason": row[6],
        "created_at": row[7],
        "edited_at": row[8],
        "deleted_at": row[9],
    }


def _user_sender_identity(user_id: str) -> tuple[str, str]:
    phone_expr, phone_expr_params = decrypt_phone_expr("u.phone_enc")
    row = fetch_one(
        f"""
        SELECT
            COALESCE(
                NULLIF(BTRIM(up.display_name), ''),
                NULLIF(BTRIM({phone_expr}), ''),
                NULLIF(BTRIM(u.email), ''),
                'Пользователь'
            ) AS display_name,
            'Пользователь' AS sender_label
        FROM users u
        LEFT JOIN user_profiles up
          ON up.user_id = u.id
        WHERE u.id = %s
        LIMIT 1
        """,
        (*phone_expr_params, user_id),
    )
    if not row:
        return ("Пользователь", "Пользователь")
    return (str(row[0] or "Пользователь"), str(row[1] or "Пользователь"))


def _get_admin_account(admin_account_id: str) -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT
            aa.id::text,
            aa.login_identifier,
            aa.email,
            aa.role,
            aa.status,
            aa.display_name,
            aa.linked_user_account_id::text
        FROM admin_accounts aa
        WHERE aa.id::text = %s
          AND aa.status = 'active'
          AND aa.disabled_at IS NULL
        LIMIT 1
        """,
        (admin_account_id,),
    )
    if not row:
        raise HTTPException(status_code=403, detail="Active admin account required")
    return {
        "id": str(row[0]),
        "login_identifier": str(row[1] or ""),
        "email": str(row[2] or "") or None,
        "role": str(row[3] or "support"),
        "status": str(row[4] or "active"),
        "display_name": str(row[5] or "").strip() or str(row[2] or row[1] or "Команда Vzaimno"),
        "linked_user_account_id": str(row[6]) if row[6] is not None else None,
    }


def _admin_sender_identity(admin_account_id: str) -> tuple[dict[str, Any], str, str]:
    account = _get_admin_account(admin_account_id)
    return (
        account,
        account["display_name"],
        _admin_sender_label(account["role"]),
    )


def _pick_default_admin_account_id(*, exclude_user_account_id: Optional[str] = None) -> Optional[str]:
    row = fetch_one(
        """
        SELECT aa.id::text
        FROM admin_accounts aa
        WHERE aa.status = 'active'
          AND aa.disabled_at IS NULL
          AND (%s::uuid IS NULL OR aa.linked_user_account_id IS NULL OR aa.linked_user_account_id <> %s::uuid)
        ORDER BY CASE aa.role
            WHEN 'support' THEN 0
            WHEN 'moderator' THEN 1
            ELSE 2
        END,
        aa.created_at ASC,
        aa.id ASC
        LIMIT 1
        """,
        (exclude_user_account_id, exclude_user_account_id),
    )
    return str(row[0]) if row and row[0] else None


def ensure_support_participant(thread_id: str, user_id: str, role: str = "user") -> None:
    if not table_has_column("chat_participants", "role"):
        execute(
            """
            INSERT INTO chat_participants (thread_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (thread_id, user_id)
            DO UPDATE SET left_at = NULL
            """,
            (thread_id, user_id),
        )
        return

    execute(
        """
        INSERT INTO chat_participants (thread_id, user_id, role)
        VALUES (%s, %s, %s)
        ON CONFLICT (thread_id, user_id)
        DO UPDATE SET role = EXCLUDED.role, left_at = NULL
        """,
        (thread_id, user_id, role),
    )


def _get_support_thread(thread_id: str) -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT
            st.id::text,
            st.user_account_id::text,
            st.assigned_admin_account_id::text,
            st.status,
            st.created_at,
            st.updated_at,
            st.closed_at
        FROM support_threads st
        WHERE st.id::text = %s
        LIMIT 1
        """,
        (thread_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Support thread not found")
    return {
        "id": str(row[0]),
        "user_account_id": str(row[1]),
        "assigned_admin_account_id": str(row[2]) if row[2] is not None else None,
        "status": str(row[3] or "open"),
        "created_at": row[4],
        "updated_at": row[5],
        "closed_at": row[6],
    }


def _canonicalize_support_thread_for_user(user_id: str) -> Optional[str]:
    candidates = fetch_all(
        """
        SELECT
            ct.id::text,
            st.user_account_id::text,
            st.closed_at,
            COALESCE(ct.last_message_at, st.updated_at, ct.created_at) AS activity_at,
            ct.created_at
        FROM chat_threads ct
        JOIN chat_participants cp
          ON cp.thread_id = ct.id
         AND cp.user_id::text = %s
         AND cp.left_at IS NULL
        LEFT JOIN support_threads st
          ON st.id = ct.id
        WHERE ct.kind::text = 'support'
          AND ct.archived_at IS NULL
        ORDER BY
            CASE
                WHEN st.user_account_id::text = %s AND st.closed_at IS NULL THEN 0
                WHEN st.id IS NULL THEN 1
                WHEN st.user_account_id::text = %s THEN 2
                ELSE 3
            END,
            COALESCE(ct.last_message_at, st.updated_at, ct.created_at) DESC,
            ct.created_at DESC,
            ct.id DESC
        """,
        (user_id, user_id, user_id),
    )
    if not candidates:
        return None

    canonical_id = str(candidates[0][0])
    support_owner_id = str(candidates[0][1]) if candidates[0][1] is not None else None
    assigned_admin_account_id = _pick_default_admin_account_id(exclude_user_account_id=user_id)

    if support_owner_id == user_id:
        execute(
            """
            UPDATE support_threads
            SET assigned_admin_account_id = COALESCE(assigned_admin_account_id, %s::uuid),
                status = CASE WHEN closed_at IS NULL THEN status ELSE 'open' END,
                closed_at = NULL,
                updated_at = now()
            WHERE id::text = %s
              AND user_account_id::text = %s
            """,
            (assigned_admin_account_id, canonical_id, user_id),
        )
    else:
        execute(
            """
            INSERT INTO support_threads (
                id,
                user_account_id,
                assigned_admin_account_id,
                status,
                created_at,
                updated_at,
                closed_at
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, 'open', now(), now(), NULL)
            ON CONFLICT (id) DO NOTHING
            """,
            (canonical_id, user_id, assigned_admin_account_id),
        )

    ensure_support_participant(canonical_id, user_id, "user")

    duplicate_rows = fetch_all(
        """
        SELECT ct.id::text
        FROM chat_threads ct
        JOIN chat_participants cp
          ON cp.thread_id = ct.id
         AND cp.user_id::text = %s
         AND cp.left_at IS NULL
        LEFT JOIN support_threads st
          ON st.id = ct.id
         AND st.user_account_id::text = %s
        WHERE ct.kind::text = 'support'
          AND ct.archived_at IS NULL
          AND ct.id::text <> %s
          AND (
                st.id IS NOT NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM support_threads st2
                    WHERE st2.id = ct.id
                )
              )
        ORDER BY COALESCE(ct.last_message_at, st.updated_at, ct.created_at) DESC, ct.created_at DESC
        """,
        (user_id, user_id, canonical_id),
    )
    for duplicate_row in duplicate_rows:
        duplicate_id = str(duplicate_row[0])
        execute(
            """
            UPDATE support_threads
            SET status = 'closed',
                closed_at = COALESCE(closed_at, now()),
                updated_at = now()
            WHERE id::text = %s
              AND user_account_id::text = %s
              AND closed_at IS NULL
            """,
            (duplicate_id, user_id),
        )
        execute(
            """
            UPDATE chat_threads
            SET archived_at = COALESCE(archived_at, now())
            WHERE id::text = %s
              AND kind::text = 'support'
            """,
            (duplicate_id,),
        )

    return canonical_id


def get_or_create_support_thread(user_id: str) -> str:
    existing = _canonicalize_support_thread_for_user(user_id)
    if existing:
        return existing

    thread_id = str(uuid.uuid4())
    assigned_admin_account_id = _pick_default_admin_account_id(exclude_user_account_id=user_id)
    execute(
        """
        INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at)
        VALUES (%s, 'support', NULL, NULL, NULL)
        """,
        (thread_id,),
    )
    execute(
        """
        INSERT INTO support_threads (
            id,
            user_account_id,
            assigned_admin_account_id,
            status,
            created_at,
            updated_at
        )
        VALUES (%s::uuid, %s::uuid, %s::uuid, 'open', now(), now())
        """,
        (thread_id, user_id, assigned_admin_account_id),
    )
    ensure_support_participant(thread_id, user_id, "user")
    log_audit_event(
        actor_type="user",
        actor_user_account_id=user_id,
        action="support_thread_created",
        target_type="support_thread",
        target_id=thread_id,
        details={"assigned_admin_account_id": assigned_admin_account_id},
    )
    publish_thread_preview_sync(thread_id, user_id=user_id)
    return thread_id


def assert_support_access(thread_id: str, user_id: str) -> None:
    row = fetch_one(
        """
        SELECT 1
        FROM support_threads
        WHERE id::text = %s
          AND user_account_id::text = %s
        LIMIT 1
        """,
        (thread_id, user_id),
    )
    if not row:
        raise HTTPException(status_code=403, detail="Thread access denied")


def _assert_support_admin_access(thread_id: str, admin_account_id: str) -> dict[str, Any]:
    account = _get_admin_account(admin_account_id)
    thread = _get_support_thread(thread_id)
    return {**thread, "admin_account": account}


def _mark_support_thread_read_for_user(thread_id: str, user_id: str, message_id: str) -> None:
    execute(
        """
        UPDATE chat_participants
        SET last_read_message_id = %s
        WHERE thread_id = %s
          AND user_id = %s
        """,
        (message_id, thread_id, user_id),
    )


def _mark_support_thread_read_for_admin(thread_id: str, admin_account_id: str, message_id: str) -> None:
    execute(
        """
        INSERT INTO support_thread_admin_reads (
            thread_id,
            admin_account_id,
            last_read_message_id,
            joined_at,
            updated_at
        )
        VALUES (%s::uuid, %s::uuid, %s::uuid, now(), now())
        ON CONFLICT (thread_id, admin_account_id)
        DO UPDATE SET last_read_message_id = EXCLUDED.last_read_message_id,
                      updated_at = now()
        """,
        (thread_id, admin_account_id, message_id),
    )


def _support_message_select_sql() -> str:
    return """
        SELECT
            id,
            thread_id,
            sender_id,
            type,
            text,
            is_blocked,
            blocked_reason,
            created_at,
            edited_at,
            deleted_at,
            sender_type,
            sender_user_account_id,
            sender_admin_account_id,
            sender_display_name,
            sender_label
        FROM chat_messages
        WHERE thread_id = %s
          AND deleted_at IS NULL
    """


def list_support_messages(
    thread_id: str,
    user_id: str,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    assert_support_access(thread_id, user_id)

    lim = max(1, min(int(limit), 100))
    params: List[Any] = [thread_id]
    sql = _support_message_select_sql()
    if before is not None:
        sql += " AND created_at < %s"
        params.append(before)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(lim)

    rows = fetch_all(sql, tuple(params))
    ordered = list(reversed(rows))
    if ordered:
        _mark_support_thread_read_for_user(thread_id, user_id, str(ordered[-1][0]))
        publish_thread_preview_sync(thread_id, user_id=user_id)
    return [_message_row_to_dict(row) for row in ordered]


def list_support_messages_for_admin(
    thread_id: str,
    admin_account_id: str,
    limit: int = 200,
    before: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    _assert_support_admin_access(thread_id, admin_account_id)
    lim = max(1, min(int(limit), 200))
    params: List[Any] = [thread_id]
    sql = _support_message_select_sql()
    if before is not None:
        sql += " AND created_at < %s"
        params.append(before)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(lim)
    rows = fetch_all(sql, tuple(params))
    ordered = list(reversed(rows))
    if ordered:
        _mark_support_thread_read_for_admin(thread_id, admin_account_id, str(ordered[-1][0]))
    return [_message_row_to_dict(row) for row in ordered]


def _fetch_support_message(message_id: str):
    return fetch_one(
        """
        SELECT
            id,
            thread_id,
            sender_id,
            type,
            text,
            is_blocked,
            blocked_reason,
            created_at,
            edited_at,
            deleted_at,
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


def _touch_support_thread(thread_id: str) -> None:
    execute("UPDATE chat_threads SET last_message_at = now() WHERE id = %s", (thread_id,))
    execute("UPDATE support_threads SET updated_at = now() WHERE id::text = %s", (thread_id,))


def post_user_support_message(thread_id: str, user_id: str, text: str) -> Dict[str, Any]:
    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    thread = _get_support_thread(thread_id)
    if thread["user_account_id"] != user_id:
        raise HTTPException(status_code=403, detail="Thread access denied")

    sender_display_name, sender_label = _user_sender_identity(user_id)
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
        (message_id, thread_id, user_id, clean_text, user_id, sender_display_name, sender_label),
    )
    _touch_support_thread(thread_id)
    _mark_support_thread_read_for_user(thread_id, user_id, message_id)

    row = _fetch_support_message(message_id)
    if not row:
        raise HTTPException(status_code=500, detail="Message was not saved")
    message = _message_row_to_dict(row)

    log_audit_event(
        actor_type="user",
        actor_user_account_id=user_id,
        action="support_message_sent",
        target_type="support_thread",
        target_id=thread_id,
        details={"message_id": message_id, "sender_type": "user"},
    )

    publish_chat_message_sync(thread_id, message)
    publish_thread_preview_sync(thread_id)
    return message


def post_support_message(
    thread_id: str,
    sender_id: str,
    text: str,
    sender_role: str,
) -> Dict[str, Any]:
    normalized_role = str(sender_role or "").strip().lower()
    if normalized_role in ADMIN_ACCESS_ROLES:
        raise HTTPException(status_code=403, detail="Admin support messages require admin auth context")
    return post_user_support_message(thread_id, sender_id, text)


def post_admin_support_message(thread_id: str, admin_account_id: str, text: str) -> Dict[str, Any]:
    clean_text = _normalize_text(text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    thread = _assert_support_admin_access(thread_id, admin_account_id)
    account, sender_display_name, sender_label = _admin_sender_identity(admin_account_id)
    if account["linked_user_account_id"] and account["linked_user_account_id"] == thread["user_account_id"]:
        raise HTTPException(
            status_code=409,
            detail="Admin account linked to this user cannot reply in their own support thread. Reassign the thread first.",
        )

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
        VALUES (%s, %s, NULL, 'text', %s, 'admin', NULL, %s::uuid, %s, %s)
        """,
        (message_id, thread_id, clean_text, admin_account_id, sender_display_name, sender_label),
    )
    execute(
        """
        UPDATE support_threads
        SET assigned_admin_account_id = COALESCE(assigned_admin_account_id, %s::uuid),
            updated_at = now()
        WHERE id::text = %s
        """,
        (admin_account_id, thread_id),
    )
    _touch_support_thread(thread_id)
    _mark_support_thread_read_for_admin(thread_id, admin_account_id, message_id)

    create_notification(
        user_id=thread["user_account_id"],
        notif_type="support",
        body=clean_text,
        payload={"thread_id": thread_id, "message_id": message_id, "sender_type": "admin"},
    )

    row = _fetch_support_message(message_id)
    if not row:
        raise HTTPException(status_code=500, detail="Message was not saved")
    message = _message_row_to_dict(row)

    log_audit_event(
        actor_type="admin",
        actor_admin_account_id=admin_account_id,
        action="support_message_sent",
        target_type="support_thread",
        target_id=thread_id,
        details={
            "message_id": message_id,
            "sender_type": "admin",
            "assigned_admin_account_id": admin_account_id,
        },
    )

    publish_chat_message_sync(thread_id, message)
    publish_thread_preview_sync(thread_id)
    return message


def list_support_threads_for_admin(admin_account_id: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
    _get_admin_account(admin_account_id)
    params: List[Any] = [admin_account_id, admin_account_id]
    sql = """
        SELECT
            st.id::text AS id,
            st.status,
            st.user_account_id::text AS user_id,
            u.email AS user_email,
            up.display_name AS user_display_name,
            st.assigned_admin_account_id::text AS assigned_admin_account_id,
            aa.display_name AS assigned_admin_display_name,
            aa.login_identifier AS assigned_admin_login,
            ct.created_at,
            COALESCE(ct.last_message_at, st.updated_at, ct.created_at) AS last_message_at,
            lm.text AS last_message_text,
            COALESCE(unread.unread_count, 0) AS unread_count
        FROM support_threads st
        JOIN chat_threads ct
          ON ct.id = st.id
        JOIN users u
          ON u.id = st.user_account_id
        LEFT JOIN user_profiles up
          ON up.user_id = st.user_account_id
        LEFT JOIN admin_accounts aa
          ON aa.id = st.assigned_admin_account_id
        LEFT JOIN support_thread_admin_reads sar
          ON sar.thread_id = st.id
         AND sar.admin_account_id::text = %s
        LEFT JOIN chat_messages read_msg
          ON read_msg.id = sar.last_read_message_id
        LEFT JOIN LATERAL (
            SELECT text, created_at
            FROM chat_messages m
            WHERE m.thread_id = st.id
              AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT 1
        ) lm ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS unread_count
            FROM chat_messages m
            WHERE m.thread_id = st.id
              AND m.deleted_at IS NULL
              AND (
                    COALESCE(m.sender_type, CASE WHEN COALESCE(m.type::text, 'text') = 'system' THEN 'system' ELSE 'user' END) = 'user'
                    OR COALESCE(m.sender_type, 'user') = 'system'
                    OR COALESCE(m.sender_admin_account_id::text, '') <> %s
              )
              AND (
                    sar.last_read_message_id IS NULL
                    OR m.created_at > COALESCE(read_msg.created_at, 'epoch'::timestamptz)
                    OR (
                        m.created_at = COALESCE(read_msg.created_at, 'epoch'::timestamptz)
                        AND m.id::text > COALESCE(sar.last_read_message_id::text, '')
                    )
                  )
        ) unread ON TRUE
        WHERE st.closed_at IS NULL
    """
    if search:
        sql += """
          AND (
                COALESCE(u.email, '') ILIKE %s
                OR COALESCE(up.display_name, '') ILIKE %s
                OR st.id::text ILIKE %s
                OR st.user_account_id::text ILIKE %s
              )
        """
        search_value = f"%{search.strip()}%"
        params.extend([search_value, search_value, search_value, search_value])
    sql += " ORDER BY COALESCE(ct.last_message_at, st.updated_at, ct.created_at) DESC LIMIT 300"

    rows = fetch_all(sql, tuple(params))
    return [
        {
            "id": str(row[0]),
            "status": str(row[1] or "open"),
            "user_id": str(row[2]),
            "user_email": row[3],
            "user_display_label": str(row[4] or row[3] or row[2]),
            "assigned_admin_account_id": str(row[5]) if row[5] is not None else None,
            "assigned_admin_display_label": str(row[6] or row[7] or "—"),
            "created_at": row[8],
            "last_message_at": row[9],
            "last_message_text": row[10],
            "unread_count": int(row[11] or 0),
        }
        for row in rows
    ]


def get_support_thread_for_admin(thread_id: str, admin_account_id: str) -> Dict[str, Any]:
    _get_admin_account(admin_account_id)
    row = fetch_one(
        """
        SELECT
            st.id::text,
            st.status,
            st.user_account_id::text,
            u.email,
            up.display_name,
            st.assigned_admin_account_id::text,
            aa.display_name,
            aa.login_identifier,
            ct.created_at,
            COALESCE(ct.last_message_at, st.updated_at, ct.created_at) AS last_message_at
        FROM support_threads st
        JOIN chat_threads ct
          ON ct.id = st.id
        JOIN users u
          ON u.id = st.user_account_id
        LEFT JOIN user_profiles up
          ON up.user_id = st.user_account_id
        LEFT JOIN admin_accounts aa
          ON aa.id = st.assigned_admin_account_id
        WHERE st.id::text = %s
        LIMIT 1
        """,
        (thread_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Support thread not found")
    return {
        "id": str(row[0]),
        "status": str(row[1] or "open"),
        "user_id": str(row[2]),
        "user_email": row[3],
        "user_display_label": str(row[4] or row[3] or row[2]),
        "assigned_admin_account_id": str(row[5]) if row[5] is not None else None,
        "assigned_admin_display_label": str(row[6] or row[7] or "—"),
        "created_at": row[8],
        "last_message_at": row[9],
    }


def assign_support_thread(thread_id: str, assignee_admin_account_id: str, actor_admin_account_id: str) -> Dict[str, Any]:
    _assert_support_admin_access(thread_id, actor_admin_account_id)
    assignee = _get_admin_account(assignee_admin_account_id)
    execute(
        """
        UPDATE support_threads
        SET assigned_admin_account_id = %s::uuid,
            updated_at = now()
        WHERE id::text = %s
        """,
        (assignee_admin_account_id, thread_id),
    )
    log_audit_event(
        actor_type="admin",
        actor_admin_account_id=actor_admin_account_id,
        action="support_thread_assigned",
        target_type="support_thread",
        target_id=thread_id,
        details={"assigned_admin_account_id": assignee_admin_account_id, "assigned_admin_role": assignee["role"]},
    )
    return get_support_thread_for_admin(thread_id, actor_admin_account_id)
