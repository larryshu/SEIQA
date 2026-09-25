"""先抽證據、再比對引句、最後才寫答案（EVIDENCE_MODE）。

為什麼需要這一層——忠實度關卡（agent._faithfulness_gate）是「寫完再查」，而且只查有標 [n]
的句子；模型寫「很多網友認為 X」卻不標編號，那句就完全沒人檢查。這裡改成「先證據、後寫作」：

  1) extract：請模型從撈到的貼文挑出論點，每條附來源編號與『逐字』原文引句（JSON）；
  2) verify ：程式檢查引句真的出現在那一則原文裡——對不上的論點直接丟，不問模型；
  3) write  ：只把通過的論點交給模型寫成答案。它看不到原始的上萬字貼文，
             也就沒有材料可以張冠李戴或憑空補一句「網友說」。

比對規則（verify）：兩邊都做 NFKC（全形/半形統一）、去掉空白與標點再比；
逐字找不到時用滑動視窗做模糊比對（difflib，≥ EVIDENCE_FUZZY_MIN），容忍模型順手修掉的
錯字或標點。引句太短（< _MIN_QUOTE 字）不算數——兩三個字到處都比對得到，等於沒驗。

全程 fail-safe：抽取失敗、或一條都沒通過時回 None，呼叫端退回原本的流程（仍有關卡把關）。
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import NamedTuple

from .config import settings

logger = logging.getLogger(__name__)

_MIN_QUOTE = 6        # 正規化後少於這麼多字的引句不採信
_TRAILING_COMMA = re.compile(r",\s*([\]}])")


class Evidence(NamedTuple):
    point: str   # 論點（模型的摘要說法）
    source: int  # 來源編號（1-based，對應 sources）
    quote: str   # 模型宣稱的逐字引句


# ---- 1) 抽證據 ----------------------------------------------------------------
_EXTRACT_RULES = (
    "【現在先不要回答使用者】請從上面撈到的社群貼文裡，挑出能回答使用者問題的論點，最多 {n} 條，"
    "盡量涵蓋不同立場與不同平台。每一條都要附：\n"
    "- source：那則貼文的編號（上面中括號裡的數字）；\n"
    "- quote：從那則貼文『逐字複製』一段 10～60 字的原文，一個字都不能改、不能自己改寫或翻譯；\n"
    "- point：用一句話說這段原文表達了什麼（不能超出 quote 的意思）。\n"
    "找不到原文依據的說法一律不要列。只輸出 JSON 陣列，不要 markdown："
    "[{{\"point\": \"...\", \"source\": 3, \"quote\": \"...\"}}]"
)


def _parse(raw: str) -> list[Evidence]:
    s = (raw or "").strip()
    if "[" in s and "]" in s:
        s = s[s.find("["):s.rfind("]") + 1]
    out: list[Evidence] = []
    for item in json.loads(_TRAILING_COMMA.sub(r"\1", s)):
        try:
            out.append(Evidence(str(item["point"]).strip(), int(item["source"]),
                                str(item["quote"]).strip()))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def extract(messages: list[dict], tools: list[dict], model: str | None) -> list[Evidence]:
    """沿用整段對話（含工具回傳的貼文），請模型只輸出論點＋引句。失敗就拋。"""
    from .llm import chat_with_tools

    msg = chat_with_tools(
        messages + [{"role": "system",
                     "content": _EXTRACT_RULES.format(n=settings.evidence_max_claims)}],
        tools, temperature=0.0, model=model, tool_choice="none")
    return _parse(msg.content or "")[:settings.evidence_max_claims]


# ---- 2) 比對引句 --------------------------------------------------------------
def _norm(text: str) -> str:
    """NFKC＋只留文字與數字（去空白、標點、符號、表情），小寫。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


# 模型引長文時常用「...」「…」跳過中間（實測推薦清單類長文，通過率因此掉到 58%）。
# 這種引句不是捏造，所以依省略號切段、要求每段都依序出現在原文裡；太短的段（例如只剩
# 清單編號「2」）不算數，免得「...2...」這種碎片到處都比對得到。
_ELLIPSIS = re.compile(r"\.{2,}|…+|⋯+|。{3,}")
_MIN_SEGMENT = 4


def _find(seg: str, src: str, start: int, threshold: float) -> int:
    """在 src[start:] 找 seg，回結束位置；找不到回 -1。先逐字，再滑動視窗模糊比對。"""
    pos = src.find(seg, start)
    if pos >= 0:
        return pos + len(seg)
    width, step = len(seg), max(1, len(seg) // 4)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(seg)
    for i in range(start, max(start + 1, len(src) - width + 1), step):
        matcher.set_seq1(src[i:i + width])
        if matcher.quick_ratio() >= threshold and matcher.ratio() >= threshold:
            return i + width
    return -1


def quote_found(quote: str, source_text: str, fuzzy_min: float | None = None) -> bool:
    """引句是否出現在原文裡：正規化後逐字包含，或滑動視窗模糊比對過門檻。

    引句含省略號時切成幾段，每段都要找得到、而且順序跟原文一致（倒過來拼的不算）。
    """
    src = _norm(source_text)
    segments = [s for s in (_norm(part) for part in _ELLIPSIS.split(quote or ""))
                if len(s) >= _MIN_SEGMENT]
    if sum(len(s) for s in segments) < _MIN_QUOTE or not src:
        return False
    threshold = settings.evidence_fuzzy_min if fuzzy_min is None else fuzzy_min
    pos = 0
    for seg in segments:
        pos = _find(seg, src, pos, threshold)
        if pos < 0:
            return False
    return True


def verify(items: list[Evidence], sources: list[dict]) -> tuple[list[Evidence], list[Evidence]]:
    """回 (通過, 不通過)。編號超出範圍、引句太短或對不上原文，都算不通過。"""
    ok, bad = [], []
    for ev in items:
        if 1 <= ev.source <= len(sources):
            src = sources[ev.source - 1]
            text = f"{src.get('title') or ''}\n{src.get('content') or ''}"
            if ev.point and quote_found(ev.quote, text):
                ok.append(ev)
                continue
        bad.append(ev)
    return ok, bad


# ---- 3) 寫答案 ----------------------------------------------------------------
def write_messages(messages: list[dict], verified: list[Evidence], sources: list[dict],
                   chart: dict | None) -> list[dict]:
    """組『寫答案』那一刀的 messages：沿用 system prompt 與對話歷史，但不帶工具回傳的原文。

    拿掉的是 tool 訊息與帶 tool_calls 的 assistant 訊息（那裡面是上萬字貼文）；
    換成一份已核對過的論點清單。模型只能從這份清單取材。
    """
    base = [m for m in messages
            if m.get("role") != "tool" and not m.get("tool_calls")]
    platform = {i: (s.get("source") or "") for i, s in enumerate(sources, 1)}
    lines = [f"- [{ev.source}]（{platform.get(ev.source, '')}）{ev.point}｜原文：「{ev.quote}」"
             for ev in verified]
    stats = ""
    if chart and chart.get("percent"):
        percent = "、".join(f"{k} {v}%" for k, v in chart["percent"].items())
        stats = (f"\n\n另有程式統計的立場分佈（共 {chart.get('total', '?')} 則）：{percent}。"
                 "圖表已由前端畫出，比例可以直接引用，不要改動數字、不要用文字拼圖表。")
    # 長度目標：不給的話模型照「朋友聊天」的口吻自然收在 600～700 字——實測把論點從 12 條
    # 加到 24 條，答案也只長 12%。所以要明講「每條都寫到、展開到多長」。使用者偏好有指定
    # 回答長度時以偏好為準（偏好已貼在 system prompt 裡）。
    length = ""
    if settings.evidence_answer_chars > 0:
        length = (f"- 盡量把清單裡的論點都寫進去，依平台或立場分段展開，每一派說清楚他們在意什麼、"
                  f"舉了什麼例子；全文大約 {settings.evidence_answer_chars} 字。"
                  "若上面的使用者偏好有指定回答長度，以偏好為準。\n")
    rules = (
        "以下是從社群討論中擷取、並已由程式逐字核對過原文的論點（中括號是來源編號）：\n\n"
        + "\n".join(lines) + stats + "\n\n"
        "請只根據上面這些論點回答使用者剛才的問題，照原本的語氣與格式規則寫。規則：\n"
        "- 說到某個論點時，在句尾標上它的來源編號，例如 [3]；編號只能用上面出現過的。\n"
        "- 不可以加入清單以外的「網友說／有人認為」——清單沒有的說法就是這次沒有依據。\n"
        "- 可以用自己的話串接、歸納，但歸納的句子不要標編號，也不要把歸納講成網友的原話。\n"
        + length +
        "- 論點彼此矛盾時如實呈現兩邊。"
    )
    return base + [{"role": "system", "content": rules}]


def pass_rate_comment(ok: list[Evidence], bad: list[Evidence]) -> str | None:
    if not bad:
        return None
    return "；".join(f"[{ev.source}]「{ev.quote[:30]}」" for ev in bad[:5])
