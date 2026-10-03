"""忠實度（faithfulness）量測：答案裡每個「有標 [n] 的論點」，被引用的原文到底有沒有支持它。

為什麼需要這一層——audit.py 的五條規則只驗**形式**：[7] 超出範圍抓得到，但「多數人覺得
續航很差 [3]」而第 3 則其實在誇續航，編號合法、稽核照樣通過。這種「引用對了、內容曲解」
是 regex 做不到的語意檢查，只能讓模型來讀原文判斷。

定位：**量測，不攔截**。分數用來知道幻覺有多少、出在哪，再回頭調 prompt／檢索，
並用 Langfuse Datasets 驗證每次改動有沒有讓分數變好（見 scripts/faithfulness_eval.py）。

流程（每題）：
  1) 拆論點：切句、抓每句句尾的 [n] → (論點, 引用編號)。沒標引用的句子不評——
     那些是模型的綜述或常識，本來就沒宣稱來自哪則貼文。
  2) 取原文：依編號從 sources 取那幾則貼文（標題＋內文，截斷到 FAITH_SOURCE_CHARS）。
  3) NLI 初篩（選配）：有設 FAITH_NLI_MODEL 且裝了 transformers 才啟用。只用來「放行」
     高信心蘊含的論點——NLI 對鄉民口語、反諷不可靠，所以它只能說「是」，不能說「不是」，
     判不準的一律交給下一關。沒裝就整批交給 LLM，結果一樣，只是比較貴。
  4) LLM-as-judge：剩下的論點一次打包成一個呼叫（不是一句一呼叫），temperature=0，
     輸出 supported / contradicted / not_found ＋ 原文依據句。
     判成 not_found／contradicted 的論點再逐條單獨判一次（_recheck_flagged），以單獨判為準。
  5) 算分：supported ÷ 有效論點數。引用編號超出範圍的論點直接算不支持（指向不存在的來源）。

線上抽樣（maybe_score_async）：依 FAITH_SAMPLE_RATE 抽題、在背景執行緒跑，結果以 score
回寫到**原本那個 trace**。預設 0＝關閉——這一步會花 LLM 錢，要不要開、開多少由人決定。
全程 fail-safe：任何一步失敗都只記 log，絕不影響回答。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import settings
from .llm import chat
from .tracing import observe

logger = logging.getLogger(__name__)

SUPPORTED, CONTRADICTED, NOT_FOUND, BAD_CITATION = (
    "supported", "contradicted", "not_found", "bad_citation")
_VERDICTS = {SUPPORTED, CONTRADICTED, NOT_FOUND}

# 一個中括號裡可能放多個編號：[1][3]、[1,3]、[1、3] 都要吃得下。
_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]")
# 模型常把引用放在句號「之後」（「……很慢。[2]」），切句前先把它搬回句號前面，
# 否則 [2] 會被切到下一句的開頭、掛錯論點。
_CITE_AFTER_STOP = re.compile(r"([。！？!?])((?:\s*\[\d{1,3}(?:\s*[,，、]\s*\d{1,3})*\])+)")
# 句號後面若緊接右引號／右括號（「……。」），要等它們一起收進這一句才切，
# 否則右引號會被切到下一句開頭，論點變成「」這些都很重要」這種樣子。
_SENT_END = re.compile(r"(?<=[。！？!?\n])(?![」』）)〕】\"'])"
                       r"|(?<=[。！？!?][」』）)〕】\"'])")
_BULLET = re.compile(r"^\s*(?:[-*•·]\s+|\d+[.、)）]\s*|#+\s*)")
_MIN_CLAIM_CHARS = 4


@dataclass
class Claim:
    text: str
    cites: list[int]
    verdict: str = ""        # supported / contradicted / not_found / bad_citation
    method: str = ""         # nli / llm / rule
    evidence: str = ""       # 原文依據句（judge 給的；nli 放行的沒有）


@dataclass
class Result:
    claims: list[Claim] = field(default_factory=list)
    error: str = ""          # judge 失敗等狀況；有值時 score 不可信、不回寫

    @property
    def score(self) -> float | None:
        """supported ÷ 論點數。沒有任何帶引用的論點時回 None（不是 1.0——沒評到不等於全對）。"""
        if not self.claims:
            return None
        return sum(c.verdict == SUPPORTED for c in self.claims) / len(self.claims)

    @property
    def unsupported(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict != SUPPORTED]


# ---- 1) 拆論點 ---------------------------------------------------------------
_CITE_RUN = re.compile(r"(?:\s*\[\d{1,3}(?:\s*[,，、]\s*\d{1,3})*\])+")


def extract_claims(answer: str) -> list[Claim]:
    """切句，句內再以「每一組引用」為界切段，抓出每段的引用編號。只回有引用的段落。

    句內要再切：「續航被抱怨 [1]，但拍照受好評 [2]」是兩個論點、各自的來源，
    合成一個會讓 judge 拿 [1][2] 一起比對，兩邊互相掩護，錯的那半就抓不到。
    最後一組引用之後的尾巴（沒標引用）不評。
    """
    text = _CITE_AFTER_STOP.sub(r"\2\1", answer or "")
    out: list[Claim] = []
    for sent in _SENT_END.split(text):
        start = 0
        for run in _CITE_RUN.finditer(sent):
            cites: list[int] = []
            for group in _CITE.findall(run.group(0)):
                for n in re.split(r"\s*[,，、]\s*", group):
                    if int(n) not in cites:
                        cites.append(int(n))
            claim = _BULLET.sub("", sent[start:run.start()]).replace("**", "")
            claim = claim.strip(" \t\n。！？!?，,、；;：:")
            start = run.end()
            if len(claim) >= _MIN_CLAIM_CHARS:
                out.append(Claim(text=claim, cites=cites))
    return out


# 「把話說成是網友講的」的字眼。沒標引用的句子只要出現這些，就是在宣稱社群上有人這樣說。
_ATTRIB = re.compile(r"網友|鄉民|有人|不少人|很多人|許多人|多數人|少數人|有些人|大家|"
                     r"PTT|Dcard|Threads|推文|留言|原PO|原po|樓主|版友|板友|討論")
_MIN_UNCITED_CHARS = 8
# 開場重述使用者的問題（「你問『大家覺得……』」）不是在轉述網友
_RESTATE = re.compile(r"^(?:你|妳|您)(?:問|想知道|好奇|提到)")


def uncited_attributions(answer: str) -> list[str]:
    """沒標任何引用、卻把內容歸給網友的句子（「PTT 上有人說……」但句子裡沒有 [n]）。

    extract_claims 只拆有引用的論點，這類句子 faithfulness 完全看不到——
    模型若把捏造的說法寫成不標引用的句子，分數照樣 100%。這裡把它們撈出來另外量。
    """
    text = _CITE_AFTER_STOP.sub(r"\2\1", answer or "")
    out = []
    for sent in _SENT_END.split(text):
        if _CITE.search(sent):
            continue
        s = _BULLET.sub("", sent).replace("**", "").strip(" \t\n。！？!?，,、；;：:")
        if len(s) >= _MIN_UNCITED_CHARS and _ATTRIB.search(s) and not _RESTATE.match(s):
            out.append(s)
    return out


def remove_claims(answer: str, bad: list[Claim]) -> str:
    """把 bad 裡的論點所在的『整句』刪掉（放行關卡重寫之後仍不過時的最後一道）。

    刪整句而不是只刪那一段：句內其他片段通常靠這個論點才讀得通，只挖掉一半會變成病句。
    代價是同句裡沒問題的片段也一起消失——寧可少講，不要講錯。
    """
    if not bad:
        return answer
    text = _CITE_AFTER_STOP.sub(r"\2\1", answer or "")
    kept = []
    drop_newline = False
    for sent in _SENT_END.split(text):
        # 切句是在「。」與「\n」之後各切一刀，所以一行「- 某句。\n」會變成兩段：
        # 句子本身、以及後面單獨一個換行。刪了句子就要連那個換行一起刪，否則會多出空行。
        if drop_newline and not sent.strip():
            drop_newline = False
            continue
        drop_newline = False
        plain = _BULLET.sub("", _CITE.sub("", sent)).replace("**", "")
        if any(c.text and c.text in plain for c in bad):
            drop_newline = not sent.endswith("\n")
            continue
        kept.append(sent)
    return re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()


def _source_text(src: dict, limit: int | None = None) -> str:
    body = f"{src.get('title') or ''}\n{src.get('content') or ''}".strip()
    return body[:limit or settings.faith_source_chars]


def _per_source_limit(n_cited: int) -> int:
    """每則原文給 judge 讀多長：總預算平均分給這次被引用的幾則，下限 FAITH_SOURCE_CHARS。

    固定截 1500 字會誤判：回答的模型讀的是全文，judge 卻只讀前段——實測「福智教育園區」
    那題，論點講的「出家」「證照」「階級制度」全在第 1700～2000 字，兩個論點因此被判
    not_found。引用只有一兩則時多讀一點很便宜；占比題一次引用二三十則時則維持短版，
    免得 prompt 暴增。
    """
    return max(settings.faith_source_chars,
               min(8000, settings.faith_source_budget // max(n_cited, 1)))


# ---- 3) NLI 初篩（選配）------------------------------------------------------
_nli_pipe = None
_nli_tried = False
_nli_lock = threading.Lock()


def _nli():
    """延遲載入 NLI 模型；沒設模型或沒裝 transformers 就回 None（整批改走 LLM）。"""
    global _nli_pipe, _nli_tried
    if _nli_tried:
        return _nli_pipe
    with _nli_lock:
        if not _nli_tried:
            _nli_tried = True
            if settings.faith_nli_model:
                try:
                    from transformers import pipeline
                    _nli_pipe = pipeline("text-classification", model=settings.faith_nli_model)
                except Exception as e:  # noqa: BLE001 — 選配元件，載不起來就退回純 LLM
                    logger.warning("NLI 模型載入失敗，改全交 LLM 判斷：%s", e)
    return _nli_pipe


def _nli_entails(premise: str, hypothesis: str) -> float:
    """原文（切成數段取最大值）蘊含論點的機率。長文切段是因為 NLI 模型的輸入上限很短。"""
    pipe = _nli()
    if pipe is None:
        return 0.0
    best = 0.0
    for i in range(0, min(len(premise), 2000), 400):
        scores = pipe({"text": premise[i:i + 500], "text_pair": hypothesis}, top_k=None)
        for s in scores:
            if s["label"].lower().startswith("entail"):
                best = max(best, float(s["score"]))
    return best


# ---- 4) LLM-as-judge ---------------------------------------------------------
_JUDGE_SYSTEM = (
    "你是嚴謹的事實查核員。下面有幾則社群貼文（原文）與幾個從某篇回答中拆出來的論點，"
    "每個論點標明它引用了哪幾則原文。請逐一判斷：被引用的原文，是否支持這個論點。\n"
    "判定：\n"
    "- supported：被引用的原文（合起來）表達了這個論點的意思。\n"
    "- contradicted：原文的意思與論點相反或明顯不同（例如原文在稱讚，論點說在抱怨）。\n"
    "- not_found：原文沒有提到這件事，論點是憑空多出來的。\n"
    "注意：\n"
    "- 原文是 PTT／Dcard／Threads 的鄉民口語，要依實際意思理解反諷與網路用語"
    "（例如「真的是『好』棒棒」多半是在酸）。\n"
    "- 不要因為量詞扣分：「有人」「不少人」「網友」這類概括說法，只要原文方向吻合就算 supported。\n"
    "- 只看被引用的那幾則，不要用你自己的常識補。\n"
    # 限長是實測踩到的：論點一次引用 25 則時，judge 會把十幾段原文全串進 evidence，
    # 輸出長到 JSON 被截斷、整批解析失敗。依據只要能讓人回頭查證就夠，一句就好。
    "- evidence 只摘錄原文中最關鍵的『一句』，60 字以內；not_found 時給空字串。\n"
    "只輸出 JSON，不要 markdown：\n"
    "{\"results\":[{\"id\":1,\"verdict\":\"supported|contradicted|not_found\",\"evidence\":\"...\"}]}"
)


_TRAILING_COMMA = re.compile(r",\s*([\]}])")


def _parse_json(raw: str) -> dict:
    """容錯解析 judge 的 JSON：去 markdown 圍籬、只取最外層 {…}、去掉結尾多餘的逗號。

    結尾逗號是實測踩到的：論點多時模型常回 `{…},\\n]}`，約每三次壞一次，
    整批判定就因此作廢。這種壞法不影響內容，修掉再解析即可。
    """
    s = (raw or "").strip()
    if "{" in s and "}" in s:
        s = s[s.find("{"):s.rfind("}") + 1]
    return json.loads(_TRAILING_COMMA.sub(r"\1", s))


def _chart_note(chart: dict | None) -> str:
    """把這一輪 stance_breakdown 的統計結果寫成給 judge 的背景說明；沒有統計就回空字串。

    為什麼需要：占比題的答案會寫「只有一成多的人適應良好 [4][9]」——比例是程式數出來的，
    本來就不在任何一則貼文裡。不告訴 judge，它會把整句判成 not_found，
    於是每一題占比題都被系統性地低估（第一次實測就是這樣：唯一的「不支持」正是這種句子）。
    """
    if not chart or not chart.get("percent"):
        return ""
    percent = "、".join(f"{k} {v}%" for k, v in chart["percent"].items())
    return (
        f"\n\n【本題另有程式統計的立場分佈】共判讀 {chart.get('total', '?')} 則：{percent}。"
        "論點中的百分比或成數若與這份統計相符（允許「一成多」「將近一半」這類約略說法），"
        "比例本身視為有依據，不必在原文中找；只判斷論點其餘的描述是否被引用的原文支持。"
        "比例與統計明顯不符時，判 contradicted。"
    )


@observe(name="faithfulness_judge", as_type="evaluator", capture_input=False)
def _judge(claims: list[Claim], sources: list[dict], chart: dict | None = None) -> list[Claim]:
    """把 claims 一次交給 LLM 判斷，結果直接寫回每個 Claim；回傳 judge 漏判的那幾條。

    漏判的不再當 not_found：實測有一次 judge 只回了 1 條，其餘 16 條全被算成「原文沒提到」，
    那題從 17/17 掉到 1/17——錯的是量測，不是答案。漏判的交給呼叫端重判。失敗就拋。
    """
    cited = sorted({n for c in claims for n in c.cites})
    limit = _per_source_limit(len(cited))
    src_block = "\n\n".join(f"[{n}] {_source_text(sources[n - 1], limit)}" for n in cited)
    claim_block = "\n".join(
        f"{i}. {c.text}（引用：{'、'.join(f'[{n}]' for n in c.cites)}）"
        for i, c in enumerate(claims, 1))
    raw = chat([
        {"role": "system", "content": _JUDGE_SYSTEM + _chart_note(chart)},
        {"role": "user", "content": f"【原文】\n{src_block}\n\n【論點】\n{claim_block}"},
    ], temperature=0.0)
    results = {int(r["id"]): r for r in _parse_json(raw).get("results", [])
               if isinstance(r, dict) and "id" in r}
    missing = []
    for i, c in enumerate(claims, 1):
        r = results.get(i) or {}
        verdict = str(r.get("verdict", "")).strip().lower()
        if verdict not in _VERDICTS:
            missing.append(c)   # 漏回或回了清單外的值：留空，交給呼叫端重判
            continue
        c.verdict, c.method = verdict, "llm"
        c.evidence = str(r.get("evidence") or "")[:200]
    return missing


# ---- 5) 組起來 ---------------------------------------------------------------
def evaluate(answer: str, sources: list[dict] | None, chart: dict | None = None) -> Result:
    """評一題。不呼叫 Langfuse score，純回傳結果（離線評測與線上抽樣共用）。

    chart：這一輪 stance_breakdown 的統計結果（有做立場統計才有），交給 judge 當背景，
    讓答案裡由程式算出的比例不被誤判成無中生有。見 _chart_note。
    """
    sources = sources or []
    result = Result(claims=extract_claims(answer))
    pending: list[Claim] = []
    for c in result.claims:
        if not all(1 <= n <= len(sources) for n in c.cites):
            c.verdict, c.method = BAD_CITATION, "rule"   # 指向不存在的來源：不用問模型
            continue
        if _nli() is not None and any(
                _nli_entails(_source_text(sources[n - 1]), c.text) >= settings.faith_nli_pass
                for n in c.cites):
            c.verdict, c.method = SUPPORTED, "nli"
            continue
        pending.append(c)
    if pending:
        try:
            missing = _judge(pending, sources, chart)
            if missing:  # 只重判漏掉的那幾條；論點少了，judge 也比較不會再漏
                missing = _judge(missing, sources, chart)
            if missing:  # 還是漏：這題的分數不可信，標成錯誤（不計分、關卡放行），不猜
                result.error = f"judge 漏判 {len(missing)} 條論點"
                logger.warning("faithfulness judge 漏判 %d 條（已重判一次）", len(missing))
            elif len(pending) > 1:
                _recheck_flagged(pending, sources, chart)
        except Exception as e:  # noqa: BLE001 — judge 壞掉：結果標成不可信，不猜
            logger.warning("faithfulness judge 失敗：%s", e)
            result.error = str(e)
    return result


def _recheck_flagged(claims: list[Claim], sources: list[dict], chart: dict | None) -> None:
    """整批判成 not_found／contradicted 的論點，逐條再單獨判一次，以單獨判的結果為準。

    整批判會誤判兩種情況（2026-10 拆防線實驗，4 條「錯誤」全是 judge 看錯、答案其實沒錯）：
      - 原文被截短：一次引用 8 則時每則只讀 3750 字，支持的段落在第 4300 字（p02）；
        單獨判時只引用 1 則，可讀到 8000 字。
      - 論點多時看漏：原文沒被截（1900 字），13 條一起判時 5 次都說 not_found，
        單獨判 3 次都是 supported（r01）。
    放行關卡用的也是這個 judge，不重判就會把正確的句子刪掉。被抓的通常每題 0～2 條，加成本很小。
    單獨重判失敗或漏回就維持原判（寧可多擋，不要少擋）。
    """
    flagged = [c for c in claims if c.method == "llm" and c.verdict in (NOT_FOUND, CONTRADICTED)]
    if not flagged:
        return

    def one(c: Claim) -> None:
        solo = Claim(c.text, c.cites)
        try:
            if _judge([solo], sources, chart):
                return
        except Exception as e:  # noqa: BLE001
            logger.warning("faithfulness 單獨重判失敗，維持原判：%s", e)
            return
        if solo.verdict != c.verdict:
            c.verdict, c.evidence = solo.verdict, solo.evidence
        c.method = "llm-recheck"

    with ThreadPoolExecutor(max_workers=min(4, len(flagged))) as pool:
        list(pool.map(one, flagged))


def _comment(res: Result) -> str | None:
    bad = res.unsupported
    if not bad:
        return None
    return "；".join(f"{c.verdict}：{c.text[:40]}（{','.join(map(str, c.cites))}）"
                    for c in bad[:5])


def maybe_score_async(answer: str, sources: list[dict] | None,
                      chart: dict | None = None) -> None:
    """線上抽樣：命中抽樣率就在背景評分，回寫到目前這個 trace。不命中／沒來源就什麼都不做。

    必須在 trace 還有效的地方呼叫（ask / ws_ask 裡），先把 trace_id 取出來再進背景執行緒——
    執行緒裡已經沒有目前的 span 可以掛了。
    """
    if not sources or random.random() >= settings.faith_sample_rate:
        return
    from . import tracing

    trace_id = tracing.current_trace_id()
    if not trace_id:
        return

    def work() -> None:
        try:
            res = evaluate(answer, sources, chart)
            if res.error or res.score is None:
                return
            tracing.score_trace(trace_id, "faithfulness", res.score, comment=_comment(res))
            tracing.flush()
        except Exception as e:  # noqa: BLE001 — 背景量測，壞掉只記 log
            logger.warning("faithfulness 抽樣評分失敗（略過）：%s", e)

    threading.Thread(target=work, name="faithfulness", daemon=True).start()


# ---- 6) 沒標引用的網友說法 ---------------------------------------------------------
UNCITED_FOUND, UNCITED_SUMMARY, UNCITED_NOT_FOUND = "found", "summary", "not_found"
_UNCITED_SYSTEM = (
    "你是嚴謹的事實查核員。下面有一批社群貼文（原文，有編號）與幾個從某篇回答中挑出的句子。"
    "這些句子沒有標引用，但都把內容歸給網友或某個平台。請逐句判斷：\n"
    "- found：句子講的是具體說法，而且在某幾則原文裡找得到（ids 列出編號）。\n"
    "- summary：句子只是整體概述（例如「大家看法兩極」「討論很熱烈」），不是具體說法，"
    "且與原文整體方向不衝突。\n"
    "- not_found：句子講的是具體說法（尤其是引號裡的原話、具體理由或細節），"
    "但整批原文都找不到，或與原文相反。\n"
    "- 說「原文裡沒有人這樣講」這類否定句，原文確實沒有就算 found。\n"
    "原文是鄉民口語，要依實際意思理解；不要用你自己的常識補。\n"
    "只輸出 JSON，不要 markdown：\n"
    "{\"results\":[{\"id\":1,\"verdict\":\"found|summary|not_found\",\"ids\":[3]}]}"
)


@dataclass
class Uncited:
    text: str
    verdict: str = ""        # found / summary / not_found；judge 失敗留空
    ids: list[int] = field(default_factory=list)


def check_uncited(answer: str, sources: list[dict] | None,
                  chart: dict | None = None) -> list[Uncited]:
    """量測用：沒標引用的網友說法，到底是在原文找得到、只是概述、還是憑空多出來的。

    不像 evaluate 有引用編號可以只看那幾則，這裡得把整批原文都給 judge，
    所以每則原文更短（FAITH_SOURCE_BUDGET 平均分給全部來源）——判成 not_found 的
    要人工抽看，可能是原文被截掉。只在離線評測用，不接進關卡。失敗回傳 verdict 留空的清單。
    """
    items = [Uncited(s) for s in uncited_attributions(answer)]
    sources = sources or []
    if not items or not sources:
        return items
    limit = max(300, settings.faith_source_budget // len(sources))
    src_block = "\n\n".join(f"[{i}] {_source_text(s, limit)}" for i, s in enumerate(sources, 1))
    sent_block = "\n".join(f"{i}. {u.text}" for i, u in enumerate(items, 1))
    try:
        raw = chat([{"role": "system", "content": _UNCITED_SYSTEM + _chart_note(chart)},
                    {"role": "user", "content": f"【原文】\n{src_block}\n\n【句子】\n{sent_block}"}],
                   temperature=0.0)
        results = {int(r["id"]): r for r in _parse_json(raw).get("results", [])
                   if isinstance(r, dict) and "id" in r}
    except Exception as e:  # noqa: BLE001
        logger.warning("沒標引用的網友說法：judge 失敗：%s", e)
        return items
    for i, u in enumerate(items, 1):
        r = results.get(i) or {}
        v = str(r.get("verdict", "")).strip().lower()
        if v in (UNCITED_FOUND, UNCITED_SUMMARY, UNCITED_NOT_FOUND):
            u.verdict = v
            u.ids = [int(n) for n in r.get("ids") or [] if str(n).isdigit()]
    return items
