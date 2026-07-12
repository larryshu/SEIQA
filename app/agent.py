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

    脈絡含『先前討論過的重點梗概』，可用來回顧、喚回聊過的內容；但加註『要最新狀況仍以本次
    查到為準』，避免模型把舊梗概當成當下事實、不再即時查證。
    """
    if not threads:
        return prompt
    lines = "\n".join(f"- {t}" for t in threads)
    return (prompt + "\n\n【先前相關對話的脈絡（供了解使用者背景、回顧先前討論過的重點；"
            "若使用者要最新狀況，仍以本次查到的最新討論為準）】\n" + lines)


class _RunContext(NamedTuple):
    """一輪對話的所有已解析設定：run() 與 run_streaming() 共用，確保兩條路徑行為一致。"""

    messages: list[dict]
    tools: list[dict]
    model: str | None
    temperature: float
    max_rounds: int


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
    if end_user_id:  # 登入使用者：meta 問題列出全部記憶；一般問題語意撈回（皆 fail-safe）
        if user_memory.is_memory_query(user_message):
            system_prompt = _apply_memory(
                system_prompt, user_memory.list_memories(end_user_id), meta=True)
        else:
            system_prompt = _apply_memory(
                system_prompt, user_memory.recall(end_user_id, user_message))
            # 脈絡記憶（thread）另一條：命中相關舊對話 → 注入背景區塊（皆 fail-safe）
            system_prompt = _apply_thread_context(
                system_prompt, user_memory.recall_threads(end_user_id, user_message))

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    return _RunContext(
        messages=messages,
        tools=repo.get_tools() or TOOLS,
        model=prefs.get("model") or cfg.get("model"),  # None → llm 用 settings.chat_model
        temperature=cfg.get("temperature", 0.2),
        max_rounds=cfg.get("max_tool_rounds") or MAX_TOOL_ROUNDS,
    )


def run(user_message: str, history: list[dict] | None = None, session_id: str = "default",
        end_user_id: int | None = None) -> dict:
    """跑一輪對話（阻塞式，一次回完整答案）。回傳 {answer, used_tools, sources, messages}。"""
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
                    "chart": charts[-1] if charts else None, "messages": messages}

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

    # 工具輪數用完 → 收尾這一刀 tool_choice="none"：不准再叫工具，逼它用手上的資料回話。
    # （否則模型可能再要一次工具、content 回空，使用者就會看到「已達工具呼叫上限」那句廢話。）
    final = chat_with_tools(messages, ctx.tools, temperature=ctx.temperature, model=ctx.model,
                            tool_choice="none")
    answer = final.content or "（已達工具呼叫上限，請換個問法或縮小範圍。）"
    messages.append({"role": "assistant", "content": answer})
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages}


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
                    "chart": charts[-1] if charts else None, "messages": messages}

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

    progress.raise_if_cancelled()
    progress.emit("stage", stage="answering", text="讀完討論了，開始生成回答…")
    final, streamed = _stream_once(ctx, messages, tool_choice="none")  # 收尾：不准再叫工具
    answer = final.get("content") or "（已達工具呼叫上限，請換個問法或縮小範圍。）"
    if not streamed:
        progress.emit("token", text=answer)
    messages.append({"role": "assistant", "content": answer})
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages}
