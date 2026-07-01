"""工具定義 + 分派。對應 Hermes 的「skill」：LLM 用 tool calling 自己決定何時呼叫。

對 LLM 只暴露一個 skill：community_search —— 內部並行 fan-out 到 Dcard 口碑庫 + 即時爬 PTT，
合併兩邊討論（各帶平台標籤）。各平台的 adapter 在 sources.py 的 registry，加平台只要加 adapter、
不動這裡。如此「兩邊一定都查」是程式保證的，不靠 LLM 記得同時叫兩個工具。

crawl_dcard（Dcard 即時爬）因 Cloudflare 已停用，程式碼保留在 crawler.py / 下方 _crawl_dcard。
"""
from __future__ import annotations

import json

from . import crawler
from .sources import community_search as _fanout_search
from .store import store

_PLATFORM_LABEL = {"dcard": "Dcard 口碑庫", "ptt": "PTT"}

# 給 LLM 看的工具清單（function calling schema）。description 寫清楚「何時該用」＝觸發條件。
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "community_search",
            "description": (
                "查網路社群討論：會『同時』查 Dcard 口碑庫與即時爬 PTT，撈與使用者問題相關的"
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
    }
]


def dispatch(name: str, arguments: str, session_id: str,
             user_query: str = "", sources: list | None = None,
             end_user_id: int | None = None) -> str:
    """執行一個 tool call，回傳塞回對話的字串結果（已標來源平台，供 LLM 綜合與引用）。

    user_query：使用者原始問句，作為各來源檢索的預設查詢字串。
    sources：若給一個 list，會把實際命中的來源（依 [n] 順序、含 source 平台標籤）append 進去，
    供上層前端分流渲染。
    """
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    if name == "community_search":
        query = (args.get("query") or "").strip() or user_query
        return _community_search(query, session_id, sources, end_user_id)
    if name == "crawl_dcard":  # 已停用，保留以便日後切回 Dcard 即時爬
        return _crawl_dcard(args.get("board", ""), user_query, session_id, sources)
    return f"[tool error] 未知工具：{name}"


def _community_search(query: str, session_id: str, sources: list | None = None,
                      end_user_id: int | None = None) -> str:
    """並行查 Dcard 口碑庫 + PTT，合併兩邊討論。沒命中→請 LLM 退回常識。

    end_user_id：有的話，平台會依該使用者的 included/excluded_platforms 偏好過濾（M5）。
    """
    posts = _fanout_search(query, end_user_id=end_user_id)
    if not posts:
        return (
            "（Dcard 口碑庫與 PTT 都沒有相關討論。請改用你既有的常識／經驗回答，"
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
