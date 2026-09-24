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
_SENT_END = re.compile(r"(?<=[。！？!?\n])")
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


def _source_text(src: dict) -> str:
    body = f"{src.get('title') or ''}\n{src.get('content') or ''}".strip()
    return body[:settings.faith_source_chars]


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
    "- evidence 請逐字摘錄原文中最關鍵的一句；not_found 時給空字串。\n"
    "只輸出 JSON，不要 markdown：\n"
    "{\"results\":[{\"id\":1,\"verdict\":\"supported|contradicted|not_found\",\"evidence\":\"...\"}]}"
)


def _parse_json(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    return json.loads(s)


@observe(name="faithfulness_judge", as_type="evaluator", capture_input=False)
def _judge(claims: list[Claim], sources: list[dict]) -> None:
    """把 claims 一次交給 LLM 判斷，結果直接寫回每個 Claim。失敗就拋，由呼叫端決定怎麼處理。"""
    cited = sorted({n for c in claims for n in c.cites})
    src_block = "\n\n".join(f"[{n}] {_source_text(sources[n - 1])}" for n in cited)
    claim_block = "\n".join(
        f"{i}. {c.text}（引用：{'、'.join(f'[{n}]' for n in c.cites)}）"
        for i, c in enumerate(claims, 1))
    raw = chat([
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": f"【原文】\n{src_block}\n\n【論點】\n{claim_block}"},
    ], temperature=0.0)
    results = {int(r["id"]): r for r in _parse_json(raw).get("results", [])
               if isinstance(r, dict) and "id" in r}
    for i, c in enumerate(claims, 1):
        r = results.get(i) or {}
        verdict = str(r.get("verdict", "")).strip().lower()
        # judge 漏回或回了清單外的值：當 not_found——寧可低估也不要把沒判到的算成支持
        c.verdict = verdict if verdict in _VERDICTS else NOT_FOUND
        c.method = "llm"
        c.evidence = str(r.get("evidence") or "")[:200]


# ---- 5) 組起來 ---------------------------------------------------------------
def evaluate(answer: str, sources: list[dict] | None) -> Result:
    """評一題。不呼叫 Langfuse score，純回傳結果（離線評測與線上抽樣共用）。"""
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
            _judge(pending, sources)
        except Exception as e:  # noqa: BLE001 — judge 壞掉：結果標成不可信，不猜
            logger.warning("faithfulness judge 失敗：%s", e)
            result.error = str(e)
    return result


def _comment(res: Result) -> str | None:
    bad = res.unsupported
    if not bad:
        return None
    return "；".join(f"{c.verdict}：{c.text[:40]}（{','.join(map(str, c.cites))}）"
                    for c in bad[:5])


def maybe_score_async(answer: str, sources: list[dict] | None) -> None:
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
            res = evaluate(answer, sources)
            if res.error or res.score is None:
                return
            tracing.score_trace(trace_id, "faithfulness", res.score, comment=_comment(res))
            tracing.flush()
        except Exception as e:  # noqa: BLE001 — 背景量測，壞掉只記 log
            logger.warning("faithfulness 抽樣評分失敗（略過）：%s", e)

    threading.Thread(target=work, name="faithfulness", daemon=True).start()
