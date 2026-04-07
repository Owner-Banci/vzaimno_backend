from __future__ import annotations

import json
import os
import uuid
from typing import Dict, Iterable, Sequence

from app.db import execute, fetch_all, fetch_one
from app.security import hash_password
from app.schema_compat import clear_schema_cache, table_has_columns
from app.task_compat import (
    TASK_PUBLIC_STATUSES,
    announcement_status_to_task_fields,
    builder_category_slug,
    derive_budget_bounds,
    derive_quick_offer_price,
    derive_reward_amount,
    ensure_task_payload,
    is_uuid_like,
    legacy_offer_status_to_canonical,
    primary_map_point,
    primary_source_address,
    route_points_from_payload,
)


CORE_TABLES: Dict[str, str] = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
          id UUID PRIMARY KEY,
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
          id UUID PRIMARY KEY,
          kind TEXT NOT NULL,
          task_id UUID NULL,
          offer_id UUID NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_message_at TIMESTAMPTZ NULL
        );
    """,
    "chat_participants": """
        CREATE TABLE IF NOT EXISTS chat_participants (
          thread_id UUID NOT NULL,
          user_id UUID NOT NULL,
          role TEXT NOT NULL,
          joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          left_at TIMESTAMPTZ NULL,
          last_read_message_id UUID NULL,
          PRIMARY KEY (thread_id, user_id)
        );
    """,
    "chat_messages": """
        CREATE TABLE IF NOT EXISTS chat_messages (
          id UUID PRIMARY KEY,
          thread_id UUID NOT NULL,
          sender_id UUID NULL,
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
          message_id UUID NOT NULL,
          user_id UUID NOT NULL,
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
    "admin_accounts": """
        CREATE TABLE IF NOT EXISTS admin_accounts (
          id UUID PRIMARY KEY,
          login_identifier TEXT NOT NULL UNIQUE,
          email TEXT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'support',
          status TEXT NOT NULL DEFAULT 'active',
          display_name TEXT NULL,
          linked_user_account_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
          created_by_admin_id UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_login_at TIMESTAMPTZ NULL,
          mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
          mfa_secret TEXT NULL,
          disabled_at TIMESTAMPTZ NULL,
          password_reset_required BOOLEAN NOT NULL DEFAULT FALSE
        );
    """,
    "admin_sessions": """
        CREATE TABLE IF NOT EXISTS admin_sessions (
          id UUID PRIMARY KEY,
          admin_account_id UUID NOT NULL REFERENCES admin_accounts(id) ON DELETE CASCADE,
          token_id UUID NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at TIMESTAMPTZ NOT NULL,
          revoked_at TIMESTAMPTZ NULL,
          user_agent TEXT NULL,
          ip_address TEXT NULL
        );
    """,
    "support_threads": """
        CREATE TABLE IF NOT EXISTS support_threads (
          id UUID PRIMARY KEY REFERENCES chat_threads(id) ON DELETE CASCADE,
          user_account_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          assigned_admin_account_id UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          closed_at TIMESTAMPTZ NULL
        );
    """,
    "support_thread_admin_reads": """
        CREATE TABLE IF NOT EXISTS support_thread_admin_reads (
          thread_id UUID NOT NULL REFERENCES support_threads(id) ON DELETE CASCADE,
          admin_account_id UUID NOT NULL REFERENCES admin_accounts(id) ON DELETE CASCADE,
          last_read_message_id UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
          joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (thread_id, admin_account_id)
        );
    """,
    "audit_logs": """
        CREATE TABLE IF NOT EXISTS audit_logs (
          id UUID PRIMARY KEY,
          actor_type TEXT NOT NULL,
          actor_user_account_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
          actor_admin_account_id UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
          action TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT 'success',
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
}


TASK_DOMAIN_TABLES: Dict[str, str] = {
    "task_assignments": """
        CREATE TABLE IF NOT EXISTS task_assignments (
          id UUID PRIMARY KEY,
          task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          offer_id UUID NOT NULL REFERENCES task_offers(id) ON DELETE CASCADE,
          customer_id UUID NOT NULL REFERENCES users(id),
          performer_id UUID NOT NULL REFERENCES users(id),
          assignment_status TEXT NOT NULL DEFAULT 'assigned'
            CHECK (assignment_status IN ('assigned', 'in_progress', 'completed', 'cancelled')),
          execution_stage TEXT NOT NULL DEFAULT 'accepted'
            CHECK (execution_stage IN ('accepted', 'en_route', 'on_site', 'in_progress', 'handoff', 'completed', 'cancelled')),
          route_visibility TEXT NOT NULL DEFAULT 'performer_only'
            CHECK (route_visibility IN ('hidden', 'performer_only', 'customer_visible')),
          chat_thread_id UUID NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ NULL,
          completed_at TIMESTAMPTZ NULL,
          cancelled_at TIMESTAMPTZ NULL,
          cancellation_reason TEXT NULL
        );
    """,
    "task_assignment_events": """
        CREATE TABLE IF NOT EXISTS task_assignment_events (
          id UUID PRIMARY KEY,
          assignment_id UUID NOT NULL REFERENCES task_assignments(id) ON DELETE CASCADE,
          task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL
            CHECK (event_type IN ('assignment_status', 'execution_stage', 'route_visibility', 'chat_bound')),
          from_value TEXT NULL,
          to_value TEXT NOT NULL,
          changed_by UUID NULL REFERENCES users(id),
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """,
}


COMPAT_DDLS: Iterable[str] = (
    "CREATE EXTENSION IF NOT EXISTS postgis;",
    "ALTER TYPE offer_status ADD VALUE IF NOT EXISTS 'withdrawn_by_sender';",
    "ALTER TYPE report_target_type ADD VALUE IF NOT EXISTS 'announcement';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'no_action';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'warning';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'mute_chat';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'restrict_posting';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'restrict_offers';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'temporary_ban';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'permanent_ban';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'custom_restriction';",
    "ALTER TYPE report_resolution ADD VALUE IF NOT EXISTS 'report_rejected';",
    "ALTER TYPE restriction_type ADD VALUE IF NOT EXISTS 'mute_chat';",
    "ALTER TYPE restriction_type ADD VALUE IF NOT EXISTS 'custom';",
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
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS author_role TEXT NULL;",
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS target_role TEXT NULL;",
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
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS budget_min NUMERIC NULL;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS budget_max NUMERIC NULL;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS quick_offer_price NUMERIC NULL;",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reoffer_policy TEXT NOT NULL DEFAULT 'blocked_after_reject';",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL DEFAULT 'counter_price';",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS agreed_price NUMERIC NULL;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS minimum_price_accepted BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS can_reoffer BOOLEAN NOT NULL DEFAULT TRUE;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS reoffer_block_reason TEXT NULL;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS chat_thread_id UUID NULL;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ NULL;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ NULL;",
    "ALTER TABLE task_offers ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMPTZ NULL;",
    "ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS assignment_id UUID NULL;",
    "ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS email TEXT NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'support';",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS display_name TEXT NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS linked_user_account_id UUID NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS created_by_admin_id UUID NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS mfa_secret TEXT NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ NULL;",
    "ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS password_reset_required BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS user_agent TEXT NULL;",
    "ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS ip_address TEXT NULL;",
    "ALTER TABLE support_threads ADD COLUMN IF NOT EXISTS assigned_admin_account_id UUID NULL;",
    "ALTER TABLE support_threads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';",
    "ALTER TABLE support_threads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE support_threads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE support_threads ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ NULL;",
    "ALTER TABLE support_thread_admin_reads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sender_type TEXT NULL;",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sender_user_account_id UUID NULL;",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sender_admin_account_id UUID NULL;",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sender_display_name TEXT NULL;",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sender_label TEXT NULL;",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;",
    "ALTER TABLE chat_messages ALTER COLUMN sender_id DROP NOT NULL;",
    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}'::jsonb;",
    "ALTER TABLE reports ADD COLUMN IF NOT EXISTS resolved_by_admin_account_id UUID NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS reason_text TEXT NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS source_type TEXT NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS source_id UUID NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS revoked_by UUID NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS issued_by_admin_account_id UUID NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS revoked_by_admin_account_id UUID NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS revocation_reason TEXT NULL;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}'::jsonb;",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE user_restrictions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_admin_accounts_role'
      ) THEN
        ALTER TABLE admin_accounts
        ADD CONSTRAINT chk_admin_accounts_role
        CHECK (role IN ('support', 'moderator', 'admin'));
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_admin_accounts_status'
      ) THEN
        ALTER TABLE admin_accounts
        ADD CONSTRAINT chk_admin_accounts_status
        CHECK (status IN ('active', 'disabled'));
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_reports_resolved_by_admin_account'
      ) THEN
        ALTER TABLE reports
        ADD CONSTRAINT fk_reports_resolved_by_admin_account
        FOREIGN KEY (resolved_by_admin_account_id) REFERENCES admin_accounts(id) ON DELETE SET NULL;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_user_restrictions_issued_by_admin_account'
      ) THEN
        ALTER TABLE user_restrictions
        ADD CONSTRAINT fk_user_restrictions_issued_by_admin_account
        FOREIGN KEY (issued_by_admin_account_id) REFERENCES admin_accounts(id) ON DELETE SET NULL;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_user_restrictions_revoked_by_admin_account'
      ) THEN
        ALTER TABLE user_restrictions
        ADD CONSTRAINT fk_user_restrictions_revoked_by_admin_account
        FOREIGN KEY (revoked_by_admin_account_id) REFERENCES admin_accounts(id) ON DELETE SET NULL;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_chat_messages_sender_user_account'
      ) THEN
        ALTER TABLE chat_messages
        ADD CONSTRAINT fk_chat_messages_sender_user_account
        FOREIGN KEY (sender_user_account_id) REFERENCES users(id) ON DELETE SET NULL;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_chat_messages_sender_admin_account'
      ) THEN
        ALTER TABLE chat_messages
        ADD CONSTRAINT fk_chat_messages_sender_admin_account
        FOREIGN KEY (sender_admin_account_id) REFERENCES admin_accounts(id) ON DELETE SET NULL;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_chat_messages_sender_identity'
      ) THEN
        ALTER TABLE chat_messages
        ADD CONSTRAINT chk_chat_messages_sender_identity
        CHECK (
          (sender_type = 'user' AND sender_user_account_id IS NOT NULL AND sender_admin_account_id IS NULL)
          OR (sender_type = 'admin' AND sender_user_account_id IS NULL AND sender_admin_account_id IS NOT NULL)
          OR (sender_type = 'system' AND sender_user_account_id IS NULL AND sender_admin_account_id IS NULL)
        ) NOT VALID;
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_support_threads_status'
      ) THEN
        ALTER TABLE support_threads
        ADD CONSTRAINT chk_support_threads_status
        CHECK (status IN ('open', 'pending', 'closed'));
      END IF;
    END $$;
    """,
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_audit_logs_actor_identity'
      ) THEN
        ALTER TABLE audit_logs
        ADD CONSTRAINT chk_audit_logs_actor_identity
        CHECK (
          (actor_type = 'user' AND actor_user_account_id IS NOT NULL AND actor_admin_account_id IS NULL)
          OR (actor_type = 'admin' AND actor_user_account_id IS NULL AND actor_admin_account_id IS NOT NULL)
          OR (actor_type = 'system' AND actor_user_account_id IS NULL AND actor_admin_account_id IS NULL)
        );
      END IF;
    END $$;
    """,
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
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_task_from_user ON reviews(task_id, from_user_id) WHERE task_id IS NOT NULL AND from_user_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_accounts_login_identifier ON admin_accounts ((lower(login_identifier)));",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_accounts_email ON admin_accounts ((lower(email))) WHERE email IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_accounts_linked_user_account_id ON admin_accounts(linked_user_account_id) WHERE linked_user_account_id IS NOT NULL;",
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
        "reviews",
        "CREATE INDEX IF NOT EXISTS idx_reviews_to_user_target_role_created_at ON reviews(to_user_id, target_role, created_at DESC);",
        ("to_user_id", "target_role", "created_at"),
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
        "chat_threads",
        "CREATE INDEX IF NOT EXISTS idx_chat_threads_assignment_id ON chat_threads(assignment_id);",
        ("assignment_id",),
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
        "user_restrictions",
        "CREATE INDEX IF NOT EXISTS idx_user_restrictions_source ON user_restrictions(source_type, source_id);",
        ("source_type", "source_id"),
    ),
    (
        "notifications",
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created_at ON notifications(user_id, created_at DESC);",
        ("user_id", "created_at"),
    ),
    (
        "admin_accounts",
        "CREATE INDEX IF NOT EXISTS idx_admin_accounts_status_role ON admin_accounts(status, role);",
        ("status", "role"),
    ),
    (
        "admin_sessions",
        "CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin_revoked_at ON admin_sessions(admin_account_id, revoked_at, expires_at);",
        ("admin_account_id", "revoked_at", "expires_at"),
    ),
    (
        "support_threads",
        "CREATE INDEX IF NOT EXISTS idx_support_threads_user_status_updated ON support_threads(user_account_id, status, updated_at DESC);",
        ("user_account_id", "status", "updated_at"),
    ),
    (
        "support_threads",
        "CREATE INDEX IF NOT EXISTS idx_support_threads_assigned_admin_updated ON support_threads(assigned_admin_account_id, updated_at DESC);",
        ("assigned_admin_account_id", "updated_at"),
    ),
    (
        "support_thread_admin_reads",
        "CREATE INDEX IF NOT EXISTS idx_support_thread_admin_reads_admin_updated ON support_thread_admin_reads(admin_account_id, updated_at DESC);",
        ("admin_account_id", "updated_at"),
    ),
    (
        "chat_messages",
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_sender_identity ON chat_messages(sender_type, sender_user_account_id, sender_admin_account_id, created_at DESC);",
        ("sender_type", "sender_user_account_id", "sender_admin_account_id", "created_at"),
    ),
    (
        "audit_logs",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_created_at ON audit_logs(actor_type, actor_admin_account_id, actor_user_account_id, created_at DESC);",
        ("actor_type", "actor_admin_account_id", "actor_user_account_id", "created_at"),
    ),
    (
        "audit_logs",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_target_created_at ON audit_logs(target_type, target_id, created_at DESC);",
        ("target_type", "target_id", "created_at"),
    ),
    (
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_deleted_status ON tasks(deleted_at, status, moderation_status);",
        ("deleted_at", "status", "moderation_status"),
    ),
    (
        "tasks",
        "CREATE INDEX IF NOT EXISTS idx_tasks_budget_range ON tasks(budget_min, budget_max, quick_offer_price);",
        ("budget_min", "budget_max", "quick_offer_price"),
    ),
    (
        "task_offers",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_offers_chat_thread_id ON task_offers(chat_thread_id) WHERE chat_thread_id IS NOT NULL;",
        ("chat_thread_id",),
    ),
    (
        "task_assignments",
        "CREATE INDEX IF NOT EXISTS idx_task_assignments_task_id ON task_assignments(task_id);",
        ("task_id",),
    ),
    (
        "task_assignments",
        "CREATE INDEX IF NOT EXISTS idx_task_assignments_performer_status_updated ON task_assignments(performer_id, assignment_status, updated_at DESC);",
        ("performer_id", "assignment_status", "updated_at"),
    ),
    (
        "task_assignments",
        "CREATE INDEX IF NOT EXISTS idx_task_assignments_customer_status_updated ON task_assignments(customer_id, assignment_status, updated_at DESC);",
        ("customer_id", "assignment_status", "updated_at"),
    ),
    (
        "task_assignments",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_assignments_offer_id ON task_assignments(offer_id);",
        ("offer_id",),
    ),
    (
        "task_assignments",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_assignments_chat_thread_id ON task_assignments(chat_thread_id) WHERE chat_thread_id IS NOT NULL;",
        ("chat_thread_id",),
    ),
    (
        "task_assignments",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_assignments_active_task ON task_assignments(task_id) WHERE assignment_status IN ('assigned', 'in_progress');",
        ("task_id", "assignment_status"),
    ),
    (
        "task_assignment_events",
        "CREATE INDEX IF NOT EXISTS idx_task_assignment_events_assignment_created ON task_assignment_events(assignment_id, created_at DESC);",
        ("assignment_id", "created_at"),
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


def ensure_task_domain_tables() -> None:
    for table_name, ddl in TASK_DOMAIN_TABLES.items():
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


def _category_id_for_slug(slug: str) -> str | None:
    row = fetch_one(
        """
        SELECT id::text
        FROM categories
        WHERE slug = %s
        LIMIT 1
        """,
        (slug,),
    )
    if row and row[0]:
        return str(row[0])
    return None


def _task_exists(task_id: str) -> bool:
    row = fetch_one("SELECT 1 FROM tasks WHERE id::text = %s", (task_id,))
    return bool(row)


def _user_exists(user_id: str) -> bool:
    row = fetch_one("SELECT 1 FROM users WHERE id::text = %s", (user_id,))
    return bool(row)


def _ensure_task_route_points(task_id: str, data: dict[str, object]) -> None:
    if fetch_one("SELECT 1 FROM task_route_points WHERE task_id::text = %s LIMIT 1", (task_id,)):
        return

    for point in route_points_from_payload(task_id, data):
        raw_point = point.get("point")
        if not isinstance(raw_point, tuple):
            continue

        execute(
            """
            INSERT INTO task_route_points (
                id, task_id, point_order, title, address_text, point, point_kind, created_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s, now()
            )
            ON CONFLICT DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                task_id,
                int(point["point_order"]),
                point.get("title"),
                point.get("address_text"),
                raw_point[1],
                raw_point[0],
                point.get("point_kind"),
            ),
        )


def backfill_legacy_tasks() -> None:
    rows = fetch_all(
        """
        SELECT
            id::text,
            user_id,
            category,
            title,
            status,
            data,
            created_at,
            updated_at,
            deleted_at,
            CASE WHEN location_point IS NULL THEN NULL ELSE ST_Y(location_point::geometry) END AS location_lat,
            CASE WHEN location_point IS NULL THEN NULL ELSE ST_X(location_point::geometry) END AS location_lon
        FROM announcements
        ORDER BY created_at ASC
        """
    )

    for row in rows:
        ann_id = str(row[0])
        user_id = str(row[1] or "")
        category = str(row[2] or "")
        title = str(row[3] or "")
        status = str(row[4] or "")
        raw_data = row[5] if isinstance(row[5], dict) else {}
        created_at = row[6]
        updated_at = row[7] or row[6]
        deleted_at = row[8]
        location_lat = row[9]
        location_lon = row[10]

        if not is_uuid_like(ann_id) or not is_uuid_like(user_id):
            continue
        if not _user_exists(user_id):
            continue
        if _task_exists(ann_id):
            _ensure_task_route_points(ann_id, ensure_task_payload(raw_data, title=title, announcement_status=status, deleted_at=deleted_at))
            continue

        has_accepted_offer = bool(
            fetch_one(
                """
                SELECT 1
                FROM announcement_offers
                WHERE announcement_id = %s
                  AND status = 'accepted'
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (ann_id,),
            )
        )
        task_status, moderation_status = announcement_status_to_task_fields(
            status,
            deleted=deleted_at is not None,
            has_accepted_offer=has_accepted_offer,
        )
        data = ensure_task_payload(raw_data, title=title, announcement_status=status, deleted_at=deleted_at)
        budget_min, budget_max = derive_budget_bounds(data)
        quick_offer_price = derive_quick_offer_price(data)
        reward_amount = derive_reward_amount(data)
        address_text = primary_source_address(data)
        point = None
        if location_lat is not None and location_lon is not None:
            point = (float(location_lat), float(location_lon))
        else:
            point = primary_map_point(data)

        category_id = _category_id_for_slug(builder_category_slug(category))
        if not category_id:
            continue

        published_at = created_at if moderation_status == "published" and task_status in TASK_PUBLIC_STATUSES | {"agreed", "in_progress", "completed", "cancelled"} else None
        closed_at = created_at if task_status in {"closed", "completed", "cancelled"} or deleted_at is not None else None
        description = (
            str(data.get("generated_description") or "")
            or str(data.get("notes") or "")
            or title
        )

        execute(
            """
            INSERT INTO tasks (
                id,
                customer_id,
                title,
                description,
                category_id,
                reward_amount,
                currency,
                price_type,
                deadline_at,
                location_point,
                address_text,
                customer_comment,
                performer_preferences,
                status,
                moderation_status,
                views_count,
                favorites_count,
                responses_count,
                accepted_offer_id,
                extra,
                created_at,
                updated_at,
                published_at,
                closed_at,
                deleted_at,
                budget_min,
                budget_max,
                quick_offer_price,
                reoffer_policy
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'RUB',
                %s,
                NULL,
                CASE
                    WHEN %s::double precision IS NULL OR %s::double precision IS NULL THEN NULL
                    ELSE ST_SetSRID(
                        ST_MakePoint(%s::double precision, %s::double precision),
                        4326
                    )::geography
                END,
                %s,
                %s,
                NULL,
                %s,
                %s,
                0,
                0,
                COALESCE((%s)::integer, 0),
                NULL,
                %s::jsonb,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            ON CONFLICT DO NOTHING
            """,
            (
                ann_id,
                user_id,
                title,
                description,
                category_id,
                reward_amount,
                "negotiable" if budget_min is not None or budget_max is not None else ("free" if reward_amount == 0 else "fixed"),
                point[1] if point else None,
                point[0] if point else None,
                point[1] if point else None,
                point[0] if point else None,
                address_text,
                data.get("notes"),
                task_status,
                moderation_status,
                int(data.get("offers_count") or 0),
                json.dumps(data, ensure_ascii=False),
                created_at,
                updated_at,
                published_at,
                closed_at,
                deleted_at,
                budget_min,
                budget_max,
                quick_offer_price,
                str(data.get("offer_policy", {}).get("reoffer_policy") or "blocked_after_reject"),
            ),
        )
        _ensure_task_route_points(ann_id, data)


def backfill_legacy_task_offers() -> None:
    rows = fetch_all(
        """
        SELECT
            ao.id::text,
            ao.announcement_id,
            ao.performer_id,
            ao.message,
            ao.proposed_price,
            ao.status,
            ao.created_at,
            ao.chat_thread_id
        FROM announcement_offers ao
        WHERE ao.deleted_at IS NULL
        ORDER BY ao.created_at ASC
        """
    )

    for row in rows:
        offer_id = str(row[0])
        task_id = str(row[1] or "")
        performer_id = str(row[2] or "")
        message = row[3]
        proposed_price = row[4]
        legacy_status = str(row[5] or "pending")
        created_at = row[6]
        chat_thread_id = str(row[7]) if row[7] else None

        if not (is_uuid_like(offer_id) and is_uuid_like(task_id) and is_uuid_like(performer_id)):
            continue
        if not _user_exists(performer_id):
            continue
        if not _task_exists(task_id):
            continue
        if fetch_one("SELECT 1 FROM task_offers WHERE id::text = %s", (offer_id,)):
            continue

        task_row = fetch_one("SELECT extra FROM tasks WHERE id::text = %s", (task_id,))
        task_data = ensure_task_payload(task_row[0] if task_row and isinstance(task_row[0], dict) else {}, title="", announcement_status="active")
        quick_offer_price = derive_quick_offer_price(task_data)

        status = legacy_offer_status_to_canonical(legacy_status)
        pricing_mode = "counter_price" if proposed_price is not None else "quick_min_price"
        minimum_price_accepted = proposed_price is None
        agreed_price = proposed_price if status == "accepted_by_customer" and proposed_price is not None else (quick_offer_price if status == "accepted_by_customer" else None)
        can_reoffer = status == "sent"

        execute(
            """
            INSERT INTO task_offers (
                id,
                task_id,
                performer_id,
                message,
                proposed_price,
                currency,
                status,
                created_at,
                updated_at,
                cancelled_at,
                pricing_mode,
                agreed_price,
                minimum_price_accepted,
                can_reoffer,
                reoffer_block_reason,
                chat_thread_id,
                accepted_at,
                rejected_at,
                withdrawn_at
            )
            VALUES (
                %s, %s, %s, %s, %s, 'RUB', %s, %s, %s, NULL,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            """,
            (
                offer_id,
                task_id,
                performer_id,
                message,
                proposed_price,
                status,
                created_at,
                created_at,
                pricing_mode,
                agreed_price,
                minimum_price_accepted,
                can_reoffer,
                None if can_reoffer else "legacy_status_terminal",
                chat_thread_id if chat_thread_id and is_uuid_like(chat_thread_id) else None,
                created_at if status == "accepted_by_customer" else None,
                created_at if status == "rejected_by_customer" else None,
                created_at if status == "withdrawn_by_sender" else None,
            ),
        )

    task_rows = fetch_all("SELECT id::text FROM tasks")
    for task_row in task_rows:
        task_id = str(task_row[0])
        accepted = fetch_one(
            """
            SELECT id::text
            FROM task_offers
            WHERE task_id::text = %s
              AND status = 'accepted_by_customer'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id,),
        )
        responses = fetch_one(
            """
            SELECT COUNT(*)
            FROM task_offers
            WHERE task_id::text = %s
              AND status IN ('sent', 'accepted_by_customer')
            """,
            (task_id,),
        )
        execute(
            """
            UPDATE tasks
            SET accepted_offer_id = %s,
                responses_count = %s,
                status = CASE
                    WHEN %s::uuid IS NOT NULL AND status IN ('published', 'in_responses') THEN 'agreed'
                    WHEN %s::integer > 0 AND status = 'published' THEN 'in_responses'
                    ELSE status
                END,
                updated_at = now()
            WHERE id::text = %s
            """,
            (
                accepted[0] if accepted else None,
                int(responses[0] or 0) if responses else 0,
                accepted[0] if accepted else None,
                int(responses[0] or 0) if responses else 0,
                task_id,
            ),
        )


def backfill_legacy_task_assignments() -> None:
    rows = fetch_all(
        """
        SELECT
            t.id::text,
            t.customer_id::text,
            o.id::text,
            o.performer_id::text,
            o.chat_thread_id::text
        FROM tasks t
        JOIN task_offers o
          ON o.id = t.accepted_offer_id
        WHERE t.accepted_offer_id IS NOT NULL
        """
    )

    for row in rows:
        task_id = str(row[0])
        customer_id = str(row[1])
        offer_id = str(row[2])
        performer_id = str(row[3])
        chat_thread_id = str(row[4]) if row[4] else None
        chat_thread_uuid = chat_thread_id if chat_thread_id and is_uuid_like(chat_thread_id) else None

        existing = fetch_one(
            """
            SELECT id::text
            FROM task_assignments
            WHERE offer_id::text = %s
            LIMIT 1
            """,
            (offer_id,),
        )
        if not existing and chat_thread_uuid:
            existing = fetch_one(
                """
                SELECT id::text
                FROM task_assignments
                WHERE chat_thread_id = %s::uuid
                LIMIT 1
                """,
                (chat_thread_uuid,),
            )
        if not existing:
            existing = fetch_one(
                """
                SELECT id::text
                FROM task_assignments
                WHERE task_id::text = %s
                  AND assignment_status IN ('assigned', 'in_progress')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (task_id,),
            )

        if existing:
            assignment_id = str(existing[0])
            execute(
                """
                UPDATE task_assignments
                SET task_id = %s,
                    offer_id = %s,
                    customer_id = %s,
                    performer_id = %s,
                    assignment_status = 'assigned',
                    execution_stage = 'accepted',
                    route_visibility = 'performer_only',
                    chat_thread_id = COALESCE(%s::uuid, chat_thread_id),
                    updated_at = now()
                WHERE id::text = %s
                """,
                (
                    task_id,
                    offer_id,
                    customer_id,
                    performer_id,
                    chat_thread_uuid,
                    assignment_id,
                ),
            )
        else:
            assignment_id = str(uuid.uuid4())
            execute(
                """
                INSERT INTO task_assignments (
                    id, task_id, offer_id, customer_id, performer_id,
                    assignment_status, execution_stage, route_visibility,
                    chat_thread_id, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    'assigned', 'accepted', 'performer_only',
                    %s, now(), now()
                )
                """,
                (
                    assignment_id,
                    task_id,
                    offer_id,
                    customer_id,
                    performer_id,
                    chat_thread_uuid,
                ),
            )

        execute(
            """
            INSERT INTO task_assignment_events (
                id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload
            )
            SELECT %s, %s, %s, 'assignment_status', NULL, 'assigned', %s, '{}'::jsonb
            WHERE NOT EXISTS (
                SELECT 1
                FROM task_assignment_events
                WHERE assignment_id = %s::uuid
                  AND event_type = 'assignment_status'
                  AND to_value = 'assigned'
            )
            """,
            (str(uuid.uuid4()), assignment_id, task_id, customer_id, assignment_id),
        )
        execute(
            """
            INSERT INTO task_assignment_events (
                id, assignment_id, task_id, event_type, from_value, to_value, changed_by, payload
            )
            SELECT %s, %s, %s, 'execution_stage', NULL, 'accepted', %s, '{}'::jsonb
            WHERE NOT EXISTS (
                SELECT 1
                FROM task_assignment_events
                WHERE assignment_id = %s::uuid
                  AND event_type = 'execution_stage'
                  AND to_value = 'accepted'
            )
            """,
            (str(uuid.uuid4()), assignment_id, task_id, customer_id, assignment_id),
        )

    assignment_rows = fetch_all(
        """
        SELECT id::text, task_id::text, chat_thread_id::text, offer_id::text
        FROM task_assignments
        WHERE chat_thread_id IS NOT NULL
        """
    )
    for row in assignment_rows:
        assignment_id = str(row[0])
        task_id = str(row[1])
        chat_thread_id = str(row[2])
        offer_id = str(row[3])
        execute(
            """
            UPDATE chat_threads
            SET task_id = %s,
                offer_id = %s,
                assignment_id = %s
            WHERE id::text = %s
            """,
            (task_id, offer_id, assignment_id, chat_thread_id),
        )
        execute(
            """
            UPDATE task_offers
            SET chat_thread_id = %s
            WHERE id::text = %s
            """,
            (chat_thread_id, offer_id),
        )


def backfill_task_status_events() -> None:
    task_rows = fetch_all(
        """
        SELECT id::text, customer_id::text, status::text
        FROM tasks
        """
    )
    for row in task_rows:
        task_id = str(row[0])
        changed_by = str(row[1])
        status = str(row[2])
        exists = fetch_one("SELECT 1 FROM task_status_events WHERE task_id::text = %s LIMIT 1", (task_id,))
        if exists:
            continue
        execute(
            """
            INSERT INTO task_status_events (
                id, task_id, from_status, to_status, changed_by, reason, created_at
            )
            VALUES (%s, %s, NULL, %s, %s, %s, now())
            ON CONFLICT DO NOTHING
            """,
            (str(uuid.uuid4()), task_id, status, changed_by, "legacy_backfill"),
        )


def _admin_role_from_legacy_user_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized == "admin":
        return "admin"
    if normalized == "moderator":
        return "moderator"
    return "support"


def _admin_sender_label(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized == "admin":
        return "Администратор"
    if normalized == "moderator":
        return "Модератор"
    return "Поддержка"


def _normalize_admin_login_identifier(email: str | None, user_id: str) -> str:
    normalized = (email or "").strip().lower()
    return normalized or f"admin-{user_id}"


def backfill_admin_accounts_from_legacy_users() -> None:
    if not table_exists("admin_accounts"):
        return

    rows = fetch_all(
        """
        SELECT
            u.id::text,
            u.email,
            u.password_hash,
            u.role::text,
            u.created_at,
            COALESCE(NULLIF(BTRIM(up.display_name), ''), NULLIF(BTRIM(u.email), ''), 'Команда Vzaimno')
        FROM users u
        LEFT JOIN user_profiles up
          ON up.user_id = u.id
        WHERE u.role::text IN ('admin', 'moderator')
        ORDER BY u.created_at ASC, u.id ASC
        """
    )

    for row in rows:
        user_id = str(row[0])
        email = str(row[1] or "").strip().lower() or None
        password_hash = str(row[2] or "").strip()
        legacy_role = str(row[3] or "admin").strip().lower()
        created_at = row[4]
        display_name = str(row[5] or "").strip() or (email or f"admin-{user_id}")
        login_identifier = _normalize_admin_login_identifier(email, user_id)
        existing = fetch_one(
            """
            SELECT id::text
            FROM admin_accounts
            WHERE linked_user_account_id::text = %s
               OR lower(login_identifier) = lower(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (user_id, login_identifier),
        )
        if existing:
            execute(
                """
                UPDATE admin_accounts
                SET linked_user_account_id = COALESCE(linked_user_account_id, %s::uuid),
                    email = COALESCE(email, %s),
                    display_name = COALESCE(NULLIF(display_name, ''), %s),
                    role = COALESCE(NULLIF(role, ''), %s),
                    updated_at = now()
                WHERE id::text = %s
                """,
                (user_id, email, display_name, _admin_role_from_legacy_user_role(legacy_role), str(existing[0])),
            )
            continue

        execute(
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
                created_at,
                updated_at,
                password_reset_required
            )
            VALUES (%s::uuid, %s, %s, %s, %s, 'active', %s, %s::uuid, %s, %s, FALSE)
            """,
            (
                str(uuid.uuid4()),
                login_identifier,
                email,
                password_hash or hash_password(str(uuid.uuid4())),
                _admin_role_from_legacy_user_role(legacy_role),
                display_name,
                user_id,
                created_at,
                created_at,
            ),
        )

    execute("UPDATE users SET role = 'user' WHERE role::text IN ('admin', 'moderator')")


def ensure_bootstrap_admin_account() -> None:
    if not table_exists("admin_accounts"):
        return
    existing = fetch_one("SELECT 1 FROM admin_accounts LIMIT 1")
    if existing:
        return

    login_identifier = (os.getenv("ADMIN_BOOTSTRAP_LOGIN") or "").strip().lower()
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD") or ""
    if not login_identifier or not password:
        return

    display_name = (os.getenv("ADMIN_BOOTSTRAP_DISPLAY_NAME") or "Super Admin").strip() or "Super Admin"
    execute(
        """
        INSERT INTO admin_accounts (
            id,
            login_identifier,
            email,
            password_hash,
            role,
            status,
            display_name,
            created_at,
            updated_at,
            password_reset_required
        )
        VALUES (%s::uuid, %s, %s, %s, 'admin', 'active', %s, now(), now(), FALSE)
        ON CONFLICT DO NOTHING
        """,
        (
            str(uuid.uuid4()),
            login_identifier,
            login_identifier if "@" in login_identifier else None,
            hash_password(password),
            display_name,
        ),
    )


def backfill_support_threads() -> None:
    if not table_exists("support_threads"):
        return

    rows = fetch_all(
        """
        SELECT ct.id::text,
               ct.created_at,
               COALESCE(ct.last_message_at, ct.created_at) AS updated_at
        FROM chat_threads ct
        WHERE ct.kind::text = 'support'
        ORDER BY ct.created_at ASC
        """
    )

    for row in rows:
        thread_id = str(row[0])
        created_at = row[1]
        updated_at = row[2]
        if fetch_one("SELECT 1 FROM support_threads WHERE id::text = %s", (thread_id,)):
            continue

        participant = fetch_one(
            """
            SELECT cp.user_id::text
            FROM chat_participants cp
            LEFT JOIN admin_accounts aa
              ON aa.linked_user_account_id = cp.user_id
            WHERE cp.thread_id::text = %s
            ORDER BY
                CASE WHEN aa.id IS NULL THEN 0 ELSE 1 END,
                cp.joined_at ASC,
                cp.user_id ASC
            LIMIT 1
            """,
            (thread_id,),
        )
        if not participant or not participant[0]:
            continue

        user_account_id = str(participant[0])
        assigned = fetch_one(
            """
            SELECT aa.id::text
            FROM chat_messages m
            JOIN admin_accounts aa
              ON aa.linked_user_account_id = m.sender_id
            WHERE m.thread_id::text = %s
              AND m.sender_id::text <> %s
            ORDER BY m.created_at DESC
            LIMIT 1
            """,
            (thread_id, user_account_id),
        )
        if not assigned:
            assigned = fetch_one(
                """
                SELECT aa.id::text
                FROM chat_participants cp
                JOIN admin_accounts aa
                  ON aa.linked_user_account_id = cp.user_id
                WHERE cp.thread_id::text = %s
                  AND cp.user_id::text <> %s
                ORDER BY cp.joined_at ASC, cp.user_id ASC
                LIMIT 1
                """,
                (thread_id, user_account_id),
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
            VALUES (%s::uuid, %s::uuid, %s::uuid, 'open', %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (thread_id, user_account_id, str(assigned[0]) if assigned and assigned[0] else None, created_at, updated_at),
        )


def backfill_chat_message_sender_identity() -> None:
    if not table_exists("chat_messages"):
        return

    rows = fetch_all(
        """
        SELECT
            m.id::text,
            COALESCE(m.type::text, 'text') AS message_type,
            m.sender_id::text,
            st.user_account_id::text AS support_user_account_id,
            aa.id::text AS admin_account_id,
            aa.role,
            COALESCE(NULLIF(BTRIM(aa.display_name), ''), NULLIF(BTRIM(up.display_name), ''), NULLIF(BTRIM(u.email), ''), NULLIF(BTRIM(aa.login_identifier), ''), 'Команда Vzaimno') AS admin_display_name,
            COALESCE(NULLIF(BTRIM(up.display_name), ''), NULLIF(BTRIM(u.email), ''), 'Пользователь') AS user_display_name
        FROM chat_messages m
        LEFT JOIN support_threads st
          ON st.id = m.thread_id
        LEFT JOIN users u
          ON u.id = m.sender_id
        LEFT JOIN user_profiles up
          ON up.user_id = m.sender_id
        LEFT JOIN admin_accounts aa
          ON aa.linked_user_account_id = m.sender_id
        ORDER BY m.created_at ASC, m.id ASC
        """
    )

    for row in rows:
        message_id = str(row[0])
        message_type = str(row[1] or "text")
        sender_id = str(row[2]) if row[2] is not None else None
        support_user_account_id = str(row[3]) if row[3] is not None else None
        admin_account_id = str(row[4]) if row[4] is not None else None
        admin_role = str(row[5] or "")
        admin_display_name = str(row[6] or "").strip() or "Команда Vzaimno"
        user_display_name = str(row[7] or "").strip() or "Пользователь"

        if message_type == "system" or sender_id is None:
            sender_type = "system"
            sender_user_account_id = None
            sender_admin_account_id = None
            sender_display_name = "Система"
            sender_label = "Система"
        elif admin_account_id and support_user_account_id and sender_id != support_user_account_id:
            sender_type = "admin"
            sender_user_account_id = None
            sender_admin_account_id = admin_account_id
            sender_display_name = admin_display_name
            sender_label = _admin_sender_label(admin_role)
        else:
            sender_type = "user"
            sender_user_account_id = sender_id
            sender_admin_account_id = None
            sender_display_name = user_display_name
            sender_label = "Пользователь"

        execute(
            """
            UPDATE chat_messages
            SET sender_type = %s,
                sender_user_account_id = %s::uuid,
                sender_admin_account_id = %s::uuid,
                sender_display_name = %s,
                sender_label = %s
            WHERE id::text = %s
            """,
            (
                sender_type,
                sender_user_account_id,
                sender_admin_account_id,
                sender_display_name,
                sender_label,
                message_id,
            ),
        )


def backfill_support_thread_admin_reads() -> None:
    if not table_exists("support_thread_admin_reads"):
        return

    rows = fetch_all(
        """
        SELECT
            st.id::text,
            aa.id::text,
            cp.last_read_message_id::text,
            COALESCE(cp.joined_at, st.created_at) AS joined_at,
            COALESCE(ct.last_message_at, st.updated_at, st.created_at) AS updated_at
        FROM support_threads st
        JOIN chat_threads ct
          ON ct.id = st.id
        JOIN chat_participants cp
          ON cp.thread_id = st.id
        JOIN admin_accounts aa
          ON aa.linked_user_account_id = cp.user_id
        WHERE cp.user_id <> st.user_account_id
        """
    )

    for row in rows:
        execute(
            """
            INSERT INTO support_thread_admin_reads (
                thread_id,
                admin_account_id,
                last_read_message_id,
                joined_at,
                updated_at
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
            ON CONFLICT (thread_id, admin_account_id)
            DO UPDATE SET last_read_message_id = COALESCE(EXCLUDED.last_read_message_id, support_thread_admin_reads.last_read_message_id),
                          updated_at = EXCLUDED.updated_at
            """,
            (str(row[0]), str(row[1]), str(row[2]) if row[2] is not None else None, row[3], row[4]),
        )


def backfill_admin_actor_columns() -> None:
    if table_exists("reports"):
        execute(
            """
            UPDATE reports r
            SET resolved_by_admin_account_id = aa.id
            FROM admin_accounts aa
            WHERE r.resolved_by_admin_account_id IS NULL
              AND r.resolved_by IS NOT NULL
              AND aa.linked_user_account_id = r.resolved_by
            """
        )

    if table_exists("user_restrictions"):
        execute(
            """
            UPDATE user_restrictions ur
            SET issued_by_admin_account_id = aa.id
            FROM admin_accounts aa
            WHERE ur.issued_by_admin_account_id IS NULL
              AND ur.issued_by IS NOT NULL
              AND aa.linked_user_account_id = ur.issued_by
            """
        )
        execute(
            """
            UPDATE user_restrictions ur
            SET revoked_by_admin_account_id = aa.id
            FROM admin_accounts aa
            WHERE ur.revoked_by_admin_account_id IS NULL
              AND ur.revoked_by IS NOT NULL
              AND aa.linked_user_account_id = ur.revoked_by
            """
        )


def backfill_audit_logs() -> None:
    if not table_exists("audit_logs"):
        return

    moderation_rows = fetch_all(
        """
        SELECT id::text,
               moderator_id::text,
               action_type,
               target_type,
               target_id::text,
               reason,
               payload,
               created_at
        FROM moderation_actions
        ORDER BY created_at ASC, id ASC
        """
    ) if table_exists("moderation_actions") else []

    for row in moderation_rows:
        source_id = str(row[0])
        if fetch_one("SELECT 1 FROM audit_logs WHERE details->>'source_moderation_action_id' = %s LIMIT 1", (source_id,)):
            continue
        moderator_user_id = str(row[1]) if row[1] is not None else None
        linked_admin = fetch_one(
            "SELECT id::text FROM admin_accounts WHERE linked_user_account_id::text = %s LIMIT 1",
            (moderator_user_id,),
        ) if moderator_user_id else None
        actor_type = "admin" if linked_admin and linked_admin[0] else "user"
        actor_user_account_id = None if actor_type == "admin" else moderator_user_id
        actor_admin_account_id = str(linked_admin[0]) if actor_type == "admin" else None
        details = dict(row[6] or {}) if isinstance(row[6], dict) else {}
        details["source_moderation_action_id"] = source_id
        if row[5]:
            details["reason"] = row[5]
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
            VALUES (%s::uuid, %s, %s::uuid, %s::uuid, %s, %s, %s, 'success', %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                actor_type,
                actor_user_account_id,
                actor_admin_account_id,
                str(row[2]),
                str(row[3]),
                str(row[4]),
                json.dumps(details, ensure_ascii=False),
                row[7],
            ),
        )

    support_rows = fetch_all(
        """
        SELECT id::text,
               thread_id::text,
               sender_admin_account_id::text,
               text,
               created_at
        FROM chat_messages
        WHERE sender_type = 'admin'
          AND sender_admin_account_id IS NOT NULL
        ORDER BY created_at ASC, id ASC
        """
    ) if table_exists("chat_messages") else []

    for row in support_rows:
        message_id = str(row[0])
        if fetch_one("SELECT 1 FROM audit_logs WHERE details->>'source_message_id' = %s LIMIT 1", (message_id,)):
            continue
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
            VALUES (%s::uuid, 'admin', NULL, %s::uuid, 'support_message_sent', 'support_thread', %s, 'success', %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                str(row[2]),
                str(row[1]),
                json.dumps(
                    {
                        "source_message_id": message_id,
                        "message_preview": str(row[3] or "")[:250],
                    },
                    ensure_ascii=False,
                ),
                row[4],
            ),
        )


def validate_identity_constraints() -> None:
    if table_exists("chat_messages"):
        execute("ALTER TABLE chat_messages VALIDATE CONSTRAINT chk_chat_messages_sender_identity")
    if table_exists("audit_logs"):
        execute("ALTER TABLE audit_logs VALIDATE CONSTRAINT chk_audit_logs_actor_identity")


def ensure_all_tables() -> None:
    ensure_core_tables()
    ensure_auxiliary_tables()
    ensure_task_domain_tables()
    ensure_compat_columns()
    ensure_chat_thread_kind_compat()
    backfill_admin_accounts_from_legacy_users()
    ensure_bootstrap_admin_account()
    backfill_support_threads()
    backfill_chat_message_sender_identity()
    backfill_support_thread_admin_reads()
    backfill_admin_actor_columns()
    clear_schema_cache()
    ensure_indexes()
    backfill_audit_logs()
    validate_identity_constraints()
    backfill_legacy_tasks()
    backfill_legacy_task_offers()
    backfill_legacy_task_assignments()
    backfill_task_status_events()
