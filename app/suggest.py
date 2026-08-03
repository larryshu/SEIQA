"""追問建議：當輪答完後，用 LLM 依這一輪問答產生 follow-up 問題。

與 expand_queries 同套路：一次 chat、行輸出、任何失敗回 []（全程 fail-safe——
產不出來前端就不顯示建議，不報錯）。只產『問題』不產事實：問題裡嚴禁塞未查證的
數字/結論，維持整套反幻覺原則。前端點了是『填入輸入框可改再送』，不是直接發問。

個人化（登入使用者）：吃 agent 這輪已撈回的使用者記憶來『挑面向』，但不把記憶寫進題目
——chip 是螢幕上常駐可見的 UI，複述記憶等於把個人事實攤在畫面上。記憶由呼叫端傳入，
這裡不自己 recall（理由見 agent.run 的 docstring）。
"""
from __future__ import annotations

import logging
import re

from .config import settings
from .llm import chat

logger = logging.getLogger(__name__)

_MAX_CHARS = 30  # 提示模型的單題長度上限（chip 太長會爆版，也逼它講精簡）
_LEAD = re.compile(r"^\s*(?:\d+[.、)）]\s*|[-•·]\s*)")  # 去掉模型可能加的編號/項目符號


def suggest_followups(question: str, answer: str,
                      sources: list[dict] | None = None,
                      n: int | None = None,
                      memories: list[str] | None = None) -> list[str]:
    """依『本輪問題＋答案（＋來源標題）』產 n 條使用者可能接著想問的問題。停用/失敗回 []。

    memories：agent 這輪已撈回的使用者原子事實（見 agent.run 的回傳值；這裡不自己再 recall）。
    只拿來『挑面向』——至多 suggest_personalize_max 題偏向他的身分/長期關心，其餘保持通用；
    且嚴禁把記憶內容寫進題目文字（chip 常駐在畫面上，複述記憶＝把個人事實攤給旁人看）。
    匿名使用者傳進來是空的，行為與未個人化前完全相同。
    """
    if not settings.suggest_enabled:
        return []
    n = settings.suggest_n if n is None else n
    if n <= 0 or not (answer or "").strip():
        return []

    titles = [str(s.get("title", "")).strip()
              for s in (sources or []) if s.get("title")]
    context = f"使用者問：{question}\n\n你的回答（摘要）：{answer[:800]}\n"
    if titles:
        context += "\n引用來源標題：\n" + "\n".join(f"- {t}" for t in titles[:8])

    facts = [str(m).strip() for m in (memories or []) if str(m).strip()]
    personalize = bool(facts) and settings.suggest_personalize
    if personalize:
        context += ("\n\n關於這位使用者（僅供你挑選面向，不可寫進問題文字）：\n"
                    + "\n".join(f"- {f}" for f in facts))

    rules = [
        "1. 每題聚焦一個不同面向，彼此不重複、也不要重問這輪已回答過的；",
        "2. 要具體、可獨立成題，且適合再去查鄉民討論（口碑／心得／時事）；",
        "3. 只產『開放式問題』——嚴禁在問題裡塞入未經查證的數字或結論"
        "（不要寫成『為什麼 A 回購率高達 90%？』這種把假數據包進問題的問法）；",
        f"4. 每題不超過 {_MAX_CHARS} 個字，用使用者第一人稱口吻；",
        f"5. 只輸出 {n} 行問題，一行一題，不要編號、不要引號、不要任何解釋。",
    ]
    if personalize:
        m = min(settings.suggest_personalize_max, n)
        rules += [
            f"6. 附了『關於這位使用者』的背景：最多 {m} 題可以順著他的身分或長期關心的主題來挑"
            "角度，其餘幾題維持一般讀者也會想問的通用面向；",
            "7. 背景只用來決定『問哪個角度』，絕對不可以把背景內容複述或改寫進問題文字裡"
            "（不要出現『我這個後端工程師…』『像我這種正在找工作的人…』這種寫法），"
            "問題讀起來要像任何人都可能問的一般問句；",
            "8. 背景與這輪主題不搭時就忽略它，全部出通用題——硬湊個人化比不個人化更糟。",
        ]
    msgs = [
        {"role": "system", "content": (
            "你是社群輿情問答助手的『追問建議器』。根據以下這一輪的問答，"
            f"提出 {n} 個使用者可能會『接著想問』的問題，讓他一鍵接續。規則：\n"
            + "\n".join(rules)
        )},
        {"role": "user", "content": context},
    ]
    try:
        text = chat(msgs, temperature=settings.suggest_temperature)
    except Exception as e:  # noqa: BLE001 — 產不出來就不顯示建議（前端 fail-safe）
        logger.warning("suggest_followups failed: %s", e)
        return []

    out: list[str] = []
    for ln in text.splitlines():
        q = _LEAD.sub("", ln).strip().strip("「」\"'").strip()
        if q and q not in out:
            out.append(q)
        if len(out) >= n:
            break
    return out
