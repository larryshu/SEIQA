"""跨平台重排（rerank）：三個平台的結果合併之後，依「跟問題有多相關」重新排序並只留前 N 則。

跟 relevance.py 的差別——那一層是**各平台各自**的 embedding cosine 過濾：
  - 分開算向量再比（bi-encoder），快但粗；
  - 各平台各濾各的，合併後的順序是「Dcard 全部 → PTT 全部 → Threads 全部」，不是依相關度，
    而且全部送進 LLM——一題常有 60～100 則，實測有一題 92 則、上萬字。
這一層在合併之後才做，讓模型「同時讀問題和貼文」打分，只把最相關的前 N 則交給回答的 LLM：
雜訊少了，模型就少了張冠李戴的材料；送進去的 token 也跟著少一大截。

後端可替換（RERANK_BACKEND）：
  - llm（目前）：分批請 LLM 給每則 0～3 分。不必裝任何套件，讀得懂鄉民用語。
  - cross_encoder（預留）：本地 cross-encoder（例如 BAAI/bge-reranker-v2-m3），要另裝 torch。
    兩者都只實作 _score_*()，挑選邏輯（_select）共用，換後端不必動其他地方。

挑選規則（_select）：
  1. 低於 RERANK_MIN_SCORE 的先剔除；
  2. 每個平台至少保留前 RERANK_PER_PLATFORM_MIN 則（只要分數 > 0）——只看分數的話，
     某個平台可能整批被刷掉，答案就只剩單一平台的觀點；
  3. 其餘名額依分數由高到低補滿 RERANK_TOP_N；同分維持原順序（各平台原本已按相關度排過）。

全程 fail-safe：某一批打分失敗，那批貼文給中間分（留著但排後面）；全部失敗就回原清單。
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from . import progress, tracing
from .config import settings
from .tracing import observe

logger = logging.getLogger(__name__)

MAX_SCORE = 3
_BATCH = 15         # 一次請模型打幾則：太多會漏標，太少呼叫次數暴增
_EXCERPT = 300      # 每則給模型看的內文長度：判斷相關與否，標題加開頭就夠


# ---- 打分後端 -----------------------------------------------------------------
_LLM_SYSTEM = (
    "你是搜尋結果的相關度評分員。使用者問了一個問題，下面是從社群平台撈回來的幾則貼文。"
    "請依『這則貼文對回答這個問題有沒有幫助』給 0～3 分：\n"
    "3：直接討論問題的主題，而且有具體看法、經驗或評價。\n"
    "2：跟主題相關，有一些可用的資訊。\n"
    "1：只是沾到邊（例如只提到同一個名詞，但在講別的事）。\n"
    "0：離題，或只是廣告、純網址、看不出內容。\n"
    "原文是鄉民口語，要依實際意思判斷，不要只看關鍵字有沒有出現。\n"
    "每一則都要評分。只輸出 JSON，不要 markdown：[{\"n\": 1, \"score\": 2}, ...]"
)
_TRAILING_COMMA = re.compile(r",\s*([\]}])")


def _parse(raw: str) -> list:
    s = (raw or "").strip()
    if "[" in s and "]" in s:
        s = s[s.find("["):s.rfind("]") + 1]
    data = json.loads(_TRAILING_COMMA.sub(r"\1", s))
    return data if isinstance(data, list) else []


def _score_batch_llm(query: str, batch: list[dict]) -> dict[int, float]:
    """一批貼文打分，回 {批內序號(1-based): 分數}。失敗就拋，由呼叫端決定怎麼處理。"""
    from .llm import chat

    lines = []
    for i, p in enumerate(batch, 1):
        body = " ".join((p.get("content") or "").split())[:_EXCERPT]
        lines.append(f"[{i}] {p.get('title') or ''}\n{body}")
    raw = chat([
        {"role": "system", "content": _LLM_SYSTEM},
        {"role": "user", "content": f"問題：{query}\n\n貼文：\n\n" + "\n\n".join(lines)},
    ], temperature=0.0, model=settings.rerank_model or None)
    out: dict[int, float] = {}
    for item in _parse(raw):
        try:
            n, score = int(item["n"]), float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= n <= len(batch):
            out[n] = max(0.0, min(float(MAX_SCORE), score))
    return out


def _score_llm(query: str, posts: list[dict]) -> list[float | None]:
    """分批並行打分。某批失敗 → 那批全是 None（呼叫端補中間分）。"""
    batches = [posts[i:i + _BATCH] for i in range(0, len(posts), _BATCH)]
    scores: list[float | None] = [None] * len(posts)

    def run(batch):
        try:
            return _score_batch_llm(query, batch)
        except Exception as e:  # noqa: BLE001 — 一批壞掉只影響那一批
            logger.warning("rerank 打分失敗（這批給中間分）：%s", e)
            return {}

    # 每個 future 各複製一份 context：否則 worker thread 裡的 LLM 呼叫掛不到目前的 trace
    with ThreadPoolExecutor(max_workers=min(len(batches), 4) or 1) as ex:
        futures = [ex.submit(contextvars.copy_context().run, run, b) for b in batches]
        for bi, fut in enumerate(futures):
            for n, s in fut.result().items():
                scores[bi * _BATCH + n - 1] = s
    return scores


def _score_cross_encoder(query: str, posts: list[dict]) -> list[float | None]:
    """預留：本地 cross-encoder。要用時裝 sentence-transformers + torch，把分數縮放到 0～3。"""
    raise NotImplementedError("cross_encoder 後端尚未實作；目前請用 RERANK_BACKEND=llm")


_BACKENDS = {"llm": _score_llm, "cross_encoder": _score_cross_encoder}


# ---- 挑選 ---------------------------------------------------------------------
def _select(posts: list[dict], scores: list[float], top_n: int, min_score: float,
            per_platform_min: int) -> list[int]:
    """回傳要保留的貼文索引（依最終順序）。純函式，離線測試直接測這支。"""
    order = sorted(range(len(posts)), key=lambda i: (-scores[i], i))  # 同分維持原順序
    chosen: list[int] = []
    # 1) 每個平台的保底：分數 > 0 的前幾則（0 分＝離題，保底也不收）
    per_platform: dict[str, int] = {}
    for i in order:
        platform = posts[i].get("source") or "?"
        if scores[i] > 0 and per_platform.get(platform, 0) < per_platform_min:
            chosen.append(i)
            per_platform[platform] = per_platform.get(platform, 0) + 1
    # 2) 其餘依分數補滿，但要過門檻
    for i in order:
        if len(chosen) >= top_n:
            break
        if i not in chosen and scores[i] >= min_score:
            chosen.append(i)
    # 保底可能讓總數略超過 top_n（平台多、保底則數大時），刻意不截：截掉的會是保底那幾則
    return sorted(chosen, key=lambda i: (-scores[i], i))


@observe(name="cross_platform_rerank", capture_input=False, capture_output=False)
def rerank(query: str, posts: list[dict]) -> list[dict]:
    """依相關度重排並截斷。關閉、貼文不多、或打分全部失敗時回原清單。"""
    if not settings.rerank_enabled or len(posts) <= settings.rerank_top_n:
        return posts
    scorer = _BACKENDS.get(settings.rerank_backend)
    if scorer is None:
        logger.warning("未知的 RERANK_BACKEND=%s，略過重排", settings.rerank_backend)
        return posts

    progress.emit("stage", stage="reranking", text=f"從 {len(posts)} 則討論裡挑出最相關的…")
    try:
        raw = scorer(query, posts)
    except Exception as e:  # noqa: BLE001 — 重排是加分項，壞了就沿用原清單
        logger.warning("rerank 失敗（沿用原清單）：%s", e)
        tracing.set_span(level="WARNING", status_message=str(e))
        return posts
    failed = sum(s is None for s in raw)
    if failed == len(raw):
        tracing.set_span(level="WARNING", status_message="全部打分失敗，沿用原清單")
        return posts
    scores = [float(MAX_SCORE) / 2 if s is None else s for s in raw]  # 失敗的給中間分

    keep = _select(posts, scores, settings.rerank_top_n, settings.rerank_min_score,
                   settings.rerank_per_platform_min)
    kept = [posts[i] for i in keep]

    before_by = _count_by_platform(posts)
    after_by = _count_by_platform(kept)
    dist = {s: sum(1 for x in scores if round(x) == s) for s in range(MAX_SCORE + 1)}
    logger.info("rerank：%d 則 → %d 則（分數分佈 %s）", len(posts), len(kept), dist)
    # 分數分佈與各平台前後則數，是之後調 TOP_N / MIN_SCORE 唯一的依據
    tracing.set_span(
        input=query,
        output={"before": len(posts), "kept": len(kept), "by_platform": after_by},
        metadata={"backend": settings.rerank_backend, "top_n": settings.rerank_top_n,
                  "min_score": settings.rerank_min_score, "score_dist": dist,
                  "before_by_platform": before_by, "failed": failed},
    )
    return kept


def _count_by_platform(posts: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in posts:
        out[p.get("source") or "?"] = out.get(p.get("source") or "?", 0) + 1
    return out
