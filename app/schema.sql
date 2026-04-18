-- =============================================================================
-- Vzaimno Backend — v2 initial schema (release-grade)
-- =============================================================================
-- Applies on a FRESH (empty) database. Not a migration file — if any v1 table
-- already exists, manual work is required to upgrade.
--
-- Design notes:
--   * UUID primary keys for new (v2) entities (users, tasks, offers, chats,
--     admins, audit). Legacy tables retain TEXT keys for backward compatibility
--     with dual-write code in app/main.py.
--   * Status / role / kind fields are TEXT + CHECK constraint (not PG ENUM) —
--     ENUMs in PG can't DROP values and complicate migrations.
--   * CHECK length limits on free-text fields to prevent DoS via huge payloads.
--   * Indexes named with idx_ (non-unique) / ux_ (unique) convention.
--   * Partial unique indexes (WHERE deleted_at IS NULL, WHERE x IS NOT NULL)
--     enforce business invariants without blocking soft-deleted rows.
--
-- Security notes:
--   * Passwords: bcrypt-sha256 (see app/security.py — never in plaintext here).
--   * Refresh tokens: hashed before storage (user_sessions.refresh_token_hash).
--   * Chat encryption at rest: NOT done at column level. Rationale: column-level
--     encryption with a server-side key does not protect against server compromise
--     (key + data on same host). Real protection = full-disk encryption at the
--     filesystem / volume / cloud layer + TLS in transit. For true privacy
--     of chats, E2E encryption (client-side keys) is required — a larger
--     architecture change tracked as future work.
--   * PII (phone, email) kept as plain TEXT — access controlled at DB role level.
--   * Audit trail (audit_logs) is append-only by convention; enforce via role
--     permissions in prod (GRANT INSERT only, no UPDATE/DELETE).
-- =============================================================================

-- Extensions -----------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), digest()
CREATE EXTENSION IF NOT EXISTS "postgis";    -- geography(Point,4326)
CREATE EXTENSION IF NOT EXISTS "citext";     -- case-insensitive unique emails


-- =============================================================================
-- 1. USERS & AUTHENTICATION
-- =============================================================================

CREATE TABLE users (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email                    TEXT NOT NULL,
  phone                    TEXT NULL,
  password_hash            TEXT NOT NULL,
  role                     TEXT NOT NULL DEFAULT 'user',
  is_email_verified        BOOLEAN NOT NULL DEFAULT FALSE,
  is_phone_verified        BOOLEAN NOT NULL DEFAULT FALSE,
  last_login_at            TIMESTAMPTZ NULL,
  failed_login_attempts    INTEGER NOT NULL DEFAULT 0,
  locked_until             TIMESTAMPTZ NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at               TIMESTAMPTZ NULL,
  CONSTRAINT chk_users_role
    CHECK (role IN ('user', 'admin', 'moderator')),
  CONSTRAINT chk_users_email_shape
    CHECK (char_length(email) BETWEEN 3 AND 320 AND email LIKE '%_@_%.__%'),
  CONSTRAINT chk_users_phone_len
    CHECK (phone IS NULL OR char_length(phone) BETWEEN 5 AND 32)
);
CREATE UNIQUE INDEX ux_users_email_lower
  ON users ((lower(email)))
  WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX ux_users_phone
  ON users (phone)
  WHERE phone IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX idx_users_role_deleted
  ON users (role, deleted_at);
CREATE INDEX idx_users_created_at
  ON users (created_at DESC);

-- Refresh-token sessions. One row per active device / login.
CREATE TABLE user_sessions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  refresh_token_hash  TEXT NOT NULL,
  device_id           TEXT NULL,
  user_agent          TEXT NULL,
  ip_address          INET NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at          TIMESTAMPTZ NOT NULL,
  revoked_at          TIMESTAMPTZ NULL,
  revoke_reason       TEXT NULL
);
CREATE UNIQUE INDEX ux_user_sessions_refresh_hash
  ON user_sessions (refresh_token_hash);
CREATE INDEX idx_user_sessions_user_active
  ON user_sessions (user_id, revoked_at, expires_at);

-- Login audit (for brute-force detection and security review).
CREATE TABLE login_attempts (
  id              BIGSERIAL PRIMARY KEY,
  email           TEXT NULL,
  ip_address      INET NULL,
  success         BOOLEAN NOT NULL,
  user_agent      TEXT NULL,
  failure_reason  TEXT NULL,
  attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_login_attempts_email_time
  ON login_attempts (lower(email), attempted_at DESC);
CREATE INDEX idx_login_attempts_ip_time
  ON login_attempts (ip_address, attempted_at DESC);
CREATE INDEX idx_login_attempts_success_time
  ON login_attempts (success, attempted_at DESC);

-- Password reset tokens (single-use, short-lived).
CREATE TABLE password_reset_tokens (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ NULL,
  ip_address  INET NULL
);
CREATE UNIQUE INDEX ux_password_reset_token_hash
  ON password_reset_tokens (token_hash);
CREATE INDEX idx_password_reset_user
  ON password_reset_tokens (user_id, created_at DESC);


-- =============================================================================
-- 2. USER PROFILES & STATS
-- =============================================================================
-- NOTE: user_id is TEXT (matches app/main.py expectations; legacy typing).

CREATE TABLE user_profiles (
  user_id        UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  display_name   TEXT NULL,
  bio            TEXT NULL,
  city           TEXT NULL,
  home_location  JSONB NULL,
  extra          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_user_profiles_display_name_len
    CHECK (display_name IS NULL OR char_length(display_name) <= 80),
  CONSTRAINT chk_user_profiles_bio_len
    CHECK (bio IS NULL OR char_length(bio) <= 1000)
);

CREATE TABLE user_stats (
  user_id          UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  rating_avg       DOUBLE PRECISION NOT NULL DEFAULT 0,
  rating_count     INTEGER NOT NULL DEFAULT 0,
  completed_count  INTEGER NOT NULL DEFAULT 0,
  cancelled_count  INTEGER NOT NULL DEFAULT 0,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_user_stats_rating_avg
    CHECK (rating_avg >= 0 AND rating_avg <= 5),
  CONSTRAINT chk_user_stats_counts_nonneg
    CHECK (rating_count >= 0 AND completed_count >= 0 AND cancelled_count >= 0)
);

-- Push devices & locale per user. Device-row `id` is an opaque TEXT handle
-- generated by the client (legacy shape preserved); `user_id` is a real FK.
CREATE TABLE user_devices (
  id            TEXT PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  platform      TEXT NOT NULL,
  device_id     TEXT NOT NULL,
  push_token    TEXT NULL,
  locale        TEXT NULL,
  timezone      TEXT NULL,
  device_name   TEXT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ NULL,
  CONSTRAINT chk_user_devices_platform
    CHECK (platform IN ('ios', 'android', 'web', 'unknown'))
);
CREATE INDEX idx_user_devices_device_id
  ON user_devices (device_id);
CREATE INDEX idx_user_devices_user_id_deleted_at
  ON user_devices (user_id, deleted_at);
CREATE INDEX idx_user_devices_push_token
  ON user_devices (push_token)
  WHERE push_token IS NOT NULL;


-- =============================================================================
-- 3. CATEGORIES (lookup table with seed)
-- =============================================================================

CREATE TABLE categories (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_categories_slug_format
    CHECK (slug ~ '^[a-z0-9_-]+$' AND char_length(slug) BETWEEN 1 AND 40)
);


-- =============================================================================
-- 4. ADMIN PANEL
-- =============================================================================

CREATE TABLE admin_accounts (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  login_identifier          TEXT NOT NULL,
  email                     TEXT NULL,
  password_hash             TEXT NOT NULL,
  role                      TEXT NOT NULL DEFAULT 'support',
  status                    TEXT NOT NULL DEFAULT 'active',
  display_name              TEXT NULL,
  linked_user_account_id    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_by_admin_id       UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at             TIMESTAMPTZ NULL,
  mfa_enabled               BOOLEAN NOT NULL DEFAULT FALSE,
  mfa_secret                TEXT NULL,
  disabled_at               TIMESTAMPTZ NULL,
  password_reset_required   BOOLEAN NOT NULL DEFAULT FALSE,
  failed_login_attempts     INTEGER NOT NULL DEFAULT 0,
  locked_until              TIMESTAMPTZ NULL,
  CONSTRAINT chk_admin_accounts_role
    CHECK (role IN ('support', 'moderator', 'admin')),
  CONSTRAINT chk_admin_accounts_status
    CHECK (status IN ('active', 'disabled'))
);
CREATE UNIQUE INDEX ux_admin_accounts_login_identifier
  ON admin_accounts ((lower(login_identifier)));
CREATE UNIQUE INDEX ux_admin_accounts_email
  ON admin_accounts ((lower(email)))
  WHERE email IS NOT NULL;
CREATE UNIQUE INDEX ux_admin_accounts_linked_user_account_id
  ON admin_accounts (linked_user_account_id)
  WHERE linked_user_account_id IS NOT NULL;
CREATE INDEX idx_admin_accounts_status_role
  ON admin_accounts (status, role);

CREATE TABLE admin_sessions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  admin_account_id  UUID NOT NULL REFERENCES admin_accounts(id) ON DELETE CASCADE,
  token_id          UUID NOT NULL UNIQUE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL,
  revoked_at        TIMESTAMPTZ NULL,
  user_agent        TEXT NULL,
  ip_address        TEXT NULL
);
CREATE INDEX idx_admin_sessions_admin_revoked_at
  ON admin_sessions (admin_account_id, revoked_at, expires_at);


-- =============================================================================
-- 5. LEGACY ANNOUNCEMENTS (kept for app/main.py dual-write compatibility)
-- =============================================================================
-- These are TEXT-keyed because that's what the dual-write code in
-- _sync_legacy_announcement_projection() expects. On v2, new code reads from
-- `tasks` — `announcements` is maintained as projection only. Future: remove.

CREATE TABLE announcements (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
  category        TEXT NOT NULL,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  data            JSONB NOT NULL DEFAULT '{}'::jsonb,
  location_point  geography(Point, 4326) NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at      TIMESTAMPTZ NULL
);
CREATE INDEX idx_announcements_user_id        ON announcements(user_id);
CREATE INDEX idx_announcements_created_at     ON announcements(created_at DESC);
CREATE INDEX idx_announcements_status         ON announcements(status);
CREATE INDEX idx_announcements_location_gist  ON announcements USING GIST (location_point);

-- Legacy offers table, projected from task_offers.
CREATE TABLE announcement_offers (
  id              TEXT PRIMARY KEY,
  announcement_id TEXT NOT NULL,
  performer_id    TEXT NOT NULL,
  message         TEXT NULL,
  proposed_price  INTEGER NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  chat_thread_id  TEXT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at      TIMESTAMPTZ NULL
);
CREATE INDEX idx_announcement_offers_announcement_id
  ON announcement_offers (announcement_id);
CREATE INDEX idx_announcement_offers_performer_id
  ON announcement_offers (performer_id);
CREATE UNIQUE INDEX idx_announcement_offers_chat_thread_id
  ON announcement_offers (chat_thread_id)
  WHERE chat_thread_id IS NOT NULL;
CREATE INDEX idx_announcement_offers_status_deleted_at
  ON announcement_offers (status, deleted_at);
CREATE UNIQUE INDEX idx_announcement_offers_unique_pending
  ON announcement_offers (announcement_id, performer_id)
  WHERE deleted_at IS NULL AND status = 'pending';


-- =============================================================================
-- 6. TASKS (v2 primary entity — customer-side posts)
-- =============================================================================

CREATE TABLE tasks (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id             UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  title                   TEXT NOT NULL,
  description             TEXT NULL,
  category_id             UUID NULL REFERENCES categories(id) ON DELETE SET NULL,
  reward_amount           NUMERIC(14, 2) NULL,
  currency                TEXT NOT NULL DEFAULT 'RUB',
  price_type              TEXT NOT NULL DEFAULT 'fixed',
  deadline_at             TIMESTAMPTZ NULL,
  location_point          geography(Point, 4326) NULL,
  address_text            TEXT NULL,
  customer_comment        TEXT NULL,
  performer_preferences   JSONB NULL,
  status                  TEXT NOT NULL DEFAULT 'active',
  moderation_status       TEXT NOT NULL DEFAULT 'pending',
  views_count             INTEGER NOT NULL DEFAULT 0,
  favorites_count         INTEGER NOT NULL DEFAULT 0,
  responses_count         INTEGER NOT NULL DEFAULT 0,
  accepted_offer_id       UUID NULL,  -- FK added after task_offers created
  budget_min              NUMERIC(14, 2) NULL,
  budget_max              NUMERIC(14, 2) NULL,
  quick_offer_price       NUMERIC(14, 2) NULL,
  reoffer_policy          TEXT NOT NULL DEFAULT 'blocked_after_reject',
  extra                   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at            TIMESTAMPTZ NULL,
  closed_at               TIMESTAMPTZ NULL,
  deleted_at              TIMESTAMPTZ NULL,
  CONSTRAINT chk_tasks_title_len
    CHECK (char_length(title) BETWEEN 1 AND 200),
  CONSTRAINT chk_tasks_description_len
    CHECK (description IS NULL OR char_length(description) <= 5000),
  CONSTRAINT chk_tasks_price_type
    CHECK (price_type IN ('fixed', 'negotiable', 'free')),
  CONSTRAINT chk_tasks_status
    CHECK (status IN ('draft', 'review', 'active', 'published', 'in_responses',
                      'agreed', 'in_progress', 'completed', 'cancelled', 'closed')),
  -- Values match app/task_compat.announcement_status_to_task_fields() and
  -- admin_panel moderation flow: pending (awaiting review), published (cleared
  -- for display), needs_fix (kicked back to author), rejected, blocked.
  CONSTRAINT chk_tasks_moderation_status
    CHECK (moderation_status IN ('pending', 'published', 'needs_fix', 'rejected', 'blocked')),
  CONSTRAINT chk_tasks_reoffer_policy
    CHECK (reoffer_policy IN ('allowed', 'blocked_after_reject', 'blocked')),
  CONSTRAINT chk_tasks_budgets_positive
    CHECK (
      (budget_min IS NULL OR budget_min >= 0)
      AND (budget_max IS NULL OR budget_max >= 0)
      AND (quick_offer_price IS NULL OR quick_offer_price >= 0)
      AND (reward_amount IS NULL OR reward_amount >= 0)
    ),
  CONSTRAINT chk_tasks_budget_range_order
    CHECK (budget_min IS NULL OR budget_max IS NULL OR budget_min <= budget_max)
);
CREATE INDEX idx_tasks_customer_id        ON tasks (customer_id);
CREATE INDEX idx_tasks_deleted_status     ON tasks (deleted_at, status, moderation_status);
CREATE INDEX idx_tasks_budget_range       ON tasks (budget_min, budget_max, quick_offer_price);
CREATE INDEX idx_tasks_created_at         ON tasks (created_at DESC);
CREATE INDEX idx_tasks_category           ON tasks (category_id) WHERE category_id IS NOT NULL;
CREATE INDEX idx_tasks_location_gist      ON tasks USING GIST (location_point);


-- =============================================================================
-- 7. TASK OFFERS (performer responses to tasks)
-- =============================================================================

CREATE TABLE task_offers (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id                  UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  performer_id             UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  message                  TEXT NULL,
  proposed_price           NUMERIC(14, 2) NULL,
  currency                 TEXT NOT NULL DEFAULT 'RUB',
  status                   TEXT NOT NULL DEFAULT 'sent',
  pricing_mode             TEXT NOT NULL DEFAULT 'counter_price',
  agreed_price             NUMERIC(14, 2) NULL,
  minimum_price_accepted   BOOLEAN NOT NULL DEFAULT FALSE,
  can_reoffer              BOOLEAN NOT NULL DEFAULT TRUE,
  reoffer_block_reason     TEXT NULL,
  chat_thread_id           UUID NULL,  -- FK added after chat_threads created
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  accepted_at              TIMESTAMPTZ NULL,
  rejected_at              TIMESTAMPTZ NULL,
  withdrawn_at             TIMESTAMPTZ NULL,
  cancelled_at             TIMESTAMPTZ NULL,
  CONSTRAINT chk_task_offers_message_len
    CHECK (message IS NULL OR char_length(message) <= 2000),
  CONSTRAINT chk_task_offers_status
    CHECK (status IN ('sent', 'accepted_by_customer', 'rejected_by_customer',
                      'withdrawn_by_sender', 'cancelled')),
  CONSTRAINT chk_task_offers_pricing_mode
    CHECK (pricing_mode IN ('counter_price', 'quick_min_price')),
  CONSTRAINT chk_task_offers_prices_positive
    CHECK (
      (proposed_price IS NULL OR proposed_price >= 0)
      AND (agreed_price IS NULL OR agreed_price >= 0)
    )
);
CREATE INDEX idx_task_offers_task           ON task_offers (task_id);
CREATE INDEX idx_task_offers_performer      ON task_offers (performer_id);
CREATE INDEX idx_task_offers_status         ON task_offers (status);
CREATE UNIQUE INDEX ux_task_offers_chat_thread_id
  ON task_offers (chat_thread_id)
  WHERE chat_thread_id IS NOT NULL;

-- Back-reference: tasks.accepted_offer_id → task_offers.id (added after both exist)
ALTER TABLE tasks
  ADD CONSTRAINT fk_tasks_accepted_offer_id
  FOREIGN KEY (accepted_offer_id)
  REFERENCES task_offers(id)
  ON DELETE SET NULL;


-- =============================================================================
-- 8. TASK ASSIGNMENTS (agreed task → ongoing work)
-- =============================================================================

CREATE TABLE task_assignments (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id              UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  offer_id             UUID NOT NULL REFERENCES task_offers(id) ON DELETE CASCADE,
  customer_id          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  performer_id         UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  assignment_status    TEXT NOT NULL DEFAULT 'assigned',
  execution_stage      TEXT NOT NULL DEFAULT 'accepted',
  route_visibility     TEXT NOT NULL DEFAULT 'performer_only',
  chat_thread_id       UUID NULL,  -- FK added below
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at           TIMESTAMPTZ NULL,
  completed_at         TIMESTAMPTZ NULL,
  cancelled_at         TIMESTAMPTZ NULL,
  cancellation_reason  TEXT NULL,
  CONSTRAINT chk_task_assignments_assignment_status
    CHECK (assignment_status IN ('assigned', 'in_progress', 'completed', 'cancelled')),
  CONSTRAINT chk_task_assignments_execution_stage
    CHECK (execution_stage IN ('accepted', 'en_route', 'on_site', 'in_progress',
                               'handoff', 'completed', 'cancelled')),
  CONSTRAINT chk_task_assignments_route_visibility
    CHECK (route_visibility IN ('hidden', 'performer_only', 'customer_visible'))
);
CREATE UNIQUE INDEX ux_task_assignments_offer_id
  ON task_assignments (offer_id);
CREATE UNIQUE INDEX ux_task_assignments_chat_thread_id
  ON task_assignments (chat_thread_id)
  WHERE chat_thread_id IS NOT NULL;
-- At most one active assignment per task.
CREATE UNIQUE INDEX ux_task_assignments_active_task
  ON task_assignments (task_id)
  WHERE assignment_status IN ('assigned', 'in_progress');
CREATE INDEX idx_task_assignments_task_id
  ON task_assignments (task_id);
CREATE INDEX idx_task_assignments_performer_status_updated
  ON task_assignments (performer_id, assignment_status, updated_at DESC);
CREATE INDEX idx_task_assignments_customer_status_updated
  ON task_assignments (customer_id, assignment_status, updated_at DESC);

CREATE TABLE task_assignment_events (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id  UUID NOT NULL REFERENCES task_assignments(id) ON DELETE CASCADE,
  task_id        UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  event_type     TEXT NOT NULL,
  from_value     TEXT NULL,
  to_value       TEXT NOT NULL,
  changed_by     UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_task_assignment_events_event_type
    CHECK (event_type IN ('assignment_status', 'execution_stage',
                          'route_visibility', 'chat_bound'))
);
CREATE INDEX idx_task_assignment_events_assignment_created
  ON task_assignment_events (assignment_id, created_at DESC);


-- =============================================================================
-- 9. TASK ROUTE POINTS & STATUS EVENTS
-- =============================================================================

CREATE TABLE task_route_points (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  point_order   INTEGER NOT NULL,
  title         TEXT NULL,
  address_text  TEXT NULL,
  point         geography(Point, 4326) NOT NULL,
  point_kind    TEXT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_task_route_points_order
    CHECK (point_order >= 0),
  CONSTRAINT chk_task_route_points_kind
    CHECK (point_kind IS NULL OR point_kind IN ('pickup', 'delivery', 'help',
                                                 'waypoint', 'start', 'end',
                                                 'source', 'destination'))
);
CREATE INDEX idx_task_route_points_task_order
  ON task_route_points (task_id, point_order);
CREATE INDEX idx_task_route_points_point_gist
  ON task_route_points USING GIST (point);

CREATE TABLE task_status_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  from_status  TEXT NULL,
  to_status    TEXT NOT NULL,
  changed_by   UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  reason       TEXT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_task_status_events_task_created
  ON task_status_events (task_id, created_at DESC);


-- =============================================================================
-- 10. CHAT (threads, participants, messages, reads)
-- =============================================================================

CREATE TABLE chat_threads (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind             TEXT NOT NULL,
  task_id          UUID NULL REFERENCES tasks(id) ON DELETE CASCADE,
  offer_id         UUID NULL REFERENCES task_offers(id) ON DELETE CASCADE,
  assignment_id    UUID NULL REFERENCES task_assignments(id) ON DELETE CASCADE,
  archived_at      TIMESTAMPTZ NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_message_at  TIMESTAMPTZ NULL,
  CONSTRAINT chk_chat_threads_kind
    CHECK (kind IN ('offer', 'support', 'task', 'system', 'assignment'))
);
CREATE INDEX idx_chat_threads_last_message_at
  ON chat_threads (last_message_at DESC NULLS LAST);
CREATE INDEX idx_chat_threads_offer_id
  ON chat_threads (offer_id) WHERE offer_id IS NOT NULL;
CREATE INDEX idx_chat_threads_assignment_id
  ON chat_threads (assignment_id) WHERE assignment_id IS NOT NULL;
CREATE INDEX idx_chat_threads_task_id
  ON chat_threads (task_id) WHERE task_id IS NOT NULL;

-- Now that chat_threads exists: add FK from task_offers.chat_thread_id
ALTER TABLE task_offers
  ADD CONSTRAINT fk_task_offers_chat_thread_id
  FOREIGN KEY (chat_thread_id)
  REFERENCES chat_threads(id)
  ON DELETE SET NULL;

-- ...and from task_assignments.chat_thread_id
ALTER TABLE task_assignments
  ADD CONSTRAINT fk_task_assignments_chat_thread_id
  FOREIGN KEY (chat_thread_id)
  REFERENCES chat_threads(id)
  ON DELETE SET NULL;

CREATE TABLE chat_participants (
  thread_id             UUID NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
  user_id               UUID NOT NULL,  -- no FK: could be admin_id too (polymorphic)
  role                  TEXT NOT NULL,
  joined_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  left_at               TIMESTAMPTZ NULL,
  last_read_message_id  UUID NULL,
  PRIMARY KEY (thread_id, user_id),
  CONSTRAINT chk_chat_participants_role
    CHECK (role IN ('owner', 'performer', 'customer', 'support', 'admin', 'user'))
);
CREATE INDEX idx_chat_participants_user_id
  ON chat_participants (user_id);
CREATE INDEX idx_chat_participants_active_user_thread
  ON chat_participants (user_id, thread_id)
  WHERE left_at IS NULL;

CREATE TABLE chat_messages (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id                 UUID NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
  sender_id                 UUID NULL,  -- legacy; may equal user_id or admin_id
  type                      TEXT NOT NULL DEFAULT 'text',
  text                      TEXT NOT NULL,
  is_blocked                BOOLEAN NOT NULL DEFAULT FALSE,
  blocked_reason            TEXT NULL,
  sender_type               TEXT NULL,
  sender_user_account_id    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  sender_admin_account_id   UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  sender_display_name       TEXT NULL,
  sender_label              TEXT NULL,
  metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  edited_at                 TIMESTAMPTZ NULL,
  deleted_at                TIMESTAMPTZ NULL,
  CONSTRAINT chk_chat_messages_type
    CHECK (type IN ('text', 'system')),
  CONSTRAINT chk_chat_messages_text_len
    CHECK (char_length(text) <= 10000),
  CONSTRAINT chk_chat_messages_sender_type
    CHECK (sender_type IS NULL OR sender_type IN ('user', 'admin', 'system')),
  -- Enforce: sender_type matches which *_account_id is set.
  CONSTRAINT chk_chat_messages_sender_identity
    CHECK (
      sender_type IS NULL
      OR (sender_type = 'user'   AND sender_user_account_id IS NOT NULL AND sender_admin_account_id IS NULL)
      OR (sender_type = 'admin'  AND sender_admin_account_id IS NOT NULL AND sender_user_account_id IS NULL)
      OR (sender_type = 'system' AND sender_user_account_id IS NULL AND sender_admin_account_id IS NULL)
    )
);
CREATE INDEX idx_chat_messages_thread_created_at
  ON chat_messages (thread_id, created_at DESC);
CREATE INDEX idx_chat_messages_sender_identity
  ON chat_messages (sender_type, sender_user_account_id, sender_admin_account_id, created_at DESC);

-- Back-reference: chat_participants.last_read_message_id → chat_messages.id
ALTER TABLE chat_participants
  ADD CONSTRAINT fk_chat_participants_last_read_msg
  FOREIGN KEY (last_read_message_id)
  REFERENCES chat_messages(id)
  ON DELETE SET NULL;

CREATE TABLE message_reads (
  message_id  UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL,
  read_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (message_id, user_id)
);


-- =============================================================================
-- 11. SUPPORT THREADS
-- =============================================================================

CREATE TABLE support_threads (
  id                         UUID PRIMARY KEY REFERENCES chat_threads(id) ON DELETE CASCADE,
  user_account_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  assigned_admin_account_id  UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  status                     TEXT NOT NULL DEFAULT 'open',
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at                  TIMESTAMPTZ NULL,
  CONSTRAINT chk_support_threads_status
    CHECK (status IN ('open', 'pending', 'closed'))
);
CREATE INDEX idx_support_threads_user_status_updated
  ON support_threads (user_account_id, status, updated_at DESC);
CREATE INDEX idx_support_threads_assigned_admin_updated
  ON support_threads (assigned_admin_account_id, updated_at DESC)
  WHERE assigned_admin_account_id IS NOT NULL;

CREATE TABLE support_thread_admin_reads (
  thread_id             UUID NOT NULL REFERENCES support_threads(id) ON DELETE CASCADE,
  admin_account_id      UUID NOT NULL REFERENCES admin_accounts(id) ON DELETE CASCADE,
  last_read_message_id  UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
  joined_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (thread_id, admin_account_id)
);
CREATE INDEX idx_support_thread_admin_reads_admin_updated
  ON support_thread_admin_reads (admin_account_id, updated_at DESC);


-- =============================================================================
-- 12. REVIEWS
-- =============================================================================
-- Legacy TEXT-keyed table; one review per (task, from_user) pair.

-- task_id stays TEXT: it references either a v2 tasks(id) (uuid string) or a
-- legacy announcements(id) (arbitrary text). Keeping it loose avoids FK churn
-- during the v1/v2 dual-write period.
CREATE TABLE reviews (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NULL,
  from_user_id  UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  to_user_id    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  stars         INTEGER NULL,
  text          TEXT NULL,
  author_role   TEXT NULL,
  target_role   TEXT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_reviews_stars_range
    CHECK (stars IS NULL OR (stars >= 1 AND stars <= 5)),
  CONSTRAINT chk_reviews_text_len
    CHECK (text IS NULL OR char_length(text) <= 2000),
  CONSTRAINT chk_reviews_author_role
    CHECK (author_role IS NULL OR author_role IN ('customer', 'performer')),
  CONSTRAINT chk_reviews_target_role
    CHECK (target_role IS NULL OR target_role IN ('customer', 'performer'))
);
CREATE INDEX idx_reviews_to_user_created_at
  ON reviews (to_user_id, created_at DESC);
CREATE INDEX idx_reviews_from_user_created_at
  ON reviews (from_user_id, created_at DESC);
CREATE INDEX idx_reviews_to_user_target_role_created_at
  ON reviews (to_user_id, target_role, created_at DESC);
CREATE UNIQUE INDEX ux_reviews_task_from_user
  ON reviews (task_id, from_user_id)
  WHERE task_id IS NOT NULL AND from_user_id IS NOT NULL;


-- =============================================================================
-- 13. REPORTS & MODERATION
-- =============================================================================

CREATE TABLE reports (
  id                              TEXT PRIMARY KEY,
  reporter_id                     TEXT NOT NULL,
  target_type                     TEXT NOT NULL,
  target_id                       TEXT NOT NULL,
  reason_code                     TEXT NOT NULL,
  reason_text                     TEXT NULL,
  status                          TEXT NOT NULL DEFAULT 'open',
  resolution                      TEXT NULL,
  resolved_by                     TEXT NULL,
  resolved_by_admin_account_id    UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  moderator_comment               TEXT NULL,
  meta                            JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at                     TIMESTAMPTZ NULL,
  CONSTRAINT chk_reports_target_type
    CHECK (target_type IN ('user', 'task', 'message', 'announcement', 'offer')),
  CONSTRAINT chk_reports_status
    CHECK (status IN ('open', 'resolved', 'rejected', 'in_review')),
  CONSTRAINT chk_reports_resolution
    CHECK (resolution IS NULL OR resolution IN (
      'no_action', 'warning', 'mute_chat', 'restrict_posting',
      'restrict_offers', 'temporary_ban', 'permanent_ban',
      'custom_restriction', 'report_rejected'
    )),
  CONSTRAINT chk_reports_reason_text_len
    CHECK (reason_text IS NULL OR char_length(reason_text) <= 2000)
);
CREATE INDEX idx_reports_target
  ON reports (target_type, target_id);
CREATE INDEX idx_reports_status_created_at
  ON reports (status, created_at DESC);
CREATE INDEX idx_reports_resolved_created_at
  ON reports (resolved_at, created_at DESC);

-- Legacy moderation actions — backfilled into audit_logs by bootstrap.
CREATE TABLE moderation_actions (
  id            TEXT PRIMARY KEY,
  moderator_id  TEXT NOT NULL,
  action_type   TEXT NOT NULL,
  target_type   TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  reason        TEXT NULL,
  payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_moderation_actions_target
  ON moderation_actions (target_type, target_id);
CREATE INDEX idx_moderation_actions_created_at
  ON moderation_actions (created_at DESC);

CREATE TABLE user_restrictions (
  id                              TEXT PRIMARY KEY,
  user_id                         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type                            TEXT NOT NULL,
  status                          TEXT NOT NULL,
  issued_by                       UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  issued_by_admin_account_id      UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  reason_text                     TEXT NULL,
  source_type                     TEXT NULL,
  source_id                       UUID NULL,
  starts_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
  ends_at                         TIMESTAMPTZ NULL,
  revoked_at                      TIMESTAMPTZ NULL,
  revoked_by                      UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  revoked_by_admin_account_id     UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  revocation_reason               TEXT NULL,
  meta                            JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_user_restrictions_type
    CHECK (type IN ('warning', 'mute_chat', 'restrict_posting', 'restrict_offers',
                    'temporary_ban', 'permanent_ban', 'custom', 'shadowban')),
  CONSTRAINT chk_user_restrictions_status
    CHECK (status IN ('active', 'revoked', 'expired')),
  CONSTRAINT chk_user_restrictions_source_type
    CHECK (source_type IS NULL OR source_type IN ('manual', 'report', 'moderation')),
  CONSTRAINT chk_user_restrictions_reason_text_len
    CHECK (reason_text IS NULL OR char_length(reason_text) <= 2000),
  CONSTRAINT chk_user_restrictions_revocation_reason_len
    CHECK (revocation_reason IS NULL OR char_length(revocation_reason) <= 2000),
  CONSTRAINT chk_user_restrictions_time_order
    CHECK (
      (ends_at IS NULL OR ends_at >= starts_at)
      AND (revoked_at IS NULL OR revoked_at >= starts_at)
    )
);
CREATE INDEX idx_user_restrictions_user_status
  ON user_restrictions (user_id, status);
CREATE INDEX idx_user_restrictions_user_revoked_at
  ON user_restrictions (user_id, revoked_at);
CREATE INDEX idx_user_restrictions_source
  ON user_restrictions (source_type, source_id);
CREATE INDEX idx_user_restrictions_status_starts_at
  ON user_restrictions (status, starts_at DESC);


-- =============================================================================
-- 14. NOTIFICATIONS
-- =============================================================================

CREATE TABLE notifications (
  id          TEXT PRIMARY KEY,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type        TEXT NOT NULL,
  body        TEXT NOT NULL,
  payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_read     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  read_at     TIMESTAMPTZ NULL,
  CONSTRAINT chk_notifications_type
    CHECK (type IN ('chat', 'chat_system', 'task', 'offer', 'support',
                    'system', 'review', 'review_received', 'offer_accepted',
                    'offer_rejected', 'moderation', 'report')),
  CONSTRAINT chk_notifications_body_len
    CHECK (char_length(body) <= 2000)
);
CREATE INDEX idx_notifications_user_created_at
  ON notifications (user_id, created_at DESC);
CREATE INDEX idx_notifications_user_unread
  ON notifications (user_id, is_read, created_at DESC)
  WHERE is_read = FALSE;


-- =============================================================================
-- 15. AUDIT LOGS (append-only action log)
-- =============================================================================
-- Convention: INSERT only. In prod, create a DB role with only INSERT grants
-- on this table, and use that role for audit writes.

CREATE TABLE audit_logs (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_type               TEXT NOT NULL,
  actor_user_account_id    UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  actor_admin_account_id   UUID NULL REFERENCES admin_accounts(id) ON DELETE SET NULL,
  action                   TEXT NOT NULL,
  target_type              TEXT NOT NULL,
  target_id                TEXT NOT NULL,
  result                   TEXT NOT NULL DEFAULT 'success',
  details                  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_audit_logs_actor_type
    CHECK (actor_type IN ('user', 'admin', 'system')),
  CONSTRAINT chk_audit_logs_result
    CHECK (result IN ('success', 'failure', 'denied')),
  CONSTRAINT chk_audit_logs_actor_identity
    CHECK (
         (actor_type = 'user'   AND actor_user_account_id IS NOT NULL AND actor_admin_account_id IS NULL)
      OR (actor_type = 'admin'  AND actor_admin_account_id IS NOT NULL AND actor_user_account_id IS NULL)
      OR (actor_type = 'system' AND actor_user_account_id IS NULL AND actor_admin_account_id IS NULL)
    )
);
CREATE INDEX idx_audit_logs_actor_created_at
  ON audit_logs (actor_type, actor_admin_account_id, actor_user_account_id, created_at DESC);
CREATE INDEX idx_audit_logs_target_created_at
  ON audit_logs (target_type, target_id, created_at DESC);
CREATE INDEX idx_audit_logs_action_created_at
  ON audit_logs (action, created_at DESC);


-- =============================================================================
-- 16. SEED DATA
-- =============================================================================

INSERT INTO categories (slug, name, sort_order) VALUES
  ('errands',  'Помощь и поручения', 10),
  ('delivery', 'Доставка',           20),
  ('shopping', 'Покупки',            30),
  ('help',     'Помощь',             40)
ON CONFLICT (slug) DO NOTHING;


-- =============================================================================
-- Schema version marker (for future migrations)
-- =============================================================================

CREATE TABLE schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version) VALUES ('v2-initial')
ON CONFLICT (version) DO NOTHING;
