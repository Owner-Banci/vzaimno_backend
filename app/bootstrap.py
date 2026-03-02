from __future__ import annotations

from typing import Dict, Iterable, Sequence

from app.db import execute, fetch_one
from app.schema_compat import clear_schema_cache, table_has_columns


CORE_TABLES: Dict[str, str] = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'user',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
    "announcements": """
        CREATE TABLE IF NOT EXISTS announcements (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          data JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          deleted_at TIMESTAMPTZ
        );
    """,
}


AUX_TABLES: Dict[str, str] = {
    "chat_threads": """
        CREATE TABLE IF NOT EXISTS chat_threads (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          task_id TEXT NULL,
          offer_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_message_at TIMESTAMPTZ NULL
        );
    """,
    "chat_participants": """
        CREATE TABLE IF NOT EXISTS chat_participants (
          thread_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          role TEXT NOT NULL,
          joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          left_at TIMESTAMPTZ NULL,
          last_read_message_id TEXT NULL,
          PRIMARY KEY (thread_id, user_id)
        );
    """,
    "chat_messages": """
        CREATE TABLE IF NOT EXISTS chat_messages (
          id TEXT PRIMARY KEY,
          thread_id TEXT NOT NULL,
          sender_id TEXT NOT NULL,
          type TEXT NOT NULL,
          text TEXT NOT NULL,
          is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
          blocked_reason TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          edited_at TIMESTAMPTZ NULL,
          deleted_at TIMESTAMPTZ NULL
        );
    """,
    "message_reads": """
        CREATE TABLE IF NOT EXISTS message_reads (
          message_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (message_id, user_id)
        );
    """,
    "reports": """
        CREATE TABLE IF NOT EXISTS reports (
          id TEXT PRIMARY KEY,
          reporter_id TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          reason_text TEXT NULL,
          status TEXT NOT NULL,
          resolution TEXT NULL,
          resolved_by TEXT NULL,
          moderator_comment TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          resolved_at TIMESTAMPTZ NULL
        );
    """,
    "moderation_actions": """
        CREATE TABLE IF NOT EXISTS moderation_actions (
          id TEXT PRIMARY KEY,
          moderator_id TEXT NOT NULL,
          action_type TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          reason TEXT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
    "user_restrictions": """
        CREATE TABLE IF NOT EXISTS user_restrictions (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          type TEXT NOT NULL,
          status TEXT NOT NULL,
          issued_by TEXT NOT NULL,
          starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          ends_at TIMESTAMPTZ NULL,
          revoked_at TIMESTAMPTZ NULL
        );
    """,
    "notifications": """
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          type TEXT NOT NULL,
          body TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          is_read BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          read_at TIMESTAMPTZ NULL
        );
    """,
}


INDEX_DDLS: Iterable[tuple[str, str, Sequence[str]]] = (
    ("announcements", "CREATE INDEX IF NOT EXISTS idx_announcements_user_id ON announcements(user_id);", ("user_id",)),
    (
        "announcements",
        "CREATE INDEX IF NOT EXISTS idx_announcements_created_at ON announcements(created_at DESC);",
        ("created_at",),
    ),
    ("announcements", "CREATE INDEX IF NOT EXISTS idx_announcements_status ON announcements(status);", ("status",)),
    (
        "chat_threads",
        "CREATE INDEX IF NOT EXISTS idx_chat_threads_last_message_at ON chat_threads(last_message_at DESC NULLS LAST);",
        ("last_message_at",),
    ),
    (
        "chat_participants",
        "CREATE INDEX IF NOT EXISTS idx_chat_participants_user_id ON chat_participants(user_id);",
        ("user_id",),
    ),
    (
        "chat_messages",
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_thread_created_at ON chat_messages(thread_id, created_at DESC);",
        ("thread_id", "created_at"),
    ),
    ("reports", "CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(target_type, target_id);", ("target_type", "target_id")),
    (
        "reports",
        "CREATE INDEX IF NOT EXISTS idx_reports_status_created_at ON reports(status, created_at DESC);",
        ("status", "created_at"),
    ),
    (
        "reports",
        "CREATE INDEX IF NOT EXISTS idx_reports_resolved_created_at ON reports(resolved_at, created_at DESC);",
        ("resolved_at", "created_at"),
    ),
    (
        "moderation_actions",
        "CREATE INDEX IF NOT EXISTS idx_moderation_actions_target ON moderation_actions(target_type, target_id);",
        ("target_type", "target_id"),
    ),
    (
        "moderation_actions",
        "CREATE INDEX IF NOT EXISTS idx_moderation_actions_created_at ON moderation_actions(created_at DESC);",
        ("created_at",),
    ),
    (
        "user_restrictions",
        "CREATE INDEX IF NOT EXISTS idx_user_restrictions_user_status ON user_restrictions(user_id, status);",
        ("user_id", "status"),
    ),
    (
        "user_restrictions",
        "CREATE INDEX IF NOT EXISTS idx_user_restrictions_user_revoked_at ON user_restrictions(user_id, revoked_at);",
        ("user_id", "revoked_at"),
    ),
    (
        "notifications",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created_at ON notifications(user_id, created_at DESC);",
        ("user_id", "created_at"),
    ),
)


def table_exists(table_name: str) -> bool:
    row = fetch_one(f"SELECT to_regclass('public.{table_name}') IS NOT NULL")
    return bool(row and row[0])


def ensure_core_tables() -> None:
    for table_name, ddl in CORE_TABLES.items():
        if not table_exists(table_name):
            execute(ddl)


def ensure_auxiliary_tables() -> None:
    for table_name, ddl in AUX_TABLES.items():
        if not table_exists(table_name):
            execute(ddl)


def ensure_indexes() -> None:
    clear_schema_cache()
    for table_name, ddl, required_columns in INDEX_DDLS:
        if table_has_columns(table_name, required_columns):
            execute(ddl)


def ensure_all_tables() -> None:
    ensure_core_tables()
    ensure_auxiliary_tables()
    clear_schema_cache()
    ensure_indexes()
