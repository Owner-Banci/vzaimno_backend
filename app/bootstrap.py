from __future__ import annotations

from typing import Dict, Iterable, Sequence

from app.db import execute, fetch_one
from app.schema_compat import clear_schema_cache, table_has_columns


CORE_TABLES: Dict[str, str] = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          phone TEXT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'user',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          deleted_at TIMESTAMPTZ NULL
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
    "user_profiles": """
        CREATE TABLE IF NOT EXISTS user_profiles (
          user_id TEXT PRIMARY KEY,
          display_name TEXT NOT NULL DEFAULT 'Пользователь',
          bio TEXT NULL,
          city TEXT NULL,
          home_location JSONB NULL,
          extra JSONB NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
    "user_stats": """
        CREATE TABLE IF NOT EXISTS user_stats (
          user_id TEXT PRIMARY KEY,
          rating_avg DOUBLE PRECISION NOT NULL DEFAULT 0,
          rating_count INTEGER NOT NULL DEFAULT 0,
          completed_count INTEGER NOT NULL DEFAULT 0,
          cancelled_count INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
    "reviews": """
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY,
          task_id TEXT NULL,
          from_user_id TEXT NOT NULL,
          to_user_id TEXT NOT NULL,
          stars INTEGER NOT NULL,
          text TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
    "user_devices": """
        CREATE TABLE IF NOT EXISTS user_devices (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          platform TEXT NOT NULL,
          device_id TEXT NOT NULL,
          push_token TEXT NULL,
          locale TEXT NULL,
          timezone TEXT NULL,
          device_name TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          deleted_at TIMESTAMPTZ NULL
        );
    """,
    "announcement_offers": """
        CREATE TABLE IF NOT EXISTS announcement_offers (
          id TEXT PRIMARY KEY,
          announcement_id TEXT NOT NULL,
          performer_id TEXT NOT NULL,
          message TEXT NULL,
          proposed_price INTEGER NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          chat_thread_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          deleted_at TIMESTAMPTZ NULL
        );
    """,
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


COMPAT_DDLS: Iterable[str] = (
    "CREATE EXTENSION IF NOT EXISTS postgis;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT NULL;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;",
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS location_point geography(Point,4326);",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS display_name TEXT NULL;",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS bio TEXT NULL;",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS city TEXT NULL;",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS home_location JSONB NULL;",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS extra JSONB NULL DEFAULT '{}'::jsonb;",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS rating_avg DOUBLE PRECISION NOT NULL DEFAULT 0;",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS rating_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS completed_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS cancelled_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS id TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS task_id TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS from_user_id TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS to_user_id TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS stars INTEGER NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS text TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS id TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS user_id TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS platform TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS device_id TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS push_token TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS locale TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS timezone TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS device_name TEXT NULL;",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS id TEXT NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS announcement_id TEXT NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS performer_id TEXT NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS message TEXT NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS proposed_price INTEGER NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS chat_thread_id TEXT NULL;",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE announcement_offers ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;",
    """
    UPDATE announcements
    SET location_point = ST_SetSRID(
        ST_MakePoint(
            COALESCE(
                NULLIF(data -> 'point' ->> 'lon', ''),
                NULLIF(data -> 'pickup_point' ->> 'lon', ''),
                NULLIF(data -> 'help_point' ->> 'lon', '')
            )::double precision,
            COALESCE(
                NULLIF(data -> 'point' ->> 'lat', ''),
                NULLIF(data -> 'pickup_point' ->> 'lat', ''),
                NULLIF(data -> 'help_point' ->> 'lat', '')
            )::double precision
        ),
        4326
    )::geography
    WHERE location_point IS NULL
      AND (
            jsonb_typeof(data -> 'point') = 'object'
            OR jsonb_typeof(data -> 'pickup_point') = 'object'
            OR jsonb_typeof(data -> 'help_point') = 'object'
      )
      AND COALESCE(
            NULLIF(data -> 'point' ->> 'lat', ''),
            NULLIF(data -> 'pickup_point' ->> 'lat', ''),
            NULLIF(data -> 'help_point' ->> 'lat', '')
          ) ~ '^-?[0-9]+(\\.[0-9]+)?$'
      AND COALESCE(
            NULLIF(data -> 'point' ->> 'lon', ''),
            NULLIF(data -> 'pickup_point' ->> 'lon', ''),
            NULLIF(data -> 'help_point' ->> 'lon', '')
          ) ~ '^-?[0-9]+(\\.[0-9]+)?$';
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_id_unique ON reviews(id) WHERE id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_announcement_offers_unique_pending ON announcement_offers(announcement_id, performer_id) WHERE deleted_at IS NULL AND status = 'pending';",
)


INDEX_DDLS: Iterable[tuple[str, str, Sequence[str]]] = (
    ("announcements", "CREATE INDEX IF NOT EXISTS idx_announcements_user_id ON announcements(user_id);", ("user_id",)),
    (
        "announcements",
        "CREATE INDEX IF NOT EXISTS idx_announcements_created_at ON announcements(created_at DESC);",
        ("created_at",),
    ),
    ("announcements", "CREATE INDEX IF NOT EXISTS idx_announcements_status ON announcements(status);", ("status",)),
    (
        "announcements",
        "CREATE INDEX IF NOT EXISTS idx_announcements_location_point_gist ON announcements USING GIST (location_point);",
        ("location_point",),
    ),
    (
        "reviews",
        "CREATE INDEX IF NOT EXISTS idx_reviews_to_user_created_at ON reviews(to_user_id, created_at DESC);",
        ("to_user_id", "created_at"),
    ),
    (
        "reviews",
        "CREATE INDEX IF NOT EXISTS idx_reviews_from_user_created_at ON reviews(from_user_id, created_at DESC);",
        ("from_user_id", "created_at"),
    ),
    (
        "user_devices",
        "CREATE INDEX IF NOT EXISTS idx_user_devices_device_id ON user_devices(device_id);",
        ("device_id",),
    ),
    (
        "user_devices",
        "CREATE INDEX IF NOT EXISTS idx_user_devices_user_id_deleted_at ON user_devices(user_id, deleted_at);",
        ("user_id", "deleted_at"),
    ),
    (
        "user_devices",
        "CREATE INDEX IF NOT EXISTS idx_user_devices_push_token ON user_devices(push_token);",
        ("push_token",),
    ),
    (
        "announcement_offers",
        "CREATE INDEX IF NOT EXISTS idx_announcement_offers_announcement_id ON announcement_offers(announcement_id);",
        ("announcement_id",),
    ),
    (
        "announcement_offers",
        "CREATE INDEX IF NOT EXISTS idx_announcement_offers_performer_id ON announcement_offers(performer_id);",
        ("performer_id",),
    ),
    (
        "announcement_offers",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_announcement_offers_chat_thread_id ON announcement_offers(chat_thread_id) WHERE chat_thread_id IS NOT NULL;",
        ("chat_thread_id",),
    ),
    (
        "announcement_offers",
        "CREATE INDEX IF NOT EXISTS idx_announcement_offers_status_deleted_at ON announcement_offers(status, deleted_at);",
        ("status", "deleted_at"),
    ),
    (
        "chat_threads",
        "CREATE INDEX IF NOT EXISTS idx_chat_threads_last_message_at ON chat_threads(last_message_at DESC NULLS LAST);",
        ("last_message_at",),
    ),
    (
        "chat_threads",
        "CREATE INDEX IF NOT EXISTS idx_chat_threads_offer_id ON chat_threads(offer_id);",
        ("offer_id",),
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


def ensure_compat_columns() -> None:
    for ddl in COMPAT_DDLS:
        execute(ddl)


def ensure_indexes() -> None:
    clear_schema_cache()
    for table_name, ddl, required_columns in INDEX_DDLS:
        if table_has_columns(table_name, required_columns):
            execute(ddl)


def ensure_chat_thread_kind_compat() -> None:
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
        return

    data_type = str(row[0] or "").lower()
    udt_name = str(row[1] or "").lower()
    if data_type == "user-defined" and udt_name == "chat_thread_kind":
        execute("ALTER TYPE chat_thread_kind ADD VALUE IF NOT EXISTS 'offer';")


def ensure_all_tables() -> None:
    ensure_core_tables()
    ensure_auxiliary_tables()
    ensure_compat_columns()
    ensure_chat_thread_kind_compat()
    clear_schema_cache()
    ensure_indexes()
