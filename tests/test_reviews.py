from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.db import execute, fetch_one
from app.main import _insert_task, app
from app.security import create_user_access_token, hash_password


class ReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.client = TestClient(app, base_url="http://localhost")

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.task_ids: list[str] = []
        self.thread_ids: list[str] = []

    def tearDown(self) -> None:
        for task_id in self.task_ids:
            execute("DELETE FROM reviews WHERE task_id = %s", (task_id,))
            execute("DELETE FROM task_assignment_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_status_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_route_points WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_assignments WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_offers WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM announcements WHERE id::text = %s", (task_id,))
            execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))

        for thread_id in self.thread_ids:
            execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM chat_messages WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM chat_participants WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_messages WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_threads WHERE id = %s::uuid", (thread_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM reviews WHERE from_user_id = %s::uuid OR to_user_id = %s::uuid", (user_id, user_id))
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_sessions WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))

    def _create_user(self, display_name: str) -> dict[str, str]:
        user_id = str(uuid.uuid4())
        email = f"{display_name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
        execute(
            """
            INSERT INTO users (
                id, email, password_hash, role,
                is_email_verified, is_phone_verified,
                created_at, updated_at
            )
            VALUES (%s::uuid, %s, %s, 'user', TRUE, FALSE, now(), now())
            """,
            (user_id, email, hash_password("UserPass123!")),
        )
        execute(
            """
            INSERT INTO user_profiles (user_id, display_name, created_at, updated_at)
            VALUES (%s::uuid, %s, now(), now())
            """,
            (user_id, display_name),
        )
        execute(
            """
            INSERT INTO user_stats (user_id, created_at, updated_at)
            VALUES (%s::uuid, now(), now())
            """,
            (user_id,),
        )
        self.user_ids.append(user_id)
        return {
            "id": user_id,
            "token": create_user_access_token(user_id),
        }

    def _create_completed_assignment(self, customer: dict[str, str], performer: dict[str, str]) -> str:
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)
        _insert_task(
            task_id,
            customer["id"],
            "help",
            "Помочь с задачей",
            "active",
            {
                "address": "Москва, Тверская 1",
                "address_text": "Москва, Тверская 1",
                "point": {"lat": 55.7558, "lon": 37.6173},
                "notes": "Нужно проверить отзывы",
            },
        )

        offer_response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer['token']}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )
        self.assertEqual(offer_response.status_code, 201, offer_response.text)
        offer_id = offer_response.json()["id"]

        accept_response = self.client.post(
            f"/announcements/{task_id}/offers/{offer_id}/accept",
            headers={"Authorization": f"Bearer {customer['token']}"},
        )
        self.assertEqual(accept_response.status_code, 200, accept_response.text)
        self.thread_ids.append(accept_response.json()["thread_id"])

        for stage in ("en_route", "on_site", "in_progress", "handoff", "completed"):
            stage_response = self.client.post(
                f"/announcements/{task_id}/execution-stage",
                headers={"Authorization": f"Bearer {performer['token']}"},
                json={"stage": stage},
            )
            self.assertEqual(stage_response.status_code, 200, stage_response.text)

        return task_id

    def test_completed_task_reviews_are_saved_and_filtered_by_target_role(self) -> None:
        customer = self._create_user("Customer")
        performer = self._create_user("Performer")
        task_id = self._create_completed_assignment(customer, performer)

        customer_context = self.client.get(
            f"/announcements/{task_id}/review-context",
            headers={"Authorization": f"Bearer {customer['token']}"},
        )
        self.assertEqual(customer_context.status_code, 200, customer_context.text)
        self.assertTrue(customer_context.json()["can_submit"])
        self.assertEqual(customer_context.json()["counterpart_role"], "performer")

        customer_review = self.client.post(
            f"/announcements/{task_id}/review",
            headers={"Authorization": f"Bearer {customer['token']}"},
            json={"stars": 5, "text": "Отличная работа"},
        )
        self.assertEqual(customer_review.status_code, 200, customer_review.text)

        duplicate_review = self.client.post(
            f"/announcements/{task_id}/review",
            headers={"Authorization": f"Bearer {customer['token']}"},
            json={"stars": 4, "text": "Повтор"},
        )
        self.assertEqual(duplicate_review.status_code, 409, duplicate_review.text)

        performer_context = self.client.get(
            f"/announcements/{task_id}/review-context",
            headers={"Authorization": f"Bearer {performer['token']}"},
        )
        self.assertEqual(performer_context.status_code, 200, performer_context.text)
        self.assertTrue(performer_context.json()["can_submit"])
        self.assertEqual(performer_context.json()["counterpart_role"], "customer")

        performer_review = self.client.post(
            f"/announcements/{task_id}/review",
            headers={"Authorization": f"Bearer {performer['token']}"},
            json={"stars": 4, "text": "Хороший заказчик"},
        )
        self.assertEqual(performer_review.status_code, 200, performer_review.text)

        performer_feed = self.client.get(
            "/users/me/reviews",
            headers={"Authorization": f"Bearer {performer['token']}"},
            params={"role": "performer"},
        )
        self.assertEqual(performer_feed.status_code, 200, performer_feed.text)
        self.assertEqual(performer_feed.json()["summary"], {"average": 5.0, "count": 1})
        self.assertEqual(performer_feed.json()["items"][0]["target_role"], "performer")

        customer_feed = self.client.get(
            "/users/me/reviews",
            headers={"Authorization": f"Bearer {customer['token']}"},
            params={"role": "customer"},
        )
        self.assertEqual(customer_feed.status_code, 200, customer_feed.text)
        self.assertEqual(customer_feed.json()["summary"], {"average": 4.0, "count": 1})
        self.assertEqual(customer_feed.json()["items"][0]["target_role"], "customer")

        public_profile = self.client.get(
            f"/users/{performer['id']}",
            headers={"Authorization": f"Bearer {customer['token']}"},
        )
        self.assertEqual(public_profile.status_code, 200, public_profile.text)
        self.assertEqual(public_profile.json()["user"]["id"], performer["id"])
        self.assertEqual(public_profile.json()["profile"]["display_name"], "Performer")
        self.assertEqual(public_profile.json()["stats"]["rating_avg"], 5.0)

        public_reviews = self.client.get(
            f"/users/{performer['id']}/reviews",
            headers={"Authorization": f"Bearer {customer['token']}"},
            params={"role": "performer"},
        )
        self.assertEqual(public_reviews.status_code, 200, public_reviews.text)
        self.assertEqual(public_reviews.json()["summary"], {"average": 5.0, "count": 1})
        self.assertEqual(public_reviews.json()["items"][0]["text"], "Отличная работа")

        stored_roles = fetch_one(
            """
            SELECT
                COUNT(*) FILTER (WHERE author_role = 'customer' AND target_role = 'performer'),
                COUNT(*) FILTER (WHERE author_role = 'performer' AND target_role = 'customer')
            FROM reviews
            WHERE task_id = %s
            """,
            (task_id,),
        )
        self.assertEqual(stored_roles, (1, 1))
