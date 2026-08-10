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

    @abstractmethod
    def all(self, session_id: str) -> list[Post]:
        """這個 session 目前存了哪些貼文（依存入順序、跨輪累積）。

        注意這是「整個 session 的全部」，跨話題混在一起——要給追問統計用的是 latest()，
        不是這個。詳見 latest() 的說明。
        """

    @abstractmethod
    def latest(self, session_id: str) -> list[Post]:
        """最近一次存進來的那批貼文（＝上一輪 community_search 撈回的東西）。

        給「追問」用：使用者說「根據上面的結論畫個圖」時，那一輪不會再爬一次，
        但上一輪抓到的貼文還在這裡——沒有這條讀回的路，追問就只能眼睜睜看著資料在手邊卻用不到。

        為什麼不能用 all()：all() 是整個 session 的累積，同一場對話問過幾個話題就會混幾個
        話題的貼文。實際踩到的狀況是——先問「輝達進駐北士科」、再問「慈濟被詐騙的立場占比，
        請畫圖」，模型認為手上已有資料就直接叫 stance_breakdown，fallback 讀 all() 把輝達那批
        也一起撈出來，於是輝達的政治口水文被判讀成慈濟議題的「政治口水」，把圓餅圖灌到 42%，
        來源清單也冒出一堆黃仁勳。統計結果被汙染比查不到資料更糟——那是看起來有憑有據的錯。
        """


class SessionFreshStore(FreshStore):
    """方案 A：純記憶體、依 session 隔離。簡單關鍵字命中即可，不需 embedding。"""

    def __init__(self) -> None:
        self._by_session: dict[str, list[Post]] = {}
        self._latest: dict[str, list[Post]] = {}  # session -> 最近一次存進來的那批

    def save(self, session_id: str, posts: list[Post]) -> None:
        bucket = self._by_session.setdefault(session_id, [])
        seen = {p["url"] for p in bucket}
        bucket.extend(p for p in posts if p["url"] not in seen)  # 同 session 內去重
        # latest 記「這一輪原本撈到什麼」，不套上面的跨輪去重：同一輪的結果要完整，
        # 否則重問同一題時（貼文多半重複）latest 會變成空的，追問畫圖就沒東西可用。
        self._latest[session_id] = list(posts)

    def search(self, session_id: str, query: str, top_k: int = 5) -> list[Post]:
        bucket = self._by_session.get(session_id, [])
        terms = [t for t in query.lower().split() if t]

        def score(p: Post) -> int:
            text = (p["title"] + " " + p["content"]).lower()
            return sum(text.count(t) for t in terms)

        ranked = sorted(bucket, key=score, reverse=True)
        return [p for p in ranked if score(p) > 0][:top_k] or bucket[:top_k]

    def all(self, session_id: str) -> list[Post]:
        return list(self._by_session.get(session_id, []))

    def latest(self, session_id: str) -> list[Post]:
        return list(self._latest.get(session_id, []))


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

    def all(self, session_id: str) -> list[Post]:
        raise NotImplementedError("方案 B 尚未實作；要持久化累積記憶時再開。")

    def latest(self, session_id: str) -> list[Post]:
        raise NotImplementedError("方案 B 尚未實作；要持久化累積記憶時再開。")


def get_store() -> FreshStore:
    if settings.fresh_store == "qdrant":
        return QdrantHotStore()
    return SessionFreshStore()


# 單例：整個 app 共用一份（方案 A 的記憶體 bucket 才不會每次 new）
store: FreshStore = get_store()
