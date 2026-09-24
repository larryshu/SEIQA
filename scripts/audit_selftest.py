"""驗證 app/audit.py 的五條檢查：該抓的有抓到，不該抓的沒有誤報。

    .venv\\Scripts\\python.exe scripts\\audit_selftest.py

不用連 Langfuse（audit.audit() 是純函式）。改了規則的樣式就重跑這支——
誤報一次就會讓稽核分數失去可信度，所以「不該抓」那一組比「該抓」那一組更重要。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit import audit, repair  # noqa: E402

SRC3 = [{"source": "dcard"}, {"source": "ptt"}, {"source": "dcard"}]

# (說明, 答案, sources, used_tools, 預期違規的規則)
CASES: list[tuple[str, str, list[dict], list[str], set[str]]] = [
    # ---------- 該抓到的 ----------
    ("字面 [n]", "很多人都這樣覺得 [n]。", SRC3, ["community_search"], {"literal_n"}),
    ("大寫 [N]", "有人提到這件事 [N]。", SRC3, ["community_search"], {"literal_n"}),
    ("編號超出來源數", "有人說很棒 [7]。", SRC3, ["community_search"], {"citation_range"}),
    ("沒來源卻標編號", "鄉民說讚 [1]。", [], [], {"citation_range"}),
    ("提到沒撈到的平台", "Threads 上很多人討論這件事。", SRC3, ["community_search"],
     {"phantom_platform"}),
    ("沒統計卻講百分比", "大概有 63% 的人反對。", SRC3, ["community_search"],
     {"unsourced_percent"}),
    ("沒統計卻講成數", "大概六成的人覺得不妥。", SRC3, ["community_search"],
     {"unsourced_percent"}),
    ("沒統計卻講幾幾開", "整體來說是六四開。", SRC3, ["community_search"],
     {"unsourced_percent"}),
    ("方塊拼圖表", "贊成 ████████ 60%\n反對 ████ 40%", SRC3, ["community_search"],
     {"unsourced_percent", "ascii_chart"}),
    ("一次踩多條", "Threads 上六成的人說讚 [n]，比例 ███。", SRC3, ["community_search"],
     {"literal_n", "phantom_platform", "unsourced_percent", "ascii_chart"}),

    # ---------- 不該誤報的 ----------
    ("正常有引用的答案", "滿多人覺得市府決策太慢 [1]，也有人說安全第一 [2]。", SRC3,
     ["community_search"], set()),
    ("有跑統計才講比例", "統計下來贊成 63%、反對 37%。", SRC3,
     ["community_search", "stance_breakdown"], set()),
    ("成功/成長/造成 不是成數", "這次成功讓討論度成長，也造成一些爭議，變成熱門話題。",
     SRC3, ["community_search"], set()),
    ("一成不變是成語", "大家的看法一成不變，跟去年差不多。", SRC3, ["community_search"], set()),
    ("反問句不算宣稱", "你是想知道大概幾成的人支持嗎？", SRC3, ["community_search"], set()),
    ("單獨的圓點不是圖表", "重點有兩個：● 時間太趕 ● 溝通不足。", SRC3,
     ["community_search"], set()),
    ("有提到的平台確實有資料", "Dcard 上比較多人這樣說，PTT 鄉民則覺得還好。", SRC3,
     ["community_search"], set()),
    ("常識題沒來源也沒引用", "攝氏 100 度等於華氏 212 度。", [], [], set()),
]


# repair()：(說明, 答案, 來源數, 預期結果)。只刪「刪了也不改變意思」的部分。
REPAIR_CASES: list[tuple[str, str, int, str]] = [
    ("刪字面 [n]", "很多人覺得太慢 [n]。", 3, "很多人覺得太慢。"),
    ("刪超出範圍的編號、留合法的", "有人說很棒 [2]，也有人說很爛 [7]。", 3,
     "有人說很棒 [2]，也有人說很爛。"),
    ("刪字元圖表那幾行", "分佈如下：\n贊成 ████\n反對 ██████\n整體偏反對 [1]。", 3,
     "分佈如下：\n整體偏反對 [1]。"),
    ("比例句不動（要改寫、不能硬刪）", "大概六成的人反對 [1]。", 3, "大概六成的人反對 [1]。"),
]


def main() -> int:
    failures = 0
    for label, answer, n, expected in REPAIR_CASES:
        got = repair(answer, n)
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} repair｜{label}")
        if not ok:
            failures += 1
            print(f"       預期 {expected!r}\n       實得 {got!r}")
    for label, answer, sources, tools, expected in CASES:
        got = {f.rule for f in audit(answer, sources, tools) if not f.passed}
        ok = got == expected
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {label}")
        if not ok:
            failures += 1
            missed = expected - got
            extra = got - expected
            if missed:
                print(f"       漏抓：{sorted(missed)}")
            if extra:
                print(f"       誤報：{sorted(extra)}   ← 這種比漏抓嚴重")
    total = len(CASES) + len(REPAIR_CASES)
    print(f"\n{total - failures}/{total} 通過")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
