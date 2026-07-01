"""共用的 Django Admin action。"""
import urllib.request

from django.conf import settings
from django.contrib import admin, messages


@admin.action(description="通知 runtime 立即重載設定（即時生效）")
def reload_runtime(modeladmin, request, queryset):
    """打 runtime 的 /internal/reload-config，讓剛改的設定不必等 TTL 立刻生效。

    這是全域動作（與選取的列無關）；Django action 需先選至少一列才能執行，選任一列即可。
    """
    url = settings.RUNTIME_URL.rstrip("/") + "/internal/reload-config"
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=5)
        modeladmin.message_user(request, "已通知 runtime 重載設定（即時生效）。")
    except Exception as e:  # noqa: BLE001
        modeladmin.message_user(
            request, f"通知 runtime 失敗（runtime 沒啟動？）：{e}", level=messages.WARNING)
