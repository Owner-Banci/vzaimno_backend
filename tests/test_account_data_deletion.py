from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.db import execute, fetch_one
from app.main import app as public_app
from app.security import hash_password


class AccountDataDeletionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.client = TestClient(public_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []

    def tearDown(self) -> None:
        for user_id in self.user_ids:
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_sessions WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM password_reset_tokens WHERE user_id = %s::uuid", (user_id,))
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
                created_at,
                updated_at
            )
            VALUES (%s::uuid, %s, %s, 'user', TRUE, now(), now())
            """,
            (user_id, email, hash_password(password)),
        )
        execute(
            """
            INSERT INTO user_profiles (user_id, display_name, bio, city, extra, created_at, updated_at)
            VALUES (%s::uuid, 'Тестовый пользователь', 'bio', 'Москва', '{"preferred_address":"Тверская"}'::jsonb, now(), now())
            """,
            (user_id,),
        )
        execute(
            """
            INSERT INTO user_stats (user_id, rating_avg, rating_count, completed_count, cancelled_count, created_at, updated_at)
            VALUES (%s::uuid, 4.5, 2, 1, 0, now(), now())
            """,
            (user_id,),
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

    def test_account_deletion_requires_confirmation_text(self) -> None:
        user = self._create_user("delete-confirm")
        token = self._login_user(user)

        response = self.client.request(
            "DELETE",
            "/users/me/data",
            headers={"Authorization": f"Bearer {token}"},
            json={"categories": ["account"], "delete_account": True},
        )

        self.assertEqual(response.status_code, 422, response.text)
        row = fetch_one("SELECT deleted_at FROM users WHERE id = %s::uuid", (user["id"],))
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])

    def test_account_deletion_scrubs_identity_and_invalidates_session(self) -> None:
        user = self._create_user("delete-account")
        token = self._login_user(user)

        response = self.client.request(
            "DELETE",
            "/users/me/data",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "categories": ["account"],
                "delete_account": True,
                "confirmation_text": "УДАЛИТЬ",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["account_deleted"])
        self.assertIn("account", payload["deleted_categories"])

        row = fetch_one(
            """
            SELECT email, phone_hash, deleted_at
            FROM users
            WHERE id = %s::uuid
            """,
            (user["id"],),
        )
        self.assertIsNotNone(row)
        self.assertTrue(str(row[0]).startswith("deleted-"))
        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])

        self.assertIsNone(fetch_one("SELECT 1 FROM user_sessions WHERE user_id = %s::uuid", (user["id"],)))
        self.assertIsNone(fetch_one("SELECT 1 FROM user_profiles WHERE user_id = %s::uuid", (user["id"],)))
        self.assertIsNone(fetch_one("SELECT 1 FROM user_stats WHERE user_id = %s::uuid", (user["id"],)))

        me_response = self.client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me_response.status_code, 401, me_response.text)

    def test_profile_only_deletion_keeps_account_active(self) -> None:
        user = self._create_user("delete-profile")
        token = self._login_user(user)

        response = self.client.request(
            "DELETE",
            "/users/me/data",
            headers={"Authorization": f"Bearer {token}"},
            json={"categories": ["profile"]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["account_deleted"])

        row = fetch_one(
            """
            SELECT display_name, bio, city, extra
            FROM user_profiles
            WHERE user_id = %s::uuid
            """,
            (user["id"],),
        )
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertEqual(row[3], {})

        me_response = self.client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me_response.status_code, 200, me_response.text)


if __name__ == "__main__":
    unittest.main()
