"""模組四 DRF：system-settings（全域）＋ end-users/{id}/preferences（每使用者）。"""
from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.audit import AuditLogMixin
from accounts.models import AuditLog, EndUser
from accounts.permissions import RoleBasedReadWrite
from common.serializers import as_item_list

from .models import SystemSetting, UserPreference
from .serializers import (
    SystemSettingSerializer,
    UserPreferenceSerializer,
    UserPreferenceUpsertSerializer,
)


class SystemSettingViewSet(AuditLogMixin, viewsets.ModelViewSet):
    queryset = SystemSetting.objects.all()
    serializer_class = SystemSettingSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "system_setting"
    lookup_field = "key"  # 用 key 當路徑參數（/system-settings/chat_model/）

    filterset_fields = ["group_name", "is_secret"]  # ?group_name=retrieval
    search_fields = ["key", "description"]

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

    @extend_schema(responses=UserPreferenceSerializer(many=True),
                   summary="這個使用者的所有偏好（含 source：人工設定 or LLM 推論）")
    def get(self, request, end_user_id):
        end_user = get_object_or_404(EndUser, pk=end_user_id)
        prefs = UserPreference.objects.filter(end_user=end_user)
        return Response(UserPreferenceSerializer(prefs, many=True).data)

    @extend_schema(
        request=UserPreferenceUpsertSerializer(many=True),
        responses=UserPreferenceSerializer(many=True),
        summary="以 key 為準 upsert 偏好",
        description="經由這支 API 寫入的一律標記 source=manual——runtime 的偏好推論"
                    "不會覆寫 manual，等於人工設定優先。",
    )
    def put(self, request, end_user_id):
        end_user = get_object_or_404(EndUser, pk=end_user_id)  # 不存在的人 → 404，不是 IntegrityError
        payload = UserPreferenceUpsertSerializer(
            data=as_item_list(request.data, "preferences"), many=True)
        payload.is_valid(raise_exception=True)  # 缺 key / 值對不起型別 → 400（原本 KeyError 變 500）
        items = payload.validated_data

        # 整批包一個交易：一次 PUT 是一個意圖，不該留下寫到一半的偏好。
        with transaction.atomic():
            for item in items:
                UserPreference.objects.update_or_create(
                    end_user=end_user, key=item["key"],
                    defaults={
                        "value": item["value"],
                        "value_type": item["value_type"],
                        # 走這支 API 就是人工設定，一定要標回 manual。
                        # runtime 登出時的偏好推論是靠 source='manual' 來「不覆寫人工設定」的
                        # （app/user_preference.py 的 ON DUPLICATE KEY UPDATE），
                        # 這裡若不標，人工改過的 inferred 偏好下次登出就會被 LLM 蓋回去。
                        "source": "manual",
                        "confidence": None,
                    },
                )
            AuditLog.objects.create(
                actor=request.user if request.user.is_authenticated else None,
                action="update", target_type="user_preference", target_id=str(end_user_id),
                changes={"preferences": items}, ip=request.META.get("REMOTE_ADDR"),
            )
        prefs = UserPreference.objects.filter(end_user=end_user)
        return Response(UserPreferenceSerializer(prefs, many=True).data)
