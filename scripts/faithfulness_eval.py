"""忠實度評測：被引用的原文，到底有沒有支持答案裡的論點。

兩種模式：

    .venv\\Scripts\\python.exe scripts\\faithfulness_eval.py history [N]
        拿 MySQL 裡最近 N 則（預設 30）「有來源」的真實答案來評。不必重跑爬蟲，
        幾分鐘、幾毛錢就有一條**基準線**——這是改任何東西之前要先量的數字。
        （message.sources 存的是完整貼文含 content，所以原文拿得回來。）

    .venv\\Scripts\\python.exe scripts\\faithfulness_eval.py dataset [run_name]
        對 Langfuse 資料集 seiqa-faithfulness 的固定題目**重跑整個 agent**再評分，
        結果進 Datasets → Runs 可跨 run 比較。改 prompt／檢索／模型之後用這個驗證。
        會真的去爬社群，一題一兩分鐘，且併發壓在 1（Dcard 共用一顆 Chrome）。

分數算法：全部論點裡被支持的比例（micro 平均），不是每題分數再平均——
長答案論點多，理應佔比較大的權重。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import faithfulness as fa  # noqa: E402

DATASET = "seiqa-faithfulness"

# 都是「該去爬社群」的題目——沒來源的答案沒有論點可評。
QUESTIONS = [
    "大家覺得台北那次颱風假放得合理嗎？",
    "輝達進駐北士科，網友怎麼看？",
    "福智教育園區的評價如何？",
    "最近外送平台漲價，大家反應怎樣？",
    "換工作到新創公司，過來人的心得如何？",
    "大家都推薦哪家的除濕機？",
    "新竹有什麼推薦的餐廳？",
    "遠端工作大家實際適應得如何？",
    "ChatGPT 和 Claude 哪個比較好用？",
]


def _report(results: list[tuple[str, fa.Result]]) -> None:
    claims = [c for _, r in results for c in r.claims]
    errors = [q for q, r in results if r.error]
    scored = [(q, r) for q, r in results if not r.error and r.score is not None]
    if not claims:
        print("沒有任何帶引用的論點可評。")
        return
    counted = [c for _, r in scored for c in r.claims]
    ok = sum(c.verdict == fa.SUPPORTED for c in counted)
    by = {v: sum(c.verdict == v for c in counted)
          for v in (fa.SUPPORTED, fa.CONTRADICTED, fa.NOT_FOUND, fa.BAD_CITATION)}
    via_nli = sum(c.method == "nli" for c in counted)

    print(f"\n評了 {len(scored)} 題、{len(counted)} 個論點"
          + (f"（另有 {len(errors)} 題 judge 失敗未計）" if errors else ""))
    print(f"faithfulness：{ok}/{len(counted)} = {ok / len(counted):.1%}")
    print("  " + "、".join(f"{k} {v}" for k, v in by.items()))
    if via_nli:
        print(f"  其中 {via_nli} 個由 NLI 直接放行，其餘由 LLM 判斷")

    worst = sorted(scored, key=lambda x: x[1].score)[:5]
    print("\n分數最低的題目：")
    for q, r in worst:
        print(f"  {r.score:.0%}  {q[:40]}")
        for c in r.unsupported[:3]:
            print(f"        ✗ {c.verdict:<13} {c.text[:50]}  {c.cites}")


# ---- history：拿 MySQL 裡的真實答案評 ----------------------------------------
def run_history(limit: int) -> None:
    from app import memory_store

    if not memory_store._enabled():  # noqa: SLF001
        sys.exit("MySQL 沒設定（DB_HOST / DB_RW_USER），history 模式無法使用。")
    conn = memory_store._connect()  # noqa: SLF001 — 同一組讀寫帳號，已有 message 的 SELECT
    try:
        with conn.cursor() as cur:
            # 每則答案配它前一則（同對話）的使用者問題，報表才看得懂是哪一題
            cur.execute(
                "SELECT a.content, a.sources, a.chart, "
                "  (SELECT u.content FROM message u WHERE u.conversation_id = a.conversation_id "
                "     AND u.role = 'user' AND u.id < a.id ORDER BY u.id DESC LIMIT 1) "
                "FROM message a WHERE a.role = 'assistant' "
                "  AND a.sources IS NOT NULL AND JSON_LENGTH(a.sources) > 0 "
                "ORDER BY a.id DESC LIMIT %s", (int(limit),))
            rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for i, (answer, sources, chart, question) in enumerate(rows, 1):
        srcs = json.loads(sources) if isinstance(sources, str) else (sources or [])
        chart = json.loads(chart) if isinstance(chart, str) else chart  # 占比題才有
        q = question or "（找不到對應問題）"
        print(f"[{i}/{len(rows)}] {q[:40]}", flush=True)
        results.append((q, fa.evaluate(answer or "", srcs, chart)))
    _report(results)


# ---- dataset：固定題目重跑 agent 再評 ----------------------------------------
def _item_id(q: str) -> str:
    return "faith-" + hashlib.sha1(q.encode("utf-8")).hexdigest()[:16]


def run_dataset(run_name: str | None) -> None:
    from langfuse import Evaluation, get_client

    from app import agent

    client = get_client()
    client.create_dataset(name=DATASET, description="忠實度：被引用的原文是否支持答案裡的論點")
    for q in QUESTIONS:  # 冪等：同一題固定 id，重跑是覆蓋不是新增（理由見 langfuse_seed._item_id）
        client.create_dataset_item(dataset_name=DATASET, input={"question": q}, id=_item_id(q))

    def task(*, item, **_) -> dict:
        q = item.input["question"] if isinstance(item.input, dict) else str(item.input)
        started = time.monotonic()
        r = agent.run(q, history=[], session_id=f"faith-eval-{_item_id(q)}", end_user_id=None)
        # 成本的代理指標：工具回傳給 LLM 的總字數（貼文全文都在這裡，是每題最大的 token 來源）。
        # 不直接讀 token 用量：那要回頭查 Langfuse，而它在負載高時會漏收 span。
        tool_chars = sum(len(m.get("content") or "") for m in r.get("messages", [])
                         if isinstance(m, dict) and m.get("role") == "tool")
        # sources 只留評分用得到的欄位；content 必須留，judge 要讀原文
        return {"answer": r["answer"], "chart": r.get("chart"),
                "elapsed": round(time.monotonic() - started, 1), "tool_chars": tool_chars,
                "sources": [{k: s.get(k, "") for k in ("title", "url", "content", "source")}
                            for s in r.get("sources", [])]}

    done: dict[str, fa.Result] = {}  # 答案 → 評分結果；報表直接沿用，不再多打一次 judge

    def faithfulness(*, output, **_):
        res = done[output["answer"]] = fa.evaluate(output["answer"], output["sources"],
                                                   output.get("chart"))
        if res.error or res.score is None:
            return []  # 沒論點或 judge 失敗：不給分，免得把 0 或 1 灌進平均
        return Evaluation(name="faithfulness", value=res.score, data_type="NUMERIC",
                          comment=fa._comment(res))  # noqa: SLF001

    result = client.run_experiment(
        name="faithfulness", run_name=run_name, description="被引用的原文是否支持論點",
        data=client.get_dataset(DATASET).items, task=task, evaluators=[faithfulness],
        max_concurrency=1,
    )
    from app.config import settings

    rows, per_q = [], []
    for r in getattr(result, "item_results", []) or []:
        q = r.item.input["question"] if isinstance(r.item.input, dict) else r.item.input
        out = r.output or {}
        res = done.get(out.get("answer", "")) or fa.Result(error="task 失敗")
        rows.append((q, res))
        if out:
            per_q.append((q, out.get("elapsed", 0), len(out.get("sources", [])),
                          out.get("tool_chars", 0), len(res.claims)))
    print(f"\nRERANK_ENABLED={settings.rerank_enabled}")
    _report(rows)
    if per_q:
        print("\n每題：耗時 / 來源數 / 送進 LLM 的字數 / 論點數")
        for q, sec, n_src, chars, n_claims in per_q:
            print(f"  {sec:>6.0f}s  {n_src:>4}  {chars:>7,}  {n_claims:>3}  {q[:30]}")
        k = len(per_q)
        print(f"  平均：{sum(x[1] for x in per_q) / k:.0f}s、{sum(x[2] for x in per_q) / k:.1f} 則、"
              f"{sum(x[3] for x in per_q) / k:,.0f} 字、{sum(x[4] for x in per_q) / k:.1f} 個論點")
    print(f"\nLangfuse → Datasets → {DATASET} → Runs 可跨 run 比較")
    client.flush()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "history"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == "history":
        run_history(int(arg) if arg else 30)
    elif mode == "dataset":
        run_dataset(arg)
    else:
        sys.exit(__doc__)
    from app import tracing
    tracing.flush()
