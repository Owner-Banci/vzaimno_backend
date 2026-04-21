from __future__ import annotations

import json
import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.chat import _offer_thread_kind_value, ensure_chat_participant
from app.db import execute
from app.main import app as public_app
from app.security import hash_password


class ChatDisputeSchemaCompatIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.client = TestClient(public_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.thread_ids: list[str] = []
        self.dispute_ids: list[str] = []

    def tearDown(self) -> None:
        for dispute_id in self.dispute_ids:
            execute("DELETE FROM dispute_events WHERE dispute_id = %s::uuid", (dispute_id,))
            execute("DELETE FROM disputes WHERE id = %s::uuid", (dispute_id,))

        for thread_id in self.thread_ids:
            execute("DELETE FROM dispute_events WHERE dispute_id IN (SELECT id FROM disputes WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM disputes WHERE thread_id = %s::uuid", (thread_id,))
            execute("UPDATE chat_participants SET last_read_message_id = NULL WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM chat_messages WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM chat_participants WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_messages WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM support_threads WHERE id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_threads WHERE id = %s::uuid", (thread_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
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

    def _login_user(self, user: dict[str, str]) -> str:
        response = self.client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _create_direct_thread(self, owner_id: str, performer_id: str) -> str:
        thread_id = str(uuid.uuid4())
        kind = _offer_thread_kind_value()
        execute(
            """
            INSERT INTO chat_threads (id, kind, task_id, offer_id, last_message_at, assignment_id)
            VALUES (%s::uuid, %s, NULL, NULL, NULL, NULL)
            """,
            (thread_id, kind),
        )
        ensure_chat_participant(thread_id, owner_id, "owner")
        ensure_chat_participant(thread_id, performer_id, "performer")
        self.thread_ids.append(thread_id)
        return thread_id

    def test_chats_and_messages_endpoints_are_compatible_with_phone_removed_schema(self) -> None:
        owner = self._create_user("chat-owner")
        performer = self._create_user("chat-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        send_response = self.client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"text": "Привет из теста"},
        )
        self.assertEqual(send_response.status_code, 201, send_response.text)

        chats_response = self.client.get(
            "/chats",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(chats_response.status_code, 200, chats_response.text)
        threads = chats_response.json()
        current_thread = next(item for item in threads if item["thread_id"] == thread_id)
        self.assertTrue(current_thread["partner_display_name"])

        messages_response = self.client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(messages_response.status_code, 200, messages_response.text)
        self.assertGreaterEqual(len(messages_response.json()), 1)

    def test_chats_realtime_capabilities_endpoint(self) -> None:
        owner = self._create_user("realtime-owner")
        owner_token = self._login_user(owner)

        response = self.client.get(
            "/chats/realtime-capabilities",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("chat_websocket_enabled", payload)
        self.assertIn("websocket_path", payload)

    def test_dispute_open_active_accept_flow(self) -> None:
        owner = self._create_user("dispute-owner")
        performer = self._create_user("dispute-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        open_response = self.client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Качество услуги",
                "problem_description": "Работа выполнена частично",
                "requested_compensation_rub": 1200,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_payload = open_response.json()
        self.dispute_ids.append(dispute_payload["id"])
        self.assertEqual(dispute_payload["status"], "open_waiting_counterparty")

        owner_active = self.client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_active.status_code, 200, owner_active.text)

        performer_active = self.client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_active.status_code, 200, performer_active.text)
        self.assertEqual(performer_active.json()["viewer_side"], "counterparty")

        accept_response = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_payload['id']}/counterparty/accept",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        self.assertEqual(accept_response.json()["status"], "closed_by_acceptance")

        owner_active_after_close = self.client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_active_after_close.status_code, 200, owner_active_after_close.text)
        self.assertIsNone(owner_active_after_close.json())

    def test_dispute_respond_and_select_options_flow(self) -> None:
        owner = self._create_user("round-owner")
        performer = self._create_user("round-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        open_response = self.client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Раунд спор",
                "problem_description": "Нужно проверить flow выбора",
                "requested_compensation_rub": 1000,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_payload = open_response.json()
        dispute_id = dispute_payload["id"]
        self.dispute_ids.append(dispute_id)

        respond_response = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/counterparty/respond",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={
                "response_description": "Готов на частичный возврат",
                "acceptable_refund_percent": 40,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(respond_response.status_code, 200, respond_response.text)
        self.assertEqual(respond_response.json()["status"], "model_thinking")

        execute(
            """
            UPDATE disputes
            SET status = 'waiting_round_1_votes',
                active_round = 1,
                round1_options = %s::jsonb,
                round1_votes = '{}'::jsonb,
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (
                json.dumps(
                    [
                        {
                            "id": "opt_1",
                            "lean": "initiator_favor",
                            "title": "Опция 1",
                            "description": "Тестовая опция 1",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 700,
                            "refund_percent": 70,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "opt_2",
                            "lean": "counterparty_favor",
                            "title": "Опция 2",
                            "description": "Тестовая опция 2",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 300,
                            "refund_percent": 30,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "opt_3",
                            "lean": "compromise",
                            "title": "Опция 3",
                            "description": "Тестовая опция 3",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 500,
                            "refund_percent": 50,
                            "resolution_kind": "partial_refund",
                        },
                    ],
                    ensure_ascii=False,
                ),
                dispute_id,
            ),
        )

        active_after_seed = self.client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(active_after_seed.status_code, 200, active_after_seed.text)
        active_options = active_after_seed.json().get("options", [])
        self.assertGreaterEqual(len(active_options), 1)
        selected_option_id = active_options[0]["id"]

        owner_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"option_id": selected_option_id},
        )
        self.assertEqual(owner_select.status_code, 200, owner_select.text)

        performer_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"option_id": selected_option_id},
        )
        self.assertEqual(performer_select.status_code, 200, performer_select.text)
        self.assertEqual(performer_select.json()["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
