"""模組二測試：終端使用者註冊 / 登入的輸入驗證、JWT 內容、操作者 /auth/me。

重點在「400 與 401 要分得開」：沒填欄位是 400（輸入錯），帳密不對是 401（認證失敗）。
前端（Streamlit / demo 頁）就是靠這條線決定要顯示「請輸入帳號密碼」還是「帳號或密碼錯誤」。
"""
from __future__ import annotations

import jwt
from django.test import override_settings
from rest_framework.test import APITestCase

from testutils import RoleAPITestCase

from .models import EndUser

REGISTER_URL = "/api/v1/end-auth/register/"
LOGIN_URL = "/api/v1/end-auth/login/"
ME_URL = "/api/v1/auth/me/"

TEST_SECRET = "test-token-secret-at-least-32-bytes-long"  # 不依賴 .env，測試才能獨立跑


@override_settings(TOKEN_SECRET=TEST_SECRET)
class EndUserRegisterTests(APITestCase):
    def test_register_returns_201_with_usable_token(self):
        resp = self.client.post(REGISTER_URL, {"username": "alice", "password": "pw123456"},
                                format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

        user = EndUser.objects.get(username="alice")
        self.assertEqual(resp.data["end_user_id"], user.id)
        self.assertEqual(resp.data["display_name"], "alice")  # 沒給 display_name → 補成 username
        self.assertTrue(user.check_password("pw123456"))
        self.assertNotIn("password", resp.data)  # 密碼是 write_only，不該回傳

        payload = jwt.decode(resp.data["token"], TEST_SECRET, algorithms=["HS256"])
        self.assertEqual(payload["end_user_id"], user.id)
        self.assertEqual(payload["type"], "end_user")  # runtime 靠 type 區分終端使用者與操作者

    def test_missing_password_returns_400_with_both_detail_and_field_errors(self):
        resp = self.client.post(REGISTER_URL, {"username": "bob"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data["errors"])  # 欄位級錯誤：給程式用
        self.assertTrue(resp.data["detail"])            # 壓平的訊息：給既有前端顯示用
        self.assertFalse(EndUser.objects.filter(username="bob").exists())

    def test_blank_username_returns_400(self):
        resp = self.client.post(REGISTER_URL, {"username": "   ", "password": "pw123456"},
                                format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("username", resp.data["errors"])

    def test_duplicate_username_returns_400(self):
        EndUser.objects.create(username="alice")

        resp = self.client.post(REGISTER_URL, {"username": "alice", "password": "pw123456"},
                                format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("username", resp.data["errors"])

    def test_duplicate_email_returns_400_not_500(self):
        # email 在 model 上是 unique：驗證層不擋的話，這裡會是 IntegrityError → 500
        EndUser.objects.create(username="alice", email="a@example.com")

        resp = self.client.post(
            REGISTER_URL,
            {"username": "bob", "password": "pw123456", "email": "a@example.com"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("email", resp.data["errors"])

    def test_malformed_email_returns_400(self):
        resp = self.client.post(
            REGISTER_URL,
            {"username": "bob", "password": "pw123456", "email": "not-an-email"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("email", resp.data["errors"])


@override_settings(TOKEN_SECRET=TEST_SECRET)
class EndUserLoginTests(APITestCase):
    def setUp(self):
        self.user = EndUser(username="alice", display_name="Alice", status="active")
        self.user.set_password("pw123456")
        self.user.save()

    def test_login_success_returns_token_and_stamps_last_login(self):
        self.assertIsNone(self.user.last_login_at)

        resp = self.client.post(LOGIN_URL, {"username": "alice", "password": "pw123456"},
                                format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["display_name"], "Alice")
        jwt.decode(resp.data["token"], TEST_SECRET, algorithms=["HS256"])  # 簽章對得起來
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login_at)

    def test_wrong_password_returns_401(self):
        resp = self.client.post(LOGIN_URL, {"username": "alice", "password": "nope"},
                                format="json")

        self.assertEqual(resp.status_code, 401)

    def test_unknown_user_returns_401(self):
        resp = self.client.post(LOGIN_URL, {"username": "ghost", "password": "pw123456"},
                                format="json")

        self.assertEqual(resp.status_code, 401)

    def test_disabled_user_cannot_login(self):
        self.user.status = "disabled"
        self.user.save(update_fields=["status"])

        resp = self.client.post(LOGIN_URL, {"username": "alice", "password": "pw123456"},
                                format="json")

        self.assertEqual(resp.status_code, 401)

    def test_missing_password_is_400_not_401(self):
        """沒填密碼是「輸入錯」不是「認證失敗」——前端要能分開提示。"""
        resp = self.client.post(LOGIN_URL, {"username": "alice"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.data["errors"])


class MeViewTests(RoleAPITestCase):
    def test_anonymous_is_rejected(self):
        self.as_anonymous()

        self.assertEqual(self.client.get(ME_URL).status_code, 401)

    def test_returns_roles_of_current_operator(self):
        self.as_role("editor")

        resp = self.client.get(ME_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["username"], "editor_user")
        self.assertEqual(resp.data["roles"], ["editor"])

    def test_superuser_is_treated_as_admin(self):
        viewer = self.users["viewer"]  # 借 viewer 帳號升成 superuser
        viewer.is_superuser = True
        viewer.save(update_fields=["is_superuser"])
        self.as_role("viewer")

        resp = self.client.get(ME_URL)

        self.assertIn("admin", resp.data["roles"])  # roles_of() 對 superuser 補上 admin
