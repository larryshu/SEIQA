"""答案自動稽核：把 SYSTEM_PROMPT 裡的反幻覺規定，變成每題都會量到的分數。

為什麼需要這一層——**prompt 裡寫了規定，不等於模型照做**。本專案花最多心力設計的就是那套
反幻覺機制（🟢🟡 燈號、[n] 來源標註、明講哪個平台沒資料、統計交給 Python 不給 LLM），
但「上週有幾成的答案違規了」這個問題以前完全答不出來。

這裡實作的五條檢查全部是純字串比對，**不呼叫 LLM**：不花錢、不增加延遲、結果可重現。
每條對應 SYSTEM_PROMPT 裡一句白紙黑字的規定：

  literal_n          「絕對不可以原樣輸出「[n]」」
  citation_range     「中括號裡一定要是實際數字」（編號要對得回真實來源）
  phantom_platform   「沒有資料的平台就完全不要提」
  unsourced_percent  「你自己絕對不要估算百分比」
  ascii_chart        「絕對不要用文字、方塊或符號拼出長條圖／圓餅圖」

判定一律「寧可漏抓，不可錯殺」：誤報會讓分數失去可信度，久了就沒人看了。所以每條規則
的樣式都刻意收緊（例如百分比只認 % 與明確的成數句型，不去猜「成」這個字的其他用法）。
"""
from __future__ import annotations

import logging
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)

# 平台中文名：用來檢查答案有沒有提到「這次根本沒撈到資料」的平台。
# 不從 sources.PLATFORM_LABELS 匯入，避免 audit 反過來相依於爬蟲那一側；
# 後台改了 display_name 時這裡漏掉頂多是少抓一條，不會誤報。
PLATFORM_NAMES: dict[str, tuple[str, ...]] = {
    "dcard": ("Dcard", "狄卡"),
    "ptt": ("PTT", "批踢踢", "鄉民版"),
    "threads": ("Threads", "脆"),
}

_LITERAL_N = re.compile(r"\[\s*n\s*\]", re.IGNORECASE)
_CITATION = re.compile(r"\[(\d{1,3})\]")

# 百分比：只認「數字%」與明確表示比例的成數句型。
# 刻意不去比對單獨的「成」——成功／成長／成本／造成／變成 全都會誤中，
# 而誤報一次就會讓整個稽核分數失去可信度。
_PERCENT = re.compile(
    r"\d+(?:\.\d+)?\s*%"                                    # 63%、6.5 %
    r"|[一二三四五六七八九十兩\d]+\s*成(?=的|左右|上下|以上|以下|多|來|[，。、；！？\s]|$)"
    r"|[一二三四五六七八九十]\s*[一二三四五六七八九十]\s*開"    # 六四開、三七開
)

# 用字元拼出來的圖表：方塊繪圖字元或實心圓點連續出現。
# 門檻設 3 個以上，避免把單獨一個「●」當項目符號的情況算成違規。
_ASCII_CHART = re.compile(r"[█▇▆▅▄▃▂▁▓▒░■□▪▫]{3,}|[●○]{3,}")


class Finding(NamedTuple):
    """一條稽核結果。passed=False 代表違規；detail 會寫進 Langfuse 的 score comment。"""

    rule: str
    passed: bool
    detail: str


def _check_literal_n(answer: str) -> Finding:
    """答案裡不可以出現字面的 [n]——那是講解用的代號，輸出出去讀者點不到來源＝假引用。"""
    hit = _LITERAL_N.search(answer)
    return Finding("literal_n", not hit,
                   "答案出現字面的 [n]，不是實際編號" if hit else "")


def _check_citation_range(answer: str, n_sources: int) -> Finding:
    """引用編號必須落在 1..來源數之間。超出範圍＝指向不存在的來源，比不標更糟。"""
    bad = sorted({int(m) for m in _CITATION.findall(answer)
                  if not 1 <= int(m) <= n_sources})
    return Finding("citation_range", not bad,
                   f"引用編號 {bad} 超出範圍（本次來源 {n_sources} 則）" if bad else "")


def _check_phantom_platform(answer: str, sources: list[dict]) -> Finding:
    """不可以提到這次沒撈到資料的平台——那等於幫它捏造討論。

    判斷依據是 sources 裡實際出現的 source 標籤，不是「有沒有啟用該平台」：
    平台有跑但撈到 0 則時，一樣不准提。
    """
    present = {str(s.get("source") or "").lower() for s in sources}
    phantom = [
        names[0] for key, names in PLATFORM_NAMES.items()
        if key not in present and any(nm in answer for nm in names)
    ]
    return Finding("phantom_platform", not phantom,
                   f"提到了這次沒有資料的平台：{'、'.join(phantom)}" if phantom else "")


def _check_unsourced_percent(answer: str, used_tools: list[str]) -> Finding:
    """沒跑過 stance_breakdown 就不准講比例——那個數字沒有人數過，是憑感覺講的。

    有跑統計時本來就該引用那些百分比，所以這條只在沒跑統計時檢查。
    """
    if "stance_breakdown" in used_tools:
        return Finding("unsourced_percent", True, "")
    hits = _PERCENT.findall(answer)
    matches = sorted({m.group(0).strip() for m in _PERCENT.finditer(answer)})
    return Finding("unsourced_percent", not hits,
                   f"沒做立場統計卻講了比例：{'、'.join(matches[:5])}" if hits else "")


def _check_ascii_chart(answer: str) -> Finding:
    """不可以用方塊字元拼圖表——那不是圖，是雜訊（圖由前端畫）。"""
    hit = _ASCII_CHART.search(answer)
    return Finding("ascii_chart", not hit,
                   f"疑似用字元拼出圖表：{hit.group(0)[:20]}" if hit else "")


def audit(answer: str, sources: list[dict] | None = None,
          used_tools: list[str] | None = None) -> list[Finding]:
    """跑完五條檢查，回傳全部結果（含通過的，呼叫端才算得出通過率）。"""
    answer = answer or ""
    sources = sources or []
    used_tools = used_tools or []
    return [
        _check_literal_n(answer),
        _check_citation_range(answer, len(sources)),
        _check_phantom_platform(answer, sources),
        _check_unsourced_percent(answer, used_tools),
        _check_ascii_chart(answer),
    ]


_FIX_HINTS = {
    "literal_n": "答案裡出現了字面的「[n]」。請改成實際的來源編號，或拿掉那個標註。",
    "citation_range": "有引用編號超出範圍（本次來源只有 1～{n} 則）。只能引用這個範圍內、內容確實支持該句的編號。",
    "phantom_platform": "提到了這次沒有撈到資料的平台。沒有資料的平台完全不要提。",
    "unsourced_percent": "這次沒有做立場統計，答案卻出現百分比或成數。請改用「不少人」「有人」這類描述，不要給比例。",
    "ascii_chart": "答案裡用字元拼出了圖表。請拿掉，圖表由前端負責。",
}


def fix_instructions(failed: list[Finding], n_sources: int) -> str:
    """把違規項目寫成給模型的修正指示（重生成那一次用）。"""
    lines = [f"- {_FIX_HINTS[f.rule].format(n=n_sources)}（{f.detail}）"
             for f in failed if f.rule in _FIX_HINTS]
    return ("你剛才的回答違反了以下規定，請修正後重新輸出『完整的回答』"
            "（內容與語氣維持原樣，只改違規的地方；不要解釋你改了什麼）：\n" + "\n".join(lines))


def repair(answer: str, n_sources: int) -> str:
    """機械式修掉「刪了也不會改變意思」的違規：字面 [n]、超出範圍的編號、字元圖表的那幾行。

    重生成之後仍違規時的最後一道。phantom_platform 與 unsourced_percent 不在這裡處理——
    那要改寫句子，硬刪會把句子刪成語意不通，寧可留著讓稽核分數記下來。
    """
    text = _LITERAL_N.sub("", answer or "")
    text = _CITATION.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_sources else "", text)
    text = "\n".join(ln for ln in text.split("\n") if not _ASCII_CHART.search(ln))
    return re.sub(r"[ \t]+([，。！？、；])", r"\1", text).strip()


def audit_and_score(answer: str, sources: list[dict] | None = None,
                    used_tools: list[str] | None = None) -> list[Finding]:
    """跑稽核並把結果打成 Langfuse score 掛在目前的 trace 上。fail-safe。

    每條規則各一個 score（1.0 通過 / 0.0 違規），另加一個 audit_pass 當總分——
    前者用來查「是哪一條在壞」，後者用來畫一條「整體違規率」的趨勢線。
    """
    from . import tracing  # 延遲匯入：audit 是純函式模組，測試時不必連 Langfuse

    findings = audit(answer, sources, used_tools)
    try:
        for f in findings:
            tracing.score(f.rule, 1.0 if f.passed else 0.0, comment=f.detail or None)
        failed = [f.rule for f in findings if not f.passed]
        tracing.score("audit_pass", 0.0 if failed else 1.0,
                      comment=("違規：" + "、".join(failed)) if failed else None)
        if failed:
            logger.warning("答案稽核未通過：%s", "、".join(
                f"{f.rule}（{f.detail}）" for f in findings if not f.passed))
    except Exception as e:  # noqa: BLE001 — 稽核壞掉不可以影響回答
        logger.warning("稽核打分失敗（略過）：%s", e)
    return findings
