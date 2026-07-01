"""模組二：帳戶 — 終端使用者、API 金鑰、稽核紀錄。

操作者沿用 Django 內建 auth_user / Group（RBAC 用 Group），不另建表。
表名以 db_table 對齊 docs/admin_backend_spec.md §6.1，方便 FastAPI runtime 直接讀。
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models


class EndUser(models.Model):
    """終端使用者（聊天的人）。密碼以 Django hasher 雜湊存 password_hash。"""

    username = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128, blank=True)
    email = models.EmailField(max_length=254, null=True, blank=True, unique=True)
    password_hash = models.CharField(max_length=255, null=True, blank=True)
    auth_provider = models.CharField(max_length=32, default="local")  # local / google / ...
    status = models.CharField(max_length=16, default="active")  # active / disabled
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "end_user"

    def __str__(self) -> str:
        return self.username

    def set_password(self, raw: str) -> None:
        self.password_hash = make_password(raw)

    def check_password(self, raw: str) -> bool:
        return bool(self.password_hash) and check_password(raw, self.password_hash)


class ApiKey(models.Model):
    """API 金鑰：只存 hash + 顯示用前綴；明碼僅產生當下顯示一次。"""

    name = models.CharField(max_length=64)
    key_hash = models.CharField(max_length=255)
    prefix = models.CharField(max_length=12, blank=True)
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="api_keys",
    )
    scopes = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_key"

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def generate_raw() -> str:
        return "ca_" + secrets.token_urlsafe(32)

    def set_key(self, raw: str) -> None:
        self.prefix = raw[:11]
        self.key_hash = make_password(raw)

    def check_key(self, raw: str) -> bool:
        return check_password(raw, self.key_hash)


class AuditLog(models.Model):
    """後台寫入稽核。M2 起由 API 寫入動作自動記錄。"""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_logs",
    )
    action = models.CharField(max_length=32)        # create / update / delete / publish
    target_type = models.CharField(max_length=64)   # 'agent' / 'skill' / 'system_setting' ...
    target_id = models.CharField(max_length=64, blank=True)
    changes = models.JSONField(null=True, blank=True)  # before / after diff
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"
