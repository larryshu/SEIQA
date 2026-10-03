"""faithfulness 評測 v2：30 題分類題庫（含追問多輪）＋貼文快照錄製／重播。

    .venv\\Scripts\\python.exe scripts\\faith_suite.py record [--force] [--only s01,f02]
        實際爬蟲跑一遍，把每次爬到的「重排之前」的原文存成快照（eval_data/snapshots/）。
        已有快照的題目會跳過（中斷後重跑可以接續）；--force 重錄。錄製時順便評分，
        結果存成 eval_data/results/record-<時間>.json。爬蟲共用一顆 Chrome，所以一次只跑一題。

    .venv\\Scripts\\python.exe scripts\\faith_suite.py run [名稱] [--workers 4] [--only ...] [--category followup]
        讀快照重播：reranker、證據模式、關卡、judge 全部照常跑，只是不再爬蟲。
        A/B 兩組讀的是同一批原文，差異只來自程式改動；不開 Chrome，可以多題並行。

    .venv\\Scripts\\python.exe scripts\\faith_suite.py compare <結果A> <結果B>
        比較兩次結果（檔名或 eval_data/results/ 下的名稱），依類別列出差異。

為什麼不放在 Langfuse Datasets：題目與結果都放本機 JSON，Langfuse 沒開也照樣能評；
有開的話每一輪的 trace 仍會照常上報。舊的 9 題 dataset 模式保留在 faithfulness_eval.py。

分數算法同 faithfulness_eval.py：全部論點裡被支持的比例（micro 平均）。
"""
from __future__ import annotations

import argparse
import contextvars
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent, faithfulness as fa, sources  # noqa: E402
from app.config import settings  # noqa: E402
from app.sources import SearchResult  # noqa: E402

SUITE = ROOT / "eval_data" / "faith_suite.json"
SNAP_DIR = ROOT / "eval_data" / "snapshots"
RESULT_DIR = ROOT / "eval_data" / "results"
_DEFAULT_PLATFORMS = [("dcard", "Dcard"), ("ptt", "PTT"), ("threads", "Threads")]

_real_fanout = sources._fanout  # noqa: SLF001
_current: contextvars.ContextVar = contextvars.ContextVar("faith_suite_item", default=None)
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


class _ItemState:
    """一題的錄製／重播狀態：依呼叫順序存放每次 fan-out 的結果。"""

    def __init__(self, mode: str, calls: list[dict] | None = None):
        self.mode = mode            # record | replay
        self.calls = calls or []
        self.used: dict[int, int] = {}  # 重播時每一輪已經回放了幾次
        self.missing = 0            # 重播時查詢次數超過快照（重複使用了同一輪的結果）
        self.turn = 0


def _patched_fanout(query: str, end_user_id: int | None = None) -> SearchResult:
    st: _ItemState | None = _current.get()
    if st is None:                  # 不在評測裡：照常爬
        return _real_fanout(query, end_user_id)
    if st.mode == "record":
        res = _real_fanout(query, end_user_id)
        st.calls.append({"turn": st.turn, "query": query,
                         "platforms": [list(p) for p in res.platforms], "posts": list(res.posts)})
        return res
    # replay：依「第幾輪」回放，同一輪內依順序；不比對 query（模型每次改寫的字可能不同）。
    # 不能只依整體順序：實測 r02 錄製時兩輪各查 1 次，重播時模型第 1 輪就連查 3 次，
    # 把第 2 輪的快照也吃掉了，第 2 輪變成「沒撈到資料」。
    mine = [c for c in st.calls if c.get("turn") == st.turn]
    used = st.used.get(st.turn, 0)
    st.used[st.turn] = used + 1
    if mine:
        # 這一輪多查的次數重複用這一輪最後一次的結果（等同「再搜一次撈到差不多的東西」）
        call = mine[min(used, len(mine) - 1)]
        if used >= len(mine):
            st.missing += 1
        return SearchResult(list(call["posts"]), [tuple(p) for p in call["platforms"]])
    # 錄製時這一輪沒查、重播時卻查了：沿用最近一輪的快照，沒有就當沒撈到
    earlier = [c for c in st.calls if c.get("turn", 0) < st.turn]
    st.missing += 1
    if earlier:
        return SearchResult(list(earlier[-1]["posts"]), [tuple(p) for p in earlier[-1]["platforms"]])
    return SearchResult([], list(_DEFAULT_PLATFORMS))


sources._fanout = _patched_fanout  # noqa: SLF001 — community_search 每次呼叫時才查這個名字


def _snap_path(item_id: str) -> Path:
    return SNAP_DIR / f"{item_id}.json"


def _run_item(item: dict, state: _ItemState, run_name: str) -> dict:
    _current.set(state)
    sid = f"suite-{run_name}-{item['id']}"
    history: list[dict] = []
    turns = []
    for i, q in enumerate(item["turns"]):
        state.turn = i
        started = time.monotonic()
        try:
            r = agent.run(q, history=history, session_id=sid, end_user_id=None)
        except Exception as e:  # noqa: BLE001 — 一題壞掉不拖垮整批
            turns.append({"turn": i, "question": q, "error": f"agent: {e}"})
            break
        elapsed = round(time.monotonic() - started, 1)
        res = fa.evaluate(r["answer"], r.get("sources"), r.get("chart"))
        uncited = fa.check_uncited(r["answer"], r.get("sources"), r.get("chart"))
        tool_chars = sum(len(m.get("content") or "") for m in r.get("messages", [])
                         if isinstance(m, dict) and m.get("role") == "tool")
        turns.append({
            "turn": i, "question": q, "answer": r["answer"], "used_tools": r.get("used_tools", []),
            "n_sources": len(r.get("sources") or []), "elapsed": elapsed, "tool_chars": tool_chars,
            "error": res.error,
            "claims": [{"text": c.text, "cites": c.cites, "verdict": c.verdict, "method": c.method,
                        "evidence": c.evidence} for c in res.claims],
            "uncited": [{"text": u.text, "verdict": u.verdict, "ids": u.ids} for u in uncited],
        })
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": r["answer"]}]
    ok = sum(c["verdict"] == fa.SUPPORTED for t in turns for c in t.get("claims", []))
    n = sum(len(t.get("claims", [])) for t in turns)
    log(f"  {item['id']:<4} {item['category']:<10} {ok}/{n}"
        + (f"  （重播時多查了 {state.missing} 次，重複使用快照）" if state.missing else ""))
    return {"id": item["id"], "category": item["category"], "turns": turns,
            "replay_missing": state.missing}


# ---- 報表 ----------------------------------------------------------------------
def _tally(turns: list[dict]) -> tuple[int, int, int]:
    ok = n = err = 0
    for t in turns:
        if t.get("error"):
            err += 1
            continue
        n += len(t["claims"])
        ok += sum(c["verdict"] == fa.SUPPORTED for c in t["claims"])
    return ok, n, err


def summarize(items: list[dict]) -> dict:
    cats: dict[str, list] = {}
    for it in items:
        cats.setdefault(it["category"], []).extend(it["turns"])
    all_turns = [t for it in items for t in it["turns"]]
    first = [t for t in all_turns if t["turn"] == 0]
    later = [t for t in all_turns if t["turn"] > 0]

    def pack(turns):
        ok, n, err = _tally(turns)
        timed = [t for t in turns if "elapsed" in t]
        unc = [u for t in turns for u in t.get("uncited") or []]
        sentences = sum(len(fa._SENT_END.split(t.get("answer") or "")) for t in timed)  # noqa: SLF001
        return {"uncited": {k: sum(u["verdict"] == k for u in unc)
                            for k in (fa.UNCITED_FOUND, fa.UNCITED_SUMMARY, fa.UNCITED_NOT_FOUND)}
                | {"total": len(unc), "sentences": sentences},
                "supported": ok, "claims": n, "score": round(ok / n, 4) if n else None,
                "judge_errors": err, "turns": len(turns),
                "avg_elapsed": round(sum(t["elapsed"] for t in timed) / len(timed), 1) if timed else None,
                "avg_tool_chars": round(sum(t["tool_chars"] for t in timed) / len(timed)) if timed else None,
                "avg_claims": round(n / len(timed), 1) if timed else None}

    return {"overall": pack(all_turns), "first_turn": pack(first), "followup_turns": pack(later),
            "by_category": {k: pack(v) for k, v in cats.items()}}


def _pct(s: dict) -> str:
    return "—" if s["score"] is None else f"{s['supported']}/{s['claims']} = {s['score']:.1%}"


def report(data: dict) -> None:
    s = data["summary"]
    print(f"\n=== {data['name']}（{data['mode']}，{len(data['items'])} 題）===")
    print(f"整體 faithfulness：{_pct(s['overall'])}"
          + (f"（judge 失敗 {s['overall']['judge_errors']} 輪未計）" if s['overall']['judge_errors'] else ""))
    print(f"  第一輪：{_pct(s['first_turn'])}　追問輪：{_pct(s['followup_turns'])}")
    print(f"  每輪平均：{s['overall']['avg_elapsed']} 秒、送進 LLM {s['overall']['avg_tool_chars']:,} 字、"
          f"{s['overall']['avg_claims']} 個論點")
    u = s["overall"].get("uncited")
    if u:
        print(f"  沒標引用的網友說法：{u['total']} 句（找得到 {u['found']}、概述 {u['summary']}、"
              f"原文找不到 {u['not_found']}）")
    print("\n依類別：")
    for cat, v in s["by_category"].items():
        print(f"  {cat:<11} {_pct(v):<22} {v['turns']} 輪")
    bad = [(it, t, c) for it in data["items"] for t in it["turns"] for c in t.get("claims", [])
           if c["verdict"] != fa.SUPPORTED]
    if bad:
        print(f"\n沒有被原文支持的論點（{len(bad)} 個，最多列 12 個）：")
        for it, t, c in bad[:12]:
            print(f"  {it['id']} 第{t['turn'] + 1}輪 ✗ {c['verdict']:<13} {c['text'][:46]} {c['cites']}")
    nf = [(it, t, u) for it in data["items"] for t in it["turns"] for u in t.get("uncited") or []
          if u["verdict"] == fa.UNCITED_NOT_FOUND]
    if nf:
        print(f"\n沒標引用、原文也找不到的網友說法（{len(nf)} 句，最多列 12 句）：")
        for it, t, u in nf[:12]:
            print(f"  {it['id']} 第{t['turn'] + 1}輪 ✗ {u['text'][:60]}")
    missing = [it["id"] for it in data["items"] if it.get("replay_missing")]
    if missing:
        print(f"\n注意：{', '.join(missing)} 重播時模型多查了幾次，多出的查詢重複使用同一輪的快照。")


def _save(name: str, mode: str, items: list[dict]) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "mode": mode, "created_at": datetime.now().isoformat(timespec="seconds"),
            "settings": {k: getattr(settings, k) for k in (
                "rerank_enabled", "evidence_mode", "faith_gate_enabled", "faith_gate_verdicts",
                "evidence_max_claims", "evidence_answer_chars", "chat_model")},
            "summary": summarize(items), "items": sorted(items, key=lambda x: x["id"])}
    path = RESULT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=list), encoding="utf-8")
    report(data)
    print(f"\n結果已存：{path.relative_to(ROOT)}")
    return path


# ---- 指令 ----------------------------------------------------------------------
def _select(items: list[dict], only: str | None, category: str | None) -> list[dict]:
    if only:
        wanted = {x.strip() for x in only.split(",")}
        items = [i for i in items if i["id"] in wanted]
    if category:
        items = [i for i in items if i["category"] == category]
    return items


def cmd_record(args) -> None:
    items = _select(json.loads(SUITE.read_text(encoding="utf-8"))["items"], args.only, args.category)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    todo = [i for i in items if args.force or not _snap_path(i["id"]).exists()]
    print(f"錄製 {len(todo)} 題（已有快照、略過 {len(items) - len(todo)} 題）；一次一題，實際爬蟲。")
    name = f"record-{datetime.now():%Y%m%d-%H%M}"
    out = []
    for item in todo:
        state = _ItemState("record")
        result = _run_item(item, state, name)
        snap = {"id": item["id"], "turns": item["turns"],
                "recorded_at": datetime.now().isoformat(timespec="seconds"), "calls": state.calls}
        _snap_path(item["id"]).write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        out.append(result)
    if out:
        _save(name, "record", out)


def cmd_run(args) -> None:
    items = _select(json.loads(SUITE.read_text(encoding="utf-8"))["items"], args.only, args.category)
    ready = [i for i in items if _snap_path(i["id"]).exists()]
    skipped = [i["id"] for i in items if i not in ready]
    if skipped:
        print(f"沒有快照、略過：{', '.join(skipped)}（先跑 record）")
    name = args.name or f"run-{datetime.now():%Y%m%d-%H%M}"
    print(f"重播 {len(ready)} 題，{args.workers} 題並行。")
    out = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = []
        for item in ready:
            snap = json.loads(_snap_path(item["id"]).read_text(encoding="utf-8"))
            state = _ItemState("replay", snap["calls"])
            futures.append(ex.submit(contextvars.copy_context().run, _run_item, item, state, name))
        for fut in as_completed(futures):
            out.append(fut.result())
    _save(name, "replay", out)


def _load_result(ref: str) -> dict:
    p = Path(ref)
    if not p.exists():
        p = RESULT_DIR / (ref if ref.endswith(".json") else f"{ref}.json")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_compare(args) -> None:
    a, b = _load_result(args.a), _load_result(args.b)
    sa, sb = a["summary"], b["summary"]

    def row(label, x, y):
        fx = "—" if x["score"] is None else f"{x['score']:.1%}"
        fy = "—" if y["score"] is None else f"{y['score']:.1%}"
        d = "" if x["score"] is None or y["score"] is None else f"{(y['score'] - x['score']) * 100:+.1f} pt"
        print(f"  {label:<14} {fx:>8} → {fy:>8}  {d}")

    print(f"{a['name']} → {b['name']}")
    row("整體", sa["overall"], sb["overall"])
    row("第一輪", sa["first_turn"], sb["first_turn"])
    row("追問輪", sa["followup_turns"], sb["followup_turns"])
    for cat in sa["by_category"]:
        if cat in sb["by_category"]:
            row(cat, sa["by_category"][cat], sb["by_category"][cat])
    oa, ob = sa["overall"], sb["overall"]
    print(f"  每輪耗時 {oa['avg_elapsed']} → {ob['avg_elapsed']} 秒；送進 LLM {oa['avg_tool_chars']:,} → "
          f"{ob['avg_tool_chars']:,} 字；論點 {oa['avg_claims']} → {ob['avg_claims']} 個")
    ua, ub = oa.get("uncited"), ob.get("uncited")
    if ua and ub:
        print(f"  沒標引用的網友說法 {ua['total']} → {ub['total']} 句；其中原文找不到 "
              f"{ua['not_found']} → {ub['not_found']} 句")
    print("  設定差異：", {k: (a["settings"].get(k), v) for k, v in b["settings"].items()
                        if a["settings"].get(k) != v} or "無")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--force", action="store_true")
    rec.add_argument("--only")
    rec.add_argument("--category")
    run = sub.add_parser("run")
    run.add_argument("name", nargs="?")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--only")
    run.add_argument("--category")
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("a")
    cmp_.add_argument("b")
    args = ap.parse_args()
    {"record": cmd_record, "run": cmd_run, "compare": cmd_compare}[args.cmd](args)
    from app import tracing
    tracing.flush()


if __name__ == "__main__":
    main()
