"""驗證 app/reranker.py：挑選邏輯（保底、門檻、同分順序）與 fail-safe。

    .venv\\Scripts\\python.exe scripts\\reranker_selftest.py          # 離線（不花錢）
    .venv\\Scripts\\python.exe scripts\\reranker_selftest.py --live   # 加上真打 LLM 打分

--live 拿 MySQL 裡最近一題「來源很多」的真實貼文來打分，印出分數分佈、各平台前後則數，
以及被剔除的貼文標題——調 RERANK_TOP_N / RERANK_MIN_SCORE 前先看這份，確認剔掉的真的是雜訊。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import reranker as rr  # noqa: E402
from app.config import settings  # noqa: E402


def _posts(spec: str) -> list[dict]:
    """'ddpt' → dcard, dcard, ptt, threads（title 帶原始序號，方便看順序）。"""
    name = {"d": "dcard", "p": "ptt", "t": "threads"}
    return [{"source": name[c], "title": f"#{i}", "content": ""} for i, c in enumerate(spec)]


# (說明, 平台序列, 分數, top_n, min_score, 每平台保底, 預期保留的原始序號（依最終順序）)
CASES = [
    ("依分數由高到低", "dddd", [1, 3, 2, 3], 3, 0, 0, [1, 3, 2]),
    ("同分維持原順序", "dddd", [2, 2, 2, 2], 2, 0, 0, [0, 1]),
    ("低於門檻不補位", "dddd", [3, 1, 1, 2], 4, 2, 0, [0, 3]),
    ("平台保底：分數低也留", "dddt", [3, 3, 3, 1], 3, 2, 1, [0, 1, 3]),
    ("保底不收 0 分（離題）", "dddt", [3, 3, 3, 0], 3, 2, 1, [0, 1, 2]),
    ("保底讓總數略超過 top_n", "ddpt", [3, 3, 1, 1], 2, 2, 1, [0, 2, 3]),
]


def check_select() -> int:
    fails = 0
    for label, spec, scores, top_n, min_s, floor, expected in CASES:
        posts = _posts(spec)
        got = rr._select(posts, [float(s) for s in scores], top_n, min_s, floor)  # noqa: SLF001
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} 挑選｜{label}")
        if not ok:
            fails += 1
            print(f"       預期 {expected}、實得 {got}")
    return fails


def check_failsafe() -> int:
    """打分全失敗 → 原清單；部分失敗 → 給中間分；關閉或數量不多 → 不動。"""
    fails = 0
    posts = _posts("d" * 40)
    saved = (settings.rerank_enabled, settings.rerank_top_n, rr._BACKENDS["llm"])  # noqa: SLF001
    try:
        settings.rerank_enabled, settings.rerank_top_n = True, 10

        rr._BACKENDS["llm"] = lambda q, p: [None] * len(p)  # noqa: SLF001
        ok = rr.rerank("q", posts) == posts
        print(f"{'OK  ' if ok else 'FAIL'} fail-safe｜全部打分失敗 → 原清單"); fails += not ok

        def boom(q, p):
            raise RuntimeError("down")
        rr._BACKENDS["llm"] = boom  # noqa: SLF001
        ok = rr.rerank("q", posts) == posts
        print(f"{'OK  ' if ok else 'FAIL'} fail-safe｜後端拋例外 → 原清單"); fails += not ok

        # 前 20 則失敗（中間分 1.5 < 門檻 2 不補位）、後 20 則 3 分 → 只留後段前 10 則
        rr._BACKENDS["llm"] = lambda q, p: [None] * 20 + [3.0] * 20  # noqa: SLF001
        got = [p["title"] for p in rr.rerank("q", posts)]
        ok = got == [f"#{i}" for i in range(20, 30)]
        print(f"{'OK  ' if ok else 'FAIL'} fail-safe｜部分失敗給中間分、排在後面"); fails += not ok

        settings.rerank_top_n = 50
        ok = rr.rerank("q", posts) == posts
        print(f"{'OK  ' if ok else 'FAIL'} 數量不超過 top_n → 不重排"); fails += not ok

        settings.rerank_enabled, settings.rerank_top_n = False, 10
        ok = rr.rerank("q", posts) == posts
        print(f"{'OK  ' if ok else 'FAIL'} 關閉 → 原樣返回"); fails += not ok
    finally:
        settings.rerank_enabled, settings.rerank_top_n, rr._BACKENDS["llm"] = saved  # noqa: SLF001
    return fails


def check_live() -> None:
    """真打 LLM：拿 MySQL 裡來源最多的一題最近答案來看打分結果合不合理。"""
    import json
    import time

    from app import memory_store, tracing

    conn = memory_store._connect()  # noqa: SLF001
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.sources, (SELECT u.content FROM message u WHERE u.conversation_id = "
                "a.conversation_id AND u.role='user' AND u.id < a.id ORDER BY u.id DESC LIMIT 1) "
                "FROM message a WHERE a.role='assistant' AND a.used_tools LIKE '%community_search%' "
                "AND JSON_LENGTH(a.sources) > 40 ORDER BY a.id DESC LIMIT 1")
            sources, question = cur.fetchone()
    finally:
        conn.close()
    posts = json.loads(sources) if isinstance(sources, str) else sources

    started = time.monotonic()
    scores = rr._score_llm(question, posts)  # noqa: SLF001
    elapsed = time.monotonic() - started
    clean = [1.5 if s is None else s for s in scores]
    keep = rr._select(posts, clean, settings.rerank_top_n, settings.rerank_min_score,  # noqa: SLF001
                      settings.rerank_per_platform_min)
    kept = {i for i in keep}

    print(f"\n問題：{question}")
    print(f"{len(posts)} 則 → 留 {len(keep)} 則，打分 {elapsed:.1f} 秒，失敗 {scores.count(None)} 則")
    print("分數分佈：", {s: sum(1 for x in clean if round(x) == s) for s in range(4)})
    print("各平台 前→後：", {k: f"{v}→{sum(1 for i in keep if posts[i].get('source') == k)}"
                             for k, v in rr._count_by_platform(posts).items()})  # noqa: SLF001
    print("\n留下的前 5 則：")
    for i in keep[:5]:
        print(f"  {clean[i]:.0f} 分 ({posts[i].get('source')}) {posts[i].get('title', '')[:40]}")
    print("被剔除的（分數低到高，前 8 則）：")
    for i in sorted((i for i in range(len(posts)) if i not in kept), key=lambda i: clean[i])[:8]:
        print(f"  {clean[i]:.0f} 分 ({posts[i].get('source')}) {posts[i].get('title', '')[:40]}")
    tracing.flush()


def main() -> int:
    fails = check_select() + check_failsafe()
    total = len(CASES) + 5
    print(f"\n{total - fails}/{total} 通過")
    if "--live" in sys.argv:
        check_live()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
