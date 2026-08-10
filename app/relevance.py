"""語意相關度過濾：共用給三個即時爬平台的 embed cosine 重排。

為什麼三個平台都需要這一道：各平台的站內搜尋都不是「語意」搜尋，各有各的失準方式——

- Dcard：全文檢索找不到時會退化成部分匹配（搜「福智教育園區」拿回一堆「幼兒園評價」）；
- PTT：搜尋是拿字串比對『標題』，LLM 若抽出「看法／評價」這種無主體泛詞就會撈回滿坑雜訊；
- Threads：只吃單一詞、不斷詞也不做交集（見 threads.py），所以「OpenAI」「越獄」只能各搜
  一次再合併，撈回來的東西與原問題的關聯全靠這一關把。

三邊原本各有一份幾乎相同的 _cosine + rerank，抽到這裡共用。差異只有「怎麼把一筆資料
變成要比對的文字」和「推哪個進度事件」，故做成參數。

fail-safe：embed 失敗一律回原 list——過濾是加分項，不該擋掉整條爬蟲。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import TypeVar

from . import llm, progress, tracing
from .config import settings
from .tracing import observe

log = logging.getLogger(__name__)

T = TypeVar("T")


# 純泛用限定詞：單獨拿去搜任何平台都會撈到成千上萬不相關的東西（問 Kimi K3 卻回一堆
# 「iPhone 實際照片」；Threads 搜「資安」第一名是台中室內設計公司）。各平台的抽詞 prompt
# 已明令禁止，但模型不一定聽——拿回後在程式層用這張表硬過濾兜底。
FILLER_KEYWORDS: frozenset[str] = frozenset({
    "實際", "心得", "評價", "看法", "推薦", "意見", "感想", "體驗", "使用", "應用",
    "分享", "討論", "開箱", "比較", "選擇", "如何", "怎樣", "怎麼", "一般", "問題",
    "請問", "介紹", "情況", "狀況", "效果", "表現", "優缺點", "值得", "覺得",
})


def clean_keywords(keywords: list[str]) -> list[str]:
    """剔掉純泛用限定詞（心得/實際/評價…），只留有主體的詞；去重保序。

    若整批都是泛用詞（模型完全沒給實體）就退回第一個，至少還有東西可搜、不致變空。
    注意這是「整字串比對」：模型回「資安問題」不會被這裡攔下（它不等於「問題」），
    那種黏著詞得靠各平台自己的 prompt 與語意過濾處理。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for k in keywords:
        if k and k not in FILLER_KEYWORDS and k not in seen:
            seen.add(k)
            kept.append(k)
    return kept or keywords[:1]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity；任一邊長度為零就回 0（fail-safe）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


@observe(name="rerank", capture_input=False, capture_output=False)
def rerank(user_query: str, items: list[T], min_score: float,
           text_of: Callable[[T], str], *, platform: str) -> list[T]:
    """依與 user_query 的語意相似度過濾並重排 items（分數高的在前）。

    分數 = cosine(embed(user_query), embed(text_of(item)))，保留 >= min_score。
    批次一次 embed 全部 items，只多一次 API call（~2s）。

    platform：只用於 log 與 `<platform>_rerank` 進度事件的命名（前端據此顯示「濾掉幾篇」）。
    """
    if not items or not (user_query or "").strip():
        return items
    try:
        query_vec = llm.embed(user_query)
        texts = [(text_of(it) or "").strip() or "空" for it in items]
        # 批次 embed：SDK 支援 input=list[str]，一次呼叫拿到所有向量
        client = llm._client()  # noqa: SLF001 — 內部共用 client
        resp = client.embeddings.create(model=settings.embed_model, input=texts)
        vecs = [d.embedding for d in resp.data]
    except Exception as e:  # noqa: BLE001 — 過濾失敗就沿用原結果，不擋整條爬蟲
        log.warning("%s 語意過濾失敗（沿用原結果）：%s", platform, e)
        tracing.set_span(name=f"{platform}_rerank", level="WARNING", status_message=str(e))
        return items

    scored = [(it, cosine(query_vec, v)) for it, v in zip(items, vecs)]
    kept = [(it, s) for it, s in scored if s >= min_score]
    kept.sort(key=lambda x: x[1], reverse=True)
    dropped = len(scored) - len(kept)
    log.info("%s 語意過濾：%d 篇 → %d 篇（門檻 %.2f，丟 %d）",
             platform, len(scored), len(kept), min_score, dropped)
    progress.emit(f"{platform}_rerank", kept=len(kept), dropped=dropped,
                  threshold=min_score, before=len(scored))
    # 分數分佈是調門檻唯一的依據——config.py 裡那些「最高 0.448 / 中位 0.356 / 最低 0.179」
    # 的註解本來是手動跑出來記上去的，現在每一次查詢都會自動留下這組數字。
    all_scores = sorted((s for _, s in scored), reverse=True)
    tracing.set_span(
        name=f"{platform}_rerank",
        input=user_query,
        output={"before": len(scored), "kept": len(kept), "dropped": dropped},
        metadata={
            "threshold": min_score,
            "score_max": round(all_scores[0], 3) if all_scores else None,
            "score_median": round(all_scores[len(all_scores) // 2], 3) if all_scores else None,
            "score_min": round(all_scores[-1], 3) if all_scores else None,
        },
    )
    return [it for it, _ in kept]
