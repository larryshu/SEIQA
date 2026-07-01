"""稽核：DRF ViewSet 寫入動作自動記一筆 AuditLog（規格 §11）。

用法：讓 ViewSet 繼承 AuditLogMixin，並設 audit_target_type（可省，預設取 model 名）。
changes 取 request.data 並濾掉含 password/key/secret 的欄位，避免機密落進稽核。
"""
from __future__ import annotations

from .models import AuditLog

_SENSITIVE = ("password", "key", "secret", "token")


class AuditLogMixin:
    audit_target_type: str | None = None

    def _audit_changes(self) -> dict | None:
        data = getattr(self.request, "data", None)
        if not data:
            return None
        try:
            return {k: v for k, v in data.items()
                    if not any(s in k.lower() for s in _SENSITIVE)}
        except AttributeError:
            return None

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
        pk = instance.pk
        instance.delete()
        self._write_audit("delete", pk, None)
