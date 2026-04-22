from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from app.bootstrap import ensure_all_tables
from app.db import execute, fetch_one
from app.main import app as public_app
from app.security import hash_password


class DeviceUnregisterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.client = TestClient(public_app)

    def setUp(self) -> None:
        self.user_ids: list[str] = []

    def tearDown(self) -> None:
        for user_id in self.user_ids:
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

    def _login_user(self, user: dict[str, str]) -> str:
        response = self.client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def test_unregister_device_with_null_push_token_does_not_crash(self) -> None:
        user = self._create_user("device-unregister")
        token = self._login_user(user)

        register_response = self.client.post(
            "/devices/register",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "device_id": "ios-device-logout-1",
                "platform": "ios",
                "push_token": None,
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)

        unregister_response = self.client.request(
            "DELETE",
            "/devices/me",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": "ios-device-logout-1", "push_token": None},
        )
        self.assertEqual(unregister_response.status_code, 200, unregister_response.text)
        self.assertTrue(unregister_response.json().get("ok"))

        row = fetch_one(
            """
            SELECT deleted_at, push_token
            FROM user_devices
            WHERE user_id = %s::uuid
              AND device_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user["id"], "ios-device-logout-1"),
        )
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])

    def test_public_health_endpoint_is_available(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json().get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
