"""跑路由評測：這題該不該去爬社群，模型判斷對了幾成。

    .venv\\Scripts\\python.exe scripts\\langfuse_eval.py [run_name]

只跑「第一次 LLM 呼叫」就能驗完——問題是否觸發 community_search，看的是模型有沒有回
tool_calls，不必真的執行工具。因此整輪十幾題只花十幾秒、幾分錢，改 prompt 或換模型後
可以隨時重跑比較，這正是 config.py 裡那些門檻註解當初手動在做的事。

結果會進 Langfuse 的 Datasets → Runs，可以跨 run 比較分數。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langfuse import Evaluation, get_client  # noqa: E402

from app import agent, llm  # noqa: E402

DATASET = "seiqa-routing"


def task(*, item, **_) -> str:
    """跑到「模型決定要不要用工具」為止，回 'search' 或 'direct'。

    刻意重用 agent._build_context：要評的是**線上實際會用的那份 system prompt**
    （含後台設定、Langfuse 版本、偏好修飾），自己另外組一份就評不到真正的行為了。
    end_user_id 傳 None＝以匿名身分評測，避免把某個使用者的長期記憶混進判斷。
    """
    question = item.input["question"] if isinstance(item.input, dict) else str(item.input)
    ctx = agent._build_context(question, None, None)  # noqa: SLF001 — 就是要評線上那份
    msg = llm.chat_with_tools(ctx.messages, ctx.tools,
                              temperature=ctx.temperature, model=ctx.model)
    called = [tc.function.name for tc in (msg.tool_calls or [])]
    return "search" if "community_search" in called else "direct"


def routing_correct(*, output, expected_output, **_) -> Evaluation:
    """判斷對不對。兩種錯法分開寫進 comment——它們的嚴重性不一樣。"""
    ok = output == expected_output
    if ok:
        comment = f"正確（{output}）"
    elif expected_output == "search":
        comment = "該查卻沒查：會拿常識瞎掰，燈號該綠變黃"
    else:
        comment = "不該查卻查了：白等九十秒，還可能撈回雜訊"
    return Evaluation(name="routing_correct", value=1.0 if ok else 0.0,
                      data_type="NUMERIC", comment=comment)


if __name__ == "__main__":
    run_name = sys.argv[1] if len(sys.argv) > 1 else None
    client = get_client()
    dataset = client.get_dataset(DATASET)

    result = client.run_experiment(
        name="routing",
        run_name=run_name,
        description="該不該呼叫 community_search",
        data=dataset.items,
        task=task,
        evaluators=[routing_correct],
        max_concurrency=4,   # 壓著併發，免得同時打爆 Azure 端點的限流
    )

    rows = getattr(result, "item_results", []) or []
    wrong = []
    for r in rows:
        score = next((e.value for e in (r.evaluations or [])
                      if e.name == "routing_correct"), None)
        if score == 0.0:
            q = r.item.input["question"] if isinstance(r.item.input, dict) else r.item.input
            wrong.append((q, r.item.expected_output, r.output))

    total = len(rows)
    print(f"\n路由正確率：{total - len(wrong)}/{total}")
    for q, expected, got in wrong:
        print(f"  ✗ {q}  期望 {expected}、實得 {got}")
    print(f"\nLangfuse → Datasets → {DATASET} → Runs 可跨 run 比較")
    client.flush()
