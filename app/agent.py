"""Agent loop：LLM ↔ 工具的多輪循環（規劃→工具→行動），借鑑 Hermes 的自主工具呼叫。

流程：把 system + 對話歷史 + 提問丟給 LLM →
  - 它若決定要最新資訊 → 回 tool_calls → 我們執行 community_search（並行即時爬 Dcard+PTT）→ 把結果塞回 →再問一次
  - 它若覺得夠了 → 直接回文字答案
fail-safe：工具炸掉/沒結果，crawler 與 tools 已各自吞例外，最終一定回得了話。

兩個入口，共用 _build_context()（prompt / 偏好 / 記憶組裝）：
  run()            → 阻塞式，一次回完整答案。/ask 走這條，行為與加串流前完全相同。
  run_streaming()  → 同樣的 loop，但過程中用 progress.emit() 推事件（含逐字 token），
                     並在每個階段檢查取消。/ws/ask 走這條。
兩者刻意不共用 loop：串流與非串流的 LLM 呼叫語意有差，隔離開來才不會讓已驗證過的 /ask
被串流路徑的問題波及。
"""
from __future__ import annotations

from typing import NamedTuple

from . import llm, progress, user_memory
from .config import settings
from .config_repo import repo
from .llm import chat_with_tools
from .tools import TOOLS, dispatch

SYSTEM_PROMPT = (
    "你是一個熟悉網路鄉民討論的貼心朋友，不是制式的查詢助理。"
    "當問題需要鄉民民間討論／口碑／心得／時事時，用 community_search 工具——"
    "它會『同時』即時爬 Dcard 與 PTT，把兩邊討論一起撈回來。"
    "純常識、定義、計算等不需要鄉民經驗的問題，直接回答即可、不用查。"
    "\n\n"
    "【回答方式——這是重點】"
    "不要把抓到的貼文做成『重點1、重點2』的條列摘要或讀書報告。"
    "請先把這些討論讀進去、消化吸收，再像朋友一樣用自己的話回應："
    "先同理對方的處境與心情，給出有溫度、有立場的建議與看法，"
    "把網友的經驗自然融進你的話裡（例如『其實滿多人會…，我自己也覺得…』），"
    "而不是逐則轉述。可以有你自己的判斷與取捨，不必中立地把所有說法都列出來。"
    "語氣口語、自然，像在跟朋友聊天，而不是寫條目。"
    "\n\n"
    "【綜合來源 + 引用】"
    "抓回來的討論開頭會標來源平台（Dcard / PTT）。請『綜合』實際有抓到的來源一起講，"
    "可以自然帶出差異或出處，例如『Dcard 上比較多人說…，PTT 鄉民則覺得…』。"
    "工具會註明這次哪些平台沒有資料；沒有資料的平台就完全不要提、不要假裝它上面有討論。"
    "當某個具體說法來自抓到的討論時，在句尾自然帶上 [n]，不用每句都標、"
    "也不要讓來源變成回答的主角。不要杜撰來源。"
    "\n\n"
    "【比例與圖表】"
    "當使用者問『比例』『幾成』『多少人覺得』『正反意見如何』或要圖表時，"
    "先 community_search 撈討論，再呼叫 stance_breakdown 工具做立場統計——它會逐則判讀並"
    "由程式加總，前端會直接把結果畫成圖。"
    "**你自己絕對不要估算百分比**（沒數過的『大概六四開』就是杜撰），"
    "**也絕對不要用文字、方塊或符號拼出長條圖／圓餅圖**——那不是圖，是雜訊。"
    "統計出來之後，你的工作是用『文字』解釋這個分佈代表什麼、兩邊各在意什麼。"
    "\n\n"
    "【兩邊都沒有相關資料時】"
    "就以朋友的身分用既有常識／經驗給建議，並誠實說這次沒在 Dcard 與 PTT 找到相關討論。"
)

# 2 輪：一輪 community_search 撈討論，必要時第二輪 stance_breakdown 做立場統計。
# （單一 community_search 內部已並行查兩邊，所以「查」本身一輪就夠。）
MAX_TOOL_ROUNDS = 2


def _apply_pref_modifiers(prompt: str, prefs: dict) -> str:
    """把使用者偏好（語氣／長度／語言）以附加指示貼到 system prompt 後面（M5）。"""
    extra = []
    if prefs.get("tone"):
        extra.append(f"語氣請偏向：{prefs['tone']}。")
    if prefs.get("answer_length"):
        extra.append(f"回答長度請控制在：{prefs['answer_length']}。")
    if prefs.get("language"):
        extra.append(f"請用這個語言回答：{prefs['language']}。")
    return (prompt + "\n\n【使用者個人偏好】" + " ".join(extra)) if extra else prompt


def _apply_memory(prompt: str, memories: list[str], meta: bool = False) -> str:
    """把使用者長期記憶附到 system prompt 後。

    meta=True：使用者在問『你記得我什麼 / 之前聊過什麼』→ 據實列出回答（沒有就誠實說沒有）。
    meta=False：一般問題 → 記憶當背景個人化，僅相關時參考、不直接複述。
    """
    if meta:
        if memories:
            lines = "\n".join(f"- {m}" for m in memories)
            return (prompt + "\n\n【使用者正在問你記得他/她什麼、或之前聊過什麼。以下是你對這位"
                    "使用者的長期記憶，請據實、自然地用這些內容回答】\n" + lines)
        return (prompt + "\n\n【使用者在問你記得他什麼，但目前還沒有記錄到關於這位使用者的長期"
                "記憶。請誠實說明還沒有、並自然邀請他多聊聊自己，而不是說『看不到對話紀錄』】")
    if not memories:
        return prompt
    lines = "\n".join(f"- {m}" for m in memories)
    return (prompt + "\n\n【關於這位使用者（過去對話的長期記憶，僅在與本題相關時參考，"
            "不要硬湊、也不要直接複述）】\n" + lines)


def _apply_thread_context(prompt: str, threads: list[str]) -> str:
    """把『先前相關對話的脈絡』(thread 記憶) 附到 system prompt 後。

    舊版只寫『供了解使用者背景』，模型於是把它當默讀資料——讀了卻一個字都不提，使用者
    完全感覺不到記憶生效。現在改成請它『開場先回顧一兩句，主體仍以本次查到的為準』：
    溫故（喚回聊過的內容）與知新（本次最新風向）並存，且能點出兩者的差異。

    settings.thread_recap_enabled 關掉時退回舊行為（只當背景、不明講）——與
    user_thread_enabled 不同層級：那個是關掉整條脈絡軌，這個只關『說出來』這件事。
    """
    if not threads:
        return prompt
    lines = "\n".join(f"- {t}" for t in threads)
    if not settings.thread_recap_enabled:
        return (prompt + "\n\n【先前相關對話的脈絡（供了解使用者背景、回顧先前討論過的重點；"
                "若使用者要最新狀況，仍以本次查到的最新討論為準）】\n" + lines)
    return (prompt + "\n\n【先前相關對話的脈絡——你和這位使用者聊過的內容。使用方式：\n"
            "1. 只要脈絡與本題『主題相關』就要回顧——**不必是同一件事**，這次問得比較廣、"
            "換了對象或換了時間點都算相關。開場先用一兩句講明那是之前聊過的、當時的重點是"
            "什麼（例：你之前問過台北市那次放颱風假的評價，當時社群主要分成…）；\n"
            "2. 回顧只是引子——主體與結論一律以本次查到的最新討論為準，不可讓舊梗概"
            "取代或稀釋這次的內容；\n"
            "3. 回顧的句子不可標 [n]：[n] 只屬於本次查到的貼文，舊脈絡沒有對應來源，"
            "標上去就是假出處；\n"
            "4. 若本次查到的風向和先前討論不同，明確點出變化（例：上次討論時主流是…，"
            "這次多了…），這比單純複述更有價值；\n"
            "5. 若脈絡與本題其實不夠相關，就完全不要提——硬扯比不提更糟。】\n" + lines)


# meta 問題（「你記得我什麼」）會列出全部記憶注入 prompt；但交給追問建議器時截短——
# 建議器只需要「這個人是誰」來選面向，不需要整份清單，也不值得為它多花 token。
_SUGGEST_MEMORY_CAP = 8


def _refresh_recap_hint(messages: list[dict], ctx: "_RunContext") -> None:
    """把回顧提醒移到 messages 最尾端（沒有脈絡就什麼都不做）。

    為什麼需要這個：工具結果很長時，system prompt 裡的回顧要求會被稀釋掉。實測同一份
    prompt／模型／溫度，工具回傳 959 字時模型會回顧，9,106 字（83 篇貼文）就完全不提了；
    而且 community_search 的回傳自己結尾就是「請綜合這些來源回答、用 [n] 標注」，近因上
    壓過了 system。所以每輪工具跑完都把提醒重新貼到最後，確保它緊鄰生成點。
    """
    if not ctx.recap_hint:
        return
    for m in [m for m in messages if m.get("content") == ctx.recap_hint]:
        messages.remove(m)
    messages.append({"role": "system", "content": ctx.recap_hint})


class _RunContext(NamedTuple):
    """一輪對話的所有已解析設定：run() 與 run_streaming() 共用，確保兩條路徑行為一致。"""

    messages: list[dict]
    tools: list[dict]
    model: str | None
    temperature: float
    max_rounds: int
    memories: list[str]  # 本輪撈回的『使用者原子事實』；順著回傳給追問建議器做個人化
    recap_hint: str      # 命中脈絡時的回顧提醒；空＝沒脈絡或關閉（見 _refresh_recap_hint）


def _build_context(user_message: str, history: list[dict] | None,
                   end_user_id: int | None) -> _RunContext:
    """組 system prompt（偏好 + 記憶 + 脈絡）與各項設定，並鋪好 messages 陣列。

    M3：優先用後台『啟用中 agent』的設定（prompt / model / temperature / max_tool_rounds /
    tools）；後台沒設或 DB 連不上時，fall back 到本檔寫死值與 .env（fail-safe）。
    """
    cfg = repo.get_active_agent() or {}
    prefs = repo.get_user_preferences(end_user_id) if end_user_id else {}
    # 取值優先序：user_preference > agent > system_setting/.env
    system_prompt = _apply_pref_modifiers(cfg.get("system_prompt") or SYSTEM_PROMPT, prefs)
    memories: list[str] = []
    threads: list[str] = []
    if end_user_id:  # 登入使用者：meta 問題列出全部記憶；一般問題語意撈回（皆 fail-safe）
        meta = user_memory.is_memory_query(user_message)
        if meta:
            listed = user_memory.list_memories(end_user_id)
            memories = listed[:_SUGGEST_MEMORY_CAP]  # meta 問題會列出全部，給建議器時截短
            n_facts = len(listed)                    # 回報「實際注入」的量，不是截短後的
            system_prompt = _apply_memory(system_prompt, listed, meta=True)
        else:
            memories = user_memory.recall(end_user_id, user_message)
            n_facts = len(memories)
            system_prompt = _apply_memory(system_prompt, memories)
            # 脈絡記憶（thread）另一條：命中相關舊對話 → 注入背景區塊（皆 fail-safe）
            threads = user_memory.recall_threads(end_user_id, user_message)
            system_prompt = _apply_thread_context(system_prompt, threads)
        # 讓「記憶有沒有生效」在前端看得見：脈絡注入後會刻意退居背景（答案仍以本次爬到的
        # 最新討論為準），使用者因此感覺不到它。這裡只回報有沒有載到、載了幾則，不動答案。
        # 沒撈到就不發，避免每題都多一行雜訊；/ask 沒有訂閱者時 emit 是 no-op。
        if n_facts or threads:
            progress.emit("memory_loaded", facts=n_facts, threads=len(threads), meta=meta)
    recap_hint = ""
    if threads and settings.thread_recap_enabled:
        recap_hint = (
            "（提醒：本輪有【先前相關對話的脈絡】。請照 system 的指示——開場先用一兩句回顧"
            "之前聊過的重點再進入主體；回顧那句不可標 [n]；主體與結論仍以上面查到的最新討論"
            "為準；風向有變就點出差異。若脈絡與本題確實不相關，就完全不要提。）"
        )

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    return _RunContext(
        messages=messages,
        tools=repo.get_tools() or TOOLS,
        model=prefs.get("model") or cfg.get("model"),  # None → llm 用 settings.chat_model
        temperature=cfg.get("temperature", 0.2),
        max_rounds=cfg.get("max_tool_rounds") or MAX_TOOL_ROUNDS,
        memories=memories,
        recap_hint=recap_hint,
    )


def run(user_message: str, history: list[dict] | None = None, session_id: str = "default",
        end_user_id: int | None = None) -> dict:
    """跑一輪對話（阻塞式，一次回完整答案）。回傳 {answer, used_tools, sources, messages, memories}。

    memories：本輪撈回的使用者原子事實，順帶回傳供追問建議器個人化——刻意不讓 suggest 自己
    再 recall 一次：一來同 query 同 collection 結果一樣、白付一次 embed；二來 api 那邊
    remember() 跑在 suggest 之前，重搜會高分命中剛寫進去的本輪事實，等於把問題換句話說餵回去。
    """
    ctx = _build_context(user_message, history, end_user_id)
    messages = ctx.messages

    used_tools: list[str] = []
    sources: list[dict] = []  # 實際抓到的來源（依 [n] 順序），供前端渲染
    charts: list[dict] = []   # stance_breakdown 的統計結果（有呼叫才會有）
    for _ in range(ctx.max_rounds):
        msg = chat_with_tools(messages, ctx.tools, temperature=ctx.temperature, model=ctx.model)
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return {"answer": msg.content or "", "used_tools": used_tools, "sources": sources,
                    "chart": charts[-1] if charts else None, "messages": messages,
                    "memories": ctx.memories}

        # 有 tool_calls：先把 assistant 這輪（含 tool_calls）原樣存回，再逐一執行
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            used_tools.append(tc.function.name)
            result = dispatch(tc.function.name, tc.function.arguments, session_id,
                              user_query=user_message, sources=sources,
                              end_user_id=end_user_id, charts=charts)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )
        _refresh_recap_hint(messages, ctx)  # 工具結果很長會蓋掉 system 的回顧要求

    # 工具輪數用完 → 收尾這一刀 tool_choice="none"：不准再叫工具，逼它用手上的資料回話。
    # （否則模型可能再要一次工具、content 回空，使用者就會看到「已達工具呼叫上限」那句廢話。）
    final = chat_with_tools(messages, ctx.tools, temperature=ctx.temperature, model=ctx.model,
                            tool_choice="none")
    answer = final.content or "（已達工具呼叫上限，請換個問法或縮小範圍。）"
    messages.append({"role": "assistant", "content": answer})
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages,
            "memories": ctx.memories}


def _stream_once(ctx: _RunContext, messages: list[dict],
                 tool_choice: str = "auto") -> tuple[dict, bool]:
    """跑一次串流補全：token 邊收邊 emit。回 (assistant message dict, 是否吐過 token)。"""
    streamed = False
    msg: dict = {}
    for kind, payload in llm.chat_with_tools_stream(
            messages, ctx.tools, temperature=ctx.temperature, model=ctx.model,
            tool_choice=tool_choice):
        if kind == "token":
            streamed = True
            progress.emit("token", text=payload)
        else:
            msg = payload  # type: ignore[assignment]
    return msg, streamed


def run_streaming(user_message: str, history: list[dict] | None = None,
                  session_id: str = "default", end_user_id: int | None = None) -> dict:
    """與 run() 同樣的 loop 與回傳值，但過程中用 progress.emit() 推事件、並可被取消。

    事件在 progress.session() 內才有訂閱者；取消會從任一檢查點拋 Cancelled 給呼叫端。
    """
    ctx = _build_context(user_message, history, end_user_id)
    messages = ctx.messages

    used_tools: list[str] = []
    sources: list[dict] = []
    charts: list[dict] = []
    progress.emit("stage", stage="planning", text="判斷這題需不需要查社群討論…")

    for _ in range(ctx.max_rounds):
        progress.raise_if_cancelled()
        msg, streamed = _stream_once(ctx, messages)

        if not msg.get("tool_calls"):  # 不需查（🟡 常識題）→ 剛剛串出去的就是答案
            answer = msg.get("content") or ""
            if not streamed:  # 模型沒串出東西（極少見）→ 補送一次，前端才有內容
                progress.emit("token", text=answer)
            messages.append({"role": "assistant", "content": answer})
            return {"answer": answer, "used_tools": used_tools, "sources": sources,
                    "chart": charts[-1] if charts else None, "messages": messages,
                    "memories": ctx.memories}

        # 少數模型會在決定用工具前先吐幾個字。那些字不是答案 → 請前端把已印出的清掉。
        if streamed:
            progress.emit("answer_reset")

        messages.append(msg)
        for tc in msg["tool_calls"]:
            progress.raise_if_cancelled()
            name = tc["function"]["name"]
            used_tools.append(name)
            progress.emit("tool_start", tool=name, arguments=tc["function"]["arguments"])
            result = dispatch(name, tc["function"]["arguments"], session_id,
                              user_query=user_message, sources=sources,
                              end_user_id=end_user_id, charts=charts)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            progress.emit("tool_done", tool=name, found=len(sources))
        _refresh_recap_hint(messages, ctx)  # 工具結果很長會蓋掉 system 的回顧要求

    progress.raise_if_cancelled()
    progress.emit("stage", stage="answering", text="讀完討論了，開始生成回答…")
    final, streamed = _stream_once(ctx, messages, tool_choice="none")  # 收尾：不准再叫工具
    answer = final.get("content") or "（已達工具呼叫上限，請換個問法或縮小範圍。）"
    if not streamed:
        progress.emit("token", text=answer)
    messages.append({"role": "assistant", "content": answer})
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages,
            "memories": ctx.memories}
