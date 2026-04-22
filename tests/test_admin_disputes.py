from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.chat import _offer_thread_kind_value, ensure_chat_participant
from app.db import execute
from app.main import app as public_app
from app.security import hash_password
from services.admin_panel.app.main import app as admin_app


class AdminDisputesIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.public_client = TestClient(public_app)
        cls.admin_client = TestClient(admin_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.admin_ids: list[str] = []
        self.thread_ids: list[str] = []
        self.dispute_ids: list[str] = []

    def tearDown(self) -> None:
        for dispute_id in self.dispute_ids:
            execute("DELETE FROM dispute_events WHERE dispute_id = %s::uuid", (dispute_id,))
            execute("DELETE FROM disputes WHERE id = %s::uuid", (dispute_id,))

        for thread_id in self.thread_ids:
            execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM chat_messages WHERE thread_id = %s::uuid)", (thread_id,))
            execute("UPDATE chat_participants SET last_read_message_id = NULL WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_participants WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_messages WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_threads WHERE id = %s::uuid", (thread_id,))

        for admin_id in self.admin_ids:
            execute("DELETE FROM admin_sessions WHERE admin_account_id = %s::uuid", (admin_id,))
            execute("DELETE FROM audit_logs WHERE actor_admin_account_id = %s::uuid", (admin_id,))
            execute("DELETE FROM moderation_actions WHERE moderator_id = %s", (admin_id,))
            execute("DELETE FROM admin_accounts WHERE id = %s::uuid", (admin_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))

    def _create_user(self, prefix: str) -> dict[str, str]:
        user_id = str(uuid.uuid4())
        email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
        password = "UserPass123!"
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
                display_name or "Admin Staff",
                linked_user_id,
            ),
        )
        self.admin_ids.append(admin_id)
        return {
            "id": admin_id,
            "login_identifier": login_identifier,
            "password": password,
            "role": role,
        }

    def _login_user(self, user: dict[str, str]) -> str:
        response = self.public_client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _login_admin(self, admin: dict[str, str]) -> str:
        response = self.admin_client.post(
            "/admin/api/auth/login",
            json={"login_identifier": admin["login_identifier"], "password": admin["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["admin_account_id"], admin["id"])
        return payload["access_token"]

    def _create_direct_thread(self, owner_id: str, performer_id: str) -> str:
        thread_id = str(uuid.uuid4())
        execute(
            """
            INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at, assignment_id)
            VALUES (%s::uuid, %s, NULL, NULL, NULL, NULL)
            """,
            (thread_id, _offer_thread_kind_value()),
        )
        ensure_chat_participant(thread_id, owner_id, "owner")
        ensure_chat_participant(thread_id, performer_id, "performer")
        self.thread_ids.append(thread_id)
        return thread_id

    def test_admin_can_join_awaiting_moderator_dispute_and_reply_in_chat(self) -> None:
        owner = self._create_user("dispute-owner")
        performer = self._create_user("dispute-performer")
        staff_user = self._create_user("dispute-staff-user")
        admin_account = self._create_admin_account(linked_user_id=staff_user["id"], prefix="dispute-admin")

        owner_token = self._login_user(owner)
        admin_token = self._login_admin(admin_account)

        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        open_response = self.public_client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Спор по заказу",
                "problem_description": "Не сошлись по результату",
                "requested_compensation_rub": 1200,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_id = open_response.json()["id"]
        self.dispute_ids.append(dispute_id)

        execute(
            """
            UPDATE disputes
            SET status = 'awaiting_moderator',
                moderator_hook = '{"status":"pending","reason":"round2_mismatch"}'::jsonb,
                resolution_summary = 'После второго раунда стороны не договорились',
                closed_at = now(),
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (dispute_id,),
        )

        disputes_response = self.admin_client.get(
            "/admin/api/disputes",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(disputes_response.status_code, 200, disputes_response.text)
        disputes_payload = disputes_response.json()
        items = disputes_payload["items"]
        self.assertTrue(any(item["id"] == dispute_id for item in items))
        self.assertGreaterEqual(int(disputes_payload["pending_count"]), 1)

        join_response = self.admin_client.post(
            f"/admin/api/disputes/{dispute_id}/join",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(join_response.status_code, 200, join_response.text)
        self.assertEqual(join_response.json()["moderation_state"], "in_progress")

        owner_messages_after_join = self.public_client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_messages_after_join.status_code, 200, owner_messages_after_join.text)
        self.assertTrue(
            any(
                message["sender_type"] == "system" and "подключился администратор" in message["text"].lower()
                for message in owner_messages_after_join.json()
            )
        )

        admin_message_response = self.admin_client.post(
            f"/admin/api/disputes/{dispute_id}/messages",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"text": "Подключился к спору, давайте зафиксируем финальные условия."},
        )
        self.assertEqual(admin_message_response.status_code, 200, admin_message_response.text)
        self.assertEqual(admin_message_response.json()["sender_type"], "admin")
        self.assertEqual(admin_message_response.json()["sender_admin_account_id"], admin_account["id"])

        owner_messages_after_reply = self.public_client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_messages_after_reply.status_code, 200, owner_messages_after_reply.text)
        self.assertTrue(
            any(
                message["sender_type"] == "admin"
                and message.get("sender_admin_account_id") == admin_account["id"]
                and "финальные условия" in message["text"].lower()
                for message in owner_messages_after_reply.json()
            )
        )


if __name__ == "__main__":
    unittest.main()
