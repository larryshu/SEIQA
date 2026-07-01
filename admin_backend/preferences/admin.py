"""模組四 Django Admin：system_setting、user_preference。"""
from django.contrib import admin

from accounts.admin_actions import reload_runtime

from .models import SystemSetting, UserPreference


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "masked_value", "value_type", "group_name", "is_secret", "updated_at")
    list_filter = ("group_name", "value_type", "is_secret")
    search_fields = ("key", "description")
    readonly_fields = ("updated_at",)
    actions = [reload_runtime]

    @admin.display(description="value")
    def masked_value(self, obj):
        return "••••••（secret）" if (obj.is_secret and obj.value) else obj.value


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("id", "end_user", "key", "value", "value_type", "updated_at")
    list_filter = ("key", "value_type")
    search_fields = ("end_user__username", "key")
    autocomplete_fields = ["end_user"]
