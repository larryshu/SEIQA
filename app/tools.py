"""工具定義 + 分派。對應 Hermes 的「skill」：LLM 用 tool calling 自己決定何時呼叫。

對 LLM 只暴露一個 skill：community_search —— 內部並行 fan-out 到 Dcard + PTT（皆即時爬），
合併兩邊討論（各帶平台標籤）。各平台的 adapter 在 sources.py 的 registry，加平台只要加 adapter、
不動這裡。如此「兩邊一定都查」是程式保證的，不靠 LLM 記得同時叫兩個工具。

crawl_dcard（Dcard 即時爬）因 Cloudflare 已停用，程式碼保留在 crawler.py / 下方 _crawl_dcard。
"""
from __future__ import annotations

import json

from . import crawler, progress, stance
from .sources import community_search as _fanout_search
from .store import store

_PLATFORM_LABEL = {"dcard": "Dcard", "ptt": "PTT"}

# 給 LLM 看的工具清單（function calling schema）。description 寫清楚「何時該用」＝觸發條件。
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "community_search",
            "description": (
                "查網路社群討論：會『同時』即時爬 Dcard 與 PTT，撈與使用者問題相關的"
                "鄉民口碑／心得／評價／經驗／時事討論。當問題需要鄉民實際討論"
                "（感情、理財、3C 評價、工作、時事、產品心得等）時呼叫此工具；"
                "純常識、定義、計算等不需鄉民經驗就能回答時，不要呼叫、直接回答即可。"
                "查詢字串會自動帶入使用者的原始問句。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "（選填）檢索關鍵字。留空就用使用者原始問句；太口語可改寫得更聚焦。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stance_breakdown",
            "description": (
                "統計『這次已撈到的社群討論』對某個議題的態度分佈，回傳結構化數據，"
                "前端會直接把它畫成圖。預設分成贊成／反對／中立，"
                "但使用者若問的是別的軸（例如同情／嘲笑／無感），就用 categories 指定那幾類。"
                "當使用者問『比例』『幾成』『多少人覺得』『正反意見如何』或要求圖表時呼叫。"
                "本工具只統計『已經抓到的貼文』、不會自己去爬："
                "這一輪若沒查，會自動沿用本次對話先前抓到的討論（所以使用者說『根據上面的結論畫圖』"
                "時直接呼叫即可）；若是全新的話題，請先呼叫 community_search。"
                "你絕對不要自己估算百分比、也不要用文字或符號畫圖表。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue": {
                        "type": "string",
                        "description": (
                            "要判讀的『議題陳述句』，必須是一句可以表態的肯定句，"
                            "例如「中國勢力介入台灣選舉的情況很嚴重」或「矢板明夫被襲擊這件事」。"
                            "不要放問句、不要放關鍵字。"
                        ),
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "（選填）分類軸，2～5 類，直接用使用者問的那幾類，"
                            "例如 [\"同情\", \"負面嘲笑\", \"無感\"]。"
                            "留空＝預設的 [\"贊成\", \"反對\", \"中立\"]。"
                            "**使用者若指名了要看哪幾種反應，一定要照他說的填，不要硬套贊成／反對。**"
                        ),
                    },
                },
                "required": ["issue"],
            },
        },
    },
]


def dispatch(name: str, arguments: str, session_id: str,
             user_query: str = "", sources: list | None = None,
             end_user_id: int | None = None, charts: list | None = None) -> str:
    """執行一個 tool call，回傳塞回對話的字串結果（已標來源平台，供 LLM 綜合與引用）。

    user_query：使用者原始問句，作為各來源檢索的預設查詢字串。
    sources：若給一個 list，會把實際命中的來源（依 [n] 順序、含 source 平台標籤）append 進去，
    供上層前端分流渲染。
    charts：同樣是 out-param——stance_breakdown 會把可畫圖的結構化數據 append 進去，
    讓 agent 一路帶回 /ask 的回應與 done 事件（兩個前端都拿得到，不是只有 WebSocket 那條）。
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    if name == "community_search":
        query = (args.get("query") or "").strip() or user_query
        return _community_search(query, session_id, sources, end_user_id)
    if name == "stance_breakdown":
        return _stance_breakdown(args.get("issue", "").strip() or user_query, session_id,
                                 sources, charts, args.get("categories"))
    if name == "crawl_dcard":  # 已停用，保留以便日後切回 Dcard 即時爬
        return _crawl_dcard(args.get("board", ""), user_query, session_id, sources)
    return f"[tool error] 未知工具：{name}"


def _community_search(query: str, session_id: str, sources: list | None = None,
                      end_user_id: int | None = None) -> str:
    """並行查 Dcard + PTT，合併兩邊討論。沒命中→請 LLM 退回常識。

    end_user_id：有的話，平台會依該使用者的 included/excluded_platforms 偏好過濾（M5）。
    """
    posts = _fanout_search(query, end_user_id=end_user_id)
    if not posts:
        return (
            "（Dcard 與 PTT 都沒有相關討論。請改用你既有的常識／經驗回答，"
            "並自然地說一句這次沒在社群找到相關討論，不要杜撰來源。）"
        )
    store.save(session_id, posts)
    if sources is not None:
        sources.extend(posts)  # 收集來源（順序即 [n]、含 source 平台標籤），供前端分流

    # 明講這次哪些平台有/沒有資料 → 防止 LLM 對沒撈到的平台杜撰討論
    present = {p.get("source") for p in posts}
    have = [_PLATFORM_LABEL[s] for s in ("dcard", "ptt") if s in present]
    missing = [_PLATFORM_LABEL[s] for s in ("dcard", "ptt") if s not in present]
    note = "本次有撈到資料的平台：" + "、".join(have) + "。"
    if missing:
        note += (
            "（" + "、".join(missing) + " 這次沒有撈到相關討論——回答時就只根據上面實際有的"
            "來源講，不要假裝引用了它、也不要說它上面有什麼討論。）"
        )

    lines = []
    for i, p in enumerate(posts):
        label = _PLATFORM_LABEL.get(p.get("source", ""), p.get("source", ""))
        lines.append(f"[{i + 1}]（{label}）{p['title']}\n{p['content']}\n來源：{p['url']}")
    return (
        note + "\n\n以下為各社群平台撈到的相關討論（開頭括號標了來源平台）。請『綜合』實際有的"
        "來源消化後回答，用 [n] 標注引用，並在敘述中自然帶出某個說法是來自 Dcard 還是 PTT：\n\n"
        + "\n\n".join(lines)
    )


def _stance_breakdown(issue: str, session_id: str, sources: list | None, charts: list | None,
                      categories=None) -> str:
    """統計已撈到的貼文對 issue 的態度分佈。統計由 stance.py 的 Python 端做，不是 LLM 估的。

    來源優先序：
      1. 這一輪 community_search 命中的 sources（順序即畫面上的 [n]）；
      2. 這一輪沒查 → 沿用本次對話先前抓到的貼文（store）。使用者說「根據上面的結論畫個圖」
         時模型通常不會再查一次，沒有這條路就只能回「查不到資料」——資料明明還在手邊。
    兩種情況都不重爬。
    categories：使用者指定的分類軸（同情／嘲笑／無感…）；留空＝贊成／反對／中立。
    """
    posts = list(sources or [])
    if not posts:
        try:
            posts = store.all(session_id)      # 追問路徑（QdrantHotStore 未實作 → 當作沒有）
        except NotImplementedError:
            posts = []
        if posts and sources is not None:
            # 把沿用的貼文補進這一輪的 sources：前端才列得出來源清單，
            # 圖上的 [n] 也才跟畫面上的編號對得起來。
            sources.extend(posts)

    if not posts:
        return (
            "（這一輪沒有撈到任何社群討論，無法統計立場。請先呼叫 community_search；"
            "若本來就查不到資料，就誠實說沒有可統計的來源——不要自己估比例、不要畫圖。）"
        )

    data = stance.breakdown(issue, posts, categories)
    if not data:
        return "（立場判讀失敗，這次沒有統計結果。請照常用文字回答，不要自己估比例、不要畫圖。）"

    progress.emit("chart", **data)   # WebSocket 前端收到就即時畫圖（/ask 那條靠回傳值，見下）
    if charts is not None:
        charts.append(data)

    counts = "、".join(f"{s} {n} 則" for s, n in data["counts"].items())
    percent = "、".join(f"{s} {p}%" for s, p in data["percent"].items())
    note = (
        f"（注意：樣本只有 {data['total']} 則，少於 {data['min_sample']} 則，"
        "講的時候要說明這只是這次抓到的樣本、不代表整體民意。）"
        if data["low_sample"] else ""
    )
    return (
        f"立場統計完成（議題：{issue}）。共判讀 {data['total']} 則：{counts}；比例：{percent}。{note}\n"
        "圖表已經由前端畫出來、顯示在使用者畫面上了。\n"
        "請用『文字』說明這個分佈代表什麼、兩邊各在意什麼（可引用 [n]）。"
        "不要重畫圖、不要用文字符號拼圖表，也不要改動上面的數字。"
    )


def _crawl_dcard(board: str, query: str, session_id: str, sources: list | None = None) -> str:
    """【已停用】Dcard 即時爬（Cloudflare 阻擋）。保留以便日後反爬解了切回。"""
    posts = crawler.crawl(board=board, query=query)
    if not posts:
        return "（站內搜尋沒有相關結果或失敗，請改用既有知識回答。）"
    store.save(session_id, posts)
    if sources is not None:
        sources.extend(posts)
    lines = [
        f"[{i + 1}] {p['title']}（{p['created_at']}）\n{p['content']}\n來源：{p['url']}"
        for i, p in enumerate(posts)
    ]
    return "以下為站內搜尋抓到的相關討論，請據此回答並用 [n] 標注引用：\n\n" + "\n\n".join(lines)
