from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.chat import _offer_thread_kind_value, ensure_chat_participant
from app.db import execute
from app.main import UPLOADS_DIR, _insert_task, app as public_app
from app.rate_limit import RateLimitError
from app.security import create_user_access_token, hash_password


class PreReleaseSecurityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.client = TestClient(public_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.task_ids: list[str] = []
        self.thread_ids: list[str] = []
        self.dispute_ids: list[str] = []
        self.upload_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self.upload_paths:
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except OSError:
                pass

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
            execute("DELETE FROM audit_logs WHERE target_type = 'support_thread' AND target_id = %s", (thread_id,))

        for task_id in self.task_ids:
            execute("DELETE FROM task_assignment_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_status_events WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_route_points WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_assignments WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM task_offers WHERE task_id = %s::uuid", (task_id,))
            execute("DELETE FROM reports WHERE target_id = %s", (task_id,))
            execute("DELETE FROM announcements WHERE id::text = %s", (task_id,))
            execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM user_restrictions WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM audit_logs WHERE actor_user_account_id = %s::uuid OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))

    def _create_user(self, prefix: str) -> dict[str, str]:
        user_id = str(uuid.uuid4())
        email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
        password = "UserPass123!"
        execute(
            """
            INSERT INTO users (
                id, email, password_hash, role,
                is_email_verified, is_phone_verified,
                created_at, updated_at
            )
            VALUES (%s::uuid, %s, %s, 'user', TRUE, FALSE, now(), now())
            """,
            (user_id, email, hash_password(password)),
        )
        self.user_ids.append(user_id)
        return {"id": user_id, "email": email, "password": password}

    def _login_user(self, user: dict[str, str]) -> str:
        return create_user_access_token(user["id"])

    def _restrict_user(self, user_id: str, restriction_type: str) -> None:
        execute(
            """
            INSERT INTO user_restrictions (
                id, user_id, type, status, reason_text, source_type,
                starts_at, ends_at, meta, created_at, updated_at
            )
            VALUES (%s, %s::uuid, %s, 'active', 'test restriction', 'manual', now(), NULL, '{}'::jsonb, now(), now())
            """,
            (str(uuid.uuid4()), user_id, restriction_type),
        )

    def _create_active_task(self, owner_id: str, prefix: str = "secure-task") -> str:
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)
        _insert_task(
            task_id,
            owner_id,
            "help",
            prefix,
            "active",
            {
                "address": "Москва, Тверская 1",
                "address_text": "Москва, Тверская 1",
                "point": {"lat": 55.7558, "lon": 37.6173},
                "notes": "Нужно помочь с бытовым поручением",
            },
        )
        return task_id

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

    def test_banned_user_cannot_create_announcement(self) -> None:
        user = self._create_user("banned-posting")
        token = self._login_user(user)
        self._restrict_user(user["id"], "temporary_ban")

        response = self.client.post(
            "/announcements",
            headers={"Authorization": f"Bearer {token}"},
            json={"category": "help", "title": "Помочь собрать шкаф", "data": {"notes": "Сегодня вечером"}},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_muted_user_cannot_send_chat_or_support_messages(self) -> None:
        owner = self._create_user("mute-owner")
        muted = self._create_user("mute-user")
        owner_token = self._login_user(owner)
        muted_token = self._login_user(muted)
        thread_id = self._create_direct_thread(owner["id"], muted["id"])
        support_response = self.client.get("/support/thread", headers={"Authorization": f"Bearer {muted_token}"})
        self.assertEqual(support_response.status_code, 200, support_response.text)
        support_thread_id = support_response.json()["thread_id"]
        self.thread_ids.append(support_thread_id)
        self._restrict_user(muted["id"], "mute_chat")

        chat_response = self.client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {muted_token}"},
            json={"text": "Не должно отправиться"},
        )
        support_message_response = self.client.post(
            f"/support/thread/{support_thread_id}/messages",
            headers={"Authorization": f"Bearer {muted_token}"},
            json={"text": "Не должно отправиться в поддержку"},
        )

        self.assertEqual(chat_response.status_code, 403, chat_response.text)
        self.assertEqual(support_message_response.status_code, 403, support_message_response.text)
        self.assertEqual(
            self.client.get(f"/chats/{thread_id}/messages", headers={"Authorization": f"Bearer {owner_token}"}).status_code,
            200,
        )

    def test_restricted_user_cannot_create_offer(self) -> None:
        owner = self._create_user("offer-owner")
        performer = self._create_user("offer-restricted")
        performer_token = self._login_user(performer)
        task_id = self._create_active_task(owner["id"])
        self._restrict_user(performer["id"], "restrict_offers")

        response = self.client.post(
            f"/announcements/{task_id}/offers",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"message": "Готов выполнить", "proposed_price": 1000},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_rate_limit_returns_429(self) -> None:
        async def always_limited(*_args, **_kwargs) -> None:
            raise RateLimitError(retry_after=60)

        with patch("app.main.enforce_rate_limit", new=always_limited):
            response = self.client.post(
                "/reports",
                json={"target_type": "user", "target_id": str(uuid.uuid4()), "reason_code": "spam"},
            )

        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers.get("retry-after"), "60")

    def test_user_cannot_access_another_users_chat_upload_support_or_dispute(self) -> None:
        owner = self._create_user("idor-owner")
        performer = self._create_user("idor-performer")
        intruder = self._create_user("idor-intruder")
        owner_token = self._login_user(owner)
        intruder_token = self._login_user(intruder)
        thread_id = self._create_direct_thread(owner["id"], performer["id"])
        task_id = self._create_active_task(owner["id"], "private upload")

        upload_dir = UPLOADS_DIR / task_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / "private.png"
        upload_path.write_bytes(b"not-an-image-but-served-file")
        self.upload_paths.append(upload_path)

        support_response = self.client.get("/support/thread", headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(support_response.status_code, 200, support_response.text)
        support_thread_id = support_response.json()["thread_id"]
        self.thread_ids.append(support_thread_id)

        dispute_response = self.client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Проблема",
                "problem_description": "Нужно проверить доступ к спору",
                "requested_compensation_rub": 0,
                "desired_resolution": "other",
            },
        )
        self.assertEqual(dispute_response.status_code, 201, dispute_response.text)
        self.dispute_ids.append(dispute_response.json()["id"])

        chat_response = self.client.get(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        upload_response = self.client.get(
            f"/uploads/{task_id}/private.png",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        support_messages_response = self.client.get(
            f"/support/thread/{support_thread_id}/messages",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        dispute_state_response = self.client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )

        self.assertEqual(chat_response.status_code, 403, chat_response.text)
        self.assertEqual(upload_response.status_code, 403, upload_response.text)
        self.assertEqual(support_messages_response.status_code, 403, support_messages_response.text)
        self.assertEqual(dispute_state_response.status_code, 403, dispute_state_response.text)

    def test_dangerous_announcement_data_fields_are_ignored(self) -> None:
        user = self._create_user("sanitize-owner")
        token = self._login_user(user)

        with patch("app.main.classify_text", return_value={"label": "LEGAL", "reason": "test"}):
            response = self.client.post(
                "/announcements",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "category": "help",
                    "title": "Помочь перенести коробки",
                    "data": {
                        "notes": "Пара коробок",
                        "moderation": {"client_marker": True, "decision": {"status": "rejected"}},
                        "media": [{"object_key": "someone-else/private.png"}],
                        "object_key": "someone-else/private.png",
                        "user_id": str(uuid.uuid4()),
                        "task": {
                            "route": {"source": {"address": "Москва, Тверская 1"}},
                            "assignment": {"performer_user_id": str(uuid.uuid4())},
                            "execution": {"status": "completed"},
                        },
                    },
                },
            )

        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.task_ids.append(payload["id"])
        data = payload["data"]
        self.assertNotIn("object_key", data)
        self.assertNotIn("media", data)
        self.assertNotEqual(data.get("user_id"), user["id"])
        self.assertNotIn("client_marker", data.get("moderation", {}))
        self.assertIsNone(data.get("task", {}).get("assignment", {}).get("performer_user_id"))
        self.assertNotEqual(data.get("task", {}).get("execution", {}).get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
