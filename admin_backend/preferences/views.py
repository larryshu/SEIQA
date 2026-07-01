"""模組四 DRF：system-settings（全域）＋ end-users/{id}/preferences（每使用者）。"""
from __future__ import annotations

from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.audit import AuditLogMixin
from accounts.models import AuditLog
from accounts.permissions import RoleBasedReadWrite

from .models import SystemSetting, UserPreference
from .serializers import SystemSettingSerializer, UserPreferenceSerializer


class SystemSettingViewSet(AuditLogMixin, viewsets.ModelViewSet):
    queryset = SystemSetting.objects.all()
    serializer_class = SystemSettingSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "system_setting"
    lookup_field = "key"  # 用 key 當路徑參數（/system-settings/chat_model/）

    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user if self.request.user.is_authenticated else None)
        self._write_audit("create", serializer.instance.key, self._audit_changes())

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user if self.request.user.is_authenticated else None)
        self._write_audit("update", serializer.instance.key, self._audit_changes())


class EndUserPreferencesView(APIView):
    """GET/PUT /api/v1/end-users/{id}/preferences/（對應規格 §7.5）。

    PUT body：[{"key","value","value_type"}, ...]，以 key 為準 upsert。
    """

    permission_classes = [RoleBasedReadWrite]

    def get(self, request, end_user_id):
        prefs = UserPreference.objects.filter(end_user_id=end_user_id)
        return Response(UserPreferenceSerializer(prefs, many=True).data)

    def put(self, request, end_user_id):
        items = request.data if isinstance(request.data, list) else request.data.get("preferences", [])
        for item in items:
            UserPreference.objects.update_or_create(
                end_user_id=end_user_id, key=item["key"],
                defaults={"value": str(item.get("value", "")),
                          "value_type": item.get("value_type", "str")},
            )
        AuditLog.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            action="update", target_type="user_preference", target_id=str(end_user_id),
            changes={"preferences": items}, ip=request.META.get("REMOTE_ADDR"),
        )
        prefs = UserPreference.objects.filter(end_user_id=end_user_id)
        return Response(UserPreferenceSerializer(prefs, many=True).data)
