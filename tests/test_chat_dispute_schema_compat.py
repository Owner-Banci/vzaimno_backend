from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.chat import _offer_thread_kind_value, ensure_chat_participant, post_system_thread_message
from app.db import execute, fetch_one
from app.main import _insert_task, app as public_app
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
        self.task_ids: list[str] = []

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

        for task_id in self.task_ids:
            execute("DELETE FROM task_assignment_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_status_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_route_points WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_assignments WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_offers WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM announcements WHERE id::text = %s", (task_id,))
            execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))

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

    def _create_route_task(self, owner_id: str, title: str) -> str:
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)
        _insert_task(
            task_id,
            owner_id,
            "delivery",
            title,
            "active",
            {
                "pickup_address": "Москва, Тверская 1",
                "dropoff_address": "Москва, Арбат 10",
                "address_text": "Москва, Тверская 1",
                "pickup_point": {"lat": 55.7558, "lon": 37.6173},
                "dropoff_point": {"lat": 55.7522, "lon": 37.5931},
                "point": {"lat": 55.7558, "lon": 37.6173},
                "notes": "Маршрутный интеграционный тест",
            },
        )
        return task_id

    def _accept_route_task(self, task_id: str, owner_token: str, performer_token: str) -> None:
        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)
        offer_id = offer_response.json()["id"]

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        self.thread_ids.append(accept_response.json()["thread_id"])

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

    def test_chat_read_receipts_clear_unread_and_update_sender_status(self) -> None:
        owner = self._create_user("receipt-owner")
        performer = self._create_user("receipt-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        send_response = self.client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"text": "Проверка галочек"},
        )
        self.assertEqual(send_response.status_code, 201, send_response.text)
        performer_message_id = send_response.json()["id"]
        system_message = post_system_thread_message(thread_id, "Системное сообщение не должно висеть в счётчике")

        chats_before_read = self.client.get(
            "/chats",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(chats_before_read.status_code, 200, chats_before_read.text)
        current_thread = next(item for item in chats_before_read.json() if item["thread_id"] == thread_id)
        self.assertEqual(current_thread["unread_count"], 1)

        read_response = self.client.post(
            f"/chats/{thread_id}/messages/read",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"message_ids": [system_message["id"]]},
        )
        self.assertEqual(read_response.status_code, 200, read_response.text)

        chats_after_read = self.client.get(
            "/chats",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(chats_after_read.status_code, 200, chats_after_read.text)
        updated_thread = next(item for item in chats_after_read.json() if item["thread_id"] == thread_id)
        self.assertEqual(updated_thread["unread_count"], 0)

        performer_messages = self.client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_messages.status_code, 200, performer_messages.text)
        performer_message = next(item for item in performer_messages.json() if item["id"] == performer_message_id)
        self.assertEqual(performer_message["delivery_status"], "read")
        self.assertTrue(performer_message["is_read_by_recipient"])

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

    def test_customer_route_ignores_unaccepted_task_even_with_legacy_execution_status(self) -> None:
        owner = self._create_user("route-owner")
        performer = self._create_user("route-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        _insert_task(
            task_id,
            owner["id"],
            "delivery",
            "Забрать коробку",
            "active",
            {
                "pickup_address": "Москва, Тверская 1",
                "dropoff_address": "Москва, Арбат 10",
                "address_text": "Москва, Тверская 1",
                "pickup_point": {"lat": 55.7558, "lon": 37.6173},
                "dropoff_point": {"lat": 55.7522, "lon": 37.5931},
                "point": {"lat": 55.7558, "lon": 37.6173},
                "execution_status": "in_progress",
                "execution_status_confirmed": True,
                "notes": "Проверяем, что несогласованная задача не попадает в маршрут",
            },
        )

        owner_tasks_before_offer = self.client.get(
            "/routes/me/customer/tasks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_tasks_before_offer.status_code, 200, owner_tasks_before_offer.text)
        self.assertFalse(
            any(item["task_id"] == task_id for item in owner_tasks_before_offer.json()),
            owner_tasks_before_offer.text,
        )

        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)

        owner_tasks_after_offer = self.client.get(
            "/routes/me/customer/tasks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_tasks_after_offer.status_code, 200, owner_tasks_after_offer.text)
        self.assertFalse(
            any(item["task_id"] == task_id for item in owner_tasks_after_offer.json()),
            owner_tasks_after_offer.text,
        )

        performer_tasks_after_offer = self.client.get(
            "/routes/me/performer/tasks",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_tasks_after_offer.status_code, 200, performer_tasks_after_offer.text)
        self.assertFalse(
            any(item["task_id"] == task_id for item in performer_tasks_after_offer.json()),
            performer_tasks_after_offer.text,
        )

        current_route = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(current_route.status_code, 404, current_route.text)

        explicit_route = self.client.get(
            f"/announcements/{task_id}/route/context",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(explicit_route.status_code, 409, explicit_route.text)

        my_tasks = self.client.get(
            "/announcements/me",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(my_tasks.status_code, 200, my_tasks.text)
        task = next(item for item in my_tasks.json() if item["id"] == task_id)
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["data"]["task"]["execution"]["status"], "open")
        self.assertIsNone(task["data"]["task"]["assignment"]["performer_user_id"])

    def test_accepting_offer_exposes_same_assignment_chat_to_owner_and_performer(self) -> None:
        owner = self._create_user("accept-owner")
        performer = self._create_user("accept-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        _insert_task(
            task_id,
            owner["id"],
            "delivery",
            "Забрать документы",
            "active",
            {
                "pickup_address": "Москва, Тверская 1",
                "dropoff_address": "Москва, Арбат 10",
                "address_text": "Москва, Тверская 1",
                "pickup_point": {"lat": 55.7558, "lon": 37.6173},
                "dropoff_point": {"lat": 55.7522, "lon": 37.5931},
                "point": {"lat": 55.7558, "lon": 37.6173},
                "notes": "Нужна аккуратная доставка документов",
            },
        )

        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)
        offer_id = offer_response.json()["id"]

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        thread_id = accept_response.json()["thread_id"]
        self.thread_ids.append(thread_id)

        assignment_row = fetch_one(
            """
            SELECT customer_id::text, performer_id::text, assignment_status, execution_stage
            FROM task_assignments
            WHERE task_id = %s::uuid
            LIMIT 1
            """,
            (task_id,),
        )
        self.assertIsNotNone(assignment_row)
        self.assertEqual(assignment_row[0], owner["id"])
        self.assertEqual(assignment_row[1], performer["id"])
        self.assertNotEqual(assignment_row[0], assignment_row[1])
        self.assertEqual(assignment_row[2], "assigned")
        self.assertEqual(assignment_row[3], "accepted")

        owner_chats = self.client.get("/chats", headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(owner_chats.status_code, 200, owner_chats.text)
        performer_chats = self.client.get("/chats", headers={"Authorization": f"Bearer {performer_token}"})
        self.assertEqual(performer_chats.status_code, 200, performer_chats.text)

        owner_thread = next((item for item in owner_chats.json() if item["thread_id"] == thread_id), None)
        performer_thread = next((item for item in performer_chats.json() if item["thread_id"] == thread_id), None)
        self.assertIsNotNone(owner_thread)
        self.assertIsNotNone(performer_thread)
        self.assertNotEqual(owner_thread["kind"], "support")
        self.assertNotEqual(performer_thread["kind"], "support")
        self.assertEqual(owner_thread["announcement_id"], task_id)
        self.assertEqual(performer_thread["announcement_id"], task_id)

        owner_tasks = self.client.get(
            "/announcements/me",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_tasks.status_code, 200, owner_tasks.text)
        owner_task = next((item for item in owner_tasks.json() if item["id"] == task_id), None)
        self.assertIsNotNone(owner_task)
        self.assertEqual(owner_task["data"]["task"]["assignment"]["performer_user_id"], performer["id"])
        self.assertEqual(owner_task["data"]["task"]["assignment"]["customer_user_id"], owner["id"])
        self.assertEqual(owner_task["data"]["task"]["assignment"]["chat_thread_id"], thread_id)

        performer_tasks = self.client.get(
            "/announcements/me",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_tasks.status_code, 200, performer_tasks.text)
        self.assertFalse(any(item["id"] == task_id for item in performer_tasks.json()))

        route_context = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(route_context.status_code, 200, route_context.text)
        self.assertEqual(route_context.json()["entity_id"], task_id)
        self.assertEqual(route_context.json()["customer_user_id"], owner["id"])
        self.assertEqual(route_context.json()["performer_user_id"], performer["id"])
        self.assertEqual(route_context.json()["viewer_role"], "performer")
        self.assertTrue(route_context.json()["can_update_execution"])

        owner_route_context = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_route_context.status_code, 200, owner_route_context.text)
        self.assertEqual(owner_route_context.json()["entity_id"], task_id)
        self.assertEqual(owner_route_context.json()["viewer_role"], "customer")
        self.assertFalse(owner_route_context.json()["can_update_execution"])

        owner_performer_tab = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {owner_token}"},
            params={"role": "performer"},
        )
        self.assertEqual(owner_performer_tab.status_code, 404, owner_performer_tab.text)

        owner_customer_tab = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {owner_token}"},
            params={"role": "customer"},
        )
        self.assertEqual(owner_customer_tab.status_code, 200, owner_customer_tab.text)
        self.assertEqual(owner_customer_tab.json()["entity_id"], task_id)
        self.assertEqual(owner_customer_tab.json()["viewer_role"], "customer")
        self.assertFalse(owner_customer_tab.json()["can_update_execution"])

        performer_customer_tab = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {performer_token}"},
            params={"role": "customer"},
        )
        self.assertEqual(performer_customer_tab.status_code, 404, performer_customer_tab.text)

        performer_performer_tab = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {performer_token}"},
            params={"role": "performer"},
        )
        self.assertEqual(performer_performer_tab.status_code, 200, performer_performer_tab.text)
        self.assertEqual(performer_performer_tab.json()["entity_id"], task_id)
        self.assertEqual(performer_performer_tab.json()["viewer_role"], "performer")
        self.assertTrue(performer_performer_tab.json()["can_update_execution"])

        owner_performer_tasks = self.client.get(
            "/routes/me/performer/tasks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_performer_tasks.status_code, 200, owner_performer_tasks.text)
        self.assertEqual(owner_performer_tasks.json(), [])

        owner_customer_tasks = self.client.get(
            "/routes/me/customer/tasks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_customer_tasks.status_code, 200, owner_customer_tasks.text)
        owner_customer_payload = owner_customer_tasks.json()
        self.assertEqual([item["task_id"] for item in owner_customer_payload], [task_id])
        self.assertEqual(owner_customer_payload[0]["viewer_role"], "customer")
        self.assertFalse(owner_customer_payload[0]["can_update_execution"])
        self.assertEqual(owner_customer_payload[0]["customer_user_id"], owner["id"])
        self.assertEqual(owner_customer_payload[0]["performer_user_id"], performer["id"])
        self.assertTrue(owner_customer_payload[0]["is_accepted"])
        self.assertNotIn("polyline", owner_customer_payload[0])
        self.assertNotIn("tasks_by_route", owner_customer_payload[0])

        performer_performer_tasks = self.client.get(
            "/routes/me/performer/tasks",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_performer_tasks.status_code, 200, performer_performer_tasks.text)
        performer_performer_payload = performer_performer_tasks.json()
        self.assertEqual([item["task_id"] for item in performer_performer_payload], [task_id])
        self.assertEqual(performer_performer_payload[0]["viewer_role"], "performer")
        self.assertTrue(performer_performer_payload[0]["can_update_execution"])
        self.assertEqual(performer_performer_payload[0]["customer_user_id"], owner["id"])
        self.assertEqual(performer_performer_payload[0]["performer_user_id"], performer["id"])
        self.assertTrue(performer_performer_payload[0]["is_accepted"])

        performer_customer_tasks = self.client.get(
            "/routes/me/customer/tasks",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_customer_tasks.status_code, 200, performer_customer_tasks.text)
        self.assertEqual(performer_customer_tasks.json(), [])

        owner_role = self.client.get(
            f"/tasks/{task_id}/my-role",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_role.status_code, 200, owner_role.text)
        self.assertEqual(owner_role.json()["task_id"], task_id)
        self.assertTrue(owner_role.json()["is_customer"])
        self.assertFalse(owner_role.json()["is_performer"])
        self.assertEqual(owner_role.json()["viewer_role"], "customer")
        self.assertFalse(owner_role.json()["can_update_execution"])

        performer_role = self.client.get(
            f"/tasks/{task_id}/my-role",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_role.status_code, 200, performer_role.text)
        self.assertFalse(performer_role.json()["is_customer"])
        self.assertTrue(performer_role.json()["is_performer"])
        self.assertEqual(performer_role.json()["viewer_role"], "performer")
        self.assertTrue(performer_role.json()["can_update_execution"])

        owner_explicit_route = self.client.get(
            f"/announcements/{task_id}/route/context",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_explicit_route.status_code, 200, owner_explicit_route.text)
        self.assertEqual(owner_explicit_route.json()["viewer_role"], "customer")
        self.assertFalse(owner_explicit_route.json()["can_update_execution"])

        performer_explicit_route = self.client.get(
            f"/announcements/{task_id}/route/context",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_explicit_route.status_code, 200, performer_explicit_route.text)
        self.assertEqual(performer_explicit_route.json()["viewer_role"], "performer")
        self.assertTrue(performer_explicit_route.json()["can_update_execution"])

        owner_stage_response = self.client.post(
            f"/announcements/{task_id}/execution-stage",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"stage": "en_route"},
        )
        self.assertEqual(owner_stage_response.status_code, 403, owner_stage_response.text)

        performer_stage_response = self.client.post(
            f"/announcements/{task_id}/execution-stage",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"stage": "en_route"},
        )
        self.assertEqual(performer_stage_response.status_code, 200, performer_stage_response.text)
        self.assertEqual(
            performer_stage_response.json()["data"]["task"]["assignment"]["performer_user_id"],
            performer["id"],
        )
        self.assertEqual(
            performer_stage_response.json()["data"]["task"]["assignment"]["customer_user_id"],
            owner["id"],
        )

        owner_route_after_progress = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_route_after_progress.status_code, 200, owner_route_after_progress.text)
        self.assertEqual(owner_route_after_progress.json()["entity_id"], task_id)
        self.assertEqual(owner_route_after_progress.json()["execution_stage"], "en_route")

        performer_message = self.client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"text": "Привет, я на связи"},
        )
        self.assertEqual(performer_message.status_code, 201, performer_message.text)

        owner_messages = self.client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_messages.status_code, 200, owner_messages.text)
        self.assertTrue(any(message["text"] == "Привет, я на связи" for message in owner_messages.json()))

    def test_performer_route_tasks_returns_all_accepted_tasks_from_different_customers(self) -> None:
        owner_one = self._create_user("multi-route-owner-one")
        owner_two = self._create_user("multi-route-owner-two")
        performer = self._create_user("multi-route-performer")
        owner_one_token = self._login_user(owner_one)
        owner_two_token = self._login_user(owner_two)
        performer_token = self._login_user(performer)

        first_task_id = self._create_route_task(owner_one["id"], "Первая согласованная задача")
        second_task_id = self._create_route_task(owner_two["id"], "Вторая согласованная задача")

        self._accept_route_task(first_task_id, owner_one_token, performer_token)
        self._accept_route_task(second_task_id, owner_two_token, performer_token)

        performer_tasks = self.client.get(
            "/routes/me/performer/tasks",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_tasks.status_code, 200, performer_tasks.text)
        payload = performer_tasks.json()

        self.assertEqual({item["task_id"] for item in payload}, {first_task_id, second_task_id})
        self.assertTrue(all(item["viewer_role"] == "performer" for item in payload))
        self.assertTrue(all(item["performer_user_id"] == performer["id"] for item in payload))
        self.assertTrue(all(item["is_accepted"] for item in payload))

    def test_accepted_offer_current_route_uses_stored_route_points_for_performer(self) -> None:
        owner = self._create_user("route-owner")
        performer = self._create_user("route-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        with patch("app.main.geocode_address", return_value=None):
            _insert_task(
                task_id,
                owner["id"],
                "delivery",
                "Помощь от профи",
                "active",
                {
                    "pickup_address": "Москва, Тверская 1",
                    "dropoff_address": "Москва, Арбат 10",
                    "notes": "Маршрутные точки уже сохранены отдельно",
                },
            )
        execute(
            """
            UPDATE tasks
            SET extra = extra - 'pickup_point' - 'dropoff_point' - 'point',
                location_point = NULL
            WHERE id = %s::uuid
            """,
            (task_id,),
        )
        execute("DELETE FROM task_route_points WHERE task_id = %s::uuid", (task_id,))
        execute(
            """
            INSERT INTO task_route_points (
                id, task_id, point_order, title, address_text, point, point_kind, created_at
            )
            VALUES
                (%s::uuid, %s::uuid, 0, 'Старт', 'Москва, Тверская 1',
                 ST_SetSRID(ST_MakePoint(37.6173, 55.7558), 4326)::geography, 'source', now()),
                (%s::uuid, %s::uuid, 1, 'Финиш', 'Москва, Арбат 10',
                 ST_SetSRID(ST_MakePoint(37.5931, 55.7522), 4326)::geography, 'destination', now())
            """,
            (str(uuid.uuid4()), task_id, str(uuid.uuid4()), task_id),
        )

        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)
        offer_id = offer_response.json()["id"]
        execute(
            """
            UPDATE task_offers
            SET status = 'accepted_by_customer',
                accepted_at = now()
            WHERE id = %s::uuid
            """,
            (offer_id,),
        )

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        self.thread_ids.append(accept_response.json()["thread_id"])

        performer_tasks = self.client.get(
            "/announcements/me",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(performer_tasks.status_code, 200, performer_tasks.text)
        self.assertFalse(any(item["id"] == task_id for item in performer_tasks.json()))

        route_context = self.client.get(
            "/routes/me/current/context",
            headers={"Authorization": f"Bearer {performer_token}"},
        )
        self.assertEqual(route_context.status_code, 200, route_context.text)
        payload = route_context.json()
        self.assertEqual(payload["entity_id"], task_id)
        self.assertEqual(payload["start_address"], "Москва, Тверская 1")
        self.assertEqual(payload["end_address"], "Москва, Арбат 10")
        self.assertAlmostEqual(payload["start"]["lat"], 55.7558, places=4)
        self.assertAlmostEqual(payload["end"]["lon"], 37.5931, places=4)

    def test_current_route_repairs_address_only_assigned_task_with_geocoding(self) -> None:
        owner = self._create_user("route-owner")
        performer = self._create_user("route-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)
        source_address = f"Тестовый проезд {uuid.uuid4().hex}"
        destination_address = f"Финишная улица {uuid.uuid4().hex}"

        with patch("app.main.geocode_address", return_value=None):
            _insert_task(
                task_id,
                owner["id"],
                "delivery",
                "Доставить пакет",
                "active",
                {
                    "pickup_address": source_address,
                    "dropoff_address": destination_address,
                    "notes": "Клиент отправил только адреса, без координат",
                },
            )

        self.assertEqual(
            fetch_one("SELECT COUNT(*) FROM task_route_points WHERE task_id = %s::uuid", (task_id,))[0],
            0,
        )

        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)
        offer_id = offer_response.json()["id"]

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        self.thread_ids.append(accept_response.json()["thread_id"])

        with patch("app.routes_module.service.ROUTE_EXTERNAL_GEOCODE_ENABLED", True), patch(
            "app.routes_module.service.geocode_address",
            side_effect=[(55.701, 37.601), (55.702, 37.602)],
        ):
            route_context = self.client.get(
                "/routes/me/current/context",
                headers={"Authorization": f"Bearer {performer_token}"},
            )

        self.assertEqual(route_context.status_code, 200, route_context.text)
        payload = route_context.json()
        self.assertEqual(payload["entity_id"], task_id)
        self.assertEqual(payload["start_address"], source_address)
        self.assertEqual(payload["end_address"], destination_address)
        self.assertAlmostEqual(payload["start"]["lat"], 55.701, places=4)
        self.assertAlmostEqual(payload["end"]["lon"], 37.602, places=4)

        repaired = fetch_one(
            """
            SELECT t.extra, COUNT(trp.id)
            FROM tasks t
            LEFT JOIN task_route_points trp
              ON trp.task_id = t.id
            WHERE t.id = %s::uuid
            GROUP BY t.extra
            """,
            (task_id,),
        )
        self.assertIsNotNone(repaired)
        repaired_data = repaired[0] if isinstance(repaired[0], dict) else json.loads(repaired[0])
        self.assertEqual(repaired[1], 2)
        self.assertEqual(repaired_data["pickup_point"], {"lat": 55.701, "lon": 37.601})
        self.assertEqual(repaired_data["dropoff_point"], {"lat": 55.702, "lon": 37.602})

    def test_accept_offer_refuses_assignment_where_customer_is_performer(self) -> None:
        owner = self._create_user("self-assignment-owner")
        owner_token = self._login_user(owner)
        task_id = str(uuid.uuid4())
        offer_id = str(uuid.uuid4())
        self.task_ids.append(task_id)

        _insert_task(
            task_id,
            owner["id"],
            "delivery",
            "Забрать документы",
            "active",
            {
                "pickup_address": "Москва, Тверская 1",
                "dropoff_address": "Москва, Арбат 10",
                "pickup_point": {"lat": 55.7558, "lon": 37.6173},
                "dropoff_point": {"lat": 55.7522, "lon": 37.5931},
                "notes": "Тест некорректного отклика",
            },
        )
        execute(
            """
            INSERT INTO task_offers (
                id, task_id, performer_id, message, proposed_price, currency, status,
                created_at, updated_at, pricing_mode, agreed_price, minimum_price_accepted,
                can_reoffer, reoffer_block_reason
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, 'bad self-offer', 1000, 'RUB', 'sent',
                now(), now(), 'counter_price', 1000, FALSE, TRUE, NULL
            )
            """,
            (offer_id, task_id, owner["id"]),
        )

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(accept_response.status_code, 409, accept_response.text)
        self.assertIn("Исполнитель не может совпадать", accept_response.text)

        assignment_row = fetch_one(
            "SELECT 1 FROM task_assignments WHERE task_id = %s::uuid",
            (task_id,),
        )
        self.assertIsNone(assignment_row)

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
        self.assertEqual(performer_select.json()["status"], "waiting_final_acceptance")
        self.assertEqual(performer_select.json()["selected_option_id"], selected_option_id)

        owner_accept = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"accepted": True},
        )
        self.assertEqual(owner_accept.status_code, 200, owner_accept.text)
        self.assertEqual(owner_accept.json()["status"], "waiting_final_acceptance")
        self.assertEqual(owner_accept.json()["my_final_acceptance_decision"], "accepted")
        self.assertEqual(owner_accept.json()["final_acceptance_votes"]["customer"], "accepted")

        performer_accept = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"accepted": True},
        )
        self.assertEqual(performer_accept.status_code, 200, performer_accept.text)
        self.assertEqual(performer_accept.json()["status"], "resolved")
        self.assertEqual(performer_accept.json()["final_acceptance_votes"]["customer"], "accepted")
        self.assertEqual(performer_accept.json()["final_acceptance_votes"]["performer"], "accepted")

    def test_dispute_multiselect_resolves_by_shared_compromise(self) -> None:
        owner = self._create_user("multi-owner")
        performer = self._create_user("multi-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        open_response = self.client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Мультивыбор",
                "problem_description": "Проверяем пересечение нескольких вариантов",
                "requested_compensation_rub": 1000,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_id = open_response.json()["id"]
        self.dispute_ids.append(dispute_id)

        execute(
            """
            UPDATE disputes
            SET status = 'waiting_round_1_votes',
                active_round = 1,
                counterparty_form = %s::jsonb,
                round1_options = %s::jsonb,
                round1_votes = '{}'::jsonb,
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (
                json.dumps(
                    {
                        "response_description": "Готов обсуждать средний вариант, но не полный возврат",
                        "acceptable_refund_percent": 0,
                        "desired_resolution": "partial_refund",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    [
                        {
                            "id": "opt_high",
                            "lean": "initiator_favor",
                            "title": "Высокий возврат",
                            "description": "Больше подходит инициатору",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 900,
                            "refund_percent": 90,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "opt_low",
                            "lean": "counterparty_favor",
                            "title": "Низкий возврат",
                            "description": "Больше подходит второй стороне",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 100,
                            "refund_percent": 10,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "opt_mid",
                            "lean": "compromise",
                            "title": "Средний возврат",
                            "description": "Серединный вариант",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 500,
                            "refund_percent": 50,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "opt_redo",
                            "lean": "compromise",
                            "title": "Переделка",
                            "description": "Альтернативный вариант",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 0,
                            "refund_percent": 0,
                            "resolution_kind": "redo",
                        },
                    ],
                    ensure_ascii=False,
                ),
                dispute_id,
            ),
        )

        owner_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"option_ids": ["opt_high", "opt_mid", "opt_redo"]},
        )
        self.assertEqual(owner_select.status_code, 200, owner_select.text)
        self.assertEqual(owner_select.json()["my_vote_option_ids"], ["opt_high", "opt_mid", "opt_redo"])

        performer_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"option_ids": ["opt_low", "opt_mid", "opt_redo"]},
        )
        self.assertEqual(performer_select.status_code, 200, performer_select.text)
        payload = performer_select.json()
        self.assertEqual(payload["status"], "waiting_final_acceptance")
        self.assertEqual(payload["selected_option_id"], "opt_mid")
        self.assertEqual(payload["vote_option_ids"]["customer"], ["opt_high", "opt_mid", "opt_redo"])
        self.assertEqual(payload["vote_option_ids"]["performer"], ["opt_low", "opt_mid", "opt_redo"])

        owner_reject = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"accepted": False},
        )
        self.assertEqual(owner_reject.status_code, 200, owner_reject.text)
        self.assertEqual(owner_reject.json()["status"], "awaiting_moderator")
        self.assertTrue(owner_reject.json()["moderator_required"])
        self.assertEqual(owner_reject.json()["final_acceptance_votes"]["customer"], "rejected")

    def test_dispute_round2_multiselect_enters_final_acceptance(self) -> None:
        owner = self._create_user("round2-multi-owner")
        performer = self._create_user("round2-multi-performer")
        owner_token = self._login_user(owner)
        performer_token = self._login_user(performer)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        open_response = self.client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Мультивыбор второго раунда",
                "problem_description": "Проверяем общий выбор во втором раунде",
                "requested_compensation_rub": 2000,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_id = open_response.json()["id"]
        self.dispute_ids.append(dispute_id)

        execute(
            """
            UPDATE disputes
            SET status = 'waiting_round_2_votes',
                active_round = 2,
                counterparty_form = %s::jsonb,
                round2_options = %s::jsonb,
                round2_votes = '{}'::jsonb,
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (
                json.dumps(
                    {
                        "response_description": "Готов обсуждать только более мягкий второй раунд",
                        "acceptable_refund_percent": 30,
                        "desired_resolution": "partial_refund",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    [
                        {
                            "id": "r2_high",
                            "lean": "initiator_favor",
                            "title": "18 000 ₽",
                            "description": "Высокий вариант второго раунда",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 18000,
                            "refund_percent": 90,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "r2_mid",
                            "lean": "compromise",
                            "title": "18 500 ₽",
                            "description": "Общий компромисс второго раунда",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 18500,
                            "refund_percent": 92,
                            "resolution_kind": "partial_refund",
                        },
                        {
                            "id": "r2_low",
                            "lean": "counterparty_favor",
                            "title": "19 000 ₽",
                            "description": "Нижний вариант второй стороны",
                            "customer_action": "Принять",
                            "performer_action": "Подтвердить",
                            "compensation_rub": 19000,
                            "refund_percent": 95,
                            "resolution_kind": "partial_refund",
                        },
                    ],
                    ensure_ascii=False,
                ),
                dispute_id,
            ),
        )

        owner_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"option_ids": ["r2_high", "r2_mid"]},
        )
        self.assertEqual(owner_select.status_code, 200, owner_select.text)

        performer_select = self.client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/options/select",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"option_ids": ["r2_mid", "r2_low"]},
        )
        self.assertEqual(performer_select.status_code, 200, performer_select.text)
        payload = performer_select.json()
        self.assertEqual(payload["status"], "waiting_final_acceptance")
        self.assertEqual(payload["selected_option_id"], "r2_mid")
        self.assertEqual(payload["vote_option_ids"]["customer"], ["r2_high", "r2_mid"])
        self.assertEqual(payload["vote_option_ids"]["performer"], ["r2_mid", "r2_low"])


if __name__ == "__main__":
    unittest.main()
