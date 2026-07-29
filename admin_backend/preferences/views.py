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

    def _sensitive_fields(self):
        """is_secret 的設定，value 本身就是機密（例：API 金鑰）——稽核要遮掉（規格 §11.5）。

        但只遮這種：一般設定（chat_model=gpt-4.1）的值留在稽核裡才查得出「誰把模型換掉了」。
        key 一律保留——它是識別碼，遮掉就不知道改的是哪一條。
        """
        fields = super()._sensitive_fields()
        return fields | {"value"} if self._target_is_secret() else fields

    def _target_is_secret(self) -> bool:
        data = getattr(self.request, "data", None)
        flag = data.get("is_secret") if hasattr(data, "get") else None
        if flag is None:  # PATCH 沒帶 is_secret → 看 DB 現況
            key = self.kwargs.get(self.lookup_field)
            if not key:
                return False
            flag = SystemSetting.objects.filter(key=key).values_list(
                "is_secret", flat=True).first()
        return str(flag).strip().lower() in ("1", "true")

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
