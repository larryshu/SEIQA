"""對話列表分頁。

只套在 ConversationViewSet——只有對話會大量累積；agents/skills/platforms 等
設定類端點資料量小，維持不分頁。搭配 conv_list_idx：分頁產生的 LIMIT 讓 MySQL
沿索引照序讀、讀夠一頁就停，不必掃整張表。
"""
from __future__ import annotations

from rest_framework.pagination import PageNumberPagination


class ConversationPagination(PageNumberPagination):
    page_size = 50                      # 預設每頁筆數
    page_size_query_param = "page_size"  # 允許前端用 ?page_size=N 覆寫
    max_page_size = 200                  # 上限，避免被要求一次撈太多
