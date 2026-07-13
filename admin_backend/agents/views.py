"""模組一 DRF viewsets（對應規格 §7.2）。寫入動作經 AuditLogMixin 記稽核。"""
from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.audit import AuditLogMixin
from accounts.permissions import RoleBasedReadWrite
from common.serializers import as_item_list

from .models import Agent, Skill, SourceConfig, SourcePlatform
from .serializers import (
    AgentSerializer,
    SkillSerializer,
    SourceConfigSerializer,
    SourceConfigUpsertSerializer,
    SourcePlatformSerializer,
)


class AgentViewSet(AuditLogMixin, viewsets.ModelViewSet):
    # prefetch_related("skills")：避免 AgentSerializer.get_skills 對每個 agent 各查一次 skills（N+1）
    queryset = Agent.objects.all().prefetch_related("skills").order_by("-is_active", "name")
    serializer_class = AgentSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "agent"

    filterset_fields = ["is_active", "model"]
    search_fields = ["name", "description"]

    @extend_schema(
        request=None,
        responses=inline_serializer("AgentActivated", {
            "id": serializers.IntegerField(),
            "name": serializers.CharField(),
            "is_active": serializers.BooleanField(),
        }),
        summary="把這個 agent 設為唯一啟用的 agent",
        description="全系統同時只能有一個 is_active——這裡在一個交易裡把其餘全部停用。",
    )
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

    @extend_schema(
        request=None,
        responses={501: OpenApiResponse(description="尚未實作")},
        summary="試跑 agent（尚未實作）",
    )
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

    filterset_fields = ["is_active"]
    search_fields = ["name", "display_name", "description"]


class SourcePlatformViewSet(AuditLogMixin, viewsets.ModelViewSet):
    # prefetch_related("configs")：避免巢狀 SourceConfigSerializer 對每個平台各查一次 configs（N+1）
    queryset = SourcePlatform.objects.all().prefetch_related("configs").order_by("sort_order")
    serializer_class = SourcePlatformSerializer
    permission_classes = [RoleBasedReadWrite]
    audit_target_type = "source_platform"

    filterset_fields = ["is_active", "kind"]  # ?kind=live_crawl
    search_fields = ["name", "display_name"]

    @extend_schema(
        methods=["GET"],
        responses=SourceConfigSerializer(many=True),
        summary="這個平台的所有檢索參數",
    )
    @extend_schema(
        methods=["PUT"],
        request=SourceConfigUpsertSerializer(many=True),
        responses=SourceConfigSerializer(many=True),
        summary="以 key 為準 upsert 檢索參數",
        description="value 會依 value_type 先驗一次（說是 int 就得轉得成 int），"
                    "整批要嘛全寫、要嘛全不寫。",
    )
    @action(detail=True, methods=["get", "put"])
    def configs(self, request, pk=None):
        """GET 取得該平台所有參數；PUT 以送進來的清單 upsert（key 為準）。"""
        platform = self.get_object()
        if request.method == "GET":
            ser = SourceConfigSerializer(platform.configs.all(), many=True)
            return Response(ser.data)
        # PUT：[{key,value,value_type}, ...]（也接受 {"configs": [...]}）
        # 先過 serializer：缺 key、value 對不起 value_type、value 過長 → 400，
        # 而不是在迴圈裡 item["key"] 撞 KeyError 變成 500。
        payload = SourceConfigUpsertSerializer(
            data=as_item_list(request.data, "configs"), many=True)
        payload.is_valid(raise_exception=True)
        items = payload.validated_data
        with transaction.atomic():
            for item in items:
                SourceConfig.objects.update_or_create(
                    platform=platform, key=item["key"],
                    defaults={"value": item["value"], "value_type": item["value_type"]},
                )
        self._write_audit("update", platform.pk, {"configs": items})
        ser = SourceConfigSerializer(platform.configs.all(), many=True)
        return Response(ser.data)
