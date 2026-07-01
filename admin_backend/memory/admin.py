"""模組三 Django Admin：conversation（含 message inline）、memory_collection。"""
from django.contrib import admin

from .models import Conversation, MemoryCollection, Message
from .sync import sync_collection


@admin.action(description="軟刪選取的對話（標記 is_deleted）")
def soft_delete_conversations(modeladmin, request, queryset):
    n = queryset.update(is_deleted=True)
    modeladmin.message_user(request, f"已軟刪 {n} 段對話。")


@admin.action(description="從 Qdrant 同步統計")
def sync_selected_collections(modeladmin, request, queryset):
    for col in queryset:
        r = sync_collection(col)
        modeladmin.message_user(
            request, f"{col.name}：status={r.get('status')}、points={r.get('point_count')}")


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    readonly_fields = ("role", "content", "used_tools", "sources", "created_at")
    fields = ("role", "content", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "sid", "title", "end_user", "agent", "message_count",
                    "last_active_at", "is_deleted")
    list_filter = ("is_deleted", "agent")
    search_fields = ("sid", "title")
    readonly_fields = ("sid", "message_count", "created_at", "updated_at", "last_active_at")
    # date_hierarchy 需 MySQL 載入時區資料表(CONVERT_TZ 才認得 'Asia/Taipei')，否則
    # 列表頁會 ValueError。要恢復日期下鑽：載入 MySQL tz 表後把下行取消註解即可。
    # date_hierarchy = "last_active_at"
    inlines = [MessageInline]
    actions = [soft_delete_conversations]


@admin.register(MemoryCollection)
class MemoryCollectionAdmin(admin.ModelAdmin):
    list_display = ("name", "display_name", "kind", "is_readonly",
                    "point_count", "vector_size", "status", "last_synced_at")
    list_filter = ("kind", "is_readonly", "status")
    search_fields = ("name", "display_name")
    readonly_fields = ("point_count", "vector_size", "status", "last_synced_at")
    actions = [sync_selected_collections]
