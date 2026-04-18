from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.db import execute, fetch_one
from app.main import _insert_task, app as public_app
from app.ops import create_notification, create_report
from app.security import hash_password
from app.support import ensure_support_participant, get_or_create_support_thread
from services.admin_panel.app import crud as admin_crud
from services.admin_panel.app.db import SessionLocal
from services.admin_panel.app.main import app as admin_app


class AdminIdentitySeparationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.public_client = TestClient(public_app)
        cls.admin_client = TestClient(admin_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.admin_ids: list[str] = []
        self.thread_ids: list[str] = []
        self.task_ids: list[str] = []
        self.report_ids: list[str] = []

    def tearDown(self) -> None:
        for thread_id in self.thread_ids:
            execute("DELETE FROM support_thread_admin_reads WHERE thread_id = %s::uuid", (thread_id,))
            execute("UPDATE chat_participants SET last_read_message_id = NULL WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM chat_messages WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM chat_participants WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_messages WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM support_threads WHERE id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_threads WHERE id = %s::uuid", (thread_id,))
            execute("DELETE FROM audit_logs WHERE target_type = 'support_thread' AND target_id = %s", (thread_id,))
            execute("DELETE FROM moderation_actions WHERE target_type = 'support_thread' AND target_id = %s", (thread_id,))

        for task_id in self.task_ids:
            execute("DELETE FROM task_assignment_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_status_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_route_points WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_assignments WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_offers WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM reports WHERE target_id = %s", (task_id,))
            execute("DELETE FROM announcements WHERE id::text = %s", (task_id,))
            execute("DELETE FROM audit_logs WHERE target_id = %s", (task_id,))
            execute("DELETE FROM moderation_actions WHERE target_id = %s", (task_id,))
            execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))

        for report_id in self.report_ids:
            execute("DELETE FROM audit_logs WHERE target_type = 'report' AND target_id = %s", (report_id,))
            execute("DELETE FROM reports WHERE id = %s", (report_id,))

        for admin_id in self.admin_ids:
            execute("DELETE FROM admin_sessions WHERE admin_account_id = %s::uuid", (admin_id,))
            execute("DELETE FROM audit_logs WHERE actor_admin_account_id = %s::uuid OR target_id = %s", (admin_id, admin_id))
            execute("DELETE FROM moderation_actions WHERE target_id = %s", (admin_id,))
            execute("DELETE FROM admin_accounts WHERE id = %s::uuid", (admin_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM audit_logs WHERE actor_user_account_id = %s::uuid OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM moderation_actions WHERE moderator_id = %s OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))

    def _create_user(self, prefix: str) -> dict[str, str]:
        user_id = str(uuid.uuid4())
        password = "UserPass123!"
        email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
        execute(
            """
            INSERT INTO users (
                id,
                email,
                password_hash,
                role,
                is_email_verified,
                is_phone_verified,
                created_at,
                updated_at
            )
            VALUES (%s::uuid, %s, %s, 'user', TRUE, FALSE, now(), now())
            """,
            (user_id, email, hash_password(password)),
        )
        self.user_ids.append(user_id)
        return {"id": user_id, "email": email, "password": password}

    def _create_admin_account(
        self,
        *,
        linked_user_id: str,
        prefix: str,
        role: str = "admin",
        display_name: str | None = None,
    ) -> dict[str, str]:
        admin_id = str(uuid.uuid4())
        login_identifier = f"{prefix}-{uuid.uuid4().hex[:8]}"
        password = "AdminPass123!"
        email = f"{login_identifier}@example.com"
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
            VALUES (%s::uuid, %s, %s, %s, %s, 'active', %s, %s::uuid, now(), now(), FALSE)
            """,
            (
                admin_id,
                login_identifier,
                email,
                hash_password(password),
                role,
                display_name or prefix.title(),
                linked_user_id,
            ),
        )
        self.admin_ids.append(admin_id)
        return {
            "id": admin_id,
            "login_identifier": login_identifier,
            "email": email,
            "password": password,
            "role": role,
        }

    def _login_user(self, user: dict[str, str]) -> str:
        response = self.public_client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["principal_type"], "user")
        return payload["access_token"]

    def _login_admin(self, admin: dict[str, str]) -> str:
        response = self.admin_client.post(
            "/admin/api/auth/login",
            json={"login_identifier": admin["login_identifier"], "password": admin["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["principal_type"], "admin")
        self.assertEqual(payload["admin_account_id"], admin["id"])
        return payload["access_token"]

    def _create_support_thread(self, user_token: str) -> str:
        response = self.public_client.get(
            "/support/thread",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        thread_id = response.json()["thread_id"]
        self.thread_ids.append(thread_id)
        return thread_id

    def test_user_and_admin_login_are_separate(self) -> None:
        user = self._create_user("dual-login")
        admin = self._create_admin_account(linked_user_id=user["id"], prefix="dual-admin", role="admin")

        user_response = self.public_client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        admin_response = self.admin_client.post(
            "/admin/api/auth/login",
            json={"login_identifier": admin["login_identifier"], "password": admin["password"]},
        )

        self.assertEqual(user_response.status_code, 200, user_response.text)
        self.assertEqual(admin_response.status_code, 200, admin_response.text)
        self.assertEqual(user_response.json()["principal_type"], "user")
        self.assertEqual(admin_response.json()["principal_type"], "admin")
        self.assertNotEqual(user["id"], admin_response.json()["admin_account_id"])

    def test_user_token_cannot_access_admin_endpoints_and_admin_token_cannot_access_user_endpoints(self) -> None:
        user = self._create_user("separate-auth")
        admin_user = self._create_user("staff-owner")
        admin = self._create_admin_account(linked_user_id=admin_user["id"], prefix="staff-auth", role="admin")

        user_token = self._login_user(user)
        admin_token = self._login_admin(admin)

        user_to_admin = self.admin_client.get(
            "/admin/api/auth/me",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(user_to_admin.status_code, 401, user_to_admin.text)

        admin_to_user = self.public_client.get(
            "/support/thread",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(admin_to_user.status_code, 401, admin_to_user.text)

        admin_message_with_user_token = self.admin_client.post(
            "/admin/api/support/threads/non-existent/messages",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"text": "forged"},
        )
        self.assertEqual(admin_message_with_user_token.status_code, 401, admin_message_with_user_token.text)

    def test_support_messages_keep_explicit_sender_identity_and_ignore_forged_sender_type(self) -> None:
        owner = self._create_user("support-owner")
        staff_user = self._create_user("support-staff-user")
        staff_admin = self._create_admin_account(linked_user_id=staff_user["id"], prefix="support-staff", role="support")

        user_token = self._login_user(owner)
        admin_token = self._login_admin(staff_admin)
        thread_id = self._create_support_thread(user_token)

        user_message = self.public_client.post(
            f"/support/thread/{thread_id}/messages",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"text": "help please", "sender_type": "admin"},
        )
        self.assertEqual(user_message.status_code, 201, user_message.text)
        self.assertEqual(user_message.json()["sender_type"], "user")
        self.assertEqual(user_message.json()["sender_user_account_id"], owner["id"])
        self.assertIsNone(user_message.json()["sender_admin_account_id"])

        admin_message = self.admin_client.post(
            f"/admin/api/support/threads/{thread_id}/messages",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"text": "support reply"},
        )
        self.assertEqual(admin_message.status_code, 200, admin_message.text)
        self.assertEqual(admin_message.json()["sender_type"], "admin")
        self.assertEqual(admin_message.json()["sender_admin_account_id"], staff_admin["id"])
        self.assertIsNone(admin_message.json()["sender_user_account_id"])

        messages = self.public_client.get(
            f"/support/thread/{thread_id}/messages",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(messages.status_code, 200, messages.text)
        last_message = messages.json()[-1]
        self.assertEqual(last_message["sender_type"], "admin")
        self.assertEqual(last_message["sender_admin_account_id"], staff_admin["id"])
        self.assertNotEqual(last_message["sender_id"], owner["id"])

        audit_row = fetch_one(
            """
            SELECT 1
            FROM audit_logs
            WHERE actor_admin_account_id = %s::uuid
              AND action = 'support_message_sent'
              AND target_type = 'support_thread'
              AND target_id = %s
            LIMIT 1
            """,
            (staff_admin["id"], thread_id),
        )
        self.assertIsNotNone(audit_row)

    def test_linked_admin_cannot_reply_to_own_support_thread(self) -> None:
        owner = self._create_user("self-thread")
        owner_admin = self._create_admin_account(linked_user_id=owner["id"], prefix="self-admin", role="admin")

        user_token = self._login_user(owner)
        admin_token = self._login_admin(owner_admin)
        thread_id = self._create_support_thread(user_token)

        user_message = self.public_client.post(
            f"/support/thread/{thread_id}/messages",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"text": "my own ticket"},
        )
        self.assertEqual(user_message.status_code, 201, user_message.text)
        self.assertEqual(user_message.json()["sender_type"], "user")

        blocked = self.admin_client.post(
            f"/admin/api/support/threads/{thread_id}/messages",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"text": "I am replying as myself"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

    def test_admin_access_creation_endpoint_creates_separate_admin_principal(self) -> None:
        manager_user = self._create_user("manager-user")
        manager_admin = self._create_admin_account(linked_user_id=manager_user["id"], prefix="manager-admin", role="admin")
        regular_user = self._create_user("regular-user")

        admin_token = self._login_admin(manager_admin)
        login_identifier = f"created-admin-{uuid.uuid4().hex[:8]}"
        password = "SeparateAdmin123!"

        create_response = self.admin_client.post(
            f"/admin/api/users/{regular_user['id']}/admin-access",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "login_identifier": login_identifier,
                "display_name": "Created Admin",
                "role": "support",
                "password": password,
                "email": regular_user["email"],
            },
        )
        self.assertEqual(create_response.status_code, 200, create_response.text)
        payload = create_response.json()
        new_admin_id = payload["admin_account_id"]
        self.admin_ids.append(new_admin_id)
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["linked_user_account_id"], regular_user["id"])
        self.assertNotEqual(new_admin_id, regular_user["id"])

        admin_login = self.admin_client.post(
            "/admin/api/auth/login",
            json={"login_identifier": login_identifier, "password": password},
        )
        self.assertEqual(admin_login.status_code, 200, admin_login.text)
        self.assertEqual(admin_login.json()["principal_type"], "admin")
        self.assertEqual(admin_login.json()["admin_account_id"], new_admin_id)

        audit_row = fetch_one(
            """
            SELECT 1
            FROM audit_logs
            WHERE action = 'admin_access_granted'
              AND target_type = 'user'
              AND target_id = %s
            LIMIT 1
            """,
            (regular_user["id"],),
        )
        self.assertIsNotNone(audit_row)

    def test_migrations_create_required_identity_tables_and_constraints(self) -> None:
        ensure_all_tables()

        for table_name in ("admin_accounts", "admin_sessions", "support_threads", "support_thread_admin_reads", "audit_logs"):
            row = fetch_one(f"SELECT to_regclass('public.{table_name}')")
            self.assertIsNotNone(row)
            self.assertEqual(row[0], table_name)

        for constraint_name in ("chk_chat_messages_sender_identity", "chk_audit_logs_actor_identity"):
            row = fetch_one(
                "SELECT 1 FROM pg_constraint WHERE conname = %s LIMIT 1",
                (constraint_name,),
            )
            self.assertIsNotNone(row)

    def test_runtime_schema_repairs_allow_current_backend_values(self) -> None:
        ensure_all_tables()
        expectations = {
            "chk_chat_participants_role": "'user'",
            "chk_notifications_type": "'review_received'",
            "chk_user_restrictions_type": "'shadowban'",
        }
        for constraint_name, expected_fragment in expectations.items():
            row = fetch_one(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = %s
                LIMIT 1
                """,
                (constraint_name,),
            )
            self.assertIsNotNone(row, constraint_name)
            self.assertIn(expected_fragment, str(row[0]), constraint_name)

    def test_existing_support_messages_are_migrated_to_explicit_sender_identity(self) -> None:
        ensure_all_tables()
        invalid_row = fetch_one(
            """
            SELECT m.id::text
            FROM chat_messages m
            JOIN support_threads st
              ON st.id = m.thread_id
            WHERE m.deleted_at IS NULL
              AND NOT (
                    (m.sender_type = 'user' AND m.sender_user_account_id IS NOT NULL AND m.sender_admin_account_id IS NULL)
                 OR (m.sender_type = 'admin' AND m.sender_user_account_id IS NULL AND m.sender_admin_account_id IS NOT NULL)
                 OR (m.sender_type = 'system' AND m.sender_user_account_id IS NULL AND m.sender_admin_account_id IS NULL)
              )
            LIMIT 1
            """
        )
        self.assertIsNone(invalid_row)

    def test_announcement_moderation_decision_updates_legacy_projection_without_sql_errors(self) -> None:
        owner = self._create_user("moderation-owner")
        staff_user = self._create_user("moderation-staff")
        staff_admin = self._create_admin_account(linked_user_id=staff_user["id"], prefix="moderation-admin", role="moderator")
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        _insert_task(
            task_id,
            owner["id"],
            "help",
            "Тестовая задача",
            "pending_review",
            {
                "address": "Москва, Тверская 1",
                "address_text": "Москва, Тверская 1",
                "point": {"lat": 55.7558, "lon": 37.6173},
                "generated_description": "Нужно помочь с документами",
                "notes": "Принести договор",
            },
        )

        with SessionLocal() as session:
            updated = admin_crud.apply_announcement_decision(
                session=session,
                ann_id=task_id,
                moderator_id=staff_user["id"],
                decision="approve",
                message="Одобрено",
                reasons=[],
                suggestions=[],
            )

        self.assertEqual(updated["status"], "active")
        legacy_row = fetch_one(
            """
            SELECT status, data->>'generated_description'
            FROM announcements
            WHERE id::text = %s
            """,
            (task_id,),
        )
        self.assertIsNotNone(legacy_row)
        self.assertEqual(legacy_row[0], "active")
        self.assertEqual(legacy_row[1], "Нужно помочь с документами")

    def test_support_thread_endpoint_reuses_single_canonical_thread(self) -> None:
        owner = self._create_user("support-dedupe")
        user_token = self._login_user(owner)
        stale_thread_id = str(uuid.uuid4())
        self.thread_ids.append(stale_thread_id)

        execute(
            """
            INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at)
            VALUES (%s::uuid, 'support', NULL, NULL, now() - interval '2 hours')
            """,
            (stale_thread_id,),
        )
        ensure_support_participant(stale_thread_id, owner["id"], "user")

        first_thread_id = get_or_create_support_thread(owner["id"])
        second_thread_id = get_or_create_support_thread(owner["id"])
        if first_thread_id not in self.thread_ids:
            self.thread_ids.append(first_thread_id)

        self.assertEqual(first_thread_id, stale_thread_id)
        self.assertEqual(second_thread_id, stale_thread_id)

        support_row = fetch_one(
            """
            SELECT COUNT(*)
            FROM support_threads
            WHERE user_account_id = %s::uuid
              AND closed_at IS NULL
            """,
            (owner["id"],),
        )
        self.assertEqual(int(support_row[0] or 0), 1)

        response = self.public_client.get(
            "/chats",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        support_threads = [item for item in response.json() if item["kind"] == "support"]
        self.assertEqual(len(support_threads), 1)
        self.assertEqual(support_threads[0]["thread_id"], stale_thread_id)

    def test_notifications_accept_runtime_types_and_truncate_long_body(self) -> None:
        owner = self._create_user("notif-owner")
        runtime_types = [
            "chat",
            "chat_system",
            "task",
            "offer",
            "support",
            "system",
            "review",
            "review_received",
            "offer_accepted",
            "offer_rejected",
            "moderation",
            "report",
        ]
        body = "x" * 2500
        created_ids = []
        for notif_type in runtime_types:
            created_ids.append(
                create_notification(
                    user_id=owner["id"],
                    notif_type=notif_type,
                    body=body,
                    payload={"type": notif_type},
                )
            )

        row = fetch_one(
            """
            SELECT COUNT(*), MAX(char_length(body))
            FROM notifications
            WHERE user_id = %s::uuid
            """,
            (owner["id"],),
        )
        self.assertEqual(int(row[0] or 0), len(runtime_types))
        self.assertLessEqual(int(row[1] or 0), 2000)

        stored = fetch_one(
            """
            SELECT body
            FROM notifications
            WHERE id = %s
            """,
            (created_ids[0],),
        )
        self.assertIsNotNone(stored)
        self.assertTrue(str(stored[0]).endswith("..."))

    def test_report_resolution_creates_canonical_restriction_and_notifications(self) -> None:
        reporter = self._create_user("reporter")
        target = self._create_user("reported")
        staff_user = self._create_user("report-staff")
        self._create_admin_account(linked_user_id=staff_user["id"], prefix="report-admin", role="moderator")

        report_id = create_report(
            reporter_id=reporter["id"],
            target_type="user",
            target_id=target["id"],
            reason_code="ABUSE",
            reason_text="abusive behavior",
            meta={"target_user_id": target["id"]},
        )
        self.report_ids.append(report_id)

        with SessionLocal() as session:
            resolved = admin_crud.resolve_report(
                session=session,
                report_id=report_id,
                moderator_id=staff_user["id"],
                resolution="restrict_posting",
                moderator_comment="posting restricted",
            )

        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution"], "restrict_posting")

        restriction_row = fetch_one(
            """
            SELECT type, status, source_type, source_id::text
            FROM user_restrictions
            WHERE user_id = %s::uuid
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (target["id"],),
        )
        self.assertIsNotNone(restriction_row)
        self.assertEqual(restriction_row[0], "restrict_posting")
        self.assertEqual(restriction_row[1], "active")
        self.assertEqual(restriction_row[2], "report")
        self.assertEqual(restriction_row[3], report_id)

        notification_types = {
            str(row[0])
            for row in (
                fetch_one(
                    """
                    SELECT type
                    FROM notifications
                    WHERE user_id = %s::uuid
                      AND type = 'report'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (reporter["id"],),
                ),
                fetch_one(
                    """
                    SELECT type
                    FROM notifications
                    WHERE user_id = %s::uuid
                      AND type = 'moderation'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (target["id"],),
                ),
            )
            if row and row[0]
        }
        self.assertEqual(notification_types, {"report", "moderation"})

    def test_public_announcement_payload_exposes_description_and_normalizes_schedule_timezone(self) -> None:
        owner = self._create_user("announcement-public")
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        execute(
            """
            INSERT INTO user_devices (
                id,
                user_id,
                platform,
                device_id,
                push_token,
                locale,
                timezone,
                device_name,
                created_at,
                last_seen_at,
                deleted_at
            )
            VALUES (%s, %s::uuid, 'ios', %s, NULL, 'ru_RU', 'Europe/Moscow', 'iPhone', now(), now(), NULL)
            """,
            (str(uuid.uuid4()), owner["id"], f"device-{uuid.uuid4().hex[:8]}"),
        )

        _insert_task(
            task_id,
            owner["id"],
            "help",
            "Забрать документы",
            "active",
            {
                "address": "Москва, Тверская 1",
                "address_text": "Москва, Тверская 1",
                "point": {"lat": 55.7558, "lon": 37.6173},
                "notes": "Нужна аккуратная доставка документов",
                "generated_description": "Нужна аккуратная доставка документов",
                "start_at": "2026-04-07T10:00:00",
            },
        )

        token = self._login_user(owner)
        details = self.public_client.get(
            f"/announcements/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(details.status_code, 200, details.text)
        payload = details.json()
        self.assertEqual(payload["description"], "Нужна аккуратная доставка документов")
        self.assertEqual(payload["address_text"], "Москва, Тверская 1")
        self.assertEqual(payload["data"]["description"], "Нужна аккуратная доставка документов")
        self.assertEqual(payload["data"]["generated_description"], "Нужна аккуратная доставка документов")
        self.assertEqual(payload["data"]["timezone"], "Europe/Moscow")
        self.assertEqual(payload["data"]["start_at"], "2026-04-07T10:00:00+03:00")
        self.assertEqual(payload["data"]["task"]["route"]["timezone"], "Europe/Moscow")
        self.assertEqual(payload["data"]["task"]["route"]["start_at"], "2026-04-07T10:00:00+03:00")

        public_list = self.public_client.get("/announcements/public")
        self.assertEqual(public_list.status_code, 200, public_list.text)
        public_item = next(item for item in public_list.json() if item["id"] == task_id)
        self.assertEqual(public_item["description"], "Нужна аккуратная доставка документов")
        self.assertEqual(public_item["data"]["description"], "Нужна аккуратная доставка документов")


if __name__ == "__main__":
    unittest.main()
