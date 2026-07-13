"""測試共用工具（放專案根目錄，檔名不符 test*.py，不會被 test runner 當測試模組蒐集）。

提供 admin / editor / viewer 三種角色的操作者，與切換身分的 as_role()。
RBAC 是後台的核心約束（accounts/permissions.py），幾乎每個模組的測試都要它。
"""
from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase


class RoleAPITestCase(APITestCase):
    """帶好三個角色使用者的 APITestCase 基底。"""

    @classmethod
    def setUpTestData(cls):
        cls.users: dict[str, User] = {}
        for role in ("admin", "editor", "viewer"):
            group, _ = Group.objects.get_or_create(name=role)
            user = User.objects.create_user(username=f"{role}_user", password="pw")
            user.groups.add(group)
            cls.users[role] = user
        # 登入了但沒被指派任何角色 —— 應該一律 403（不是 401，也不是放行）
        cls.roleless_user = User.objects.create_user(username="roleless_user", password="pw")

    def as_role(self, role: str) -> None:
        """以指定角色發後續請求。role 可為 admin / editor / viewer / roleless。"""
        user = self.roleless_user if role == "roleless" else self.users[role]
        self.client.force_authenticate(user=user)

    def as_anonymous(self) -> None:
        self.client.force_authenticate(user=None)

    def count_queries(self, url: str) -> int:
        """發一次 GET，回傳這次請求打了幾條 SQL（N+1 迴歸測試用）。"""
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return len(ctx.captured_queries)
