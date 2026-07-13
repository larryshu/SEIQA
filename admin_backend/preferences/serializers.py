"""模組四 DRF serializers。"""
from __future__ import annotations

from rest_framework import serializers

from common.serializers import TypedKeyValueSerializer, TypedValueField

from .models import SystemSetting, UserPreference


class SystemSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["id", "key", "value", "value_type", "group_name",
                  "description", "is_secret", "updated_at"]
        read_only_fields = ["updated_at"]


class UserPreferenceSerializer(serializers.ModelSerializer):
    """輸出用。source/confidence 一起吐出來，後台才看得出這條是人設的還是 LLM 推論的。"""

    class Meta:
        model = UserPreference
        fields = ["id", "end_user", "key", "value", "value_type",
                  "source", "confidence", "updated_at"]
        read_only_fields = ["updated_at"]


class UserPreferenceUpsertSerializer(TypedKeyValueSerializer):
    """輸入用：PUT /end-users/{id}/preferences/ 的單筆。value 對齊 UserPreference.value 的 512。"""

    value = TypedValueField(max_length=512)
