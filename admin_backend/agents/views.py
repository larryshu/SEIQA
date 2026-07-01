"""模組一 DRF viewsets（對應規格 §7.2）。寫入動作經 AuditLogMixin 記稽核。"""
from __future__ import annotations

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.audit import AuditLogMixin
from accounts.permissions import RoleBasedReadWrite

from .models import Agent, Skill, SourceConfig, SourcePlatform
from .serializers import (
    AgentSerializer,
    SkillSerializer,
    SourceConfigSerializer,
    SourcePlatformSerializer,
)


class AgentViewSet(AuditLogMixin, viewsets.ModelViewSet):
    # prefetch_related("skills")：避免 AgentSerializer.get_skills 對每個 agent 各查一次 skills（N+1）
    queryset = Agent.objects.all().prefetch_related("skills").order_by("-is_active", "name")
    serializer_class = AgentSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "agent"

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """把這個 agent 設為唯一啟用；其餘全部停用。"""
        agent = self.get_object()
        with transaction.atomic():
            Agent.objects.filter(is_active=True).exclude(pk=agent.pk).update(is_active=False)
            agent.is_active = True
            agent.save(update_fields=["is_active"])
        self._write_audit("publish", agent.pk, {"is_active": True})
        return Response({"id": agent.pk, "name": agent.name, "is_active": True})

    @action(detail=True, methods=["post"], url_path="test-run")
    def test_run(self, request, pk=None):
        """試跑（樁）：M3 接通 runtime 後才會真的呼叫 agent。"""
        agent = self.get_object()
        return Response(
            {"detail": "試跑功能將於 M3（runtime 接線）完成後可用。",
             "agent": agent.name, "query": request.data.get("query", "")},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class SkillViewSet(AuditLogMixin, viewsets.ModelViewSet):
    queryset = Skill.objects.all().order_by("name")
    serializer_class = SkillSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "skill"


class SourcePlatformViewSet(AuditLogMixin, viewsets.ModelViewSet):
    # prefetch_related("configs")：避免巢狀 SourceConfigSerializer 對每個平台各查一次 configs（N+1）
    queryset = SourcePlatform.objects.all().prefetch_related("configs").order_by("sort_order")
    serializer_class = SourcePlatformSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "source_platform"

    @action(detail=True, methods=["get", "put"])
    def configs(self, request, pk=None):
        """GET 取得該平台所有參數；PUT 以送進來的清單 upsert（key 為準）。"""
        platform = self.get_object()
        if request.method == "GET":
            ser = SourceConfigSerializer(platform.configs.all(), many=True)
            return Response(ser.data)
        # PUT：[{key,value,value_type}, ...]
        items = request.data if isinstance(request.data, list) else request.data.get("configs", [])
        with transaction.atomic():
            for item in items:
                SourceConfig.objects.update_or_create(
                    platform=platform, key=item["key"],
                    defaults={"value": str(item.get("value", "")),
                              "value_type": item.get("value_type", "str")},
                )
        self._write_audit("update", platform.pk, {"configs": items})
        ser = SourceConfigSerializer(platform.configs.all(), many=True)
        return Response(ser.data)
