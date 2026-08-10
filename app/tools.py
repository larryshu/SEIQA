"""工具定義 + 分派。對應 Hermes 的「skill」：LLM 用 tool calling 自己決定何時呼叫。

對 LLM 只暴露一個 skill：community_search —— 內部並行 fan-out 到所有啟用的社群平台
（目前 Dcard / PTT / Threads，皆即時爬），合併各邊討論（各帶平台標籤）。各平台的 adapter
在 sources.py 的 registry，加平台只要加 adapter、不動這裡。如此「每一邊都會查」是程式
保證的，不靠 LLM 記得逐一叫工具。

平台名稱刻意不寫死在本檔：給 LLM 的「哪些平台有／沒有資料」提示一律用 fan-out 回報的
platforms 組出來（見 _community_search）。

crawl_dcard（Dcard 即時爬）因 Cloudflare 已停用，程式碼保留在 crawler.py / 下方 _crawl_dcard。
"""
from __future__ import annotations

import json

from . import crawler, progress, relevance, stance, tracing
from .config import settings
from .sources import PLATFORM_LABELS as _PLATFORM_LABEL
from .sources import community_search as _fanout_search
from .store import store
from .tracing import observe

# 給 LLM 看的工具清單（function calling schema）。description 寫清楚「何時該用」＝觸發條件。
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "community_search",
            "description": (
                "查網路社群討論：會『同時』即時爬所有已啟用的社群平台，撈與使用者問題相關的"
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


@observe(as_type="tool", capture_input=False)
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

    # span 直接以工具名命名（trace 樹上一眼看出這輪叫了哪個 skill）。capture_input 關掉是
    # 因為 sources/charts 是 out-param：它們在第二輪已經裝著整份貼文清單，自動擷取會把
    # 那幾萬字重複記進每個 tool span。這裡只留真正是「呼叫參數」的 arguments。
    tracing.set_span(name=name, input={"arguments": args, "user_query": user_query})

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
    """並行查所有啟用的社群平台，合併各邊討論。沒命中→請 LLM 退回常識。

    end_user_id：有的話，平台會依該使用者的 included/excluded_platforms 偏好過濾（M5）。

    平台名稱一律取自 fan-out 回報的 platforms，不在這裡寫死清單——啟用哪些平台是後台
    與使用者偏好決定的，寫死會讓新增的平台永遠不出現在給 LLM 的提示裡。
    """
    posts, platforms = _fanout_search(query, end_user_id=end_user_id)
    labels = dict(platforms)
    if not posts:
        names = [label for _, label in platforms]
        tried = ("、".join(names) + "都") if len(names) > 1 else (names[0] if names else "社群平台")
        return (
            f"（{tried}沒有相關討論。請改用你既有的常識／經驗回答，"
            "並自然地說一句這次沒在社群找到相關討論，不要杜撰來源。）"
        )
    store.save(session_id, posts)
    if sources is not None:
        sources.extend(posts)  # 收集來源（順序即 [n]、含 source 平台標籤），供前端分流

    # 明講這次哪些平台有/沒有資料 → 防止 LLM 對沒撈到的平台杜撰討論
    present = {p.get("source") for p in posts}
    have = [label for name, label in platforms if name in present]
    missing = [label for name, label in platforms if name not in present]
    note = "本次有撈到資料的平台：" + "、".join(have) + "。"
    if missing:
        note += (
            "（" + "、".join(missing) + " 這次沒有撈到相關討論——回答時就只根據上面實際有的"
            "來源講，不要假裝引用了它、也不要說它上面有什麼討論。）"
        )

    lines = []
    for i, p in enumerate(posts):
        src = p.get("source", "")
        label = labels.get(src) or _PLATFORM_LABEL.get(src, src)
        lines.append(f"[{i + 1}]（{label}）{p['title']}\n{p['content']}\n來源：{p['url']}")

    # 引用規則重複貼在貼文清單「之後」，而不是只放在前面：這批內容動輒上萬字（實測一題
    # 92 則），開頭的指示會被整個稀釋掉——模型於是照抄字面的「[n]」而不是填實際編號。
    # 這與 agent._refresh_recap_hint 解決回顧提醒被蓋掉是同一招：要求緊鄰生成點。
    cite_rule = (
        f"【引用規則——務必照做】上面每則討論開頭中括號裡的數字就是它的編號（本次是 1～{len(posts)}）。"
        "當某個說法來自其中某則時，在該句句尾標上『那一則的實際編號』，"
        "例如引用第 3 則就寫 [3]、引用第 17 則就寫 [17]。\n"
        "**「n」只是代號，不是要你輸出的字。絕對不可以在答案裡出現「[n]」這三個字元**——"
        f"那樣讀者點不到來源，等同假引用。每個中括號裡都必須是 1～{len(posts)} 之間的實際數字。\n"
        "不用每句都標，也不要讓來源變成回答的主角；沒有對應貼文的句子就不要標。"
    )
    return (
        note + "\n\n以下為各社群平台撈到的相關討論（開頭括號標了編號與來源平台）。請『綜合』"
        "實際有的來源消化後回答，並在敘述中自然帶出某個說法來自哪個平台：\n\n"
        + "\n\n".join(lines) + "\n\n" + cite_rule
    )


def _stance_breakdown(issue: str, session_id: str, sources: list | None, charts: list | None,
                      categories=None) -> str:
    """統計已撈到的貼文對 issue 的態度分佈。統計由 stance.py 的 Python 端做，不是 LLM 估的。

    來源優先序：
      1. 這一輪 community_search 命中的 sources（順序即畫面上的 [n]）；
      2. 這一輪沒查 → 沿用『上一輪』抓到的貼文（store.latest）。使用者說「根據上面的結論
         畫個圖」時模型通常不會再查一次，沒有這條路就只能回「查不到資料」——資料明明還在手邊。
    兩種情況都不重爬。

    走第 2 條時多一道語意檢查：沿用的貼文必須真的在講這個 issue。同一場對話問過好幾個
    話題時，「上一輪」不保證就是使用者現在要統計的那一輪（例：問完輝達，回頭要慈濟那題的
    圖）。實測兩批真實貼文對「慈濟被詐騙10億這件事」的分數是——對題 0.387~0.719、
    離題 0.163~0.355，中間有明顯空隙，故門檻取 0.35（對題全留、離題幾乎全丟）。
    統計結果被別的話題汙染，比查不到資料更糟：那是看起來有憑有據的錯。

    categories：使用者指定的分類軸（同情／嘲笑／無感…）；留空＝贊成／反對／中立。
    """
    posts = list(sources or [])
    if not posts:
        try:
            posts = store.latest(session_id)   # 追問路徑（QdrantHotStore 未實作 → 當作沒有）
        except NotImplementedError:
            posts = []
        before = len(posts)
        if posts:
            posts = relevance.rerank(
                issue, posts, settings.stance_reuse_min_score,
                lambda p: (p.get("title") or "") + " " + (p.get("content") or "")[:300],
                platform="stance_reuse")
        if not posts and before:
            return (
                "（手邊沿用的是先前另一個話題的討論，跟這次要統計的議題不符，不能拿來充數。"
                "請先呼叫 community_search 重查這個議題再統計；在那之前不要自己估比例、不要畫圖。）"
            )
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
        "請用『文字』說明這個分佈代表什麼、兩邊各在意什麼"
        "（可引用貼文編號，寫成 [3] [17] 這種實際數字，不要寫成 [n]）。"
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
    return ("以下為站內搜尋抓到的相關討論，請據此回答，並在句尾標上該則的實際編號"
            "（例如 [3]，不要寫成 [n]）：\n\n" + "\n\n".join(lines))
