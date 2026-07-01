"""模組一 Django Admin：agent（含 skill inline）、skill、source_platform（含 config inline）。"""
from django.contrib import admin, messages

from accounts.admin_actions import reload_runtime

from .models import Agent, AgentSkill, Skill, SourceConfig, SourcePlatform


@admin.action(description="設為啟用中的 agent（其餘自動停用）")
def make_active(modeladmin, request, queryset):
    if queryset.count() != 1:
        modeladmin.message_user(request, "請只選『一個』agent 來啟用。", level=messages.WARNING)
        return
    agent = queryset.first()
    Agent.objects.filter(is_active=True).exclude(pk=agent.pk).update(is_active=False)
    agent.is_active = True
    agent.save(update_fields=["is_active"])
    modeladmin.message_user(
        request, f"已啟用 agent：{agent.name}。記得再執行「通知 runtime 重載設定」讓它即時生效。")


class AgentSkillInline(admin.TabularInline):
    model = AgentSkill
    extra = 1
    autocomplete_fields = ["skill"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "model", "temperature", "max_tool_rounds",
                    "is_active", "version", "updated_at")
    list_filter = ("is_active", "model")
    search_fields = ("name", "description")
    inlines = [AgentSkillInline]
    actions = [make_active, reload_runtime]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # 維持「全系統只有一個 active」的不變式
        if obj.is_active:
            Agent.objects.filter(is_active=True).exclude(pk=obj.pk).update(is_active=False)


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "display_name", "handler_key", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "display_name")
    actions = [reload_runtime]


class SourceConfigInline(admin.TabularInline):
    model = SourceConfig
    extra = 1


@admin.register(SourcePlatform)
class SourcePlatformAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "display_name", "adapter_key", "kind", "is_active", "sort_order")
    list_filter = ("is_active", "kind")
    search_fields = ("name", "display_name")
    inlines = [SourceConfigInline]
    actions = [reload_runtime]
