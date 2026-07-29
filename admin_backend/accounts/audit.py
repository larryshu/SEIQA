"""稽核：DRF ViewSet 寫入動作自動記一筆 AuditLog（規格 §11）。

用法：讓 ViewSet 繼承 AuditLogMixin，並設 audit_target_type（可省，預設取 model 名）。
changes 取 request.data；機密欄位只遮掉「值」，欄位名保留——稽核要答得出「他動了什麼」，
只是不記內容。要再加機密欄位的 ViewSet 覆寫 audit_sensitive_fields 或 _sensitive_fields()。
"""
from __future__ import annotations

from .models import AuditLog

REDACTED = "***"

# 欄位名【精確比對】。不要用「包含」：key 是識別碼、不是機密，用包含比對會把
# system_setting.key、skill.handler_key、source_platform.adapter_key 一起誤殺，
# 稽核就再也看不出「改的是哪一個設定」——而真正的機密（value）反而留著。
_SENSITIVE_FIELDS = frozenset({
    "password", "password_hash", "old_password", "new_password",
    "secret", "client_secret",
    "token", "access", "refresh",
    "api_key", "key_hash", "raw_key",
})


class AuditLogMixin:
    audit_target_type: str | None = None
    audit_sensitive_fields: frozenset[str] = frozenset()  # 各 ViewSet 可再追加

    def _sensitive_fields(self) -> frozenset[str]:
        """這次請求要遮罩哪些欄位。子類可覆寫成依請求內容動態決定。"""
        return _SENSITIVE_FIELDS | self.audit_sensitive_fields

    def _audit_changes(self) -> dict | None:
        data = getattr(self.request, "data", None)
        if not data:
            return None
        try:
            items = list(data.items())
        except AttributeError:  # 批次 PUT 的 body 是 list，沒有 .items()
            return None
        sensitive = self._sensitive_fields()
        return {k: (REDACTED if k.lower() in sensitive else v) for k, v in items}

    def _write_audit(self, action: str, target_id, changes: dict | None) -> None:
        user = getattr(self.request, "user", None)
        AuditLog.objects.create(
            actor=user if (user and user.is_authenticated) else None,
            action=action,
            target_type=self.audit_target_type or self.queryset.model.__name__.lower(),
            target_id=str(target_id),
            changes=changes,
            ip=self.request.META.get("REMOTE_ADDR"),
        )

    def perform_create(self, serializer):
        obj = serializer.save()
        self._write_audit("create", obj.pk, self._audit_changes())

    def perform_update(self, serializer):
        obj = serializer.save()
        self._write_audit("update", obj.pk, self._audit_changes())

    def perform_destroy(self, instance):
        # 先抄 pk：Django 的 delete() 會把 instance.pk 設成 None
        # （django/db/models/deletion.py 的 setattr(instance, pk.attname, None)），
        # 刪完再讀就只剩一排「刪除了 None」的廢稽核。
        pk = instance.pk
        instance.delete()
        self._write_audit("delete", pk, None)
