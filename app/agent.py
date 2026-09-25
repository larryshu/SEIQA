"""Agent loop：LLM ↔ 工具的多輪循環（規劃→工具→行動），借鑑 Hermes 的自主工具呼叫。

流程：把 system + 對話歷史 + 提問丟給 LLM →
  - 它若決定要最新資訊 → 回 tool_calls → 我們執行 community_search（並行即時爬各社群平台）→ 把結果塞回 →再問一次
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

from . import audit, faithfulness, llm, progress, tracing, user_memory
from .config import settings
from .config_repo import repo
from .llm import chat_with_tools
from .tools import TOOLS, dispatch
from .tracing import observe

# 刻意不寫死平台名稱：啟用哪些平台由後台與使用者偏好決定，而 community_search 的回傳
# 開頭就會列出「本次有撈到資料的平台／沒撈到的平台」。prompt 裡再列一次清單，只會在加減
# 平台時變成過期資訊，並讓模型提到根本沒查的平台。
SYSTEM_PROMPT = (
    "你是一個熟悉網路鄉民討論的貼心朋友，不是制式的查詢助理。"
    "當問題需要鄉民民間討論／口碑／心得／時事時，用 community_search 工具——"
    "它會『同時』即時爬多個社群平台，把各邊討論一起撈回來。"
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
    "抓回來的每則討論開頭都會用括號標出它的來源平台。請『綜合』實際有抓到的來源一起講，"
    "可以自然帶出平台之間的差異或出處，例如『Dcard 上比較多人說…，PTT 鄉民則覺得…』"
    "（實際講哪些平台，以這次真的有撈到的為準）。"
    "工具開頭會註明這次哪些平台有資料、哪些沒有；沒有資料的平台就完全不要提、"
    "不要假裝它上面有討論。"
    "當某個具體說法來自抓到的討論時，在句尾標上『那則討論的實際編號』——編號就是工具回傳裡"
    "每則開頭中括號中的數字，例如引用第 3 則就寫 [3]、第 17 則就寫 [17]。"
    "**絕對不可以原樣輸出「[n]」**：n 只是講解時的代號，不是真的字；輸出 [n] 讀者點不到來源，"
    "等同假引用。中括號裡一定要是實際數字。"
    "不用每句都標、也不要讓來源變成回答的主角。不要杜撰來源。"
    "\n\n"
    "【比例與圖表】"
    "當使用者問『比例』『幾成』『多少人覺得』『正反意見如何』或要圖表時，"
    "先 community_search 撈討論，再呼叫 stance_breakdown 工具做立場統計——它會逐則判讀並"
    "由程式加總，前端會直接把結果畫成圖。"
    "**你自己絕對不要估算百分比**（沒數過的『大概六四開』就是杜撰），"
    "**也絕對不要用文字、方塊或符號拼出長條圖／圓餅圖**——那不是圖，是雜訊。"
    "統計出來之後，你的工作是用『文字』解釋這個分佈代表什麼、兩邊各在意什麼。"
    "\n\n"
    "【所有平台都沒有相關資料時】"
    "就以朋友的身分用既有常識／經驗給建議，並誠實說這次沒在社群平台上找到相關討論。"
)

# 2 輪：一輪 community_search 撈討論，必要時第二輪 stance_breakdown 做立場統計。
# （單一 community_search 內部已並行查所有平台，所以「查」本身一輪就夠。）
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
            "3. 回顧的句子不可標來源編號：編號只屬於本次查到的貼文，舊脈絡沒有對應來源，"
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


def _resolve_system_prompt(cfg: dict) -> tuple[str, str]:
    """決定這輪用哪份 system prompt，回 (內容, 來源標記)。

    優先序：**後台 agent > Langfuse > 本檔寫死值**。

    為什麼 Langfuse 不排第一：後台的 prompt 管理是既有的產品功能（M3），使用者在後台改了
    prompt 卻被 Langfuse 悄悄蓋掉，是最難查的那種 bug。Langfuse 補的是「後台沒設／DB 連不上」
    那一格，順便帶來版本歷史、diff 與回滾——而不是搶走主導權。

    來源標記會寫進 span：改完 prompt 回頭看舊 trace 時，要分得出那個答案是哪一版生的。
    """
    if cfg.get("system_prompt"):
        return cfg["system_prompt"], "admin_backend"
    if not settings.langfuse_prompt_enabled:
        return SYSTEM_PROMPT, "builtin"
    return tracing.get_prompt(
        settings.langfuse_prompt_name, fallback=SYSTEM_PROMPT,
        label=settings.langfuse_prompt_label, ttl_seconds=settings.langfuse_prompt_ttl,
    )


def _finish_span(user_message: str, answer: str, used_tools: list[str],
                 sources: list[dict]) -> None:
    """收尾時把這輪的重點寫進 agent span（兩個 loop 各有兩個出口，故抽成一支）。

    刻意不把 messages 整包放進去：那裡面是工具回傳的上萬字貼文，底下的 tool span 已經
    完整記過一次，重複只會讓 trace 難讀、也讓 ClickHouse 白吃儲存空間。
    """
    tracing.set_span(
        input=user_message,
        output=answer,
        metadata={"used_tools": used_tools, "sources": len(sources),
                  "light": "green" if sources else "yellow"},
    )


class _RunContext(NamedTuple):
    """一輪對話的所有已解析設定：run() 與 run_streaming() 共用，確保兩條路徑行為一致。"""

    messages: list[dict]
    tools: list[dict]
    model: str | None
    temperature: float
    max_rounds: int
    memories: list[str]  # 本輪撈回的『使用者原子事實』；順著回傳給追問建議器做個人化
    recap_hint: str      # 命中脈絡時的回顧提醒；空＝沒脈絡或關閉（見 _refresh_recap_hint）


@observe(name="build_context", capture_input=False, capture_output=False)
def _build_context(user_message: str, history: list[dict] | None,
                   end_user_id: int | None) -> _RunContext:
    """組 system prompt（偏好 + 記憶 + 脈絡）與各項設定，並鋪好 messages 陣列。

    M3：優先用後台『啟用中 agent』的設定（prompt / model / temperature / max_tool_rounds /
    tools）；後台沒設或 DB 連不上時，fall back 到本檔寫死值與 .env（fail-safe）。
    """
    cfg = repo.get_active_agent() or {}
    prefs = repo.get_user_preferences(end_user_id) if end_user_id else {}
    # 取值優先序：user_preference > agent > system_setting/.env
    base_prompt, prompt_source = _resolve_system_prompt(cfg)
    system_prompt = _apply_pref_modifiers(base_prompt, prefs)
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
            "之前聊過的重點再進入主體；回顧那句不可標來源編號；主體與結論仍以上面查到的最新"
            "討論為準；風向有變就點出差異。若脈絡與本題確實不相關，就完全不要提。）"
        )

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    # 把「組好的 system prompt」原文放進 span：偏好／原子記憶／脈絡是一層層疊上去的，
    # 最終長什麼樣以前只能靠推測（_refresh_recap_hint 那個「回顧要求被工具結果稀釋」的
    # 問題就是這樣發現的）。這裡記下來，之後改 prompt 有沒有生效可以直接比對。
    tracing.set_span(
        output={"system_prompt": system_prompt, "system_prompt_chars": len(system_prompt)},
        metadata={
            "facts": len(memories),
            "threads": len(threads),
            "history_turns": len(history or []),
            "model": prefs.get("model") or cfg.get("model") or settings.chat_model,
            "recap": bool(recap_hint),
            "prompt_source": prompt_source,   # admin_backend / seiqa-system:v3 / builtin / fallback
        },
    )

    return _RunContext(
        messages=messages,
        tools=repo.get_tools() or TOOLS,
        model=prefs.get("model") or cfg.get("model"),  # None → llm 用 settings.chat_model
        temperature=cfg.get("temperature", 0.2),
        max_rounds=cfg.get("max_tool_rounds") or MAX_TOOL_ROUNDS,
        memories=memories,
        recap_hint=recap_hint,
    )


def _audit_retry(ctx: "_RunContext", messages: list[dict], answer: str,
                 sources: list[dict], used_tools: list[str]) -> str:
    """答案違反 audit.py 的規則時，把違規項目告訴模型、請它重寫一次。只用在非串流的 run()。

    為什麼串流版不做：串流的答案在稽核之前已經一個字一個字送到使用者畫面上了，
    事後重寫等於當著使用者的面換掉整段字，體驗比留著一個小違規更糟。串流版維持只記分。

    只重試一次：重寫後違規變少才採用（變多或一樣就留原答案，重寫不保證變好）；
    最後再用 audit.repair 機械式清掉刪了也不影響語意的部分（假引用、字元圖表）。
    違規之後才會多一次 LLM 呼叫，正常答案零成本。
    """
    if not settings.audit_retry_enabled:
        return answer
    failed = [f for f in audit.audit(answer, sources, used_tools) if not f.passed]
    if not failed:
        return answer

    chosen, after = answer, failed
    try:
        msg = chat_with_tools(
            messages + [{"role": "assistant", "content": answer},
                        {"role": "system", "content": audit.fix_instructions(failed, len(sources))}],
            ctx.tools, temperature=ctx.temperature, model=ctx.model, tool_choice="none")
        retried = msg.content or ""
        retry_failed = [f for f in audit.audit(retried, sources, used_tools) if not f.passed]
        if retried and len(retry_failed) < len(failed):
            chosen, after = retried, retry_failed
    except Exception as e:  # noqa: BLE001 — 重寫失敗就用原答案，不可以弄死回答
        logger.warning("稽核重寫失敗（沿用原答案）：%s", e)

    if after:
        chosen = audit.repair(chosen, len(sources))
    left = [f.rule for f in audit.audit(chosen, sources, used_tools) if not f.passed]
    # 記下「原本違規了什麼、修完還剩什麼」。api 那邊的 audit_pass 評的是修正後的答案，
    # 沒有這一筆就看不出模型本身的違規率——修正機制會把問題藏起來。
    tracing.score("audit_retry", 0.0 if left else 1.0,
                  comment=f"原違規：{'、'.join(f.rule for f in failed)}"
                          + (f"；修正後仍有：{'、'.join(left)}" if left else "；已全部修正"))
    return chosen


def _faithfulness_gate(ctx: "_RunContext", messages: list[dict], answer: str,
                       sources: list[dict], chart: dict | None) -> str:
    """答案送出前的語意關卡：judge 判定跟原文不符的論點，請模型修正；仍不過就刪句。

    audit.py 只擋得住格式錯（假引用、超範圍編號）；「引用編號正確、內容卻曲解原文」
    要讀原文才判斷得出來，這一關就是把 faithfulness 的 judge 從「事後打分」搬到「送出前」。

    處理哪些判定由 FAITH_GATE_VERDICTS 決定（預設只處理 contradicted）。流程：
      1) judge 核對原答案 → 沒有要處理的論點就原樣放行；
      2) 帶著「哪幾句、原文實際怎麼說」請模型只改那幾句，重新核對，問題變少才採用；
      3) 還有剩 → faithfulness.remove_claims 刪掉那幾句（寧可少講，不要講錯）。
    judge 失敗一律放行原答案（fail-safe）。只用在非串流 run()，理由同 _audit_retry。
    """
    if not settings.faith_gate_enabled or not sources:
        return answer
    handled = set(settings.faith_gate_verdicts)
    first = faithfulness.evaluate(answer, sources, chart)
    if first.error or not first.claims:
        return answer
    # 原答案的忠實度照記：關卡會把錯誤修掉，不記這一筆就看不出模型本身的錯誤率
    tracing.score("faithfulness", first.score, comment=faithfulness._comment(first))  # noqa: SLF001
    bad = [c for c in first.claims if c.verdict in handled]
    if not bad:
        return answer

    chosen, left = answer, bad
    try:
        lines = "\n".join(
            f"- 「{c.text}」（引用 {'、'.join(f'[{n}]' for n in c.cites)}）："
            + ("原文的意思跟這句相反" if c.verdict == faithfulness.CONTRADICTED else "原文沒有提到這件事")
            + (f"。原文實際寫的是：「{c.evidence}」" if c.evidence else "")
            for c in bad)
        fix = ("你剛才的回答裡，下面這幾句跟所引用的原文對不上：\n" + lines + "\n"
               "請重新輸出『完整的回答』：這幾句改成原文實際的意思，改不了就整句刪掉；"
               "其他部分維持原樣，不要解釋你改了什麼。")
        msg = chat_with_tools(
            messages + [{"role": "assistant", "content": answer},
                        {"role": "system", "content": fix}],
            ctx.tools, temperature=ctx.temperature, model=ctx.model, tool_choice="none")
        retried = msg.content or ""
        if retried:
            second = faithfulness.evaluate(retried, sources, chart)
            retry_bad = [c for c in second.claims if c.verdict in handled]
            if not second.error and len(retry_bad) < len(bad):
                chosen, left = retried, retry_bad
    except Exception as e:  # noqa: BLE001 — 重寫失敗就沿用原答案，再走刪句
        logger.warning("忠實度關卡重寫失敗（改走刪句）：%s", e)

    if left:
        trimmed = faithfulness.remove_claims(chosen, left)
        # 刪到幾乎不剩就不硬給：誠實說沒有能支持的內容，比給一段殘缺的回答好
        chosen = trimmed if len(trimmed) >= 20 else (
            "這次撈到的社群討論裡，找不到能明確支持這個問題的內容，建議換個問法再試一次。")
    chosen = audit.repair(chosen, len(sources))  # 重寫可能帶進格式問題，順手清掉
    tracing.score("faithfulness_gate", 0.0 if left else 1.0,
                  comment=f"原有 {len(bad)} 句不符"
                          + (f"；重寫後仍有 {len(left)} 句，已刪除" if left else "；已全部修正"))
    return chosen


@observe(name="agent_loop", capture_input=False, capture_output=False)
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
            answer = _audit_retry(ctx, messages, msg.content or "", sources, used_tools)
            answer = _faithfulness_gate(ctx, messages, answer, sources,
                                        charts[-1] if charts else None)
            messages.append({"role": "assistant", "content": answer})
            _finish_span(user_message, answer, used_tools, sources)
            return {"answer": answer, "used_tools": used_tools, "sources": sources,
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
    answer = _audit_retry(ctx, messages, answer, sources, used_tools)
    answer = _faithfulness_gate(ctx, messages, answer, sources, charts[-1] if charts else None)
    messages.append({"role": "assistant", "content": answer})
    _finish_span(user_message, answer, used_tools, sources)
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages,
            "memories": ctx.memories}


def _stream_once(ctx: _RunContext, messages: list[dict],
                 tool_choice: str = "auto", emit_tokens: bool = True) -> tuple[dict, bool]:
    """跑一次串流補全：token 邊收邊 emit。回 (assistant message dict, 是否吐過 token)。

    emit_tokens=False：照樣串流（取消檢查照常），但先不送到畫面——答案要先過稽核與
    忠實度關卡才能給使用者看（見 _verify_then_emit）。
    """
    streamed = False
    msg: dict = {}
    for kind, payload in llm.chat_with_tools_stream(
            messages, ctx.tools, temperature=ctx.temperature, model=ctx.model,
            tool_choice=tool_choice):
        if kind == "token":
            if emit_tokens:
                streamed = True
                progress.emit("token", text=payload)
        else:
            msg = payload  # type: ignore[assignment]
    return msg, streamed


_EMIT_CHUNK = 20  # 核對完的答案分段送出，每段幾個字：前端照樣看到逐步長出來，前端程式不必改


def _verify_then_emit(ctx: _RunContext, messages: list[dict], answer: str,
                      sources: list[dict], used_tools: list[str], charts: list[dict]) -> str:
    """串流版的「先核對、再送出」：答案收齊後跑稽核重寫與忠實度關卡，通過的才分段送出。

    串流版原本做不了這兩關——答案在稽核之前就一個字一個字上了使用者的畫面，事後改寫
    等於當面換掉整段字。改成先收齊、核對完再送，代價是第一個字要晚十秒左右才出現，
    所以用 FAITH_GATE_STREAM 另外控制，並推一則 stage 告訴使用者在等什麼。
    """
    progress.emit("stage", stage="verifying", text="正在核對答案與引用的原文…")
    progress.raise_if_cancelled()
    answer = _audit_retry(ctx, messages, answer, sources, used_tools)
    progress.raise_if_cancelled()
    answer = _faithfulness_gate(ctx, messages, answer, sources, charts[-1] if charts else None)
    progress.raise_if_cancelled()
    for i in range(0, len(answer), _EMIT_CHUNK):
        progress.emit("token", text=answer[i:i + _EMIT_CHUNK])
    return answer


@observe(name="agent_loop_streaming", capture_input=False, capture_output=False)
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
        # 已經查到來源的回合，這次吐出來的可能就是要引用來源的答案 → 先扣住、核對完再送。
        # 還沒查到來源（第一回合、常識題）照舊即時串流：沒有來源就沒有引用可核對。
        hold = settings.faith_gate_stream and bool(sources)
        msg, streamed = _stream_once(ctx, messages, emit_tokens=not hold)

        if not msg.get("tool_calls"):  # 不需查（🟡 常識題）→ 剛剛串出去的就是答案
            answer = msg.get("content") or ""
            if hold:
                answer = _verify_then_emit(ctx, messages, answer, sources, used_tools, charts)
            elif not streamed:  # 模型沒串出東西（極少見）→ 補送一次，前端才有內容
                progress.emit("token", text=answer)
            messages.append({"role": "assistant", "content": answer})
            _finish_span(user_message, answer, used_tools, sources)
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
    hold = settings.faith_gate_stream and bool(sources)
    final, streamed = _stream_once(ctx, messages, tool_choice="none",  # 收尾：不准再叫工具
                                   emit_tokens=not hold)
    answer = final.get("content") or "（已達工具呼叫上限，請換個問法或縮小範圍。）"
    if hold:
        answer = _verify_then_emit(ctx, messages, answer, sources, used_tools, charts)
    elif not streamed:
        progress.emit("token", text=answer)
    messages.append({"role": "assistant", "content": answer})
    _finish_span(user_message, answer, used_tools, sources)
    return {"answer": answer, "used_tools": used_tools, "sources": sources,
            "chart": charts[-1] if charts else None, "messages": messages,
            "memories": ctx.memories}
