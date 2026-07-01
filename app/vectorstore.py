"""Dcard 口碑庫（dcard_insight）唯讀向量檢索。

取代「即時爬 Dcard」：把使用者問句 embed 成 1536 維，打 Qdrant REST 的 points/search，
撈最相關的討論。資料由 dcard_insight 專案批次建好，這裡只查不寫。

兩個檢索品質設計：
- 多面向查詢（multi_search）：對「同一問題的多條改寫查詢」各查一次再合併，避免單一稠密
  向量被某個強勢詞綁架（例如「健身有助性生活品質？」整碗被端去健身版）。
- 分數門檻（search_min_score）：合併後低於門檻視為不夠對題 → 回空，讓上層走黃燈用常識答，
  而不是自信地引用一堆其實不相關的貼文。

其他：
- 用 requests 走 Qdrant REST（不引入 qdrant-client）。
- fail-safe：embed 或 Qdrant 任何一步炸掉都略過該條查詢；全失敗就回空陣列。
- 庫是「分塊(chunk)」存的（同一篇文多個 chunk）→ 以 url 去重，每篇只留分數最高的那則 chunk。
"""
from __future__ import annotations

import logging

import requests

from .config import settings
from .crawler import Post  # 沿用同一個 Post 結構，下游（store/UI）不必改
from .llm import embed

logger = logging.getLogger(__name__)


def _qdrant_search(vector: list[float], limit: int) -> list[dict]:
    """打 Qdrant REST points/search，回原始 hits（含 score/payload）。失敗丟例外給呼叫端。"""
    url = f"{settings.qdrant_url.rstrip('/')}/collections/{settings.insight_collection}/points/search"
    resp = requests.post(
        url, json={"vector": vector, "limit": limit, "with_payload": True}, timeout=15
    )
    resp.raise_for_status()
    return resp.json().get("result", []) or []


def multi_search(
    queries: list[str], top_k: int | None = None, min_score: float | None = None
) -> list[Post]:
    """對多條查詢各查一次，再以 round-robin（各查詢輪流出一篇）合併，套門檻後回貼文。

    為什麼不用全域比分：純語意比分會被「強勢面向」通吃——例如『健身有助性生活？』裡
    『健身 運動』查詢把健身文衝到 0.6+，把對題的感情文（0.56）整批壓下去。改用輪流取，
    讓每個面向（健身面向、性生活面向…）都有代表，避免單一面向洗版。

    流程：每條查詢各自取「分數高→低、url 去重」的清單 → 第 1 輪各取第 1 名、第 2 輪各取
    第 2 名…輪流填，直到湊滿 top_k；同一篇只收一次；分數（跨查詢取最高）低於門檻者不收。
    """
    k = top_k or settings.search_top_k
    thr = settings.search_min_score if min_score is None else min_score

    per_query: list[list[str]] = []        # 每條查詢 → 依分數排好的 url 清單
    payload_by_url: dict[str, dict] = {}
    best_score: dict[str, float] = {}      # url → 跨查詢的最高分（套門檻用）
    for q in queries:
        if not q:
            continue
        try:
            vector = embed(q)
        except Exception as e:  # noqa: BLE001 — 該條查詢 embed 失敗就略過
            logger.warning("embed failed for query %r: %s", q, e)
            continue
        try:
            hits = _qdrant_search(vector, max(k * 4, 20))
        except Exception as e:  # noqa: BLE001 — Qdrant 沒開/collection 不存在等就略過
            logger.warning("qdrant search failed for query %r: %s", q, e)
            continue
        ranked_urls: list[str] = []
        seen_in_q: set[str] = set()
        for h in hits:  # Qdrant 已依分數高→低
            payload = h.get("payload") or {}
            u = payload.get("url") or ""
            if not u or u in seen_in_q:  # 同查詢內同篇多 chunk 只留最高分那則
                continue
            seen_in_q.add(u)
            ranked_urls.append(u)
            payload_by_url[u] = payload
            best_score[u] = max(best_score.get(u, 0.0), float(h.get("score", 0.0)))
        per_query.append(ranked_urls)

    # round-robin：各查詢輪流出一篇，讓每個面向都有代表
    chosen: list[str] = []
    chosen_set: set[str] = set()
    depth = 0
    while len(chosen) < k and any(depth < len(r) for r in per_query):
        for r in per_query:
            if depth >= len(r):
                continue
            u = r[depth]
            if u in chosen_set or best_score[u] < thr:  # 已選過、或不夠對題就跳過
                continue
            chosen.append(u)
            chosen_set.add(u)
            if len(chosen) >= k:
                break
        depth += 1

    return [
        Post(
            title=payload_by_url[u].get("title", "") or "",
            url=u,
            content=payload_by_url[u].get("text", "") or "",
            created_at="",  # dcard_insight 沒有時間欄位
            source="dcard",
        )
        for u in chosen
    ]


def search(query: str, top_k: int | None = None) -> list[Post]:
    """單一查詢（保留相容介面）：等同 multi_search([query])。"""
    return multi_search([query], top_k)
