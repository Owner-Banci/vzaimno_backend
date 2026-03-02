from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from .models_sqlalchemy import (
    Announcement,
    ChatMessage,
    ChatParticipant,
    ChatThread,
    User,
)


STAFF_ROLES = {"admin", "moderator", "support"}
QUEUE_STATUSES = {"pending_review", "needs_fix", "rejected"}
_SCHEMA_CACHE: dict[str, frozenset[str]] = {}
_COLUMN_INFO_CACHE: dict[tuple[str, str], tuple[Optional[str], Optional[str]]] = {}
_ENUM_LABELS_CACHE: dict[str, tuple[str, ...]] = {}
_OPENISH_STATUSES = (
    "open",
    "pending",
    "new",
    "created",
    "active",
    "review",
    "under_review",
    "in_review",
    "queued",
)
_RESOLVEDISH_STATUSES = (
    "resolved",
    "closed",
    "done",
    "processed",
    "valid",
    "invalid",
    "approved",
    "rejected",
    "revoked",
    "completed",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _get_table_columns(session: Session, table_name: str) -> frozenset[str]:
    cached = _SCHEMA_CACHE.get(table_name)
    if cached is not None:
        return cached
    rows = session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    ).scalars().all()
    columns = frozenset(str(row) for row in rows)
    _SCHEMA_CACHE[table_name] = columns
    return columns


def _has_column(session: Session, table_name: str, column_name: str) -> bool:
    return column_name in _get_table_columns(session, table_name)


def _text_eq(left_expr: str, right_expr: str) -> str:
    return f"({left_expr})::text = ({right_expr})::text"


def _text_expr(expr: str) -> str:
    return f"({expr})::text"


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _get_column_info(session: Session, table_name: str, column_name: str) -> tuple[Optional[str], Optional[str]]:
    cache_key = (table_name, column_name)
    cached = _COLUMN_INFO_CACHE.get(cache_key)
    if cached is not None:
        return cached
    row = session.execute(
        text(
            """
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).first()
    value = (str(row[0]), str(row[1])) if row else (None, None)
    _COLUMN_INFO_CACHE[cache_key] = value
    return value


def _get_enum_labels(session: Session, enum_name: str) -> tuple[str, ...]:
    cached = _ENUM_LABELS_CACHE.get(enum_name)
    if cached is not None:
        return cached
    rows = session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public'
              AND t.typname = :enum_name
            ORDER BY e.enumsortorder
            """
        ),
        {"enum_name": enum_name},
    ).scalars().all()
    labels = tuple(str(row) for row in rows)
    _ENUM_LABELS_CACHE[enum_name] = labels
    return labels


def _map_status_assignment(
    session: Session,
    table_name: str,
    column_name: str,
    target_status: str,
) -> Optional[str]:
    if not _has_column(session, table_name, column_name):
        return None
    data_type, udt_name = _get_column_info(session, table_name, column_name)
    if data_type != "USER-DEFINED" or not udt_name:
        return target_status
    labels = _get_enum_labels(session, udt_name)
    by_lower = {label.lower(): label for label in labels}
    candidates = (target_status,) + (
        _OPENISH_STATUSES if target_status == "open" else _RESOLVEDISH_STATUSES
    )
    for candidate in candidates:
        value = by_lower.get(candidate.lower())
        if value:
            return value
    return None


def _report_status_expr(session: Session, alias: str = "r") -> str:
    if _has_column(session, "reports", "status"):
        return (
            f"CASE WHEN lower({_text_expr(f'{alias}.status')}) "
            f"IN ({_quoted(_RESOLVEDISH_STATUSES)}) THEN 'resolved' ELSE 'open' END"
        )
    if _has_column(session, "reports", "resolved_at"):
        return f"CASE WHEN {alias}.resolved_at IS NULL THEN 'open' ELSE 'resolved' END"
    if _has_column(session, "reports", "resolution"):
        return f"CASE WHEN {alias}.resolution IS NULL THEN 'open' ELSE 'resolved' END"
    return "'open'"


def _report_open_condition(session: Session, alias: str = "reports") -> str:
    return f"{_report_status_expr(session, alias)} = 'open'"


def _restriction_status_expr(session: Session, alias: str = "r") -> str:
    if _has_column(session, "user_restrictions", "status"):
        return (
            f"CASE WHEN lower({_text_expr(f'{alias}.status')}) "
            f"IN ('revoked', 'inactive', 'closed', 'disabled') THEN 'revoked' ELSE 'active' END"
        )
    if _has_column(session, "user_restrictions", "revoked_at"):
        return f"CASE WHEN {alias}.revoked_at IS NULL THEN 'active' ELSE 'revoked' END"
    return "'active'"


def _get_user_row(session: Session, user_id: str) -> Optional[dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT id::text AS id,
                   email,
                   password_hash,
                   role,
                   created_at
            FROM users
            WHERE id::text = :user_id
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    return dict(row) if row else None


def _get_chat_user_filter(session: Session, participant_alias: str, user_alias: str) -> str:
    parts = []
    if _has_column(session, "chat_participants", "left_at"):
        parts.append(f"{participant_alias}.left_at IS NULL")
    if _has_column(session, "chat_participants", "role"):
        parts.append(f"{participant_alias}.role = 'user'")
    elif _has_column(session, "users", "role"):
        parts.append(f"COALESCE({_text_expr(f'{user_alias}.role')}, 'user') NOT IN ('admin', 'moderator', 'support')")
    return " AND ".join(parts) if parts else "1 = 1"


def _insert_row(
    session: Session,
    table_name: str,
    values: Dict[str, Any],
    jsonb_columns: Optional[set[str]] = None,
) -> None:
    jsonb_columns = jsonb_columns or set()
    columns = list(values.keys())
    placeholders: list[str] = []
    params: dict[str, Any] = {}
    for index, column in enumerate(columns):
        param_name = f"p_{index}"
        if column in jsonb_columns:
            placeholders.append(f":{param_name}::jsonb")
            params[param_name] = json.dumps(values[column], ensure_ascii=False)
        else:
            placeholders.append(f":{param_name}")
            params[param_name] = values[column]
    sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
    session.execute(text(sql), params)


def _restriction_timestamp_expr(session: Session, alias: str = "r") -> str:
    if _has_column(session, "user_restrictions", "starts_at"):
        return f"{alias}.starts_at"
    if _has_column(session, "user_restrictions", "created_at"):
        return f"{alias}.created_at"
    return "NULL::timestamptz"


def _ensure_obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ensure_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _set_decision(mod: Dict[str, Any], status: str, message: str) -> None:
    mod["decision"] = {"status": status, "message": message}


def _normalize_reason(reason: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "field": str(reason.get("field", "")).strip(),
        "code": str(reason.get("code", "")).strip(),
        "details": str(reason.get("details", "")).strip(),
        "can_appeal": bool(reason.get("can_appeal", True)),
    }


def _merge_reasons(existing: Iterable[Any], incoming: Optional[Sequence[Dict[str, Any]]]) -> list[Dict[str, Any]]:
    merged: list[Dict[str, Any]] = []
    seen: set[tuple[str, str, str, bool]] = set()

    for raw in list(existing) + list(incoming or []):
        if not isinstance(raw, dict):
            continue
        item = _normalize_reason(raw)
        if not item["field"] or not item["code"] or not item["details"]:
            continue
        key = (item["field"], item["code"], item["details"], item["can_appeal"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _normalize_suggestions(suggestions: Optional[Sequence[str]]) -> list[str]:
    if suggestions is None:
        return []
    clean: list[str] = []
    for suggestion in suggestions:
        value = str(suggestion or "").strip()
        if value:
            clean.append(value)
    return clean


def _reset_appeal(mod: Dict[str, Any]) -> None:
    appeal = _ensure_obj(mod.get("appeal"))
    if appeal:
        appeal["requested"] = False
        appeal["resolved_at"] = _now_iso()
        mod["appeal"] = appeal


def _has_hard_block_reason(reasons: Sequence[Dict[str, Any]]) -> bool:
    for reason in reasons:
        code = str(reason.get("code", "")).lower()
        details = str(reason.get("details", "")).lower()
        if "hard" in code or "hard-block" in code or "hard-block" in details:
            return True
    return False


def _add_action(
    session: Session,
    moderator_id: str,
    action_type: str,
    target_type: str,
    target_id: str,
    reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    columns = _get_table_columns(session, "moderation_actions")
    action_id = str(uuid.uuid4())
    values: Dict[str, Any] = {
        "id": action_id,
        "moderator_id": moderator_id,
        "action_type": action_type,
        "target_type": target_type,
        "target_id": target_id,
    }
    if "reason" in columns:
        values["reason"] = reason
    if "payload" in columns:
        values["payload"] = payload or {}
    _insert_row(session, "moderation_actions", values, jsonb_columns={"payload"} if "payload" in values else set())
    return action_id


def _add_notification(
    session: Session,
    user_id: str,
    notif_type: str,
    body: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    columns = _get_table_columns(session, "notifications")
    notification_id = str(uuid.uuid4())
    values: Dict[str, Any] = {"id": notification_id, "user_id": user_id, "type": notif_type, "body": body}
    if "payload" in columns:
        values["payload"] = payload or {}
    if "is_read" in columns:
        values["is_read"] = False
    _insert_row(session, "notifications", values, jsonb_columns={"payload"} if "payload" in values else set())
    return notification_id


def list_users(session: Session, search: Optional[str] = None) -> list[dict[str, Any]]:
    stmt = select(User).order_by(User.created_at.desc())
    if search:
        stmt = stmt.where(User.email.ilike(f"%{search.strip()}%"))
    rows = session.scalars(stmt).all()
    return [
        {
            "id": row.id,
            "email": row.email,
            "role": row.role,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def get_user_detail(session: Session, user_id: str) -> dict[str, Any]:
    user = _get_user_row(session, user_id)
    if not user:
        raise ValueError("User not found")

    restrictions = list_restrictions(session, search=str(user["email"]), status=None)
    return {
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "created_at": user["created_at"],
        "restrictions": [item for item in restrictions if str(item["user_id"]) == str(user["id"])],
    }


def update_user_role(
    session: Session,
    target_user_id: str,
    role: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    if actor_role != "admin":
        raise PermissionError("Only admin can change roles")
    if role not in {"user", "support", "moderator", "admin"}:
        raise ValueError("Unsupported role")

    user = _get_user_row(session, target_user_id)
    if not user:
        raise ValueError("User not found")

    previous_role = str(user["role"])
    session.execute(
        text("UPDATE users SET role = :role WHERE id::text = :user_id"),
        {"role": role, "user_id": target_user_id},
    )
    _add_action(
        session=session,
        moderator_id=actor_id,
        action_type="user_role_change",
        target_type="user",
        target_id=str(user["id"]),
        reason=f"{previous_role} -> {role}",
        payload={"old_role": previous_role, "new_role": role},
    )
    _add_notification(
        session=session,
        user_id=str(user["id"]),
        notif_type="moderation",
        body=f"Роль учётной записи изменена: {role}",
        payload={"user_id": str(user["id"]), "new_role": role},
    )
    session.commit()
    return {
        "id": user["id"],
        "email": user["email"],
        "role": role,
        "created_at": user["created_at"],
    }


def list_moderation_announcements(
    session: Session,
    status_filter: Optional[str] = None,
    appeals_only: bool = False,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT a.id,
               a.user_id,
               u.email AS user_email,
               a.category,
               a.title,
               a.status,
               a.data,
               a.created_at,
               a.updated_at,
               COALESCE((a.data->'moderation'->'appeal'->>'requested')::boolean, FALSE) AS appeal_requested
        FROM announcements a
        LEFT JOIN users u ON {user_join}
        WHERE a.deleted_at IS NULL
    """
    params: dict[str, Any] = {}
    sql = sql.format(user_join=_text_eq("u.id", "a.user_id"))

    if status_filter and status_filter in QUEUE_STATUSES | {"active", "archived"}:
        sql += " AND a.status = :status_filter"
        params["status_filter"] = status_filter
    else:
        sql += " AND a.status IN ('pending_review', 'needs_fix', 'rejected')"

    if appeals_only:
        sql += " AND COALESCE((a.data->'moderation'->'appeal'->>'requested')::boolean, FALSE) = TRUE"

    if search:
        params["search"] = f"%{search.strip()}%"
        sql += " AND (a.title ILIKE :search OR a.category ILIKE :search OR COALESCE(u.email, '') ILIKE :search)"

    sql += " ORDER BY appeal_requested DESC, a.updated_at DESC, a.created_at DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_announcement_detail(session: Session, ann_id: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT a.id,
                   a.user_id,
                   u.email AS user_email,
                   a.category,
                   a.title,
                   a.status,
                   a.data,
                   a.created_at,
                   a.updated_at,
                   a.deleted_at
            FROM announcements a
            LEFT JOIN users u ON {user_join}
            WHERE a.id::text = :ann_id
            """
            .format(user_join=_text_eq("u.id", "a.user_id"))
        ),
        {"ann_id": ann_id},
    ).mappings().first()
    if not row:
        raise ValueError("Announcement not found")
    return dict(row)


def apply_announcement_decision(
    session: Session,
    ann_id: str,
    moderator_id: str,
    decision: str,
    message: str,
    reasons: Optional[Sequence[Dict[str, Any]]] = None,
    suggestions: Optional[Sequence[str]] = None,
) -> Announcement:
    announcement = session.execute(
        text(
            """
            SELECT id::text AS id,
                   user_id::text AS user_id,
                   status,
                   data,
                   created_at,
                   updated_at,
                   deleted_at
            FROM announcements
            WHERE id::text = :ann_id
              AND deleted_at IS NULL
            """
        ),
        {"ann_id": ann_id},
    ).mappings().first()
    if not announcement:
        raise ValueError("Announcement not found")

    if decision not in {"approve", "needs_fix", "reject", "archive", "delete"}:
        raise ValueError("Unsupported decision")

    old_status = str(announcement["status"])
    payload_data = dict(announcement["data"] or {})
    moderation = _ensure_obj(payload_data.get("moderation"))
    merged_reasons = _merge_reasons(_ensure_list(moderation.get("reasons")), reasons)
    clean_suggestions = _normalize_suggestions(suggestions)
    new_status = old_status
    now = _now()

    if decision == "approve":
        new_status = "active"
        _set_decision(moderation, new_status, message)
        moderation["reasons"] = []
        moderation["suggestions"] = []
        moderation.pop("penalty_stub", None)
        _reset_appeal(moderation)
    elif decision == "needs_fix":
        new_status = "needs_fix"
        _set_decision(moderation, new_status, message)
        moderation["reasons"] = merged_reasons
        moderation["suggestions"] = clean_suggestions
        _reset_appeal(moderation)
    elif decision == "reject":
        new_status = "rejected"
        _set_decision(moderation, new_status, message)
        moderation["reasons"] = merged_reasons
        if suggestions is not None:
            moderation["suggestions"] = clean_suggestions
        if _has_hard_block_reason(moderation["reasons"]):
            moderation["penalty_stub"] = {"type": "warning", "points": 1, "applied_at": None}
        _reset_appeal(moderation)
    elif decision == "archive":
        new_status = "archived"
        _set_decision(moderation, new_status, message)
        _reset_appeal(moderation)
    elif decision == "delete":
        _set_decision(moderation, old_status, message)
        _reset_appeal(moderation)

    payload_data["moderation"] = moderation
    update_clauses = [
        "data = CAST(:data AS jsonb)",
        "updated_at = :updated_at",
    ]
    params: dict[str, Any] = {
        "ann_id": ann_id,
        "data": json.dumps(payload_data, ensure_ascii=False),
        "updated_at": now,
    }
    if decision == "delete":
        update_clauses.append("deleted_at = :deleted_at")
        params["deleted_at"] = now
    else:
        update_clauses.append("status = :new_status")
        params["new_status"] = new_status
    session.execute(
        text(f"UPDATE announcements SET {', '.join(update_clauses)} WHERE id::text = :ann_id"),
        params,
    )

    if decision != "delete":
        report_columns = _get_table_columns(session, "reports")
        report_resolution = "valid" if decision in {"approve", "needs_fix"} else "invalid"
        update_clauses: list[str] = []
        params: dict[str, Any] = {"target_id": str(announcement["id"])}
        status_value = _map_status_assignment(session, "reports", "status", "resolved")
        if "status" in report_columns and status_value is not None:
            update_clauses.append("status = :status_value")
            params["status_value"] = status_value
        if "resolution" in report_columns:
            update_clauses.append("resolution = :resolution")
            params["resolution"] = report_resolution
        if "resolved_by" in report_columns:
            update_clauses.append("resolved_by = :moderator_id")
            params["moderator_id"] = moderator_id
        if "moderator_comment" in report_columns:
            update_clauses.append("moderator_comment = :comment")
            params["comment"] = message
        if "resolved_at" in report_columns:
            update_clauses.append("resolved_at = :resolved_at")
            params["resolved_at"] = now
        if update_clauses:
            session.execute(
                text(
                    f"""
                    UPDATE reports
                    SET {', '.join(update_clauses)}
                    WHERE target_type = 'announcement'
                      AND target_id::text = :target_id
                      AND reason_code = 'APPEAL'
                      AND {_report_open_condition(session)}
                    """
                ),
                params,
            )

    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type=decision,
        target_type="announcement",
        target_id=str(announcement["id"]),
        reason=message,
        payload={
            "old_status": old_status,
            "new_status": new_status if decision != "delete" else old_status,
            "message": message,
            "reasons": merged_reasons if decision in {"needs_fix", "reject"} else [],
            "suggestions": clean_suggestions if suggestions is not None else _ensure_list(moderation.get("suggestions")),
            "deleted": decision == "delete",
        },
    )
    _add_notification(
        session=session,
        user_id=str(announcement["user_id"]),
        notif_type="moderation",
        body=message,
        payload={"ann_id": str(announcement["id"]), "decision": decision},
    )

    session.commit()
    refreshed = session.execute(
        text(
            """
            SELECT id::text AS id,
                   user_id::text AS user_id,
                   category,
                   title,
                   status,
                   data,
                   created_at,
                   updated_at,
                   deleted_at
            FROM announcements
            WHERE id::text = :ann_id
            """
        ),
        {"ann_id": ann_id},
    ).mappings().first()
    if not refreshed:
        raise ValueError("Announcement not found after update")
    return Announcement(
        id=str(refreshed["id"]),
        user_id=str(refreshed["user_id"]),
        category=str(refreshed["category"]),
        title=str(refreshed["title"]),
        status=str(refreshed["status"]),
        data=dict(refreshed["data"] or {}),
        created_at=refreshed["created_at"],
        updated_at=refreshed["updated_at"],
        deleted_at=refreshed["deleted_at"],
    )


def list_reports(
    session: Session,
    search: Optional[str] = None,
    status: Optional[str] = "open",
) -> list[dict[str, Any]]:
    status_expr = _report_status_expr(session, "r")
    resolved_by_join = (
        f"LEFT JOIN users mu ON {_text_eq('mu.id', 'r.resolved_by')}"
        if _has_column(session, "reports", "resolved_by")
        else ""
    )
    resolved_by_expr = _text_expr("r.resolved_by") if _has_column(session, "reports", "resolved_by") else "NULL::text"
    resolved_by_email_expr = "mu.email" if _has_column(session, "reports", "resolved_by") else "NULL::text"
    resolution_expr = _text_expr("r.resolution") if _has_column(session, "reports", "resolution") else "NULL::text"
    moderator_comment_expr = (
        "r.moderator_comment" if _has_column(session, "reports", "moderator_comment") else "NULL::text"
    )
    resolved_at_expr = "r.resolved_at" if _has_column(session, "reports", "resolved_at") else "NULL::timestamptz"
    sql = """
        SELECT r.id,
               r.reporter_id,
               ru.email AS reporter_email,
               r.target_type,
               r.target_id,
               r.reason_code,
               r.reason_text,
               {status_expr} AS status,
               {resolution_expr} AS resolution,
               {resolved_by_expr} AS resolved_by,
               {resolved_by_email_expr} AS resolved_by_email,
               {moderator_comment_expr} AS moderator_comment,
               r.created_at,
               {resolved_at_expr} AS resolved_at
        FROM reports r
        LEFT JOIN users ru ON {reporter_join}
        {resolved_by_join}
        WHERE 1 = 1
    """.format(
        status_expr=status_expr,
        resolution_expr=resolution_expr,
        resolved_by_expr=resolved_by_expr,
        resolved_by_email_expr=resolved_by_email_expr,
        moderator_comment_expr=moderator_comment_expr,
        resolved_at_expr=resolved_at_expr,
        resolved_by_join=resolved_by_join,
        reporter_join=_text_eq("ru.id", "r.reporter_id"),
    )
    params: dict[str, Any] = {}
    if status:
        sql += f" AND {status_expr} = :status"
        params["status"] = status
    if search:
        sql += (
            " AND (COALESCE(ru.email, '') ILIKE :search"
            " OR r.target_id::text ILIKE :search"
            " OR r.reason_code::text ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    sql += " ORDER BY r.created_at DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_report_detail(session: Session, report_id: str) -> dict[str, Any]:
    status_expr = _report_status_expr(session, "r")
    resolved_by_join = (
        f"LEFT JOIN users mu ON {_text_eq('mu.id', 'r.resolved_by')}"
        if _has_column(session, "reports", "resolved_by")
        else ""
    )
    resolved_by_expr = _text_expr("r.resolved_by") if _has_column(session, "reports", "resolved_by") else "NULL::text"
    resolved_by_email_expr = "mu.email" if _has_column(session, "reports", "resolved_by") else "NULL::text"
    resolution_expr = _text_expr("r.resolution") if _has_column(session, "reports", "resolution") else "NULL::text"
    moderator_comment_expr = (
        "r.moderator_comment" if _has_column(session, "reports", "moderator_comment") else "NULL::text"
    )
    resolved_at_expr = "r.resolved_at" if _has_column(session, "reports", "resolved_at") else "NULL::timestamptz"
    row = session.execute(
        text(
            """
            SELECT r.id,
                   r.reporter_id,
                   ru.email AS reporter_email,
                   r.target_type,
                   r.target_id,
                   r.reason_code,
                   r.reason_text,
                   {status_expr} AS status,
                   {resolution_expr} AS resolution,
                   {resolved_by_expr} AS resolved_by,
                   {resolved_by_email_expr} AS resolved_by_email,
                   {moderator_comment_expr} AS moderator_comment,
                   r.created_at,
                   {resolved_at_expr} AS resolved_at
            FROM reports r
            LEFT JOIN users ru ON {reporter_join}
            {resolved_by_join}
            WHERE r.id::text = :report_id
            """.format(
                status_expr=status_expr,
                resolution_expr=resolution_expr,
                resolved_by_expr=resolved_by_expr,
                resolved_by_email_expr=resolved_by_email_expr,
                moderator_comment_expr=moderator_comment_expr,
                resolved_at_expr=resolved_at_expr,
                resolved_by_join=resolved_by_join,
                reporter_join=_text_eq("ru.id", "r.reporter_id"),
            )
        ),
        {"report_id": report_id},
    ).mappings().first()
    if not row:
        raise ValueError("Report not found")
    return dict(row)


def resolve_report(
    session: Session,
    report_id: str,
    moderator_id: str,
    resolution: str,
    moderator_comment: Optional[str] = None,
) -> dict[str, Any]:
    if resolution not in {"valid", "invalid"}:
        raise ValueError("Unsupported resolution")

    report = session.execute(
        text(
            """
            SELECT id::text AS id,
                   reporter_id::text AS reporter_id,
                   target_type,
                   target_id
            FROM reports
            WHERE id::text = :report_id
            """
        ),
        {"report_id": report_id},
    ).mappings().first()
    if not report:
        raise ValueError("Report not found")

    columns = _get_table_columns(session, "reports")
    now = _now()
    update_clauses: list[str] = []
    params: dict[str, Any] = {"report_id": report_id}
    status_value = _map_status_assignment(session, "reports", "status", "resolved")
    if "status" in columns and status_value is not None:
        update_clauses.append("status = :status_value")
        params["status_value"] = status_value
    if "resolution" in columns:
        update_clauses.append("resolution = :resolution")
        params["resolution"] = resolution
    if "resolved_by" in columns:
        update_clauses.append("resolved_by = :moderator_id")
        params["moderator_id"] = moderator_id
    if "moderator_comment" in columns:
        update_clauses.append("moderator_comment = :moderator_comment")
        params["moderator_comment"] = moderator_comment
    if "resolved_at" in columns:
        update_clauses.append("resolved_at = :resolved_at")
        params["resolved_at"] = now
    if update_clauses:
        session.execute(text(f"UPDATE reports SET {', '.join(update_clauses)} WHERE id::text = :report_id"), params)

    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="report_resolve",
        target_type="report",
        target_id=str(report["id"]),
        reason=moderator_comment,
        payload={
            "resolution": resolution,
            "target_type": report["target_type"],
            "target_id": report["target_id"],
        },
    )
    _add_notification(
        session=session,
        user_id=str(report["reporter_id"]),
        notif_type="report",
        body=f"Жалоба обработана: {resolution}",
        payload={"report_id": str(report["id"]), "resolution": resolution},
    )
    session.commit()
    return get_report_detail(session, report_id)


def list_support_threads(session: Session, search: Optional[str] = None) -> list[dict[str, Any]]:
    user_filter = _get_chat_user_filter(session, "up", "uu")
    sql = """
        SELECT ct.id,
               ct.kind,
               ct.created_at,
               ct.last_message_at,
               up.user_id AS user_id,
               uu.email AS user_email,
               lm.text AS last_message_text,
               lm.created_at AS last_message_created_at
        FROM chat_threads ct
        JOIN chat_participants up
          ON up.thread_id = ct.id
        LEFT JOIN users uu ON {user_join}
        LEFT JOIN LATERAL (
            SELECT m.text, m.created_at
            FROM chat_messages m
            WHERE m.thread_id = ct.id
              AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT 1
        ) lm ON TRUE
        WHERE ct.kind = 'support'
    """
    sql = sql.format(user_join=_text_eq("uu.id", "up.user_id"))
    params: dict[str, Any] = {}
    if user_filter:
        sql += f" AND {user_filter}"
    if search:
        sql += " AND COALESCE(uu.email, '') ILIKE :search"
        params["search"] = f"%{search.strip()}%"
    sql += " ORDER BY COALESCE(ct.last_message_at, ct.created_at) DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def get_support_thread(session: Session, thread_id: str) -> dict[str, Any]:
    user_filter = _get_chat_user_filter(session, "up", "uu")
    row = session.execute(
        text(
            """
            SELECT ct.id,
                   ct.kind,
                   ct.created_at,
                   ct.last_message_at,
                   up.user_id AS user_id,
                   uu.email AS user_email
            FROM chat_threads ct
            JOIN chat_participants up
              ON up.thread_id = ct.id
            LEFT JOIN users uu ON {user_join}
            WHERE ct.id::text = :thread_id
              AND ct.kind = 'support'
              AND {user_filter}
            """
            .format(user_join=_text_eq("uu.id", "up.user_id"), user_filter=user_filter)
        ),
        {"thread_id": thread_id},
    ).mappings().first()
    if not row:
        raise ValueError("Support thread not found")
    return dict(row)


def get_support_messages(session: Session, thread_id: str) -> list[dict[str, Any]]:
    thread = session.execute(
        text("SELECT id::text AS id, kind FROM chat_threads WHERE id::text = :thread_id"),
        {"thread_id": thread_id},
    ).mappings().first()
    if not thread or str(thread["kind"]) != "support":
        raise ValueError("Support thread not found")
    sender_role_expr = (
        "COALESCE(cp.role::text, u.role::text, 'user')"
        if _has_column(session, "chat_participants", "role")
        else "COALESCE(u.role::text, 'user')"
    )
    cp_join = (
        f"LEFT JOIN chat_participants cp ON cp.thread_id = m.thread_id AND {_text_eq('cp.user_id', 'm.sender_id')}"
        if _has_column(session, "chat_participants", "role")
        else ""
    )
    rows = session.execute(
        text(
            """
            SELECT m.id,
                   m.thread_id,
                   m.sender_id,
                   u.email AS sender_email,
                   {sender_role_expr} AS sender_role,
                   m.type,
                   m.text,
                   m.is_blocked,
                   m.blocked_reason,
                   m.created_at,
                   m.edited_at,
                   m.deleted_at
            FROM chat_messages m
            LEFT JOIN users u ON {user_join}
            {cp_join}
            WHERE m.thread_id::text = :thread_id
              AND m.deleted_at IS NULL
            ORDER BY m.created_at ASC
            """
            .format(
                sender_role_expr=sender_role_expr,
                user_join=_text_eq("u.id", "m.sender_id"),
                cp_join=cp_join,
            )
        ),
        {"thread_id": thread_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def post_support_message(
    session: Session,
    thread_id: str,
    sender_id: str,
    text_value: str,
) -> dict[str, Any]:
    thread = session.execute(
        text("SELECT id::text AS id, kind FROM chat_threads WHERE id::text = :thread_id"),
        {"thread_id": thread_id},
    ).mappings().first()
    if not thread or str(thread["kind"]) != "support":
        raise ValueError("Support thread not found")

    sender = _get_user_row(session, sender_id)
    if not sender or str(sender["role"]) not in STAFF_ROLES:
        raise PermissionError("Support sender must be staff")

    clean_text = " ".join((text_value or "").strip().split())
    if not clean_text:
        raise ValueError("Message text is required")

    participant_exists = session.execute(
        text(
            """
            SELECT 1
            FROM chat_participants
            WHERE thread_id::text = :thread_id
              AND user_id::text = :user_id
            LIMIT 1
            """
        ),
        {"thread_id": thread_id, "user_id": sender_id},
    ).first()
    participant_columns = _get_table_columns(session, "chat_participants")
    message_columns = _get_table_columns(session, "chat_messages")
    message_id = str(uuid.uuid4())
    created_at = _now()

    if not participant_exists:
        participant_values: Dict[str, Any] = {"thread_id": thread_id, "user_id": sender_id}
        if "role" in participant_columns:
            participant_values["role"] = "support"
        if "joined_at" in participant_columns:
            participant_values["joined_at"] = created_at
        if "left_at" in participant_columns:
            participant_values["left_at"] = None
        if "last_read_message_id" in participant_columns:
            participant_values["last_read_message_id"] = None
        _insert_row(session, "chat_participants", participant_values)

    message_values: Dict[str, Any] = {
        "id": message_id,
        "thread_id": thread_id,
        "sender_id": sender_id,
        "type": "text",
        "text": clean_text,
    }
    if "is_blocked" in message_columns:
        message_values["is_blocked"] = False
    if "blocked_reason" in message_columns:
        message_values["blocked_reason"] = None
    if "created_at" in message_columns:
        message_values["created_at"] = created_at
    if "edited_at" in message_columns:
        message_values["edited_at"] = None
    if "deleted_at" in message_columns:
        message_values["deleted_at"] = None
    _insert_row(session, "chat_messages", message_values)
    session.execute(
        text("UPDATE chat_threads SET last_message_at = :last_message_at WHERE id::text = :thread_id"),
        {"thread_id": thread_id, "last_message_at": created_at},
    )
    if "last_read_message_id" in participant_columns:
        session.execute(
            text(
                """
                UPDATE chat_participants
                SET last_read_message_id = :message_id
                WHERE thread_id::text = :thread_id
                  AND user_id::text = :user_id
                """
            ),
            {"thread_id": thread_id, "user_id": sender_id, "message_id": message_id},
        )

    user_filter = _get_chat_user_filter(session, "cpu", "uu")
    user_participant = session.execute(
        text(
            """
            SELECT cpu.user_id::text AS user_id
            FROM chat_participants cpu
            LEFT JOIN users uu ON {user_join}
            WHERE cpu.thread_id::text = :thread_id
              AND {user_filter}
            LIMIT 1
            """.format(user_join=_text_eq("uu.id", "cpu.user_id"), user_filter=user_filter)
        ),
        {"thread_id": thread_id},
    ).mappings().first()
    if user_participant:
        _add_notification(
            session=session,
            user_id=str(user_participant["user_id"]),
            notif_type="support",
            body=clean_text,
            payload={"thread_id": thread_id, "message_id": message_id},
        )

    session.commit()
    return {
        "id": message_id,
        "thread_id": thread_id,
        "sender_id": sender_id,
        "sender_email": sender["email"],
        "sender_role": "support",
        "type": "text",
        "text": clean_text,
        "is_blocked": False,
        "blocked_reason": None,
        "created_at": created_at,
        "edited_at": None,
        "deleted_at": None,
    }


def list_restrictions(
    session: Session,
    search: Optional[str] = None,
    status: Optional[str] = "active",
) -> list[dict[str, Any]]:
    status_expr = _restriction_status_expr(session, "r")
    issued_by_exists = _has_column(session, "user_restrictions", "issued_by")
    issued_by_join = f"LEFT JOIN users iu ON {_text_eq('iu.id', 'r.issued_by')}" if issued_by_exists else ""
    type_expr = _text_expr("r.type") if _has_column(session, "user_restrictions", "type") else "NULL::text"
    issued_by_expr = _text_expr("r.issued_by") if issued_by_exists else "NULL::text"
    issued_by_email_expr = "iu.email" if issued_by_exists else "NULL::text"
    starts_at_expr = _restriction_timestamp_expr(session, "r")
    ends_at_expr = "r.ends_at" if _has_column(session, "user_restrictions", "ends_at") else "NULL::timestamptz"
    revoked_at_expr = "r.revoked_at" if _has_column(session, "user_restrictions", "revoked_at") else "NULL::timestamptz"
    sql = """
        SELECT r.id,
               r.user_id,
               uu.email AS user_email,
               {type_expr} AS type,
               {status_expr} AS status,
               {issued_by_expr} AS issued_by,
               {issued_by_email_expr} AS issued_by_email,
               {starts_at_expr} AS starts_at,
               {ends_at_expr} AS ends_at,
               {revoked_at_expr} AS revoked_at
        FROM user_restrictions r
        LEFT JOIN users uu ON {user_join}
        {issued_by_join}
        WHERE 1 = 1
    """.format(
        type_expr=type_expr,
        status_expr=status_expr,
        issued_by_expr=issued_by_expr,
        issued_by_email_expr=issued_by_email_expr,
        starts_at_expr=starts_at_expr,
        ends_at_expr=ends_at_expr,
        revoked_at_expr=revoked_at_expr,
        issued_by_join=issued_by_join,
        user_join=_text_eq("uu.id", "r.user_id"),
    )
    params: dict[str, Any] = {}
    if status:
        sql += f" AND {status_expr} = :status"
        params["status"] = status
    if search:
        sql += " AND COALESCE(uu.email, '') ILIKE :search"
        params["search"] = f"%{search.strip()}%"
    sql += f" ORDER BY {starts_at_expr} DESC NULLS LAST, r.id DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def list_moderation_actions(
    session: Session,
    action_type: Optional[str] = None,
    target_type: Optional[str] = None,
    moderator_search: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT a.id,
               a.moderator_id,
               u.email AS moderator_email,
               a.action_type,
               a.target_type,
               a.target_id,
               a.reason,
               a.payload,
               a.created_at
        FROM moderation_actions a
        LEFT JOIN users u ON {user_join}
        WHERE 1 = 1
    """
    sql = sql.format(user_join=_text_eq("u.id", "a.moderator_id"))
    params: dict[str, Any] = {}
    if action_type:
        sql += " AND a.action_type = :action_type"
        params["action_type"] = action_type
    if target_type:
        sql += " AND a.target_type = :target_type"
        params["target_type"] = target_type
    if moderator_search:
        sql += " AND COALESCE(u.email, '') ILIKE :moderator_search"
        params["moderator_search"] = f"%{moderator_search.strip()}%"
    sql += " ORDER BY a.created_at DESC LIMIT 500"
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def create_restriction(
    session: Session,
    user_id: str,
    restriction_type: str,
    moderator_id: str,
    ends_at: Optional[datetime] = None,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    if restriction_type not in {"warning", "ban", "shadowban"}:
        raise ValueError("Unsupported restriction type")

    target_user = _get_user_row(session, user_id)
    if not target_user:
        raise ValueError("User not found")

    restriction_id = str(uuid.uuid4())
    now = _now()
    columns = _get_table_columns(session, "user_restrictions")
    values: Dict[str, Any] = {"id": restriction_id, "user_id": user_id}
    if "type" in columns:
        values["type"] = restriction_type
    status_value = _map_status_assignment(session, "user_restrictions", "status", "active")
    if "status" in columns and status_value is not None:
        values["status"] = status_value
    if "issued_by" in columns:
        values["issued_by"] = moderator_id
    if "starts_at" in columns:
        values["starts_at"] = now
    elif "created_at" in columns:
        values["created_at"] = now
    if "ends_at" in columns:
        values["ends_at"] = ends_at
    if "reason" in columns and comment:
        values["reason"] = comment
    _insert_row(session, "user_restrictions", values)
    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="restriction_set",
        target_type="user",
        target_id=user_id,
        reason=comment,
        payload={"type": restriction_type, "ends_at": ends_at.isoformat() if ends_at else None},
    )
    _add_notification(
        session=session,
        user_id=user_id,
        notif_type="moderation",
        body=f"Для вашей учётной записи установлено ограничение: {restriction_type}",
        payload={"restriction_id": restriction_id, "type": restriction_type},
    )
    session.commit()
    rows = [item for item in list_restrictions(session, status=None) if str(item["id"]) == restriction_id]
    return rows[0] if rows else {
        "id": restriction_id,
        "user_id": user_id,
        "type": restriction_type,
        "status": "active",
        "issued_by": moderator_id,
        "issued_by_email": None,
        "starts_at": now,
        "ends_at": ends_at,
        "revoked_at": None,
    }


def revoke_restriction(
    session: Session,
    restriction_id: str,
    moderator_id: str,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    type_expr = "type" if _has_column(session, "user_restrictions", "type") else "NULL::text AS type"
    restriction = session.execute(
        text(
            f"""
            SELECT id, user_id, {type_expr}
            FROM user_restrictions
            WHERE id = :restriction_id
            """
        ),
        {"restriction_id": restriction_id},
    ).mappings().first()
    if not restriction:
        raise ValueError("Restriction not found")

    columns = _get_table_columns(session, "user_restrictions")
    now = _now()
    update_clauses: list[str] = []
    params: dict[str, Any] = {"restriction_id": restriction_id}
    status_value = _map_status_assignment(session, "user_restrictions", "status", "revoked")
    if "status" in columns and status_value is not None:
        update_clauses.append("status = :status_value")
        params["status_value"] = status_value
    if "revoked_at" in columns:
        update_clauses.append("revoked_at = :revoked_at")
        params["revoked_at"] = now
    if "reason" in columns and comment:
        update_clauses.append("reason = :reason")
        params["reason"] = comment
    if update_clauses:
        session.execute(
            text(f"UPDATE user_restrictions SET {', '.join(update_clauses)} WHERE id::text = :restriction_id"),
            params,
        )
    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="restriction_revoke",
        target_type="user",
        target_id=str(restriction["user_id"]),
        reason=comment,
        payload={"restriction_id": str(restriction["id"]), "type": restriction.get("type")},
    )
    _add_notification(
        session=session,
        user_id=str(restriction["user_id"]),
        notif_type="moderation",
        body=f"Ограничение снято: {restriction.get('type') or 'restriction'}",
        payload={"restriction_id": str(restriction['id'])},
    )
    session.commit()
    rows = [item for item in list_restrictions(session, status=None) if str(item["id"]) == restriction_id]
    return rows[0] if rows else {
        "id": restriction_id,
        "user_id": str(restriction["user_id"]),
        "type": restriction.get("type"),
        "status": "revoked",
        "issued_by": None,
        "issued_by_email": None,
        "starts_at": None,
        "ends_at": None,
        "revoked_at": now,
    }
