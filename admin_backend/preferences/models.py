"""模組四（部分提前到 M2/M3）：system_setting — 取代 app/config.py 的全域業務設定。

注意：欄位名用 group_name（DB 欄位同名），不用 `group`——GROUP 是 MySQL 保留字，
避免 FastAPI runtime 之後用 raw SQL 讀時還要特別跳脫。
per-user 偏好（user_preference）留到 M5。
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class SystemSetting(models.Model):
    """全域系統設定（key-value typed）。對應規格 §6.4。"""

    key = models.CharField(max_length=64, unique=True)  # chat_model / search_min_score ...
    value = models.CharField(max_length=512)
    value_type = models.CharField(max_length=16, default="str")  # int/float/str/bool/json
    group_name = models.CharField(max_length=32, default="general")  # llm/retrieval/crawler/ptt/general
    description = models.CharField(max_length=255, blank=True)
    is_secret = models.BooleanField(default=False)  # 顯示時遮罩
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="updated_settings",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "system_setting"
        ordering = ["group_name", "key"]

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class UserPreference(models.Model):
    """每使用者偏好覆寫（M5）。runtime 取值優先序：user_preference > agent > system_setting。

    常見 key：tone（語氣）、answer_length（答案長度）、language（語言）、model（指定模型）、
    included_platforms / excluded_platforms（限定/排除平台，value_type=json，存平台 name 清單）。
    """

    end_user = models.ForeignKey(
        "accounts.EndUser", on_delete=models.CASCADE, related_name="preferences",
    )
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=512)
    value_type = models.CharField(max_length=16, default="str")  # str/int/float/bool/json
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_preference"
        unique_together = ("end_user", "key")

    def __str__(self) -> str:
        return f"{self.end_user_id}.{self.key}={self.value}"
