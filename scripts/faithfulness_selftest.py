"""驗證 app/faithfulness.py：拆論點對不對、judge 判得準不準。

    .venv\\Scripts\\python.exe scripts\\faithfulness_selftest.py          # 只驗拆論點（不花錢）
    .venv\\Scripts\\python.exe scripts\\faithfulness_selftest.py --judge  # 加驗 judge（呼叫 LLM）

第二部分是「評審本身可不可信」的校準：faithfulness 分數要有意義，前提是 judge 判得對。
LABELED 是人工標好答案的「論點＋原文」，刻意放了鄉民口語、反諷、量詞這些容易判錯的題型。
judge 準確率低於 85% 時，faithfulness 分數就不該拿來下結論——先修 judge 的 prompt。
換模型、改 _JUDGE_SYSTEM 之後都要重跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import faithfulness as fa  # noqa: E402

# ---------- 1) 拆論點：(說明, 答案, 預期 [(論點, 引用)]) ----------
EXTRACT_CASES: list[tuple[str, str, list[tuple[str, list[int]]]]] = [
    ("一般句尾引用", "很多人覺得市府決策太慢 [1]。也有人說安全第一 [2]。",
     [("很多人覺得市府決策太慢", [1]), ("也有人說安全第一", [2])]),
    ("引用在句號後面", "很多人覺得太慢。[1] 也有人支持。[2][3]",
     [("很多人覺得太慢", [1]), ("也有人支持", [2, 3])]),
    ("一個括號多個編號", "續航普遍被抱怨 [1,3]，但拍照受好評 [2、4]。",
     [("續航普遍被抱怨", [1, 3]), ("但拍照受好評", [2, 4])]),
    ("同句兩論點各自引用", "Dcard 上多數人覺得太貴 [1]，PTT 則覺得還好 [2]，所以看個人。",
     [("Dcard 上多數人覺得太貴", [1]), ("PTT 則覺得還好", [2])]),
    ("沒標引用的句子不評", "整體來說評價兩極。續航被抱怨 [2]。",
     [("續航被抱怨", [2])]),
    ("條列與粗體", "- **價格**偏高 [1]\n- 售後服務不錯 [2]",
     [("價格偏高", [1]), ("售後服務不錯", [2])]),
    ("常識題沒有引用", "攝氏 100 度等於華氏 212 度。", []),
    ("句號後的右引號歸前一句", "有人說「安靜最重要。」價格也很重要 [2]。",
     [("價格也很重要", [2])]),
]

# ---------- 1b) 刪句（放行關卡的最後一道）：(說明, 答案, 要刪的論點關鍵字, 預期結果) ----------
_LIST = ("**整體來看**，大家對遠端工作評價兩極。\n"
         "- 有人覺得生活品質變好 [1]，也有人說很孤單 [2]。\n"
         "- 多數人認為薪水比較高 [3]。\n"
         "所以還是看個人。")
REMOVE_CASES: list[tuple[str, str, str, str]] = [
    ("刪條列項目、不留空行", _LIST, "薪水",
     "**整體來看**，大家對遠端工作評價兩極。\n- 有人覺得生活品質變好 [1]，也有人說很孤單 [2]。\n所以還是看個人。"),
    ("同句另一段有問題就刪整句", _LIST, "孤單",
     "**整體來看**，大家對遠端工作評價兩極。\n- 多數人認為薪水比較高 [3]。\n所以還是看個人。"),
    ("段落之間的句子", "第一段很好 [1]。\n\n第二段有問題 [2]。\n\n第三段 [3]。", "第二段",
     "第一段很好 [1]。\n\n第三段 [3]。"),
    ("沒有要刪的就原樣", "很好 [1]。", "不存在", "很好 [1]。"),
]

# ---------- 2) judge 校準：(說明, 論點, 原文, 正解) ----------
LABELED: list[tuple[str, str, str, str]] = [
    ("直接支持", "有網友抱怨電池續航很差",
     "買了三個月，電池一天要充兩次，續航真的很爛", fa.SUPPORTED),
    ("意思相反", "有網友稱讚電池續航很好",
     "買了三個月，電池一天要充兩次，續航真的很爛", fa.CONTRADICTED),
    ("原文沒提", "有網友抱怨價格太貴",
     "買了三個月，電池一天要充兩次，續航真的很爛", fa.NOT_FOUND),
    ("反諷要讀懂", "有鄉民不滿市府颱風假的決定",
     "市府真的是『好』棒棒，風雨最大的時候叫大家去上班，佩服佩服", fa.SUPPORTED),
    ("反諷別被字面騙", "有鄉民稱讚市府颱風假決定得好",
     "市府真的是『好』棒棒，風雨最大的時候叫大家去上班，佩服佩服", fa.CONTRADICTED),
    ("網路用語", "有人覺得這家餐廳不值得去",
     "排了一小時結果普普，雷，不會再來第二次", fa.SUPPORTED),
    ("量詞不扣分", "不少網友認為遠端工作比較有效率",
     "我遠端之後產出反而變多，通勤省下的時間都拿來做事了", fa.SUPPORTED),
    ("主題對但細節捏造", "有網友說除濕機一年電費超過一萬元",
     "這台除濕機很安靜，就是耗電有點兇", fa.NOT_FOUND),
    ("部分正確不算", "有網友說新創薪水高而且工時短",
     "新創薪水確實比較高，但加班是家常便飯", fa.CONTRADICTED),
    ("推文口氣的支持", "有人支持輝達進駐北士科",
     "推 終於有大廠願意來了 北投要起飛了", fa.SUPPORTED),
    ("無關的原文", "有網友認為外送平台漲價不合理",
     "今天天氣很好，去河濱騎腳踏車", fa.NOT_FOUND),
    ("把個人經驗說成事實", "該品牌的除濕機故障率很高",
     "我家那台用兩年就壞了，不知道是不是運氣不好", fa.NOT_FOUND),
]


def check_extract() -> int:
    fails = 0
    for label, answer, expected in EXTRACT_CASES:
        got = [(c.text, c.cites) for c in fa.extract_claims(answer)]
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} 拆論點｜{label}")
        if not ok:
            fails += 1
            print(f"       預期 {expected}\n       實得 {got}")
    return fails


def check_remove() -> int:
    fails = 0
    for label, answer, kw, expected in REMOVE_CASES:
        bad = [c for c in fa.extract_claims(answer) if kw in c.text]
        got = fa.remove_claims(answer, bad)
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} 刪句｜{label}")
        if not ok:
            fails += 1
            print(f"       預期 {expected!r}\n       實得 {got!r}")
    return fails


def check_judge() -> int:
    """每題單獨一個 source，走 evaluate() 真實路徑（含 NLI 初篩，若有啟用）。"""
    wrong = 0
    for label, claim, source, expected in LABELED:
        res = fa.evaluate(f"{claim} [1]。", [{"title": "", "content": source}])
        if res.error:
            print(f"ERR  judge｜{label}：{res.error}")
            return len(LABELED)
        got = res.claims[0].verdict if res.claims else "（沒拆到論點）"
        ok = got == expected
        wrong += not ok
        via = res.claims[0].method if res.claims else "-"
        print(f"{'OK  ' if ok else 'FAIL'} judge｜{label}（{via}）"
              + ("" if ok else f"  預期 {expected}、判成 {got}"))
    acc = (len(LABELED) - wrong) / len(LABELED)
    print(f"\njudge 準確率：{len(LABELED) - wrong}/{len(LABELED)} = {acc:.0%}"
          + ("" if acc >= 0.85 else "  ← 低於 85%，faithfulness 分數先別拿來下結論"))
    return 0 if acc >= 0.85 else 1


def main() -> int:
    fails = check_extract()
    print(f"\n拆論點：{len(EXTRACT_CASES) - fails}/{len(EXTRACT_CASES)} 通過\n")
    removed = check_remove()
    print(f"\n刪句：{len(REMOVE_CASES) - removed}/{len(REMOVE_CASES)} 通過\n")
    fails += removed
    if "--judge" in sys.argv:
        fails += check_judge()
        from app import tracing
        tracing.flush()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
