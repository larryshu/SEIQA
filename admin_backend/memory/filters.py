"""對話列表的過濾條件。

只有對話會無限累積，所以也只有它需要過濾——設定類的端點資料量小，掃全表就好。
時間區間、匿名與否沒辦法用 filterset_fields 一行帶過（要 lookup_expr），所以獨立成一個 FilterSet。
"""
from __future__ import annotations

import django_filters

from .models import Conversation


class ConversationFilter(django_filters.FilterSet):
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")
    active_after = django_filters.IsoDateTimeFilter(field_name="last_active_at", lookup_expr="gte")
    # 匿名對話（沒登入就問的）：end_user 是 NULL
    anonymous = django_filters.BooleanFilter(field_name="end_user", lookup_expr="isnull")
    # 快過期的：拿來預覽 purge 會清掉哪些
    expires_before = django_filters.IsoDateTimeFilter(field_name="expires_at", lookup_expr="lt")

    class Meta:
        model = Conversation
        fields = ["end_user", "agent", "created_after", "created_before",
                  "active_after", "anonymous", "expires_before"]
