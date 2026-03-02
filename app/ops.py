from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from app.db import execute, fetch_one
from app.schema_compat import get_table_columns, table_has_column


def _build_insert_sql(table_name: str, values: Dict[str, Any], jsonb_columns: set[str] | None = None) -> tuple[str, tuple[Any, ...]]:
    jsonb_columns = jsonb_columns or set()
    columns = list(values.keys())
    placeholders = []
    params: list[Any] = []
    for column in columns:
        if column in jsonb_columns:
            placeholders.append("%s::jsonb")
            params.append(json.dumps(values[column], ensure_ascii=False))
        else:
            placeholders.append("%s")
            params.append(values[column])
    sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
    return sql, tuple(params)


def _report_open_filter_sql() -> str:
    if table_has_column("reports", "status"):
        return "status = 'open'"
    if table_has_column("reports", "resolved_at"):
        return "resolved_at IS NULL"
    if table_has_column("reports", "resolution"):
        return "resolution IS NULL"
    return "1 = 1"


def report_status_select_sql(alias: str = "reports") -> str:
    if table_has_column("reports", "status"):
        return f"{alias}.status"
    if table_has_column("reports", "resolved_at"):
        return f"CASE WHEN {alias}.resolved_at IS NULL THEN 'open' ELSE 'resolved' END"
    if table_has_column("reports", "resolution"):
        return f"CASE WHEN {alias}.resolution IS NULL THEN 'open' ELSE 'resolved' END"
    return "'open'"


def create_notification(
    user_id: str,
    notif_type: str,
    body: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    notification_id = str(uuid.uuid4())
    columns = get_table_columns("notifications")
    values: Dict[str, Any] = {"id": notification_id, "user_id": user_id, "type": notif_type, "body": body}
    if "payload" in columns:
        values["payload"] = payload or {}
    if "is_read" in columns:
        values["is_read"] = False
    sql, params = _build_insert_sql("notifications", values, jsonb_columns={"payload"} if "payload" in values else set())
    execute(sql, params)
    return notification_id


def create_report(
    reporter_id: str,
    target_type: str,
    target_id: str,
    reason_code: str,
    reason_text: Optional[str] = None,
) -> str:
    report_id = str(uuid.uuid4())
    columns = get_table_columns("reports")
    values: Dict[str, Any] = {
        "id": report_id,
        "reporter_id": reporter_id,
        "target_type": target_type,
        "target_id": target_id,
        "reason_code": reason_code,
    }
    if "reason_text" in columns:
        values["reason_text"] = reason_text
    if "status" in columns:
        values["status"] = "open"
    sql, params = _build_insert_sql("reports", values)
    execute(sql, params)
    return report_id


def ensure_appeal_report(
    reporter_id: str,
    announcement_id: str,
    reason_text: Optional[str] = None,
) -> str:
    existing = fetch_one(
        f"""
        SELECT id
        FROM reports
        WHERE reporter_id = %s
          AND target_type = 'announcement'
          AND target_id = %s
          AND reason_code = 'APPEAL'
          AND {_report_open_filter_sql()}
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (reporter_id, announcement_id),
    )
    if existing:
        return existing[0]
    return create_report(
        reporter_id=reporter_id,
        target_type="announcement",
        target_id=announcement_id,
        reason_code="APPEAL",
        reason_text=reason_text,
    )


def log_moderation_action(
    moderator_id: str,
    action_type: str,
    target_type: str,
    target_id: str,
    reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    action_id = str(uuid.uuid4())
    columns = get_table_columns("moderation_actions")
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
    sql, params = _build_insert_sql(
        "moderation_actions",
        values,
        jsonb_columns={"payload"} if "payload" in values else set(),
    )
    execute(sql, params)
    return action_id
