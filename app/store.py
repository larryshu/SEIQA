"""FreshStore：live 抓到的資料放哪的「抽象層」——greenfield 的關鍵。

業務邏輯只認 FreshStore 介面，要從方案 A 換到方案 B 只是換實作、不動 agent。
- SessionFreshStore（方案 A，預設）：放當次 session 記憶體，用完即丟，零持久化地雷。
- QdrantHotStore（方案 B，預留）：寫獨立 hot collection 做向量檢索，跟主庫分離。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .config import settings
from .crawler import Post


class FreshStore(ABC):
    @abstractmethod
    def save(self, session_id: str, posts: list[Post]) -> None:
        """把 live 抓到的貼文寫入。"""

    @abstractmethod
    def search(self, session_id: str, query: str, top_k: int = 5) -> list[Post]:
        """從已存的 live 資料找出與 query 相關的貼文。"""


class SessionFreshStore(FreshStore):
    """方案 A：純記憶體、依 session 隔離。簡單關鍵字命中即可，不需 embedding。"""

    def __init__(self) -> None:
        self._by_session: dict[str, list[Post]] = {}

    def save(self, session_id: str, posts: list[Post]) -> None:
        bucket = self._by_session.setdefault(session_id, [])
        seen = {p["url"] for p in bucket}
        bucket.extend(p for p in posts if p["url"] not in seen)  # 同 session 內去重

    def search(self, session_id: str, query: str, top_k: int = 5) -> list[Post]:
        bucket = self._by_session.get(session_id, [])
        terms = [t for t in query.lower().split() if t]

        def score(p: Post) -> int:
            text = (p["title"] + " " + p["content"]).lower()
            return sum(text.count(t) for t in terms)

        ranked = sorted(bucket, key=score, reverse=True)
        return [p for p in ranked if score(p) > 0][:top_k] or bucket[:top_k]


class QdrantHotStore(FreshStore):
    """方案 B（預留）：要展示「記憶累積、越用越強」時才實作。

    待辦：connect Qdrant(settings.qdrant_url) → upsert((url, chunk) 為唯一鍵) →
    向量檢索。注意 dcard_insight 記憶裡列的 upsert 地雷（孤兒清理 / content-hash 去重 /
    半套殘留），所以才跟主 collection 分開、獨立 hot collection。
    """

    def save(self, session_id: str, posts: list[Post]) -> None:
        raise NotImplementedError("方案 B 尚未實作；要持久化累積記憶時再開。")

    def search(self, session_id: str, query: str, top_k: int = 5) -> list[Post]:
        raise NotImplementedError("方案 B 尚未實作；要持久化累積記憶時再開。")


def get_store() -> FreshStore:
    if settings.fresh_store == "qdrant":
        return QdrantHotStore()
    return SessionFreshStore()


# 單例：整個 app 共用一份（方案 A 的記憶體 bucket 才不會每次 new）
store: FreshStore = get_store()
