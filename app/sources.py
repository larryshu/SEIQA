"""社群來源 registry + 並行 fan-out（方案 C 的核心）。

對 agent 只暴露一個 skill（community_search）；底下掛多個「來源 adapter」，每個 adapter 把
某平台包成統一的 `fetch(query) -> list[Post]`（Post 已帶 source 平台標籤）。查詢時並行 fan-out
到所有 adapter、合併結果。

擴充新平台＝在這裡多寫一個 Source、加進 _ADAPTERS 即可——agent / loop / prompt 全部不用動。

M3：啟用哪些平台、各平台參數（top_k / min_score / expand_n / PTT 預算）改由後台 MySQL 決定
（config_repo）。後台沒設或 DB 連不上時 fall back 到 _DEFAULT_REGISTRY 與 .env（settings）。
"""
from __future__ import annotations

import contextvars
import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import llm, progress, ptt, vectorstore
from .config import settings
from .config_repo import repo
from .crawler import Post

logger = logging.getLogger(__name__)

# 取消輪詢間隔：fan-out 在等爬蟲時，每隔這麼久回頭看一次有沒有被取消（＝前端感受到的停止延遲）
_CANCEL_POLL_SEC = 0.5


class Source(ABC):
    """一個社群來源 adapter。name 用於日誌；fetch 回傳已帶 source 標籤的 Post 清單。

    cfg：後台該平台的 source_config（已 typed）；取不到的 key 一律 fall back 到 settings。
    """

    name: str

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}

    @abstractmethod
    def fetch(self, query: str) -> list[Post]:
        ...


class DcardSource(Source):
    """Dcard 口碑庫（向量檢索）：多面向查詢改寫 + round-robin 合併 + 門檻。"""

    name = "dcard"

    def fetch(self, query: str) -> list[Post]:
        expand_n = int(self.cfg.get("expand_n", settings.search_expand_n))
        top_k = int(self.cfg.get("top_k", settings.search_top_k))
        min_score = float(self.cfg.get("min_score", settings.search_min_score))
        queries = [query, *llm.expand_queries(query, n=expand_n)]
        seen: set[str] = set()
        queries = [q for q in queries if q and not (q in seen or seen.add(q))]
        return vectorstore.multi_search(queries, top_k=top_k, min_score=min_score)


class DcardLiveSource(Source):
    """Dcard 即時爬（DrissionPage 全站『文章』搜尋）。DCARD_MODE=live 時走這條。

    即時爬失敗 / 沒撈到（Cloudflare 擋、DrissionPage 未裝、無結果…）→ 自動 fallback 到
    向量庫（DcardSource），確保 Dcard 這條總是回得了話。輸出仍是 source="dcard" 的 Post，
    前端 [n] 引用與燈號完全沿用，不用改。
    """

    name = "dcard"

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__(cfg)
        self._vector = DcardSource(cfg)  # fallback

    def fetch(self, query: str) -> list[Post]:
        from . import dcard_live  # lazy：DrissionPage 沒裝也不影響 app 啟動
        time_budget = int(self.cfg.get("time_budget", settings.dcard_time_budget))
        deep_max = int(self.cfg.get("deep_max", settings.dcard_deep_max))
        posts = dcard_live.crawl(query, max_posts=deep_max, time_budget=time_budget)
        if posts:
            return posts
        logger.info("Dcard 即時爬無結果，fallback 向量庫")
        # 這條分層 fail-safe 原本只寫進 log；推給前端後，降級行為就變成看得見的產品行為
        progress.emit("source_fallback", platform="dcard",
                      **{"from": "live", "to": "vector"})  # from 是保留字，只能這樣傳
        return self._vector.fetch(query)


class PttSource(Source):
    """PTT：挑看板後在時間預算內即時爬站內搜尋結果。"""

    name = "ptt"

    def fetch(self, query: str) -> list[Post]:
        time_budget = int(self.cfg.get("time_budget", settings.ptt_time_budget))
        return ptt.search(query, time_budget=time_budget)


def _dcard_cls() -> type[Source]:
    """DCARD_MODE 決定 Dcard 這條走哪個 adapter：live=即時爬（+向量 fallback）｜vector=純向量庫。"""
    return DcardLiveSource if settings.dcard_mode == "live" else DcardSource


# adapter_key（後台 source_platform.adapter_key）→ adapter 類別。加平台就在這裡多掛一個。
# 「dcard」依 DCARD_MODE 解析成即時爬或向量；後台若另設 adapter_key=dcard_vector 可強制走向量。
_ADAPTERS: dict[str, type[Source]] = {
    "dcard": _dcard_cls(),
    "dcard_vector": DcardSource,
    "ptt": PttSource,
}

# fallback：後台不可用時用的預設（等同 M3 之前的寫死 registry）
_DEFAULT_REGISTRY: list[Source] = [_dcard_cls()(), PttSource()]

# 對外相容：保留 REGISTRY 名稱（指向預設）
REGISTRY: list[Source] = _DEFAULT_REGISTRY


def _build_registry(end_user_id: int | None = None) -> list[Source]:
    """依後台啟用的平台＋順序組 registry，再套使用者 included/excluded_platforms 偏好（M5）。

    DB 不可用 → 用 _DEFAULT_REGISTRY；偏好把平台濾光 → 回空（community_search 就回零結果，
    LLM 改用常識答——這是使用者刻意排除平台的合理結果）。
    """
    prefs = repo.get_user_preferences(end_user_id) if end_user_id else {}
    included = prefs.get("included_platforms")  # list[str] 或 None
    excluded = set(prefs.get("excluded_platforms") or [])

    def allowed(name: str) -> bool:
        if included and name not in included:
            return False
        return name not in excluded

    enabled = repo.get_enabled_sources()
    if not enabled:  # DB 不可用 → 預設兩個 adapter，仍套使用者過濾
        return [s for s in _DEFAULT_REGISTRY if allowed(s.name)]
    built: list[Source] = []
    for s in enabled:
        if not allowed(s.get("name", "")):
            continue
        cls = _ADAPTERS.get(s.get("adapter_key", ""))
        if cls:
            built.append(cls(s.get("configs")))
    return built


def _safe_fetch(source: Source, query: str) -> list[Post]:
    """單一來源 fail-safe：任一平台炸掉只少那一邊，不影響其他來源。

    注意：下面的 except Exception 不會攔到 progress.Cancelled（它繼承 BaseException），
    取消因此不會被誤記成「這個平台掛了」而回空清單。
    """
    progress.emit("source_start", platform=source.name)
    started = time.monotonic()
    try:
        posts = source.fetch(query)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s fetch failed, skipped: %s", source.name, e)
        progress.emit("source_error", platform=source.name, message=str(e))
        return []
    progress.emit("source_done", platform=source.name, count=len(posts),
                  elapsed=round(time.monotonic() - started, 1))
    return posts


def community_search(query: str, end_user_id: int | None = None) -> list[Post]:
    """並行 fan-out 到所有啟用來源（套使用者平台偏好），依順序合併（每篇已帶 source 平台標籤）。"""
    registry = _build_registry(end_user_id)
    progress.emit("search_start", query=query, platforms=[s.name for s in registry])

    results: list[Post] = []
    executor = ThreadPoolExecutor(max_workers=max(len(registry), 1))
    try:
        # 先全部 submit（才是真並行），再依順序收結果 → 合併順序穩定。
        # contextvars 不會自動流進 worker thread，且同一個 Context 不能被兩條執行緒同時 run，
        # 故每個 future 各複製一份——否則底層爬蟲的 emit / 取消檢查全都看不到訂閱者。
        futures = [executor.submit(contextvars.copy_context().run, _safe_fetch, s, query)
                   for s in registry]
        for fut in futures:
            while True:
                progress.raise_if_cancelled()
                try:
                    results.extend(fut.result(timeout=_CANCEL_POLL_SEC))
                    break
                except FutureTimeout:
                    continue  # 還在爬 → 回頭檢查取消，別死等
    finally:
        # 取消時不等在途的爬蟲：DrissionPage 是阻塞的、無法從外部中斷，只能不再等它。
        # 那顆 Chrome 會自己跑到時間預算結束後收工，結果丟棄。
        executor.shutdown(wait=False, cancel_futures=True)

    counts: dict[str, int] = {}
    for post in results:
        platform = post.get("source", "?")
        counts[platform] = counts.get(platform, 0) + 1
    progress.emit("search_done", total=len(results), counts=counts)
    return results
