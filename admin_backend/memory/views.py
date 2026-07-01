"""模組三 DRF viewsets（對應規格 §7.4）。

conversation：列表/單筆/軟刪/messages/export/purge。
memory_collection：列表/單筆/sync（打 Qdrant 更新 metadata）。
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.audit import AuditLogMixin
from accounts.permissions import IsAdminRole, RoleBasedReadWrite

from .models import Conversation, MemoryCollection
from .pagination import ConversationPagination
from .serializers import (
    ConversationSerializer,
    MemoryCollectionSerializer,
    MessageSerializer,
)
from .sync import sync_collection


class ConversationViewSet(AuditLogMixin, mixins.ListModelMixin,
                          mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                          viewsets.GenericViewSet):
    queryset = Conversation.objects.all()
    serializer_class = ConversationSerializer
    permission_classes = [RoleBasedReadWrite]  # 讀=viewer+；刪=editor+
    pagination_class = ConversationPagination   # 只有對話列表分頁（每頁 50，?page=/?page_size=）
    audit_target_type = "conversation"

    def get_queryset(self):
        # 列表不顯示已軟刪的對話（正確性 + 配 conv_list_idx 走索引）；
        # retrieve/export/messages 仍可存取單筆（含已軟刪，供清理前檢視）。
        qs = super().get_queryset()
        return qs.filter(is_deleted=False) if self.action == "list" else qs

    def perform_destroy(self, instance):
        instance.is_deleted = True  # 軟刪
        instance.save(update_fields=["is_deleted"])
        self._write_audit("delete", instance.pk, {"soft": True})

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        conv = self.get_object()
        return Response(MessageSerializer(conv.messages.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        conv = self.get_object()
        return Response({
            "sid": conv.sid,
            "title": conv.title,
            "messages": MessageSerializer(conv.messages.all(), many=True).data,
        })

    @action(detail=False, methods=["post"], permission_classes=[IsAdminRole])
    def purge(self, request):
        """硬刪『已軟刪』或『已過期(expires_at < now)』的對話。需 admin。"""
        now = timezone.now()
        qs = Conversation.objects.filter(Q(is_deleted=True) | Q(expires_at__lt=now))
        count = qs.count()
        qs.delete()
        self._write_audit("delete", "purge", {"purged": count})
        return Response({"purged": count})


class MemoryCollectionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                              viewsets.GenericViewSet):
    queryset = MemoryCollection.objects.all()
    serializer_class = MemoryCollectionSerializer
    permission_classes = [RoleBasedReadWrite]

    @action(detail=True, methods=["post"])
    def sync(self, request, pk=None):
        """打 Qdrant 更新 metadata（共用 memory.sync.sync_collection）。"""
        return Response(sync_collection(self.get_object()))
