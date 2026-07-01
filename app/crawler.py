"""即時爬蟲轉接層：把 Dcard_demo 的 `BoardCrawler` 接成一個簡單的 `crawl()` 介面。

Agent 只認本檔的 `crawl(board, keyword, max_posts) -> list[Post]`，不認大專案內部細節。
- 設了 .env 的 CRAWLER_PATH 就動態 import `dcard_crawler` 並真的去爬；
- 沒設時用內建 stub 假資料，骨架先跑通。

接 Dcard_demo 時處理掉的三個坑：
1. 爬蟲只抓「某版最新 N 篇」、沒有關鍵字搜尋 → 抓回後在本檔用 keyword 過濾。
2. BoardCrawler 會用 seen 庫跳過爬過的貼文（增量去重）→ 即時 agent 要「當下最新」，
   故注入「用完即丟的空 seen」(_EphemeralSeen) 繞過去重，每次都重抓最新。
3. 沒有 iphone/apple 版，真實版面只有 3c → 未知 board 一律 fallback 到 3c。
"""
from __future__ import annotations

import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from .config import settings

logger = logging.getLogger(__name__)


class Post(TypedDict):
    title: str
    url: str
    content: str
    created_at: str  # 'YYYY-MM-DD HH:MM:SS'（台北時間，越新越前）
    source: str      # 平台標籤：'dcard' | 'ptt'（給合併後的來源分流／UI 分組用）


# 使用者可能講的板名 → Dcard_demo 真實 alias（清單見 dcard_crawler.config.BOARDS）
_BOARD_FALLBACK = {"iphone": "3c", "apple": "3c", "ios": "3c", "手機": "3c", "科技": "3c"}


def crawl(board: str, query: str, max_posts: int | None = None) -> list[Post]:
    """以 query（使用者問句）站內搜尋某 Dcard 版的相關貼文。失敗回空陣列（上層 fail-safe）。"""
    limit = max_posts or settings.crawl_max_posts
    if settings.crawler_path:
        try:
            return _real_crawl(board, query, limit)
        except Exception as e:  # noqa: BLE001 — live 爬蟲什麼都可能炸，一律 fail-safe
            logger.warning("real crawl failed, falling back to empty: %s", e)
            return []
    return _stub_crawl(board, query, limit)


class _EphemeralSeen:
    """用完即丟的空 seen：讓 BoardCrawler 不跳過任何貼文，每次都重抓當下最新。"""

    def __contains__(self, _post_id) -> bool:
        return False

    def add(self, _post_id) -> None:
        pass

    def flush(self) -> None:
        pass


def _real_crawl(board: str, query: str, limit: int) -> list[Post]:
    if settings.crawler_path not in sys.path:
        sys.path.insert(0, settings.crawler_path)
    from dcard_crawler import config as dconf  # noqa: PLC0415
    from dcard_crawler.crawler import BoardCrawler  # noqa: PLC0415

    alias = board if board in dconf.BOARDS else _BOARD_FALLBACK.get(board.lower(), "3c")
    state_dir = Path(settings.crawler_path) / "state"

    # 站內搜尋 query 取相關 limit 篇、每篇少量留言（壓低 live 延遲）；空 seen 繞過增量去重
    bc = BoardCrawler(alias, state_dir, seen=_EphemeralSeen())
    # 硬性時間預算：live 爬蟲是逐篇進頁，可能拖很久而衝破前端 timeout。到時間就停、
    # 回傳「目前已抓到」的部分結果（generator 邊爬邊吐，所以前面幾篇是完整的）。
    deadline = time.monotonic() + settings.crawl_timeout
    records: list[dict] = []
    gen = bc.search(query=query, max_posts=limit, max_comments=15)
    try:
        for rec in gen:
            records.append(rec)
            if time.monotonic() >= deadline:
                logger.info("hit %ss budget, returning %d records so far", settings.crawl_timeout, len(records))
                break
    finally:
        gen.close()  # 提早結束時收掉 generator 內還開著的 page
        # 有頭瀏覽器在 Windows 上 teardown 偶發掉連線（Browser.close: Connection
        # closed...）。那只是收尾噪音，資料早已抓到——不可讓它把 records 丟掉而 fallback 空。
        try:
            bc.close()  # 關閉瀏覽器 session
        except Exception as e:  # noqa: BLE001
            logger.warning("browser close failed (ignored, data already fetched): %s", e)

    # 搜尋結果已是 Dcard 按相關度排序的相關文，不再做本地關鍵字過濾（會誤刪語意相關文）
    return _records_to_posts(records)[:limit]


def _strip_tags(s: str) -> str:
    """清掉 Dcard 站內搜尋摘要夾帶的高亮標記（<em>…</em> 等 HTML tag）。"""
    return re.sub(r"<[^>]+>", "", s or "")


def _records_to_posts(records: list[dict]) -> list[Post]:
    """LOWI 紀錄（主文 P + 留言 R）→ 以討論串為單位的 Post（主文內文後附前幾則留言）。"""
    replies: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r.get("glb_posttype") == "R" and r.get("drecontent"):
            replies[r.get("glb_lowikey", "")].append(_strip_tags(r["drecontent"]))

    posts: list[Post] = []
    for r in records:
        if r.get("glb_posttype") != "P":
            continue
        body = _strip_tags(r.get("drecontent", "") or "")
        cs = replies.get(r.get("glb_lowikey", ""), [])
        if cs:
            body += "\n— 熱門留言：" + " / ".join(cs[:5])
        posts.append(
            Post(
                title=_strip_tags(r.get("dretitle", "") or ""),
                url=r.get("glbdis_linkurl", "") or "",
                content=body,
                created_at=r.get("dredate", "") or "",
                source="dcard",
            )
        )
    return posts


def _stub_crawl(board: str, query: str, limit: int) -> list[Post]:
    """假資料：CRAWLER_PATH 未設定時用，讓 agent loop 端到端先跑通。"""
    return [
        Post(
            title=f"[{board}] {query} 災情回報 #{i + 1}",
            url=f"https://www.dcard.tw/f/{board}/p/stub-{i + 1}",
            content=f"（stub 假資料）關於「{query}」的第 {i + 1} 則最新討論。"
            f"設定 CRAWLER_PATH 後會換成真的 Dcard 即時資料。",
            created_at=f"2026-06-21 0{i % 9}:00:00",
            source="dcard",
        )
        for i in range(min(limit, 5))
    ]
