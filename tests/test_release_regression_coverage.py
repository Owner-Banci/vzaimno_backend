from __future__ import annotations

import io
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app.bootstrap import ensure_all_tables
from app.chat import _offer_thread_kind_value, ensure_chat_participant
from app.db import execute, fetch_one
from app.main import UPLOADS_DIR, _insert_task, app as public_app
from app.security import create_user_access_token, decode_user_access_token, hash_password
from services.admin_panel.app.main import app as admin_app


class ReleaseRegressionCoverageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_all_tables()
        cls.public_client = TestClient(public_app, base_url="http://localhost")
        cls.admin_client = TestClient(admin_app, base_url="http://localhost")

    def setUp(self) -> None:
        self.user_ids: list[str] = []
        self.user_emails: list[str] = []
        self.admin_ids: list[str] = []
        self.thread_ids: list[str] = []
        self.task_ids: list[str] = []
        self.report_ids: list[str] = []
        self.dispute_ids: list[str] = []
        self.upload_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self.upload_paths:
            try:
                path.unlink(missing_ok=True)
                parent = path.parent
                while parent != UPLOADS_DIR.resolve() and parent.exists():
                    parent.rmdir()
                    parent = parent.parent
            except OSError:
                pass

        for dispute_id in self.dispute_ids:
            execute("DELETE FROM dispute_events WHERE dispute_id = %s::uuid", (dispute_id,))
            execute("DELETE FROM disputes WHERE id = %s::uuid", (dispute_id,))

        for thread_id in self.thread_ids:
            execute("DELETE FROM dispute_events WHERE dispute_id IN (SELECT id FROM disputes WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM disputes WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM support_thread_admin_reads WHERE thread_id = %s::uuid", (thread_id,))
            execute("UPDATE chat_participants SET last_read_message_id = NULL WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM chat_messages WHERE thread_id = %s::uuid)", (thread_id,))
            execute("DELETE FROM chat_participants WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_messages WHERE thread_id = %s::uuid", (thread_id,))
            execute("DELETE FROM support_threads WHERE id = %s::uuid", (thread_id,))
            execute("DELETE FROM chat_threads WHERE id = %s::uuid", (thread_id,))
            execute("DELETE FROM audit_logs WHERE target_type = 'support_thread' AND target_id = %s", (thread_id,))

        for report_id in self.report_ids:
            execute("DELETE FROM notifications WHERE payload->>'report_id' = %s", (report_id,))
            execute("DELETE FROM audit_logs WHERE target_type = 'report' AND target_id = %s", (report_id,))
            execute("DELETE FROM moderation_actions WHERE target_type = 'report' AND target_id = %s", (report_id,))
            execute("DELETE FROM reports WHERE id = %s", (report_id,))

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

        for admin_id in self.admin_ids:
            execute("DELETE FROM admin_sessions WHERE admin_account_id = %s::uuid", (admin_id,))
            execute("DELETE FROM audit_logs WHERE actor_admin_account_id = %s::uuid OR target_id = %s", (admin_id, admin_id))
            execute("DELETE FROM moderation_actions WHERE moderator_id = %s OR target_id = %s", (admin_id, admin_id))
            execute("DELETE FROM admin_accounts WHERE id = %s::uuid", (admin_id,))

        for user_id in self.user_ids:
            execute("DELETE FROM user_restrictions WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM notifications WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_sessions WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM password_reset_tokens WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_profiles WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_stats WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM user_devices WHERE user_id = %s::uuid", (user_id,))
            execute("DELETE FROM audit_logs WHERE actor_user_account_id = %s::uuid OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM moderation_actions WHERE moderator_id = %s OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM reports WHERE reporter_id::text = %s OR target_id = %s", (user_id, user_id))
            execute("DELETE FROM users WHERE id = %s::uuid", (user_id,))

        for email in self.user_emails:
            execute("DELETE FROM login_attempts WHERE email = %s", (email,))

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
        self.user_emails.append(email)
        return {"id": user_id, "email": email, "password": password}

    def _register_user(self, prefix: str) -> dict[str, str]:
        email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
        password = "UserPass123!"
        response = self.public_client.post("/auth/register", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        user_id = str(decode_user_access_token(payload["access_token"])["sub"])
        self.user_ids.append(user_id)
        self.user_emails.append(email)
        return {"id": user_id, "email": email, "password": password, **payload}

    def _login_user(self, user: dict[str, str]) -> dict[str, str]:
        response = self.public_client.post(
            "/auth/login",
            json={"email": user["email"], "password": user["password"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _create_admin_account(self, *, linked_user_id: str, prefix: str, role: str = "admin") -> dict[str, str]:
        admin_id = str(uuid.uuid4())
        login_identifier = f"{prefix}-{uuid.uuid4().hex[:8]}"
        password = "AdminPass123!"
        execute(
            """
            INSERT INTO admin_accounts (
                id, login_identifier, email, password_hash, role, status,
                display_name, linked_user_account_id, created_at, updated_at,
                password_reset_required
            )
            VALUES (%s::uuid, %s, %s, %s, %s, 'active', %s, %s::uuid, now(), now(), FALSE)
            """,
            (
                admin_id,
                login_identifier,
                f"{login_identifier}@example.com",
                hash_password(password),
                role,
                "Release QA Admin",
                linked_user_id,
            ),
        )
        self.admin_ids.append(admin_id)
        return {"id": admin_id, "login_identifier": login_identifier, "password": password}

    def _login_admin(self, admin: dict[str, str]) -> str:
        async def allow_rate_limit(*_args, **_kwargs) -> None:
            return None

        with patch("services.admin_panel.app.auth.enforce_rate_limit", new=allow_rate_limit):
            response = self.admin_client.post(
                "/admin/api/auth/login",
                json={"login_identifier": admin["login_identifier"], "password": admin["password"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _create_active_task(self, owner_id: str, title: str = "release regression task") -> str:
        task_id = str(uuid.uuid4())
        self.task_ids.append(task_id)
        _insert_task(
            task_id,
            owner_id,
            "help",
            title,
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

    def _png_bytes(self) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (2, 2), color=(40, 120, 220)).save(output, format="PNG")
        return output.getvalue()

    def _assert_websocket_rejected(self, path: str, expected_close_code: int) -> None:
        url = f"ws://localhost{path}" if path.startswith("/") else path
        try:
            with self.public_client.websocket_connect(url):
                self.fail("WebSocket connection unexpectedly succeeded")
        except Exception as exc:
            close_code = getattr(exc, "code", None)
            if close_code is not None:
                self.assertEqual(close_code, expected_close_code)
                return
            status_code = getattr(exc, "status_code", None)
            self.assertIsNotNone(status_code, repr(exc))
            self.assertIn(status_code, {403, expected_close_code})

    def test_auth_register_login_refresh_rotation_logout_and_revoked_access_tokens(self) -> None:
        registered = self._register_user("auth-register")
        me_response = self.public_client.get("/me", headers={"Authorization": f"Bearer {registered['access_token']}"})
        self.assertEqual(me_response.status_code, 200, me_response.text)

        login_payload = self._login_user(registered)
        refresh_response = self.public_client.post("/auth/refresh", json={"refresh_token": login_payload["refresh_token"]})
        self.assertEqual(refresh_response.status_code, 200, refresh_response.text)
        rotated_refresh = refresh_response.json()["refresh_token"]
        self.assertNotEqual(rotated_refresh, login_payload["refresh_token"])

        old_refresh_response = self.public_client.post("/auth/refresh", json={"refresh_token": login_payload["refresh_token"]})
        self.assertEqual(old_refresh_response.status_code, 401, old_refresh_response.text)

        logout_response = self.public_client.post("/auth/logout", json={"refresh_token": rotated_refresh})
        self.assertEqual(logout_response.status_code, 200, logout_response.text)
        revoked_access_response = self.public_client.get(
            "/me",
            headers={"Authorization": f"Bearer {refresh_response.json()['access_token']}"},
        )
        self.assertEqual(revoked_access_response.status_code, 401, revoked_access_response.text)

    def test_auth_revoke_one_revoke_all_invalid_and_expired_tokens(self) -> None:
        user = self._create_user("auth-revoke")
        first = self._login_user(user)
        second = self._login_user(user)
        second_session_id = str(decode_user_access_token(second["access_token"])["sid"])

        revoke_response = self.public_client.post(
            "/auth/sessions/revoke",
            headers={"Authorization": f"Bearer {first['access_token']}"},
            json={"session_id": second_session_id},
        )
        self.assertEqual(revoke_response.status_code, 200, revoke_response.text)
        self.assertEqual(
            self.public_client.get("/me", headers={"Authorization": f"Bearer {second['access_token']}"}).status_code,
            401,
        )
        self.assertEqual(
            self.public_client.get("/me", headers={"Authorization": f"Bearer {first['access_token']}"}).status_code,
            200,
        )

        revoke_all_response = self.public_client.post(
            "/auth/sessions/revoke-all",
            headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        self.assertEqual(revoke_all_response.status_code, 200, revoke_all_response.text)
        self.assertEqual(
            self.public_client.get("/me", headers={"Authorization": f"Bearer {first['access_token']}"}).status_code,
            401,
        )
        self.assertEqual(self.public_client.get("/me", headers={"Authorization": "Bearer not-a-token"}).status_code, 401)

        expired = create_user_access_token(user["id"], session_id=None, expires_minutes=-1)
        self.assertEqual(self.public_client.get("/me", headers={"Authorization": f"Bearer {expired}"}).status_code, 401)

    def test_upload_validation_and_download_authorization_edges(self) -> None:
        owner = self._create_user("upload-owner")
        intruder = self._create_user("upload-intruder")
        owner_token = self._login_user(owner)["access_token"]
        intruder_token = self._login_user(intruder)["access_token"]
        task_id = self._create_active_task(owner["id"])
        png = self._png_bytes()

        invalid_mime = self.public_client.post(
            f"/announcements/{task_id}/media",
            headers={"Authorization": f"Bearer {owner_token}"},
            files={"files": ("image.png", png, "text/plain")},
        )
        self.assertEqual(invalid_mime.status_code, 400, invalid_mime.text)

        broken_image = self.public_client.post(
            f"/announcements/{task_id}/media",
            headers={"Authorization": f"Bearer {owner_token}"},
            files={"files": ("broken.png", b"\x89PNG\r\n\x1a\nnot-a-real-png", "image/png")},
        )
        self.assertEqual(broken_image.status_code, 400, broken_image.text)

        oversized = self.public_client.post(
            f"/announcements/{task_id}/media",
            headers={"Authorization": f"Bearer {owner_token}"},
            files={"files": ("huge.png", b"x" * (9 * 1024 * 1024), "image/png")},
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)

        path_traversal_download = self.public_client.get(
            f"/uploads/{task_id}/..%5Csecret.png",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(path_traversal_download.status_code, 400, path_traversal_download.text)

        fake_detector = SimpleNamespace(
            predict_bytes=lambda _content: SimpleNamespace(
                nsfw=0.0,
                sfw=1.0,
                top_label="safe",
                top_prob=1.0,
                infer_seconds=0.001,
            )
        )
        with patch("app.main.get_nsfw_detector", return_value=fake_detector):
            valid_upload = self.public_client.post(
                f"/announcements/{task_id}/media",
                headers={"Authorization": f"Bearer {owner_token}"},
                files={"files": ("..\\avatar.exe", png, "application/octet-stream")},
            )
        self.assertEqual(valid_upload.status_code, 200, valid_upload.text)
        media_item = valid_upload.json()["data"]["media"][0]
        self.assertTrue(media_item["filename"].endswith(".png"))
        self.assertNotIn("..", media_item["object_key"])
        self.assertNotIn("\\", media_item["object_key"])
        uploaded_path = (UPLOADS_DIR / media_item["object_key"]).resolve()
        self.upload_paths.append(uploaded_path)

        unauthorized = self.public_client.get(media_item["path"], headers={"Authorization": f"Bearer {intruder_token}"})
        self.assertEqual(unauthorized.status_code, 403, unauthorized.text)
        authorized = self.public_client.get(media_item["path"], headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(authorized.status_code, 200, authorized.text)
        self.assertEqual(authorized.content, png)

    def test_chat_rest_permissions_length_validation_and_websocket_rejections(self) -> None:
        owner = self._create_user("chat-owner")
        performer = self._create_user("chat-performer")
        intruder = self._create_user("chat-intruder")
        owner_token = self._login_user(owner)["access_token"]
        performer_token = self._login_user(performer)["access_token"]
        intruder_token = self._login_user(intruder)["access_token"]
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        own_message = self.public_client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"text": "Своя ветка"},
        )
        self.assertEqual(own_message.status_code, 201, own_message.text)

        foreign_thread_message = self.public_client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"text": "Чужая ветка"},
        )
        self.assertEqual(foreign_thread_message.status_code, 403, foreign_thread_message.text)

        too_long = self.public_client.post(
            f"/chats/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"text": "x" * 5001},
        )
        self.assertEqual(too_long.status_code, 422, too_long.text)

        self._assert_websocket_rejected(f"/ws/chats/{thread_id}", 4401)
        self._assert_websocket_rejected(f"/ws/chats/{thread_id}?token=invalid", 4401)
        self._assert_websocket_rejected(f"/ws/chats/{thread_id}?token={intruder_token}", 4403)

        with self.public_client.websocket_connect(f"ws://localhost/ws/chats/{thread_id}?token={performer_token}") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "ready")

    def test_support_thread_access_is_owner_scoped_and_admin_identity_stays_separate(self) -> None:
        owner = self._create_user("support-owner")
        other = self._create_user("support-other")
        staff_user = self._create_user("support-admin-user")
        admin = self._create_admin_account(linked_user_id=staff_user["id"], prefix="support-admin", role="support")
        owner_token = self._login_user(owner)["access_token"]
        other_token = self._login_user(other)["access_token"]
        admin_token = self._login_admin(admin)

        thread_response = self.public_client.get("/support/thread", headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(thread_response.status_code, 200, thread_response.text)
        thread_id = thread_response.json()["thread_id"]
        self.thread_ids.append(thread_id)

        own_messages = self.public_client.get(
            f"/support/thread/{thread_id}/messages",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(own_messages.status_code, 200, own_messages.text)

        foreign_support_messages = self.public_client.get(
            f"/support/thread/{thread_id}/messages",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(foreign_support_messages.status_code, 403, foreign_support_messages.text)

        admin_to_user_api = self.public_client.get("/support/thread", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(admin_to_user_api.status_code, 401, admin_to_user_api.text)

    def test_reports_valid_invalid_rate_limit_and_admin_audit_log(self) -> None:
        reporter = self._create_user("reporter")
        target = self._create_user("reported")
        staff_user = self._create_user("report-admin-user")
        admin = self._create_admin_account(linked_user_id=staff_user["id"], prefix="report-admin", role="moderator")
        reporter_token = self._login_user(reporter)["access_token"]
        admin_token = self._login_admin(admin)

        valid_report = self.public_client.post(
            "/reports",
            headers={"Authorization": f"Bearer {reporter_token}"},
            json={"target_type": "user", "target_id": target["id"], "reason_code": "spam"},
        )
        self.assertEqual(valid_report.status_code, 201, valid_report.text)
        report_id = valid_report.json()["id"]
        self.report_ids.append(report_id)

        invalid_target = self.public_client.post(
            "/reports",
            headers={"Authorization": f"Bearer {reporter_token}"},
            json={"target_type": "spaceship", "target_id": str(uuid.uuid4()), "reason_code": "spam"},
        )
        self.assertEqual(invalid_target.status_code, 400, invalid_target.text)

        async def always_limited(*_args, **_kwargs) -> None:
            from app.rate_limit import RateLimitError

            raise RateLimitError(retry_after=60)

        with patch("app.main.enforce_rate_limit", new=always_limited):
            limited = self.public_client.post(
                "/reports",
                headers={"Authorization": f"Bearer {reporter_token}"},
                json={"target_type": "user", "target_id": target["id"], "reason_code": "spam"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.headers.get("retry-after"), "60")

        resolve = self.admin_client.post(
            f"/admin/api/reports/{report_id}/resolve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"resolution": "valid", "moderator_comment": "confirmed by regression test"},
        )
        self.assertEqual(resolve.status_code, 200, resolve.text)
        audit_row = fetch_one(
            """
            SELECT 1
            FROM audit_logs
            WHERE actor_admin_account_id = %s::uuid
              AND action = 'report_resolve'
              AND target_type = 'report'
              AND target_id = %s
            """,
            (admin["id"], report_id),
        )
        self.assertIsNotNone(audit_row)

    def test_dispute_participant_permissions_counterparty_actions_and_final_acceptance(self) -> None:
        owner = self._create_user("dispute-owner")
        performer = self._create_user("dispute-performer")
        intruder = self._create_user("dispute-intruder")
        owner_token = self._login_user(owner)["access_token"]
        performer_token = self._login_user(performer)["access_token"]
        intruder_token = self._login_user(intruder)["access_token"]
        thread_id = self._create_direct_thread(owner["id"], performer["id"])

        intruder_open = self.public_client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={
                "problem_title": "Не должен открыть",
                "problem_description": "Пользователь не участник чата",
                "requested_compensation_rub": 100,
                "desired_resolution": "other",
            },
        )
        self.assertEqual(intruder_open.status_code, 403, intruder_open.text)

        open_response = self.public_client.post(
            f"/chats/{thread_id}/disputes/open",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "problem_title": "Спор по задаче",
                "problem_description": "Описание проблемы для regression coverage",
                "requested_compensation_rub": 1000,
                "desired_resolution": "partial_refund",
            },
        )
        self.assertEqual(open_response.status_code, 201, open_response.text)
        dispute_id = open_response.json()["id"]
        self.dispute_ids.append(dispute_id)

        owner_counterparty_accept = self.public_client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/counterparty/accept",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        self.assertEqual(owner_counterparty_accept.status_code, 403, owner_counterparty_accept.text)

        intruder_read = self.public_client.get(
            f"/chats/{thread_id}/disputes/active",
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
        self.assertEqual(intruder_read.status_code, 403, intruder_read.text)

        execute(
            """
            UPDATE disputes
            SET status = 'waiting_final_acceptance',
                selected_option_id = 'r1_opt_1',
                round1_options = %s::jsonb,
                moderator_hook = '{"final_acceptance_votes": {}, "selected_option_id": "r1_opt_1"}'::jsonb,
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (
                '[{"id":"r1_opt_1","lean":"compromise","title":"Компромисс","description":"Тестовый вариант","customer_action":"Принять","performer_action":"Принять","compensation_rub":500,"refund_percent":50,"resolution_kind":"partial_refund"}]',
                dispute_id,
            ),
        )

        intruder_final = self.public_client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {intruder_token}"},
            json={"accepted": True},
        )
        self.assertEqual(intruder_final.status_code, 403, intruder_final.text)

        owner_final = self.public_client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"accepted": True},
        )
        self.assertEqual(owner_final.status_code, 200, owner_final.text)
        self.assertEqual(owner_final.json()["final_acceptance_votes"]["customer"], "accepted")

        performer_final = self.public_client.post(
            f"/chats/{thread_id}/disputes/{dispute_id}/final-acceptance",
            headers={"Authorization": f"Bearer {performer_token}"},
            json={"accepted": True},
        )
        self.assertEqual(performer_final.status_code, 200, performer_final.text)
        self.assertEqual(performer_final.json()["status"], "resolved")

    def test_healthz_readyz_and_dependency_failure_paths(self) -> None:
        code = textwrap.dedent(
            """
            from unittest.mock import patch

            from fastapi.testclient import TestClient

            import app.runtime as runtime_module

            client = TestClient(runtime_module.app, base_url="http://localhost")

            health = client.get("/healthz")
            assert health.status_code == 200, health.text
            assert health.json()["status"] == "ok", health.text

            ready = client.get("/readyz")
            assert ready.status_code in {200, 503}, ready.text
            if ready.status_code == 200:
                assert ready.json()["status"] == "ready", ready.text

            with patch.object(runtime_module, "_db_ready", return_value=False), patch.object(runtime_module, "redis_url", return_value=""):
                db_down = client.get("/readyz")
            assert db_down.status_code == 503, db_down.text
            assert db_down.json()["detail"]["db"] is False, db_down.text

            async def redis_not_ready() -> bool:
                return False

            with (
                patch.object(runtime_module, "_db_ready", return_value=True),
                patch.object(runtime_module, "redis_url", return_value="redis://localhost:6379/0"),
                patch.object(runtime_module, "check_redis_ready", new=redis_not_ready),
            ):
                redis_down = client.get("/readyz")
            assert redis_down.status_code == 503, redis_down.text
            assert redis_down.json()["detail"]["redis"] is False, redis_down.text
            """
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
