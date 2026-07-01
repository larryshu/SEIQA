"""模組二的 Django Admin 介面（M1 主要管理入口）。"""
from django.contrib import admin

from .models import ApiKey, AuditLog, EndUser


@admin.register(EndUser)
class EndUserAdmin(admin.ModelAdmin):
    list_display = ("id", "username", "display_name", "email", "auth_provider",
                    "status", "created_at", "last_login_at")
    list_filter = ("status", "auth_provider")
    search_fields = ("username", "display_name", "email")
    readonly_fields = ("password_hash", "created_at", "updated_at", "last_login_at")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "prefix", "owner_user", "is_active",
                    "created_at", "expires_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "prefix")
    readonly_fields = ("prefix", "key_hash", "created_at", "last_used_at")

    def save_model(self, request, obj, form, change):
        # 新增時自動產生金鑰：只存 hash，明碼用訊息顯示一次
        if not change and not obj.key_hash:
            raw = ApiKey.generate_raw()
            obj.set_key(raw)
            super().save_model(request, obj, form, change)
            self.message_user(request, f"金鑰已產生，請立即複製（只顯示這一次）：{raw}")
        else:
            super().save_model(request, obj, form, change)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "actor", "action", "target_type", "target_id", "ip")
    list_filter = ("action", "target_type")
    search_fields = ("target_id",)
    readonly_fields = ("actor", "action", "target_type", "target_id", "changes", "ip", "created_at")

    def has_add_permission(self, request):
        return False  # 稽核只由系統寫入

    def has_change_permission(self, request, obj=None):
        return False  # 唯讀
