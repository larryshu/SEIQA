"""模組三 DRF serializers。"""
from __future__ import annotations

from rest_framework import serializers

from .models import Conversation, MemoryCollection, Message


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ["id", "role", "content", "used_tools", "sources", "created_at"]


class ConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ["id", "sid", "end_user", "agent", "title", "message_count",
                  "created_at", "updated_at", "last_active_at", "expires_at", "is_deleted"]


class MemoryCollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemoryCollection
        fields = ["id", "name", "display_name", "kind", "is_readonly",
                  "point_count", "vector_size", "status", "last_synced_at", "note"]
