"""模組一 DRF serializers。"""
from __future__ import annotations

from rest_framework import serializers

from common.serializers import TypedKeyValueSerializer, TypedValueField

from .models import Agent, Skill, SourceConfig, SourcePlatform


class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ["id", "name", "display_name", "description", "json_schema",
                  "handler_key", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class AgentSerializer(serializers.ModelSerializer):
    skills = serializers.SerializerMethodField()
    skill_ids = serializers.PrimaryKeyRelatedField(
        many=True, write_only=True, required=False, queryset=Skill.objects.all(),
    )

    class Meta:
        model = Agent
        fields = ["id", "name", "description", "system_prompt", "model", "temperature",
                  "max_tool_rounds", "is_active", "version", "skills", "skill_ids",
                  "created_at", "updated_at"]
        read_only_fields = ["is_active", "created_at", "updated_at"]  # is_active 改用 activate 動作

    def get_skills(self, obj) -> list[dict]:
        return [{"id": s.id, "name": s.name} for s in obj.skills.all()]

    def create(self, validated_data):
        skill_ids = validated_data.pop("skill_ids", None)
        agent = super().create(validated_data)
        if skill_ids is not None:
            agent.skills.set(skill_ids)
        return agent

    def update(self, instance, validated_data):
        skill_ids = validated_data.pop("skill_ids", None)
        agent = super().update(instance, validated_data)
        if skill_ids is not None:
            agent.skills.set(skill_ids)
        return agent


class SourceConfigSerializer(serializers.ModelSerializer):
    """輸出用（GET / 巢狀在平台底下）。"""

    class Meta:
        model = SourceConfig
        fields = ["id", "key", "value", "value_type"]


class SourceConfigUpsertSerializer(TypedKeyValueSerializer):
    """輸入用：PUT /source-platforms/{id}/configs/ 的單筆。value 對齊 SourceConfig.value 的 255。"""

    value = TypedValueField(max_length=255)


class SourcePlatformSerializer(serializers.ModelSerializer):
    configs = SourceConfigSerializer(many=True, read_only=True)

    class Meta:
        model = SourcePlatform
        fields = ["id", "name", "display_name", "adapter_key", "kind", "is_active",
                  "sort_order", "configs", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]
