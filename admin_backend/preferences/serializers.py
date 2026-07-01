"""模組四 DRF serializers。"""
from __future__ import annotations

from rest_framework import serializers

from .models import SystemSetting, UserPreference


class SystemSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSetting
        fields = ["id", "key", "value", "value_type", "group_name",
                  "description", "is_secret", "updated_at"]
        read_only_fields = ["updated_at"]


class UserPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPreference
        fields = ["id", "end_user", "key", "value", "value_type", "updated_at"]
        read_only_fields = ["updated_at"]
