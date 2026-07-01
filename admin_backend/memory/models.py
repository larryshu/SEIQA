"""模組三：記憶 — 對話（conversation）、訊息（message）、Qdrant collection metadata。

對話由 runtime（FastAPI）寫入（M4 步驟一：只寫、前端不動）；後台只做檢視/匯出/清理。
向量本體在 Qdrant，MySQL 只存 memory_collection 的 metadata/統計。表名對齊規格 §6.3。
"""
from __future__ import annotations

from django.db import models


class Conversation(models.Model):
    """一段對話（= 一個 Streamlit sid）。匿名時 end_user 為 NULL。"""

    sid = models.CharField(max_length=32, unique=True)
    end_user = models.ForeignKey(
        "accounts.EndUser", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="conversations",
    )
    agent = models.ForeignKey(
        "agents.Agent", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="conversations",
    )
    title = models.CharField(max_length=255, blank=True)
    message_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_active_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)  # TTL；過期可清
    is_deleted = models.BooleanField(default=False)  # 軟刪

    class Meta:
        db_table = "conversation"
        ordering = ["-last_active_at", "-created_at"]
        indexes = [
            models.Index(fields=["end_user"]),
            models.Index(fields=["expires_at"]),
            # 對齊列表查詢 filter(is_deleted=False).order_by(-last_active_at, -created_at)：
            # 讓 MySQL 直接走索引取序，免去 Using filesort + 全表掃描。
            models.Index(fields=["is_deleted", "-last_active_at", "-created_at"],
                         name="conv_list_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.sid} ({self.title[:20]})"


class Message(models.Model):
    """對話中的一則訊息。"""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16)  # user / assistant / system / tool
    content = models.TextField()
    used_tools = models.JSONField(null=True, blank=True)   # ["community_search"]
    sources = models.JSONField(null=True, blank=True)      # [{title,url,source,created_at}]
    token_usage = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "message"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"[{self.role}] {self.content[:30]}"


class MemoryCollection(models.Model):
    """Qdrant collection 的 metadata / 統計（檢視用；向量本體不進 MySQL）。"""

    name = models.CharField(max_length=64, unique=True)  # Qdrant collection 名
    display_name = models.CharField(max_length=128, blank=True)
    kind = models.CharField(max_length=16, default="dcard")  # dcard / hot（hot 預留）
    is_readonly = models.BooleanField(default=True)
    point_count = models.IntegerField(null=True, blank=True)
    vector_size = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, blank=True)  # green / red / unknown
    last_synced_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "memory_collection"

    def __str__(self) -> str:
        return self.name
