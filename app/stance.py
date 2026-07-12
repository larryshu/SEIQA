"""立場統計：把「已撈到的社群討論」逐則貼標籤，再由程式加總成可畫圖的數據。

為什麼要獨立這一層——**統計不能交給 LLM**。
直接問模型「大家贊成還是反對？大概幾成？」，它會回「大概六四開」：那個 60% 沒有人數過，
是憑感覺講的。一旦畫成圓餅圖，假精確度就被放大成看起來像事實的東西，這與本專案
「🟢🟡 燈號 + [n] 來源 + 明講哪個平台沒資料」的反幻覺原則直接牴觸。

所以這裡把工作切成兩半：
  - LLM 只做**分類**：對每一則貼文，判斷作者對某個議題陳述句的立場（贊成／反對／中立）。
  - Python 做**統計**：Counter 加總、算百分比、判斷樣本夠不夠、決定要不要標警語。

每一筆都帶著它在 sources 裡的序號（n），所以圓餅圖的每一片都回得去原文——可被查證。
"""
from __future__ import annotations

import json
import logging
from collections import Counter

from . import llm, progress

logger = logging.getLogger(__name__)

STANCES = ("贊成", "反對", "中立")   # 預設分類軸：對一句議題陳述的立場
MAX_CATEGORIES = 5    # 上限：切太細每類都只剩一兩則，圓餅圖會變成沒有意義的碎片
BATCH_SIZE = 10       # 一次丟幾則給 LLM 分類（太多會讓它偷懶漏標）
CONTENT_CHARS = 400   # 每則餵進去的內文長度：夠判斷立場即可，不必整篇
MIN_SAMPLE = 8        # 低於這個則數 → 標警語（3 則裡的 2 則不該被講成 67%）


def normalize(categories) -> tuple[str, ...]:
    """整理呼叫端給的分類軸：去空白、去重、限長度與個數。不合格就退回預設的贊成／反對／中立。

    為什麼要能自訂：使用者問的不一定是「贊成／反對」。像「大家是同情、嘲笑、還是無感？」
    問的是**態度**而不是立場——硬套贊成／反對只會得到一組答非所問的標籤。
    """
    if not isinstance(categories, (list, tuple)):
        return STANCES
    out: list[str] = []
    for c in categories:
        label = str(c).strip()[:8]
        if label and label not in out:
            out.append(label)
    return tuple(out[:MAX_CATEGORIES]) if len(out) >= 2 else STANCES


def _prompt(categories: tuple[str, ...]) -> str:
    options = "、".join(f"「{c}」" for c in categories)
    return (
        "你是輿情標註員。使用者會給你一個『議題陳述句』和數則社群貼文。\n"
        f"請針對**每一則**貼文，把它歸到這幾類其中之一：{options}。\n"
        "判斷依據是貼文作者對那句議題陳述所表現出的態度／立場。\n"
        "沒表態、只問問題、或純粹離題的，歸到最接近『中立／無感／其他』的那一類；"
        "若沒有這種類別，就挑語意上最接近的一類。\n"
        "注意：立場不等於情緒。一篇語氣憤怒的貼文，立場可能正是『贊成這件事很嚴重』。\n"
        "只輸出 JSON 陣列，每則一個物件："
        "[{\"n\": 1, \"stance\": \"<上列類別之一>\", \"why\": \"12 字內的依據\"}]\n"
        "不要輸出其他任何文字、不要 markdown 圍籬。每一則都必須有一個標籤，不可跳過。"
    )


def _parse(raw: str) -> list[dict]:
    """容錯解析 LLM 回的 JSON 陣列（去 markdown 圍籬；壞掉就回空）。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("["):] if "[" in s else s
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        logger.warning("stance 分類回傳不是合法 JSON，這批略過")
        return []
    return data if isinstance(data, list) else []


def _classify(issue: str, posts: list[dict], categories: tuple[str, ...]) -> dict[int, dict]:
    """逐批分類，回 {序號(1-based): {stance, why}}。任一批失敗只少那一批（fail-safe）。"""
    labels: dict[int, dict] = {}
    system = _prompt(categories)
    for start in range(0, len(posts), BATCH_SIZE):
        progress.raise_if_cancelled()          # 逐批檢查點：按停止最多再等一批
        batch = posts[start:start + BATCH_SIZE]
        lines = []
        for offset, post in enumerate(batch):
            n = start + offset + 1
            body = (post.get("content") or "")[:CONTENT_CHARS]
            lines.append(f"[{n}]（{post.get('source', '')}）{post.get('title', '')}\n{body}")

        try:
            raw = llm.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": f"議題陳述句：{issue}\n\n貼文：\n\n" + "\n\n".join(lines)},
            ], temperature=0)
        except Exception as e:  # noqa: BLE001 — 分類失敗不該弄死整題問答
            logger.warning("stance 分類失敗（這批略過）：%s", e)
            continue

        for item in _parse(raw):
            if not isinstance(item, dict):
                continue
            stance = str(item.get("stance", "")).strip()
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            # 只收在分類軸內的標籤：模型自創類別（例如多冒出一個「其他」）一律丟掉，
            # 否則圓餅圖會長出一片沒人要求、也沒有顏色的切片。
            if stance in categories and 1 <= n <= len(posts):
                labels[n] = {"stance": stance, "why": str(item.get("why", "")).strip()[:20]}

        progress.emit("stance_progress", done=len(labels), total=len(posts))
    return labels


def breakdown(issue: str, posts: list[dict], categories=None) -> dict | None:
    """統計 posts 對 issue 的分佈。分不出任何一則 → None（上層就不畫圖）。

    posts 用的是這一輪 community_search 實際命中的來源清單，順序即畫面上的 [n]。
    categories：自訂分類軸（例如同情／嘲笑／無感）；留空＝贊成／反對／中立。
    """
    if not posts:
        return None

    cats = normalize(categories)
    progress.emit("stage", stage="counting", text=f"逐則判讀（{'／'.join(cats)}）：{issue}")
    labels = _classify(issue, posts, cats)
    if not labels:
        return None

    counts: Counter = Counter()
    by_platform: dict[str, Counter] = {}
    items = []
    for n, label in sorted(labels.items()):
        post = posts[n - 1]
        platform = post.get("source", "") or "其他"
        counts[label["stance"]] += 1
        by_platform.setdefault(platform, Counter())[label["stance"]] += 1
        items.append({
            "n": n, "stance": label["stance"], "why": label["why"],
            "title": post.get("title", ""), "url": post.get("url", ""), "source": platform,
        })

    total = sum(counts.values())
    return {
        "issue": issue,
        "categories": list(cats),
        "total": total,
        # 依 categories 的順序輸出，前端配色才不會每次跳動；
        # 沒人講的類別也保留 0，讓「完全沒有反對聲音」這件事看得見。
        "counts": {s: counts.get(s, 0) for s in cats},
        "percent": {s: round(counts.get(s, 0) * 100 / total) for s in cats},
        "by_platform": {p: {s: c.get(s, 0) for s in cats} for p, c in by_platform.items()},
        "items": items,
        "low_sample": total < MIN_SAMPLE,   # 前端據此標警語、改秀則數而非百分比
        "min_sample": MIN_SAMPLE,
    }
