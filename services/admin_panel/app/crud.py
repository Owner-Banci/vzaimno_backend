from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit import log_audit_event
from app.chat import publish_chat_message_sync, publish_thread_preview_sync
from app.security import hash_password
from app.support import (
    assign_support_thread as shared_assign_support_thread,
    get_support_thread_for_admin as shared_get_support_thread_for_admin,
    list_support_messages_for_admin as shared_list_support_messages_for_admin,
    list_support_threads_for_admin as shared_list_support_threads_for_admin,
    post_admin_support_message as shared_post_admin_support_message,
)
from app.task_compat import ensure_task_payload, task_to_announcement_status

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
_ACTIONABLE_REPORT_OUTCOMES = {
    "warning",
    "mute_chat",
    "restrict_posting",
    "restrict_offers",
    "temporary_ban",
    "permanent_ban",
    "custom_restriction",
}
_REPORT_RESOLUTION_ALIASES = {
    "valid": "no_action",
    "invalid": "report_rejected",
}
_RESTRICTION_TYPE_ALIASES = {
    "warning": "warning",
    "mute_chat": "mute_chat",
    "restrict_posting": "restrict_posting",
    "publish_ban": "restrict_posting",
    "restrict_offers": "restrict_offers",
    "response_ban": "restrict_offers",
    "temporary_ban": "temporary_ban",
    "temp_ban": "temporary_ban",
    "permanent_ban": "permanent_ban",
    "perm_ban": "permanent_ban",
    "custom": "custom",
    "custom_restriction": "custom",
    "shadowban": "shadowban",
}


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
    return _map_enum_value(
        session,
        table_name,
        column_name,
        target_status,
        _OPENISH_STATUSES if target_status == "open" else _RESOLVEDISH_STATUSES,
    )


def _map_enum_value(
    session: Session,
    table_name: str,
    column_name: str,
    target_value: str,
    aliases: Sequence[str] = (),
) -> Optional[str]:
    if not _has_column(session, table_name, column_name):
        return None
    data_type, udt_name = _get_column_info(session, table_name, column_name)
    if data_type != "USER-DEFINED" or not udt_name:
        return target_value
    labels = _get_enum_labels(session, udt_name)
    if not labels:
        return target_value
    by_lower = {label.lower(): label for label in labels}
    for candidate in (target_value, *aliases):
        value = by_lower.get(candidate.lower())
        if value:
            return value
    return labels[0]


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


def _get_admin_account_row(session: Session, admin_account_id: str) -> Optional[dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT
                aa.id::text AS id,
                aa.login_identifier,
                aa.email,
                aa.password_hash,
                aa.role,
                aa.status,
                aa.display_name,
                aa.linked_user_account_id::text AS linked_user_account_id,
                aa.created_by_admin_id::text AS created_by_admin_id,
                aa.created_at,
                aa.updated_at,
                aa.last_login_at,
                aa.disabled_at,
                aa.password_reset_required
            FROM admin_accounts aa
            WHERE aa.id::text = :admin_account_id
            LIMIT 1
            """
        ),
        {"admin_account_id": admin_account_id},
    ).mappings().first()
    return dict(row) if row else None


def _resolve_actor_identity(session: Session, actor_id: str) -> dict[str, Any]:
    admin = _get_admin_account_row(session, actor_id)
    if admin:
        return {
            "actor_type": "admin",
            "actor_admin_account_id": str(admin["id"]),
            "actor_user_account_id": None,
            "legacy_user_account_id": admin.get("linked_user_account_id"),
            "display_name": _display_name(admin.get("display_name"), admin.get("email"), admin.get("login_identifier"), admin.get("id")),
            "role": str(admin.get("role") or "support"),
        }

    user = _get_user_row(session, actor_id)
    if user:
        return {
            "actor_type": "user",
            "actor_admin_account_id": None,
            "actor_user_account_id": str(user["id"]),
            "legacy_user_account_id": str(user["id"]),
            "display_name": _display_name(user.get("email"), user.get("id")),
            "role": str(user.get("role") or "user"),
        }

    return {
        "actor_type": "system",
        "actor_admin_account_id": None,
        "actor_user_account_id": None,
        "legacy_user_account_id": None,
        "display_name": actor_id,
        "role": "system",
    }


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
            placeholders.append(f"CAST(:{param_name} AS JSONB)")
            params[param_name] = json.dumps(_json_ready(values[column]), ensure_ascii=False)
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


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


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
    actor = _resolve_actor_identity(session, moderator_id)
    action_id = str(uuid.uuid4())
    audit_values: Dict[str, Any] = {
        "id": action_id,
        "actor_type": actor["actor_type"],
        "actor_user_account_id": actor["actor_user_account_id"],
        "actor_admin_account_id": actor["actor_admin_account_id"],
        "action": action_type,
        "target_type": target_type,
        "target_id": target_id,
        "result": "success",
        "details": _json_ready(payload or {}),
    }
    if reason:
        audit_values["details"]["reason"] = reason
    audit_columns = _get_table_columns(session, "audit_logs")
    values = {key: value for key, value in audit_values.items() if key in audit_columns}
    _insert_row(session, "audit_logs", values, jsonb_columns={"details"} if "details" in values else set())

    legacy_columns = _get_table_columns(session, "moderation_actions")
    if actor["legacy_user_account_id"] and legacy_columns:
        columns = legacy_columns
        legacy_values: Dict[str, Any] = {
            "id": action_id,
            "moderator_id": actor["legacy_user_account_id"],
            "action_type": action_type,
            "target_type": target_type,
            "target_id": target_id,
        }
        if "reason" in columns:
            legacy_values["reason"] = reason
        if "payload" in columns:
            legacy_values["payload"] = payload or {}
        _insert_row(
            session,
            "moderation_actions",
            legacy_values,
            jsonb_columns={"payload"} if "payload" in legacy_values else set(),
        )
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


def _normalize_text_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(str(value).strip().split())
    return normalized or None


def _display_name(*values: Any) -> str:
    for value in values:
        normalized = _normalize_text_value(value)
        if normalized:
            return normalized
    return "—"


def _normalize_media_url(value: Any) -> Optional[str]:
    normalized = _normalize_text_value(value)
    if not normalized:
        return None
    if normalized.startswith(("http://", "https://", "/")):
        return normalized
    return "/" + normalized.lstrip("/")


def _extract_media_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _normalize_media_url(value)
    if not isinstance(value, dict):
        return None
    for key in (
        "preview_url",
        "previewUrl",
        "thumbnail_url",
        "thumbnailUrl",
        "url",
        "image_url",
        "imageUrl",
        "file_url",
        "fileUrl",
        "path",
        "src",
    ):
        candidate = _normalize_media_url(value.get(key))
        if candidate:
            return candidate
    return _extract_media_url(value.get("file"))


def _extract_media_items(data: Dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates: list[Any] = []
    moderation = _ensure_obj(data.get("moderation"))
    image_info = _ensure_obj(moderation.get("image"))
    for key in ("media", "images", "photos"):
        value = data.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    if isinstance(image_info.get("items"), list):
        candidates.extend(image_info["items"])

    for raw in candidates:
        item = raw if isinstance(raw, dict) else {"path": raw}
        url = _extract_media_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "url": url,
                "filename": _normalize_text_value(item.get("filename") or item.get("name")) or url.rsplit("/", 1)[-1],
                "nsfw": item.get("nsfw"),
                "top_label": _normalize_text_value(item.get("top_label")),
                "top_prob": item.get("top_prob"),
            }
        )
    return items


def _task_status_for_admin(row: Dict[str, Any]) -> str:
    return task_to_announcement_status(
        row.get("task_status"),
        row.get("moderation_status"),
        row.get("deleted_at"),
        assignment_status=row.get("assignment_status"),
        execution_stage=row.get("execution_stage"),
    )


def _task_row_base(session: Session, task_id: str) -> Optional[dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT
                t.id::text AS id,
                t.customer_id::text AS user_id,
                u.email AS user_email,
                up.display_name AS user_display_name,
                COALESCE(
                    c.slug,
                    t.extra->'task'->'builder'->>'resolved_category',
                    t.extra->>'category',
                    t.extra->>'main_group',
                    'help'
                ) AS category_slug,
                t.title,
                t.description,
                t.status::text AS task_status,
                t.moderation_status::text AS moderation_status,
                t.created_at,
                t.updated_at,
                t.published_at,
                t.closed_at,
                t.deleted_at,
                t.responses_count,
                t.accepted_offer_id::text AS accepted_offer_id,
                t.address_text,
                CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_Y(t.location_point::geometry) END AS location_lat,
                CASE WHEN t.location_point IS NULL THEN NULL ELSE ST_X(t.location_point::geometry) END AS location_lon,
                t.reward_amount,
                t.price_type::text AS price_type,
                t.budget_min,
                t.budget_max,
                t.quick_offer_price,
                t.reoffer_policy,
                t.extra,
                a.id::text AS legacy_id,
                a.status AS legacy_status,
                a.data AS legacy_data,
                a.deleted_at AS legacy_deleted_at,
                assn.id::text AS assignment_id,
                assn.assignment_status,
                assn.execution_stage,
                assn.performer_id::text AS performer_id,
                pu.email AS performer_email,
                pup.display_name AS performer_display_name,
                assn.chat_thread_id::text AS assignment_chat_thread_id,
                assn.route_visibility
            FROM tasks t
            LEFT JOIN users u
              ON {user_join}
            LEFT JOIN user_profiles up
              ON {profile_join}
            LEFT JOIN categories c
              ON c.id = t.category_id
            LEFT JOIN announcements a
              ON a.id::text = t.id::text
            LEFT JOIN LATERAL (
                SELECT
                    ta.id,
                    ta.assignment_status,
                    ta.execution_stage,
                    ta.performer_id,
                    ta.chat_thread_id,
                    ta.route_visibility
                FROM task_assignments ta
                WHERE ta.task_id = t.id
                ORDER BY
                    CASE
                        WHEN ta.assignment_status IN ('assigned', 'in_progress') THEN 0
                        WHEN ta.assignment_status = 'completed' THEN 1
                        ELSE 2
                    END,
                    ta.updated_at DESC NULLS LAST,
                    ta.created_at DESC NULLS LAST
                LIMIT 1
            ) assn ON TRUE
            LEFT JOIN users pu
              ON {performer_join}
            LEFT JOIN user_profiles pup
              ON {performer_profile_join}
            WHERE t.id::text = :task_id
            LIMIT 1
            """
            .format(
                user_join=_text_eq("u.id", "t.customer_id"),
                profile_join=_text_eq("up.user_id", "t.customer_id"),
                performer_join=_text_eq("pu.id", "assn.performer_id"),
                performer_profile_join=_text_eq("pup.user_id", "assn.performer_id"),
            )
        ),
        {"task_id": task_id},
    ).mappings().first()
    return dict(row) if row else None


def _task_route_points(session: Session, task_id: str, data: Dict[str, Any]) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT
                point_order,
                title,
                address_text,
                point_kind,
                CASE WHEN point IS NULL THEN NULL ELSE ST_Y(point::geometry) END AS lat,
                CASE WHEN point IS NULL THEN NULL ELSE ST_X(point::geometry) END AS lon
            FROM task_route_points
            WHERE task_id::text = :task_id
            ORDER BY point_order ASC
            """
        ),
        {"task_id": task_id},
    ).mappings().all()
    route_points = [
        {
            "point_order": int(row["point_order"] or 0),
            "title": _normalize_text_value(row["title"]) or f"Точка {int(row['point_order'] or 0) + 1}",
            "address_text": _normalize_text_value(row["address_text"]),
            "point_kind": _normalize_text_value(row["point_kind"]),
            "lat": float(row["lat"]) if row["lat"] is not None else None,
            "lon": float(row["lon"]) if row["lon"] is not None else None,
        }
        for row in rows
    ]
    if route_points:
        return route_points

    task_payload = _ensure_obj(data.get("task"))
    route = _ensure_obj(task_payload.get("route"))
    fallback_points: list[dict[str, Any]] = []
    for index, (label, value, kind) in enumerate(
        (
            ("Старт", _ensure_obj(route.get("source")), "source"),
            ("Финиш", _ensure_obj(route.get("destination")), "destination"),
        )
    ):
        point = _ensure_obj(value.get("point"))
        if not point:
            continue
        fallback_points.append(
            {
                "point_order": index,
                "title": label,
                "address_text": _normalize_text_value(value.get("address")),
                "point_kind": kind,
                "lat": point.get("lat"),
                "lon": point.get("lon"),
            }
        )
    return fallback_points


def _task_sections(data: Dict[str, Any]) -> list[dict[str, Any]]:
    task_payload = _ensure_obj(data.get("task"))
    builder = _ensure_obj(task_payload.get("builder"))
    attributes = _ensure_obj(task_payload.get("attributes"))
    contacts = _ensure_obj(task_payload.get("contacts"))
    offer_policy = _ensure_obj(task_payload.get("offer_policy"))
    execution = _ensure_obj(task_payload.get("execution"))
    sections: list[dict[str, Any]] = []
    for title, source in (
        (
            "Параметры задачи",
            {
                "Тип действия": builder.get("action_type"),
                "Кратко": builder.get("task_brief"),
                "Срочность": builder.get("urgency"),
                "Категория": builder.get("resolved_category"),
                "Заметки": data.get("notes"),
            },
        ),
        (
            "Контакты",
            {
                "Контакт": contacts.get("name"),
                "Телефон": contacts.get("phone"),
                "Способ связи": contacts.get("method"),
                "Аудитория": contacts.get("audience"),
            },
        ),
        (
            "Исполнение",
            {
                "Статус исполнения": execution.get("status"),
                "Route visibility": execution.get("route_visibility"),
                "Chat thread": execution.get("chat_thread_id"),
            },
        ),
        (
            "Отклики",
            {
                "Быстрый отклик": offer_policy.get("quick_offer_price"),
                "Повторный отклик": offer_policy.get("reoffer_policy"),
            },
        ),
        (
            "Атрибуты",
            {
                "Нужен транспорт": attributes.get("requires_vehicle"),
                "Нужен багажник": attributes.get("needs_trunk"),
                "Нужен грузчик": attributes.get("needs_loader"),
                "Фотоотчёт": attributes.get("photo_report_required"),
                "Время ожидания, мин": attributes.get("waiting_minutes"),
            },
        ),
    ):
        items = [
            {"label": label, "value": value}
            for label, value in source.items()
            if value not in (None, "", [], {}, False)
        ]
        if items:
            sections.append({"title": title, "items": items})
    return sections


def _task_card(session: Session, row: Dict[str, Any]) -> dict[str, Any]:
    assignment = {
        "assignment_status": row.get("assignment_status"),
        "execution_stage": row.get("execution_stage"),
        "performer_id": row.get("performer_id"),
        "chat_thread_id": row.get("assignment_chat_thread_id"),
        "route_visibility": row.get("route_visibility"),
    }
    status = _task_status_for_admin(row)
    data = ensure_task_payload(
        _ensure_obj(row.get("extra")),
        title=str(row.get("title") or ""),
        announcement_status=status,
        deleted_at=row.get("deleted_at"),
        assignment=assignment,
    )
    moderation = _ensure_obj(data.get("moderation"))
    route_points = _task_route_points(session, str(row["id"]), data)
    return {
        "id": str(row["id"]),
        "user_id": row.get("user_id"),
        "user_email": row.get("user_email"),
        "user_display_name": _display_name(row.get("user_display_name"), row.get("user_email"), row.get("user_id")),
        "category": _display_name(row.get("category_slug")),
        "title": str(row.get("title") or ""),
        "status": status,
        "task_status": row.get("task_status"),
        "moderation_status": row.get("moderation_status"),
        "legacy_status": row.get("legacy_status"),
        "description": _display_name(
            _normalize_text_value(row.get("description")),
            _normalize_text_value(data.get("generated_description")),
            _normalize_text_value(data.get("notes")),
        ),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "published_at": row.get("published_at"),
        "closed_at": row.get("closed_at"),
        "deleted_at": row.get("deleted_at"),
        "responses_count": int(row.get("responses_count") or 0),
        "accepted_offer_id": row.get("accepted_offer_id"),
        "reward_amount": row.get("reward_amount"),
        "price_type": row.get("price_type"),
        "budget_min": row.get("budget_min"),
        "budget_max": row.get("budget_max"),
        "quick_offer_price": row.get("quick_offer_price"),
        "address_text": _normalize_text_value(row.get("address_text")) or _normalize_text_value(data.get("address_text")),
        "source_address": _normalize_text_value(data.get("source_address")),
        "destination_address": _normalize_text_value(data.get("destination_address")),
        "location_lat": float(row["location_lat"]) if row.get("location_lat") is not None else None,
        "location_lon": float(row["location_lon"]) if row.get("location_lon") is not None else None,
        "media": _extract_media_items(data),
        "moderation": moderation,
        "moderation_summary": {
            "decision_status": _normalize_text_value(_ensure_obj(moderation.get("decision")).get("status")),
            "decision_message": _normalize_text_value(_ensure_obj(moderation.get("decision")).get("message")),
            "reasons": _ensure_list(moderation.get("reasons")),
            "suggestions": _ensure_list(moderation.get("suggestions")),
            "appeal": _ensure_obj(moderation.get("appeal")),
        },
        "route_points": route_points,
        "assignment": {
            "id": row.get("assignment_id"),
            "assignment_status": row.get("assignment_status"),
            "execution_stage": row.get("execution_stage"),
            "performer_id": row.get("performer_id"),
            "performer_display_name": _display_name(
                row.get("performer_display_name"),
                row.get("performer_email"),
                row.get("performer_id"),
            ),
            "performer_email": row.get("performer_email"),
            "chat_thread_id": row.get("assignment_chat_thread_id"),
            "route_visibility": row.get("route_visibility"),
        }
        if row.get("assignment_id")
        else None,
        "sections": _task_sections(data),
        "data": data,
        "raw_json": {
            "task": _json_ready(row),
            "payload": _json_ready(data),
            "legacy": {
                "id": row.get("legacy_id"),
                "status": row.get("legacy_status"),
                "deleted_at": _json_ready(row.get("legacy_deleted_at")),
                "data": _json_ready(row.get("legacy_data")),
            },
            "route_points": _json_ready(route_points),
        },
    }


def _sync_legacy_announcement_row(
    session: Session,
    row: Dict[str, Any],
    *,
    data: Dict[str, Any],
    task_status: str,
    moderation_status: str,
    deleted_at: Optional[datetime],
    updated_at: datetime,
) -> None:
    legacy_status = task_to_announcement_status(
        task_status,
        moderation_status,
        deleted_at,
        assignment_status=row.get("assignment_status"),
        execution_stage=row.get("execution_stage"),
    )
    lat = row.get("location_lat")
    lon = row.get("location_lon")
    payload_json = json.dumps(data, ensure_ascii=False)
    params = {
        "task_id": row["id"],
        "user_id": row["user_id"],
        "category": _normalize_text_value(row.get("category_slug")) or "help",
        "title": row.get("title"),
        "status": legacy_status,
        "data": payload_json,
        "created_at": row.get("created_at") or updated_at,
        "updated_at": updated_at,
        "deleted_at": deleted_at,
        "lon_a": lon,
        "lat_a": lat,
        "lon_b": lon,
        "lat_b": lat,
    }
    if row.get("legacy_id"):
        session.execute(
            text(
                """
                UPDATE announcements
                SET user_id = :user_id,
                    category = :category,
                    title = :title,
                    status = :status,
                    data = CAST(:data AS jsonb),
                    updated_at = :updated_at,
                    deleted_at = :deleted_at,
                    location_point = CASE
                        WHEN CAST(:lon_a AS double precision) IS NULL OR CAST(:lat_a AS double precision) IS NULL THEN NULL
                        ELSE ST_SetSRID(
                            ST_MakePoint(
                                CAST(:lon_b AS double precision),
                                CAST(:lat_b AS double precision)
                            ),
                            4326
                        )::geography
                    END
                WHERE id::text = :task_id
                """
            ),
            params,
        )
        return

    session.execute(
        text(
            """
            INSERT INTO announcements (
                id, user_id, category, title, status, data, created_at, updated_at, deleted_at, location_point
            )
            VALUES (
                :task_id, :user_id, :category, :title, :status, CAST(:data AS jsonb),
                :created_at, :updated_at, :deleted_at,
                CASE
                    WHEN CAST(:lon_a AS double precision) IS NULL OR CAST(:lat_a AS double precision) IS NULL THEN NULL
                    ELSE ST_SetSRID(
                        ST_MakePoint(
                            CAST(:lon_b AS double precision),
                            CAST(:lat_b AS double precision)
                        ),
                        4326
                    )::geography
                END
            )
            """
        ),
        params,
    )


def _user_brief(session: Session, user_id: str) -> Optional[dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT u.id::text AS id,
                   u.email,
                   up.display_name,
                   u.role
            FROM users u
            LEFT JOIN user_profiles up
              ON {profile_join}
            WHERE u.id::text = :user_id
            LIMIT 1
            """.format(profile_join=_text_eq("up.user_id", "u.id"))
        ),
        {"user_id": user_id},
    ).mappings().first()
    if not row:
        return None
    result = dict(row)
    result["display_label"] = _display_name(result.get("display_name"), result.get("email"), result.get("id"))
    return result


def _report_target_user_id(session: Session, target_type: str, target_id: str) -> Optional[str]:
    normalized_type = str(target_type or "").lower()
    if normalized_type == "user":
        return target_id
    if normalized_type == "message":
        row = session.execute(
            text("SELECT sender_id::text FROM chat_messages WHERE id::text = :target_id"),
            {"target_id": target_id},
        ).first()
        return str(row[0]) if row and row[0] else None
    if normalized_type in {"task", "announcement"}:
        row = session.execute(
            text(
                """
                SELECT customer_id::text
                FROM tasks
                WHERE id::text = :target_id
                LIMIT 1
                """
            ),
            {"target_id": target_id},
        ).first()
        if row and row[0]:
            return str(row[0])
        row = session.execute(
            text(
                """
                SELECT user_id::text
                FROM announcements
                WHERE id::text = :target_id
                LIMIT 1
                """
            ),
            {"target_id": target_id},
        ).first()
        return str(row[0]) if row and row[0] else None
    return None


def _report_target_context(session: Session, report: Dict[str, Any]) -> dict[str, Any]:
    def _report_message_payload(item: dict[str, Any]) -> dict[str, Any]:
        sender_type = str(item.get("sender_type") or "user")
        if sender_type == "admin":
            sender_display_label = _display_name(
                item.get("sender_admin_display_name"),
                item.get("sender_label"),
                item.get("sender_admin_email"),
                item.get("sender_admin_login_identifier"),
                item.get("sender_admin_account_id"),
            )
        elif sender_type == "system":
            sender_display_label = _display_name(item.get("sender_label"), "Система")
        else:
            sender_display_label = _display_name(
                item.get("sender_user_display_name"),
                item.get("sender_user_email"),
                item.get("sender_user_account_id"),
                item.get("sender_id"),
            )
        return {
            **item,
            "sender_display_label": sender_display_label,
            "sender_badge": _display_name(item.get("sender_label"), sender_type),
        }

    target_type = str(report.get("target_type") or "")
    target_id = str(report.get("target_id") or "")
    if target_type in {"task", "announcement"}:
        row = _task_row_base(session, target_id)
        if not row:
            return {"kind": target_type, "missing": True}
        return {"kind": "task", "task": _task_card(session, row)}

    if target_type == "user":
        return {"kind": "user", "user": _user_brief(session, target_id), "missing": _user_brief(session, target_id) is None}

    if target_type != "message":
        return {"kind": target_type, "missing": True}

    message = session.execute(
        text(
            """
            SELECT
                m.id::text AS id,
                m.thread_id::text AS thread_id,
                m.sender_id::text AS sender_id,
                COALESCE(m.sender_type, CASE WHEN COALESCE(m.type::text, 'text') = 'system' THEN 'system' ELSE 'user' END) AS sender_type,
                m.sender_user_account_id::text AS sender_user_account_id,
                m.sender_admin_account_id::text AS sender_admin_account_id,
                m.sender_label,
                m.text,
                m.created_at,
                ct.kind::text AS thread_kind,
                COALESCE(ct.task_id::text, ta.task_id::text, tf.task_id::text) AS task_id,
                su.email AS sender_user_email,
                sup.display_name AS sender_user_display_name,
                aa.email AS sender_admin_email,
                aa.login_identifier AS sender_admin_login_identifier,
                aa.display_name AS sender_admin_display_name
            FROM chat_messages m
            JOIN chat_threads ct
              ON ct.id = m.thread_id
            LEFT JOIN task_assignments ta
              ON ta.id = ct.assignment_id
            LEFT JOIN task_offers tf
              ON tf.id = COALESCE(ct.offer_id, ta.offer_id)
            LEFT JOIN users su
              ON {sender_join}
            LEFT JOIN user_profiles sup
              ON {sender_profile_join}
            LEFT JOIN admin_accounts aa
              ON aa.id = m.sender_admin_account_id
            WHERE m.id::text = :message_id
            LIMIT 1
            """
            .format(
                sender_join=_text_eq("su.id", "m.sender_id"),
                sender_profile_join=_text_eq("sup.user_id", "m.sender_id"),
            )
        ),
        {"message_id": target_id},
    ).mappings().first()
    if not message:
        return {"kind": "message", "missing": True}

    thread_messages = session.execute(
        text(
            """
            SELECT
                m.id::text AS id,
                m.sender_id::text AS sender_id,
                COALESCE(m.sender_type, CASE WHEN COALESCE(m.type::text, 'text') = 'system' THEN 'system' ELSE 'user' END) AS sender_type,
                m.sender_user_account_id::text AS sender_user_account_id,
                m.sender_admin_account_id::text AS sender_admin_account_id,
                m.sender_label,
                su.email AS sender_user_email,
                sup.display_name AS sender_user_display_name,
                aa.email AS sender_admin_email,
                aa.login_identifier AS sender_admin_login_identifier,
                aa.display_name AS sender_admin_display_name,
                m.text,
                m.created_at
            FROM chat_messages m
            LEFT JOIN users su
              ON {sender_join}
            LEFT JOIN user_profiles sup
              ON {sender_profile_join}
            LEFT JOIN admin_accounts aa
              ON aa.id = m.sender_admin_account_id
            WHERE m.thread_id::text = :thread_id
              AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT 8
            """
            .format(
                sender_join=_text_eq("su.id", "m.sender_id"),
                sender_profile_join=_text_eq("sup.user_id", "m.sender_id"),
            )
        ),
        {"thread_id": message["thread_id"]},
    ).mappings().all()
    task_card = None
    if message["task_id"]:
        row = _task_row_base(session, str(message["task_id"]))
        if row:
            task_card = _task_card(session, row)

    return {
        "kind": "message",
        "message": _report_message_payload(dict(message)),
        "thread_messages": [_report_message_payload(dict(item)) for item in reversed(thread_messages)],
        "task": task_card,
    }


def _report_target_summary(session: Session, target_type: str, target_id: str) -> Optional[str]:
    normalized_type = str(target_type or "")
    if normalized_type in {"task", "announcement"}:
        row = _task_row_base(session, target_id)
        if not row:
            return None
        return _display_name(row.get("title"), row.get("id"))
    if normalized_type == "user":
        user = _user_brief(session, target_id)
        return user["display_label"] if user else None
    if normalized_type == "message":
        row = session.execute(
            text(
                """
                SELECT text
                FROM chat_messages
                WHERE id::text = :message_id
                LIMIT 1
                """
            ),
            {"message_id": target_id},
        ).first()
        return _normalize_text_value(row[0]) if row and row[0] else None
    return None

def _admin_access_row_for_user(session: Session, user_id: str) -> Optional[dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT
                aa.id::text AS id,
                aa.login_identifier,
                aa.email,
                aa.role,
                aa.status,
                aa.display_name,
                aa.linked_user_account_id::text AS linked_user_account_id,
                aa.created_by_admin_id::text AS created_by_admin_id,
                COALESCE(caa.display_name, caa.email, caa.login_identifier) AS created_by_display_name,
                aa.created_at,
                aa.updated_at,
                aa.last_login_at,
                aa.disabled_at,
                aa.password_reset_required
            FROM admin_accounts aa
            LEFT JOIN admin_accounts caa
              ON caa.id = aa.created_by_admin_id
            WHERE aa.linked_user_account_id::text = :user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    return dict(row) if row else None


def _admin_access_payload(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return {
            "status": "absent",
            "admin_account_id": None,
            "login_identifier": None,
            "email": None,
            "role": None,
            "display_name": None,
            "linked_user_account_id": None,
            "created_by_admin_account_id": None,
            "created_by_display_label": None,
            "created_at": None,
            "updated_at": None,
            "last_login_at": None,
            "disabled_at": None,
            "password_reset_required": False,
            "is_active": False,
        }
    status = str(row.get("status") or "active")
    return {
        "status": "disabled" if status == "disabled" or row.get("disabled_at") else "active",
        "admin_account_id": row.get("id"),
        "login_identifier": row.get("login_identifier"),
        "email": row.get("email"),
        "role": row.get("role"),
        "display_name": row.get("display_name"),
        "linked_user_account_id": row.get("linked_user_account_id"),
        "created_by_admin_account_id": row.get("created_by_admin_id"),
        "created_by_display_label": _display_name(
            row.get("created_by_display_name"),
            row.get("created_by_admin_id"),
        ),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
        "disabled_at": row.get("disabled_at"),
        "password_reset_required": bool(row.get("password_reset_required")),
        "is_active": status == "active" and not row.get("disabled_at"),
    }


def get_user_admin_access(session: Session, user_id: str) -> dict[str, Any]:
    user = _get_user_row(session, user_id)
    if not user:
        raise ValueError("User not found")
    payload = _admin_access_payload(_admin_access_row_for_user(session, user_id))
    payload["user_id"] = str(user["id"])
    payload["user_email"] = user.get("email")
    return payload


def list_users(session: Session, search: Optional[str] = None) -> list[dict[str, Any]]:
    sql = """
        SELECT
            u.id::text AS id,
            u.email,
            u.role::text AS role,
            u.created_at,
            aa.id::text AS admin_account_id,
            aa.status AS admin_status,
            aa.role AS admin_role
        FROM users u
        LEFT JOIN admin_accounts aa
          ON aa.linked_user_account_id = u.id
        WHERE u.deleted_at IS NULL
    """
    params: dict[str, Any] = {}
    if search:
        sql += " AND COALESCE(u.email, '') ILIKE :search"
        params["search"] = f"%{search.strip()}%"
    sql += " ORDER BY u.created_at DESC"
    rows = session.execute(text(sql), params).mappings().all()
    return [
        {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"],
            "created_at": row["created_at"],
            "admin_access_status": "absent"
            if row["admin_account_id"] is None
            else ("disabled" if str(row["admin_status"] or "") == "disabled" else "active"),
            "admin_account_id": str(row["admin_account_id"]) if row["admin_account_id"] is not None else None,
            "admin_role": str(row["admin_role"]) if row["admin_role"] is not None else None,
        }
        for row in rows
    ]


def get_user_detail(session: Session, user_id: str) -> dict[str, Any]:
    user = _get_user_row(session, user_id)
    if not user:
        raise ValueError("User not found")

    restrictions = list_restrictions(session, search=str(user["email"]), status=None)
    admin_access = _admin_access_payload(_admin_access_row_for_user(session, user_id))
    audit_trail = list_moderation_actions(session, target_type="user", target_id=str(user["id"]))
    if admin_access.get("admin_account_id"):
        audit_trail.extend(
            list_moderation_actions(
                session,
                target_type="admin_account",
                target_id=str(admin_access["admin_account_id"]),
            )
        )
    audit_trail = sorted(
        audit_trail,
        key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:20]
    return {
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "created_at": user["created_at"],
        "admin_access": admin_access,
        "audit_trail": audit_trail,
        "restrictions": [item for item in restrictions if str(item["user_id"]) == str(user["id"])],
    }


def update_user_role(
    session: Session,
    target_user_id: str,
    role: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    raise ValueError("Direct role mutation is disabled. Use separate admin access management.")


def _require_admin_access_manager(session: Session, actor_admin_account_id: str) -> dict[str, Any]:
    actor = _get_admin_account_row(session, actor_admin_account_id)
    if not actor or str(actor.get("role") or "").strip().lower() != "admin":
        raise PermissionError("Only admin can manage admin access")
    if str(actor.get("status") or "").strip().lower() != "active" or actor.get("disabled_at"):
        raise PermissionError("Only active admin accounts can manage admin access")
    return actor


def create_admin_access_for_user(
    session: Session,
    user_id: str,
    login_identifier: str,
    display_name: str,
    role: str,
    password: str,
    actor_admin_account_id: str,
    email: Optional[str] = None,
) -> dict[str, Any]:
    _require_admin_access_manager(session, actor_admin_account_id)

    user = _get_user_row(session, user_id)
    if not user:
        raise ValueError("User not found")
    if _admin_access_row_for_user(session, user_id):
        raise ValueError("Admin account already linked to this user")

    normalized_login = _normalize_text_value(login_identifier)
    normalized_display_name = _normalize_text_value(display_name)
    normalized_role = (role or "").strip().lower()
    normalized_email = _normalize_text_value(email) or user.get("email")
    if not normalized_login or not normalized_display_name:
        raise ValueError("Admin login and display name are required")
    if normalized_role not in {"support", "moderator", "admin"}:
        raise ValueError("Unsupported admin role")
    if session.execute(
        text(
            """
            SELECT 1
            FROM admin_accounts
            WHERE lower(login_identifier) = lower(:login_identifier)
            LIMIT 1
            """
        ),
        {"login_identifier": normalized_login},
    ).first():
        raise ValueError("Admin login already exists")

    admin_account_id = str(uuid.uuid4())
    now = _now()
    session.execute(
        text(
            """
            INSERT INTO admin_accounts (
                id,
                login_identifier,
                email,
                password_hash,
                role,
                status,
                display_name,
                linked_user_account_id,
                created_by_admin_id,
                created_at,
                updated_at,
                password_reset_required
            )
            VALUES (
                CAST(:admin_account_id AS uuid),
                :login_identifier,
                :email,
                :password_hash,
                :role,
                'active',
                :display_name,
                CAST(:linked_user_account_id AS uuid),
                CAST(:created_by_admin_id AS uuid),
                :now,
                :now,
                TRUE
            )
            """
        ),
        {
            "admin_account_id": admin_account_id,
            "login_identifier": normalized_login.lower(),
            "email": normalized_email,
            "password_hash": hash_password(password),
            "role": normalized_role,
            "display_name": normalized_display_name,
            "linked_user_account_id": user_id,
            "created_by_admin_id": actor_admin_account_id,
            "now": now,
        },
    )
    _add_action(
        session=session,
        moderator_id=actor_admin_account_id,
        action_type="admin_access_granted",
        target_type="user",
        target_id=user_id,
        reason=normalized_role,
        payload={
            "admin_account_id": admin_account_id,
            "login_identifier": normalized_login.lower(),
            "role": normalized_role,
        },
    )
    session.commit()
    return get_user_admin_access(session, user_id)


def disable_admin_account(session: Session, admin_account_id: str, actor_admin_account_id: str) -> dict[str, Any]:
    _require_admin_access_manager(session, actor_admin_account_id)
    account = _get_admin_account_row(session, admin_account_id)
    if not account:
        raise ValueError("Admin account not found")
    if admin_account_id == actor_admin_account_id:
        raise ValueError("You cannot disable your own admin account")

    now = _now()
    session.execute(
        text(
            """
            UPDATE admin_accounts
            SET status = 'disabled',
                disabled_at = :now,
                updated_at = :now
            WHERE id::text = :admin_account_id
            """
        ),
        {"admin_account_id": admin_account_id, "now": now},
    )
    session.execute(
        text(
            """
            UPDATE admin_sessions
            SET revoked_at = :now,
                updated_at = :now
            WHERE admin_account_id::text = :admin_account_id
              AND revoked_at IS NULL
            """
        ),
        {"admin_account_id": admin_account_id, "now": now},
    )
    _add_action(
        session=session,
        moderator_id=actor_admin_account_id,
        action_type="admin_access_disabled",
        target_type="admin_account",
        target_id=admin_account_id,
        reason=account.get("linked_user_account_id"),
        payload={"linked_user_account_id": account.get("linked_user_account_id")},
    )
    session.commit()
    linked_user_account_id = account.get("linked_user_account_id")
    if linked_user_account_id:
        return get_user_admin_access(session, str(linked_user_account_id))
    return _admin_access_payload(_get_admin_account_row(session, admin_account_id))


def enable_admin_account(session: Session, admin_account_id: str, actor_admin_account_id: str) -> dict[str, Any]:
    _require_admin_access_manager(session, actor_admin_account_id)
    account = _get_admin_account_row(session, admin_account_id)
    if not account:
        raise ValueError("Admin account not found")

    session.execute(
        text(
            """
            UPDATE admin_accounts
            SET status = 'active',
                disabled_at = NULL,
                updated_at = now()
            WHERE id::text = :admin_account_id
            """
        ),
        {"admin_account_id": admin_account_id},
    )
    _add_action(
        session=session,
        moderator_id=actor_admin_account_id,
        action_type="admin_access_enabled",
        target_type="admin_account",
        target_id=admin_account_id,
        reason=account.get("linked_user_account_id"),
        payload={"linked_user_account_id": account.get("linked_user_account_id")},
    )
    session.commit()
    linked_user_account_id = account.get("linked_user_account_id")
    if linked_user_account_id:
        return get_user_admin_access(session, str(linked_user_account_id))
    return _admin_access_payload(_get_admin_account_row(session, admin_account_id))


def reset_admin_account_credentials(
    session: Session,
    admin_account_id: str,
    password: str,
    actor_admin_account_id: str,
) -> dict[str, Any]:
    _require_admin_access_manager(session, actor_admin_account_id)
    account = _get_admin_account_row(session, admin_account_id)
    if not account:
        raise ValueError("Admin account not found")

    now = _now()
    session.execute(
        text(
            """
            UPDATE admin_accounts
            SET password_hash = :password_hash,
                password_reset_required = TRUE,
                updated_at = :now
            WHERE id::text = :admin_account_id
            """
        ),
        {"admin_account_id": admin_account_id, "password_hash": hash_password(password), "now": now},
    )
    session.execute(
        text(
            """
            UPDATE admin_sessions
            SET revoked_at = :now,
                updated_at = :now
            WHERE admin_account_id::text = :admin_account_id
              AND revoked_at IS NULL
            """
        ),
        {"admin_account_id": admin_account_id, "now": now},
    )
    _add_action(
        session=session,
        moderator_id=actor_admin_account_id,
        action_type="admin_credentials_reset",
        target_type="admin_account",
        target_id=admin_account_id,
        reason="password_reset_required",
        payload={"linked_user_account_id": account.get("linked_user_account_id")},
    )
    session.commit()
    linked_user_account_id = account.get("linked_user_account_id")
    if linked_user_account_id:
        return get_user_admin_access(session, str(linked_user_account_id))
    return _admin_access_payload(_get_admin_account_row(session, admin_account_id))


def list_moderation_announcements(
    session: Session,
    status_filter: Optional[str] = None,
    appeals_only: bool = False,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            t.id::text AS id,
            t.customer_id::text AS user_id,
            u.email AS user_email,
            up.display_name AS user_display_name,
            COALESCE(
                c.slug,
                t.extra->'task'->'builder'->>'resolved_category',
                t.extra->>'category',
                t.extra->>'main_group',
                'help'
            ) AS category_slug,
            t.title,
            t.status::text AS task_status,
            t.moderation_status::text AS moderation_status,
            t.created_at,
            t.updated_at,
            t.deleted_at,
            t.responses_count,
            t.extra,
            assn.assignment_status,
            assn.execution_stage
        FROM tasks t
        LEFT JOIN users u
          ON {user_join}
        LEFT JOIN user_profiles up
          ON {profile_join}
        LEFT JOIN categories c
          ON c.id = t.category_id
        LEFT JOIN LATERAL (
            SELECT assignment_status, execution_stage
            FROM task_assignments ta
            WHERE ta.task_id = t.id
            ORDER BY
                CASE
                    WHEN ta.assignment_status IN ('assigned', 'in_progress') THEN 0
                    WHEN ta.assignment_status = 'completed' THEN 1
                    ELSE 2
                END,
                ta.updated_at DESC NULLS LAST,
                ta.created_at DESC NULLS LAST
            LIMIT 1
        ) assn ON TRUE
        WHERE 1 = 1
    """.format(
        user_join=_text_eq("u.id", "t.customer_id"),
        profile_join=_text_eq("up.user_id", "t.customer_id"),
    )
    params: dict[str, Any] = {}
    if search:
        params["search"] = f"%{search.strip()}%"
        sql += (
            " AND (t.id::text ILIKE :search"
            " OR t.title ILIKE :search"
            " OR COALESCE(c.slug, '') ILIKE :search"
            " OR COALESCE(u.email, '') ILIKE :search"
            " OR COALESCE(up.display_name, '') ILIKE :search)"
        )
    sql += " ORDER BY t.updated_at DESC, t.created_at DESC LIMIT 400"

    results: list[dict[str, Any]] = []
    queue_statuses = QUEUE_STATUSES | {"active", "archived", "deleted"}
    effective_filter = status_filter if status_filter in queue_statuses else None
    for raw_row in session.execute(text(sql), params).mappings().all():
        row = dict(raw_row)
        status = _task_status_for_admin(row)
        moderation = _ensure_obj(_ensure_obj(row.get("extra")).get("moderation"))
        appeal_requested = bool(_ensure_obj(moderation.get("appeal")).get("requested"))
        if effective_filter:
            if status != effective_filter:
                continue
        elif status not in QUEUE_STATUSES:
            continue
        if appeals_only and not appeal_requested:
            continue
        results.append(
            {
                "id": row["id"],
                "user_id": row.get("user_id"),
                "user_email": row.get("user_email"),
                "user_display_name": row.get("user_display_name"),
                "user_display_label": _display_name(row.get("user_display_name"), row.get("user_email"), row.get("user_id")),
                "category": row.get("category_slug"),
                "title": row.get("title"),
                "status": status,
                "task_status": row.get("task_status"),
                "moderation_status": row.get("moderation_status"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "appeal_requested": appeal_requested,
                "has_media": bool(_extract_media_items(_ensure_obj(row.get("extra")))),
                "responses_count": int(row.get("responses_count") or 0),
                "decision_message": _normalize_text_value(_ensure_obj(moderation.get("decision")).get("message")),
            }
        )
    return results


def get_announcement_detail(session: Session, ann_id: str) -> dict[str, Any]:
    row = _task_row_base(session, ann_id)
    if not row:
        raise ValueError("Announcement not found")
    return _task_card(session, row)


def apply_announcement_decision(
    session: Session,
    ann_id: str,
    moderator_id: str,
    decision: str,
    message: str,
    reasons: Optional[Sequence[Dict[str, Any]]] = None,
    suggestions: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    row = _task_row_base(session, ann_id)
    if not row:
        raise ValueError("Announcement not found")
    actor = _resolve_actor_identity(session, moderator_id)

    if decision not in {"approve", "needs_fix", "reject", "archive", "delete"}:
        raise ValueError("Unsupported decision")

    card = _task_card(session, row)
    old_status = card["status"]
    payload_data = dict(card["data"] or {})
    moderation = _ensure_obj(payload_data.get("moderation"))
    merged_reasons = _merge_reasons(_ensure_list(moderation.get("reasons")), reasons)
    clean_suggestions = _normalize_suggestions(suggestions)
    now = _now()
    published_at = row.get("published_at")
    closed_at = row.get("closed_at")
    deleted_at = row.get("deleted_at")
    display_status = old_status
    task_status_target = str(row.get("task_status") or "review")
    moderation_status_target = str(row.get("moderation_status") or "pending")

    if decision == "approve":
        display_status = "active"
        task_status_target = "in_responses" if int(row.get("responses_count") or 0) > 0 else "published"
        moderation_status_target = "published"
        published_at = published_at or now
        closed_at = None
        deleted_at = None
        _set_decision(moderation, display_status, message)
        moderation["reasons"] = []
        moderation["suggestions"] = []
        moderation.pop("penalty_stub", None)
        _reset_appeal(moderation)
    elif decision == "needs_fix":
        display_status = "needs_fix"
        task_status_target = "draft"
        moderation_status_target = "needs_fix"
        deleted_at = None
        closed_at = None
        _set_decision(moderation, display_status, message)
        moderation["reasons"] = merged_reasons
        moderation["suggestions"] = clean_suggestions
        _reset_appeal(moderation)
    elif decision == "reject":
        display_status = "rejected"
        task_status_target = "draft"
        moderation_status_target = "rejected"
        deleted_at = None
        closed_at = None
        _set_decision(moderation, display_status, message)
        moderation["reasons"] = merged_reasons
        moderation["suggestions"] = clean_suggestions
        if _has_hard_block_reason(moderation["reasons"]):
            moderation["penalty_stub"] = {"type": "warning", "points": 1, "applied_at": None}
        _reset_appeal(moderation)
    elif decision == "archive":
        display_status = "archived"
        task_status_target = "closed"
        moderation_status_target = "published"
        deleted_at = None
        closed_at = now
        _set_decision(moderation, display_status, message)
        _reset_appeal(moderation)
    elif decision == "delete":
        display_status = "deleted"
        task_status_target = "closed"
        moderation_status_target = "rejected"
        deleted_at = now
        closed_at = now
        _set_decision(moderation, display_status, message)
        _reset_appeal(moderation)

    moderation["reviewed_at"] = _now_iso()
    moderation["reviewed_by"] = actor["actor_admin_account_id"] or actor["legacy_user_account_id"] or moderator_id
    moderation["summary"] = {
        "task_status": task_status_target,
        "moderation_status": moderation_status_target,
        "decision": decision,
    }
    payload_data["moderation"] = moderation
    task_status_value = _map_enum_value(session, "tasks", "status", task_status_target, ("review", "draft"))
    moderation_status_value = _map_enum_value(
        session,
        "tasks",
        "moderation_status",
        moderation_status_target,
        ("published", "rejected", "needs_fix", "blocked"),
    )
    params: dict[str, Any] = {
        "ann_id": ann_id,
        "task_status": task_status_value,
        "moderation_status": moderation_status_value,
        "extra": json.dumps(payload_data, ensure_ascii=False),
        "published_at": published_at,
        "closed_at": closed_at,
        "deleted_at": deleted_at,
        "updated_at": now,
    }
    session.execute(
        text(
            """
            UPDATE tasks
            SET status = :task_status,
                moderation_status = :moderation_status,
                extra = CAST(:extra AS jsonb),
                published_at = :published_at,
                closed_at = :closed_at,
                deleted_at = :deleted_at,
                updated_at = :updated_at
            WHERE id::text = :ann_id
            """
        ),
        params,
    )
    _sync_legacy_announcement_row(
        session,
        row,
        data=payload_data,
        task_status=str(task_status_value or task_status_target),
        moderation_status=str(moderation_status_value or moderation_status_target),
        deleted_at=deleted_at,
        updated_at=now,
    )

    report_columns = _get_table_columns(session, "reports")
    report_resolution = "no_action" if decision in {"approve", "needs_fix", "archive"} else "report_rejected"
    update_clauses: list[str] = []
    appeal_params: dict[str, Any] = {"target_id": str(row["id"])}
    status_value = _map_status_assignment(session, "reports", "status", "resolved")
    if "status" in report_columns and status_value is not None:
        update_clauses.append("status = :status_value")
        appeal_params["status_value"] = status_value
    if "resolution" in report_columns:
        appeal_resolution = _map_enum_value(
            session,
            "reports",
            "resolution",
            report_resolution,
            ("justified", "not_justified"),
        )
        if appeal_resolution:
            update_clauses.append("resolution = :resolution")
            appeal_params["resolution"] = appeal_resolution
    if "resolved_by_admin_account_id" in report_columns and actor["actor_admin_account_id"]:
        update_clauses.append("resolved_by_admin_account_id = :resolved_by_admin_account_id")
        appeal_params["resolved_by_admin_account_id"] = actor["actor_admin_account_id"]
    if "resolved_by" in report_columns and actor["legacy_user_account_id"]:
        update_clauses.append("resolved_by = :moderator_id")
        appeal_params["moderator_id"] = actor["legacy_user_account_id"]
    if "moderator_comment" in report_columns:
        update_clauses.append("moderator_comment = :comment")
        appeal_params["comment"] = message
    if "resolved_at" in report_columns:
        update_clauses.append("resolved_at = :resolved_at")
        appeal_params["resolved_at"] = now
    if update_clauses:
        session.execute(
            text(
                f"""
                UPDATE reports
                SET {', '.join(update_clauses)}
                WHERE target_id::text = :target_id
                  AND target_type IN ('announcement', 'task')
                  AND reason_code = 'APPEAL'
                  AND {_report_open_condition(session)}
                """
            ),
            appeal_params,
        )

    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type=decision,
        target_type="task",
        target_id=str(row["id"]),
        reason=message,
        payload={
            "old_status": old_status,
            "new_status": display_status,
            "task_status": task_status_target,
            "moderation_status": moderation_status_target,
            "message": message,
            "reasons": merged_reasons if decision in {"needs_fix", "reject"} else [],
            "suggestions": _ensure_list(moderation.get("suggestions")),
            "deleted": decision == "delete",
        },
    )
    _add_notification(
        session=session,
        user_id=str(row["user_id"]),
        notif_type="moderation",
        body=message,
        payload={"ann_id": str(row["id"]), "decision": decision},
    )

    session.commit()
    return get_announcement_detail(session, ann_id)


def list_reports(
    session: Session,
    search: Optional[str] = None,
    status: Optional[str] = "open",
) -> list[dict[str, Any]]:
    status_expr = _report_status_expr(session, "r")
    resolved_by_admin_exists = _has_column(session, "reports", "resolved_by_admin_account_id")
    resolved_by_user_exists = _has_column(session, "reports", "resolved_by")
    resolved_by_join = ""
    resolved_by_expr = "NULL::text"
    resolved_by_email_expr = "NULL::text"
    resolved_by_display_name_expr = "NULL::text"
    if resolved_by_admin_exists:
        resolved_by_join = "LEFT JOIN admin_accounts ma ON ma.id = r.resolved_by_admin_account_id"
        resolved_by_expr = _text_expr("r.resolved_by_admin_account_id")
        resolved_by_email_expr = "ma.email"
        resolved_by_display_name_expr = "ma.display_name"
    elif resolved_by_user_exists:
        resolved_by_join = f"LEFT JOIN users mu ON {_text_eq('mu.id', 'r.resolved_by')}"
        resolved_by_expr = _text_expr("r.resolved_by")
        resolved_by_email_expr = "mu.email"
    resolution_expr = _text_expr("r.resolution") if _has_column(session, "reports", "resolution") else "NULL::text"
    moderator_comment_expr = (
        "r.moderator_comment" if _has_column(session, "reports", "moderator_comment") else "NULL::text"
    )
    resolved_at_expr = "r.resolved_at" if _has_column(session, "reports", "resolved_at") else "NULL::timestamptz"
    reporter_profile_join = "LEFT JOIN user_profiles rup ON {profile_join}".format(
        profile_join=_text_eq("rup.user_id", "r.reporter_id")
    )
    sql = """
        SELECT r.id,
               r.reporter_id,
               ru.email AS reporter_email,
               rup.display_name AS reporter_display_name,
               r.target_type,
               r.target_id,
               r.reason_code,
               r.reason_text,
               {status_expr} AS status,
               {resolution_expr} AS resolution,
               {resolved_by_expr} AS resolved_by,
               {resolved_by_email_expr} AS resolved_by_email,
               {resolved_by_display_name_expr} AS resolved_by_display_name,
               {moderator_comment_expr} AS moderator_comment,
               r.created_at,
               {resolved_at_expr} AS resolved_at
        FROM reports r
        LEFT JOIN users ru ON {reporter_join}
        {reporter_profile_join}
        {resolved_by_join}
        WHERE 1 = 1
    """.format(
        status_expr=status_expr,
        resolution_expr=resolution_expr,
        resolved_by_expr=resolved_by_expr,
        resolved_by_email_expr=resolved_by_email_expr,
        resolved_by_display_name_expr=resolved_by_display_name_expr,
        moderator_comment_expr=moderator_comment_expr,
        resolved_at_expr=resolved_at_expr,
        resolved_by_join=resolved_by_join,
        reporter_profile_join=reporter_profile_join,
        reporter_join=_text_eq("ru.id", "r.reporter_id"),
    )
    params: dict[str, Any] = {}
    if status:
        sql += f" AND {status_expr} = :status"
        params["status"] = status
    if search:
        sql += (
            " AND (COALESCE(ru.email, '') ILIKE :search"
            " OR COALESCE(rup.display_name, '') ILIKE :search"
            " OR r.target_id::text ILIKE :search"
            " OR r.reason_code::text ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    sql += " ORDER BY r.created_at DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["reporter_display_label"] = _display_name(
            item.get("reporter_display_name"),
            item.get("reporter_email"),
            item.get("reporter_id"),
        )
        item["resolved_by_display_label"] = _display_name(
            item.get("resolved_by_display_name"),
            item.get("resolved_by_email"),
            item.get("resolved_by"),
        )
        item["target_summary"] = _report_target_summary(session, str(item.get("target_type")), str(item.get("target_id")))
        results.append(item)
    return results


def get_report_detail(session: Session, report_id: str) -> dict[str, Any]:
    status_expr = _report_status_expr(session, "r")
    resolved_by_admin_exists = _has_column(session, "reports", "resolved_by_admin_account_id")
    resolved_by_user_exists = _has_column(session, "reports", "resolved_by")
    resolved_by_join = ""
    resolved_by_expr = "NULL::text"
    resolved_by_email_expr = "NULL::text"
    resolved_by_display_name_expr = "NULL::text"
    if resolved_by_admin_exists:
        resolved_by_join = "LEFT JOIN admin_accounts ma ON ma.id = r.resolved_by_admin_account_id"
        resolved_by_expr = _text_expr("r.resolved_by_admin_account_id")
        resolved_by_email_expr = "ma.email"
        resolved_by_display_name_expr = "ma.display_name"
    elif resolved_by_user_exists:
        resolved_by_join = f"LEFT JOIN users mu ON {_text_eq('mu.id', 'r.resolved_by')}"
        resolved_by_expr = _text_expr("r.resolved_by")
        resolved_by_email_expr = "mu.email"
    resolution_expr = _text_expr("r.resolution") if _has_column(session, "reports", "resolution") else "NULL::text"
    moderator_comment_expr = (
        "r.moderator_comment" if _has_column(session, "reports", "moderator_comment") else "NULL::text"
    )
    resolved_at_expr = "r.resolved_at" if _has_column(session, "reports", "resolved_at") else "NULL::timestamptz"
    reporter_profile_join = "LEFT JOIN user_profiles rup ON {profile_join}".format(
        profile_join=_text_eq("rup.user_id", "r.reporter_id")
    )
    meta_expr = "r.meta" if _has_column(session, "reports", "meta") else "'{}'::jsonb"
    row = session.execute(
        text(
            """
            SELECT r.id,
                   r.reporter_id,
                   ru.email AS reporter_email,
                   rup.display_name AS reporter_display_name,
                   r.target_type,
                   r.target_id,
                   r.reason_code,
                   r.reason_text,
                   {status_expr} AS status,
                   {resolution_expr} AS resolution,
                   {resolved_by_expr} AS resolved_by,
                   {resolved_by_email_expr} AS resolved_by_email,
                   {resolved_by_display_name_expr} AS resolved_by_display_name,
                   {moderator_comment_expr} AS moderator_comment,
                   {meta_expr} AS meta,
                   r.created_at,
                   {resolved_at_expr} AS resolved_at
            FROM reports r
            LEFT JOIN users ru ON {reporter_join}
            {reporter_profile_join}
            {resolved_by_join}
            WHERE r.id::text = :report_id
            """.format(
                status_expr=status_expr,
                resolution_expr=resolution_expr,
                resolved_by_expr=resolved_by_expr,
                resolved_by_email_expr=resolved_by_email_expr,
                resolved_by_display_name_expr=resolved_by_display_name_expr,
                moderator_comment_expr=moderator_comment_expr,
                meta_expr=meta_expr,
                resolved_at_expr=resolved_at_expr,
                resolved_by_join=resolved_by_join,
                reporter_profile_join=reporter_profile_join,
                reporter_join=_text_eq("ru.id", "r.reporter_id"),
            )
        ),
        {"report_id": report_id},
    ).mappings().first()
    if not row:
        raise ValueError("Report not found")
    result = dict(row)
    result["reporter_display_label"] = _display_name(
        result.get("reporter_display_name"),
        result.get("reporter_email"),
        result.get("reporter_id"),
    )
    result["resolved_by_display_label"] = _display_name(
        result.get("resolved_by_display_name"),
        result.get("resolved_by_email"),
        result.get("resolved_by"),
    )
    result["target_context"] = _report_target_context(session, result)
    result["target_summary"] = _report_target_summary(session, str(result.get("target_type")), str(result.get("target_id")))
    return result


def resolve_report(
    session: Session,
    report_id: str,
    moderator_id: str,
    resolution: str,
    moderator_comment: Optional[str] = None,
    ends_at: Optional[datetime] = None,
    custom_restriction_label: Optional[str] = None,
) -> dict[str, Any]:
    actor = _resolve_actor_identity(session, moderator_id)
    normalized_resolution = _REPORT_RESOLUTION_ALIASES.get(resolution, resolution)
    if normalized_resolution not in {
        "no_action",
        "warning",
        "mute_chat",
        "restrict_posting",
        "restrict_offers",
        "temporary_ban",
        "permanent_ban",
        "custom_restriction",
        "report_rejected",
    }:
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
        resolution_value = _map_enum_value(
            session,
            "reports",
            "resolution",
            normalized_resolution,
            ("justified", "not_justified"),
        )
        update_clauses.append("resolution = :resolution")
        params["resolution"] = resolution_value
    if "resolved_by_admin_account_id" in columns and actor["actor_admin_account_id"]:
        update_clauses.append("resolved_by_admin_account_id = :resolved_by_admin_account_id")
        params["resolved_by_admin_account_id"] = actor["actor_admin_account_id"]
    if "resolved_by" in columns and actor["legacy_user_account_id"]:
        update_clauses.append("resolved_by = :moderator_id")
        params["moderator_id"] = actor["legacy_user_account_id"]
    if "moderator_comment" in columns:
        update_clauses.append("moderator_comment = :moderator_comment")
        params["moderator_comment"] = moderator_comment
    if "resolved_at" in columns:
        update_clauses.append("resolved_at = :resolved_at")
        params["resolved_at"] = now
    if update_clauses:
        session.execute(text(f"UPDATE reports SET {', '.join(update_clauses)} WHERE id::text = :report_id"), params)

    restriction = None
    if normalized_resolution in _ACTIONABLE_REPORT_OUTCOMES:
        target_user_id = _report_target_user_id(session, str(report["target_type"]), str(report["target_id"]))
        if not target_user_id:
            raise ValueError("Could not resolve report target user")
        restriction = create_restriction(
            session=session,
            user_id=target_user_id,
            restriction_type=normalized_resolution,
            moderator_id=moderator_id,
            ends_at=ends_at,
            comment=moderator_comment,
            source_type="report",
            source_id=str(report["id"]),
            meta={"custom_label": _normalize_text_value(custom_restriction_label)} if custom_restriction_label else None,
            commit=False,
        )

    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="report_resolve",
        target_type="report",
        target_id=str(report["id"]),
        reason=moderator_comment,
        payload={
            "resolution": normalized_resolution,
            "target_type": report["target_type"],
            "target_id": report["target_id"],
            "restriction_id": restriction["id"] if restriction else None,
        },
    )
    _add_notification(
        session=session,
        user_id=str(report["reporter_id"]),
        notif_type="report",
        body=f"Жалоба обработана: {normalized_resolution}",
        payload={
            "report_id": str(report["id"]),
            "resolution": normalized_resolution,
            "restriction_id": restriction["id"] if restriction else None,
        },
    )
    session.commit()
    return get_report_detail(session, report_id)


def list_support_threads(
    session: Session,
    admin_account_id: str,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    del session
    return shared_list_support_threads_for_admin(admin_account_id, search=search)


def get_support_thread(session: Session, thread_id: str, admin_account_id: str) -> dict[str, Any]:
    del session
    return shared_get_support_thread_for_admin(thread_id, admin_account_id)


def get_support_messages(session: Session, thread_id: str, admin_account_id: str) -> list[dict[str, Any]]:
    del session
    return shared_list_support_messages_for_admin(thread_id, admin_account_id)


def post_support_message(
    session: Session,
    thread_id: str,
    sender_id: str,
    text_value: str,
) -> dict[str, Any]:
    del session
    return shared_post_admin_support_message(thread_id, sender_id, text_value)


def assign_support_thread(
    session: Session,
    thread_id: str,
    assigned_admin_account_id: str,
    actor_admin_account_id: str,
) -> dict[str, Any]:
    del session
    return shared_assign_support_thread(thread_id, assigned_admin_account_id, actor_admin_account_id)


def list_active_admin_accounts(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT
                aa.id::text AS id,
                aa.login_identifier,
                aa.email,
                aa.role,
                aa.display_name
            FROM admin_accounts aa
            WHERE aa.status = 'active'
              AND aa.disabled_at IS NULL
            ORDER BY CASE aa.role
                WHEN 'support' THEN 0
                WHEN 'moderator' THEN 1
                ELSE 2
            END,
            aa.created_at ASC
            """
        )
    ).mappings().all()
    return [
        {
            "id": str(row["id"]),
            "role": str(row["role"] or "support"),
            "display_label": _display_name(row.get("display_name"), row.get("email"), row.get("login_identifier"), row.get("id")),
        }
        for row in rows
    ]


def list_restrictions(
    session: Session,
    search: Optional[str] = None,
    status: Optional[str] = "active",
) -> list[dict[str, Any]]:
    status_expr = _restriction_status_expr(session, "r")
    issued_by_admin_exists = _has_column(session, "user_restrictions", "issued_by_admin_account_id")
    revoked_by_admin_exists = _has_column(session, "user_restrictions", "revoked_by_admin_account_id")
    issued_by_exists = _has_column(session, "user_restrictions", "issued_by")
    revoked_by_exists = _has_column(session, "user_restrictions", "revoked_by")
    type_expr = _text_expr("r.type") if _has_column(session, "user_restrictions", "type") else "NULL::text"
    if issued_by_admin_exists:
        issued_by_expr = _text_expr("r.issued_by_admin_account_id")
        issued_by_email_expr = "ia.email"
        issued_by_display_name_expr = "ia.display_name"
        issued_by_join = "LEFT JOIN admin_accounts ia ON ia.id = r.issued_by_admin_account_id"
    elif issued_by_exists:
        issued_by_expr = _text_expr("r.issued_by")
        issued_by_email_expr = "iu.email"
        issued_by_display_name_expr = "iup.display_name"
        issued_by_join = (
            f"LEFT JOIN users iu ON {_text_eq('iu.id', 'r.issued_by')}"
            f" LEFT JOIN user_profiles iup ON {_text_eq('iup.user_id', 'r.issued_by')}"
        )
    else:
        issued_by_expr = "NULL::text"
        issued_by_email_expr = "NULL::text"
        issued_by_display_name_expr = "NULL::text"
        issued_by_join = ""
    if revoked_by_admin_exists:
        revoked_by_expr = _text_expr("r.revoked_by_admin_account_id")
        revoked_by_email_expr = "ra.email"
        revoked_by_display_name_expr = "ra.display_name"
        revoked_by_join = "LEFT JOIN admin_accounts ra ON ra.id = r.revoked_by_admin_account_id"
    elif revoked_by_exists:
        revoked_by_expr = _text_expr("r.revoked_by")
        revoked_by_email_expr = "ru.email"
        revoked_by_display_name_expr = "rup.display_name"
        revoked_by_join = (
            f"LEFT JOIN users ru ON {_text_eq('ru.id', 'r.revoked_by')}"
            f" LEFT JOIN user_profiles rup ON {_text_eq('rup.user_id', 'r.revoked_by')}"
        )
    else:
        revoked_by_expr = "NULL::text"
        revoked_by_email_expr = "NULL::text"
        revoked_by_display_name_expr = "NULL::text"
        revoked_by_join = ""
    starts_at_expr = _restriction_timestamp_expr(session, "r")
    ends_at_expr = "r.ends_at" if _has_column(session, "user_restrictions", "ends_at") else "NULL::timestamptz"
    revoked_at_expr = "r.revoked_at" if _has_column(session, "user_restrictions", "revoked_at") else "NULL::timestamptz"
    source_type_expr = _text_expr("r.source_type") if _has_column(session, "user_restrictions", "source_type") else "NULL::text"
    source_id_expr = _text_expr("r.source_id") if _has_column(session, "user_restrictions", "source_id") else "NULL::text"
    source_id_search_expr = "COALESCE(r.source_id::text, '')" if _has_column(session, "user_restrictions", "source_id") else "''"
    meta_expr = "r.meta" if _has_column(session, "user_restrictions", "meta") else "'{}'::jsonb"
    updated_at_expr = "r.updated_at" if _has_column(session, "user_restrictions", "updated_at") else "NULL::timestamptz"
    reason_expr = (
        "COALESCE(r.reason_text, r.reason)"
        if _has_column(session, "user_restrictions", "reason_text") and _has_column(session, "user_restrictions", "reason")
        else "r.reason_text"
        if _has_column(session, "user_restrictions", "reason_text")
        else "r.reason"
        if _has_column(session, "user_restrictions", "reason")
        else "NULL::text"
    )
    revocation_reason_expr = (
        "r.revocation_reason" if _has_column(session, "user_restrictions", "revocation_reason") else "NULL::text"
    )
    sql = """
        SELECT r.id::text AS id,
               r.user_id::text AS user_id,
               uu.email AS user_email,
               uup.display_name AS user_display_name,
               {type_expr} AS type,
               {status_expr} AS status,
               {issued_by_expr} AS issued_by,
               {issued_by_email_expr} AS issued_by_email,
               {issued_by_display_name_expr} AS issued_by_display_name,
               {revoked_by_expr} AS revoked_by,
               {revoked_by_email_expr} AS revoked_by_email,
               {revoked_by_display_name_expr} AS revoked_by_display_name,
               {starts_at_expr} AS starts_at,
               {ends_at_expr} AS ends_at,
               {revoked_at_expr} AS revoked_at,
               {source_type_expr} AS source_type,
               {source_id_expr} AS source_id,
               {reason_expr} AS reason_text,
               {revocation_reason_expr} AS revocation_reason,
               {meta_expr} AS meta,
               {updated_at_expr} AS updated_at
        FROM user_restrictions r
        LEFT JOIN users uu ON {user_join}
        LEFT JOIN user_profiles uup ON {user_profile_join}
        {issued_by_join}
        {revoked_by_join}
        WHERE 1 = 1
    """.format(
        type_expr=type_expr,
        status_expr=status_expr,
        issued_by_expr=issued_by_expr,
        issued_by_email_expr=issued_by_email_expr,
        issued_by_display_name_expr=issued_by_display_name_expr,
        revoked_by_expr=revoked_by_expr,
        revoked_by_email_expr=revoked_by_email_expr,
        revoked_by_display_name_expr=revoked_by_display_name_expr,
        starts_at_expr=starts_at_expr,
        ends_at_expr=ends_at_expr,
        revoked_at_expr=revoked_at_expr,
        source_type_expr=source_type_expr,
        source_id_expr=source_id_expr,
        reason_expr=reason_expr,
        revocation_reason_expr=revocation_reason_expr,
        meta_expr=meta_expr,
        updated_at_expr=updated_at_expr,
        issued_by_join=issued_by_join,
        revoked_by_join=revoked_by_join,
        user_join=_text_eq("uu.id", "r.user_id"),
        user_profile_join=_text_eq("uup.user_id", "r.user_id"),
    )
    params: dict[str, Any] = {}
    if status:
        sql += f" AND {status_expr} = :status"
        params["status"] = status
    if search:
        sql += (
            " AND (COALESCE(uu.email, '') ILIKE :search"
            " OR COALESCE(uup.display_name, '') ILIKE :search"
            " OR r.user_id::text ILIKE :search"
            f" OR {source_id_search_expr} ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    sql += f" ORDER BY {starts_at_expr} DESC NULLS LAST, r.id DESC LIMIT 300"
    rows = session.execute(text(sql), params).mappings().all()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["user_display_label"] = _display_name(
            item.get("user_display_name"),
            item.get("user_email"),
            item.get("user_id"),
        )
        item["issued_by_display_label"] = _display_name(
            item.get("issued_by_display_name"),
            item.get("issued_by_email"),
            item.get("issued_by"),
        )
        item["revoked_by_display_label"] = _display_name(
            item.get("revoked_by_display_name"),
            item.get("revoked_by_email"),
            item.get("revoked_by"),
        )
        item["meta"] = _ensure_obj(item.get("meta"))
        item["custom_label"] = _normalize_text_value(item["meta"].get("custom_label"))
        results.append(item)
    return results


def list_moderation_actions(
    session: Session,
    action_type: Optional[str] = None,
    target_type: Optional[str] = None,
    moderator_search: Optional[str] = None,
    target_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            a.id::text AS id,
            a.actor_type,
            a.actor_user_account_id::text AS moderator_id,
            a.actor_admin_account_id::text AS moderator_admin_account_id,
            au.email AS moderator_email,
            aa.display_name AS moderator_display_name,
            aa.login_identifier AS moderator_login_identifier,
            a.action AS action_type,
            a.target_type,
            a.target_id,
            a.result,
            COALESCE(a.details->>'reason', '') AS reason,
            a.details AS payload,
            a.created_at
        FROM audit_logs a
        LEFT JOIN users au
          ON au.id = a.actor_user_account_id
        LEFT JOIN admin_accounts aa
          ON aa.id = a.actor_admin_account_id
        WHERE 1 = 1
    """
    params: dict[str, Any] = {}
    if action_type:
        sql += " AND a.action = :action_type"
        params["action_type"] = action_type
    if target_type:
        sql += " AND a.target_type = :target_type"
        params["target_type"] = target_type
    if target_id:
        sql += " AND a.target_id = :target_id"
        params["target_id"] = target_id
    if moderator_search:
        sql += " AND (COALESCE(au.email, '') ILIKE :moderator_search OR COALESCE(aa.login_identifier, '') ILIKE :moderator_search)"
        params["moderator_search"] = f"%{moderator_search.strip()}%"
    sql += " ORDER BY a.created_at DESC LIMIT 500"
    rows = session.execute(text(sql), params).mappings().all()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["moderator_display_label"] = _display_name(
            item.get("moderator_display_name"),
            item.get("moderator_email"),
            item.get("moderator_login_identifier"),
            item.get("moderator_admin_account_id"),
            item.get("moderator_id"),
        )
        results.append(item)
    return results


def create_restriction(
    session: Session,
    user_id: str,
    restriction_type: str,
    moderator_id: str,
    ends_at: Optional[datetime] = None,
    comment: Optional[str] = None,
    source_type: Optional[str] = "manual",
    source_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> dict[str, Any]:
    normalized_type = _RESTRICTION_TYPE_ALIASES.get(restriction_type, restriction_type)
    allowed_types = set(_RESTRICTION_TYPE_ALIASES.values())
    if normalized_type not in allowed_types:
        raise ValueError("Unsupported restriction type")
    normalized_source_type = _normalize_text_value(source_type) or "manual"
    if normalized_source_type not in {"manual", "report", "moderation"}:
        raise ValueError("Unsupported restriction source")

    target_user = _get_user_row(session, user_id)
    if not target_user:
        raise ValueError("User not found")

    restriction_id = str(uuid.uuid4())
    now = _now()
    columns = _get_table_columns(session, "user_restrictions")
    values: Dict[str, Any] = {"id": restriction_id, "user_id": user_id}
    if "type" in columns:
        values["type"] = _map_enum_value(
            session,
            "user_restrictions",
            "type",
            normalized_type,
            tuple(allowed_types),
        ) or normalized_type
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
    clean_comment = _normalize_text_value(comment)
    if "reason_text" in columns and clean_comment:
        values["reason_text"] = clean_comment
    if "reason" in columns and clean_comment:
        values["reason"] = clean_comment
    clean_source_id = _normalize_text_value(source_id)
    if clean_source_id and "source_id" in columns:
        source_id_type, _ = _get_column_info(session, "user_restrictions", "source_id")
        if source_id_type == "uuid":
            try:
                uuid.UUID(clean_source_id)
            except ValueError as exc:
                raise ValueError("Source id must be a UUID") from exc
    if "source_type" in columns:
        values["source_type"] = normalized_source_type
    if "source_id" in columns and clean_source_id:
        values["source_id"] = clean_source_id
    meta_payload = dict(meta or {})
    if "meta" in columns:
        values["meta"] = meta_payload
    if "updated_at" in columns:
        values["updated_at"] = now
    _insert_row(
        session,
        "user_restrictions",
        values,
        jsonb_columns={"meta"} if "meta" in values else set(),
    )
    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="restriction_set",
        target_type="user",
        target_id=user_id,
        reason=clean_comment,
        payload={
            "type": normalized_type,
            "ends_at": ends_at.isoformat() if ends_at else None,
            "source_type": normalized_source_type,
            "source_id": clean_source_id,
            "meta": meta_payload or None,
        },
    )
    _add_notification(
        session=session,
        user_id=user_id,
        notif_type="moderation",
        body=f"Для вашей учётной записи установлено ограничение: {normalized_type}",
        payload={
            "restriction_id": restriction_id,
            "type": normalized_type,
            "source_type": normalized_source_type,
            "source_id": clean_source_id,
        },
    )
    if commit:
        session.commit()
    rows = [item for item in list_restrictions(session, status=None) if str(item["id"]) == restriction_id]
    return rows[0] if rows else {
        "id": restriction_id,
        "user_id": user_id,
        "type": normalized_type,
        "status": "active",
        "issued_by": moderator_id,
        "issued_by_email": None,
        "issued_by_display_label": moderator_id,
        "starts_at": now,
        "ends_at": ends_at,
        "revoked_at": None,
        "source_type": normalized_source_type,
        "source_id": clean_source_id,
        "reason_text": clean_comment,
        "meta": meta_payload,
    }


def revoke_restriction(
    session: Session,
    restriction_id: str,
    moderator_id: str,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    type_expr = _text_expr("r.type") if _has_column(session, "user_restrictions", "type") else "NULL::text"
    status_expr = _restriction_status_expr(session, "r")
    restriction = session.execute(
        text(
            f"""
            SELECT r.id::text AS id,
                   r.user_id::text AS user_id,
                   {type_expr} AS type,
                   {status_expr} AS status
            FROM user_restrictions r
            WHERE r.id::text = :restriction_id
            """
        ),
        {"restriction_id": restriction_id},
    ).mappings().first()
    if not restriction:
        raise ValueError("Restriction not found")
    if str(restriction.get("status")) == "revoked":
        rows = [item for item in list_restrictions(session, status=None) if str(item["id"]) == restriction_id]
        if rows:
            return rows[0]

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
    if "revoked_by" in columns:
        update_clauses.append("revoked_by = :revoked_by")
        params["revoked_by"] = moderator_id
    clean_comment = _normalize_text_value(comment)
    if "revocation_reason" in columns and clean_comment:
        update_clauses.append("revocation_reason = :revocation_reason")
        params["revocation_reason"] = clean_comment
    if "updated_at" in columns:
        update_clauses.append("updated_at = :updated_at")
        params["updated_at"] = now
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
        reason=clean_comment,
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


def extend_restriction(
    session: Session,
    restriction_id: str,
    moderator_id: str,
    ends_at: datetime,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    type_expr = _text_expr("r.type") if _has_column(session, "user_restrictions", "type") else "NULL::text"
    status_expr = _restriction_status_expr(session, "r")
    restriction = session.execute(
        text(
            f"""
            SELECT r.id::text AS id,
                   r.user_id::text AS user_id,
                   {type_expr} AS type,
                   {status_expr} AS status,
                   {"r.ends_at" if _has_column(session, "user_restrictions", "ends_at") else "NULL::timestamptz"} AS ends_at
            FROM user_restrictions r
            WHERE r.id::text = :restriction_id
            """
        ),
        {"restriction_id": restriction_id},
    ).mappings().first()
    if not restriction:
        raise ValueError("Restriction not found")
    if str(restriction.get("status")) == "revoked":
        raise ValueError("Cannot extend a revoked restriction")
    if not _has_column(session, "user_restrictions", "ends_at"):
        raise ValueError("Restriction expiration is not supported")

    now = _now()
    clean_comment = _normalize_text_value(comment)
    update_clauses = ["ends_at = :ends_at"]
    params: dict[str, Any] = {"restriction_id": restriction_id, "ends_at": ends_at}
    if _has_column(session, "user_restrictions", "updated_at"):
        update_clauses.append("updated_at = :updated_at")
        params["updated_at"] = now
    session.execute(
        text(f"UPDATE user_restrictions SET {', '.join(update_clauses)} WHERE id::text = :restriction_id"),
        params,
    )
    _add_action(
        session=session,
        moderator_id=moderator_id,
        action_type="restriction_extend",
        target_type="user",
        target_id=str(restriction["user_id"]),
        reason=clean_comment,
        payload={
            "restriction_id": str(restriction["id"]),
            "type": restriction.get("type"),
            "old_ends_at": restriction.get("ends_at").isoformat() if restriction.get("ends_at") else None,
            "ends_at": ends_at.isoformat(),
        },
    )
    _add_notification(
        session=session,
        user_id=str(restriction["user_id"]),
        notif_type="moderation",
        body=f"Срок ограничения обновлён: {restriction.get('type') or 'restriction'}",
        payload={"restriction_id": str(restriction["id"]), "ends_at": ends_at.isoformat()},
    )
    session.commit()
    rows = [item for item in list_restrictions(session, status=None) if str(item["id"]) == restriction_id]
    return rows[0] if rows else {
        "id": restriction_id,
        "user_id": str(restriction["user_id"]),
        "type": restriction.get("type"),
        "status": "active",
        "issued_by": None,
        "issued_by_email": None,
        "starts_at": None,
        "ends_at": ends_at,
        "revoked_at": None,
    }
