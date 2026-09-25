"""驗證 app/evidence.py：引句比對（逐字、容錯、該擋的要擋）與寫作 messages 的組裝。

    .venv\\Scripts\\python.exe scripts\\evidence_selftest.py          # 離線（不花錢）
    .venv\\Scripts\\python.exe scripts\\evidence_selftest.py --live   # 加上真打 LLM 走一次抽取＋核對

比對規則放寬一分就多一分讓假引句混過去的機會，所以「該擋」那組跟「該過」那組一樣重要。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import evidence as ev  # noqa: E402

SRC = "用了三年，這台除濕機很安靜，晚上開著睡覺完全不會吵，就是耗電有點兇，夏天電費多了兩三百。"

# (說明, 引句, 預期是否通過)
QUOTE_CASES = [
    ("逐字", "晚上開著睡覺完全不會吵", True),
    ("標點與空白不同", "晚上開著睡覺 完全不會吵！", True),
    ("全形半形不同", "夏天電費多了兩三百", True),
    ("少一兩個字（模糊比對）", "這台除濕機很安靜晚上開著睡覺完全不吵", True),
    ("意思相反的改寫", "晚上開著睡覺吵到睡不著", False),
    ("原文沒有的內容", "售後服務是業界最好的", False),
    ("太短（到處都比對得到）", "很安靜", False),
    ("空字串", "", False),
    ("用省略號跳過中間", "用了三年，這台除濕機很安靜...夏天電費多了兩三百", True),
    ("全形省略號", "這台除濕機很安靜……就是耗電有點兇", True),
    ("省略號但順序顛倒", "夏天電費多了兩三百...這台除濕機很安靜", False),
    ("省略號拼接捏造的段落", "這台除濕機很安靜...售後服務是業界最好的", False),
    ("只剩碎片（段太短）", "很安靜...兩三百", False),
]


def check_quotes() -> int:
    fails = 0
    for label, quote, expected in QUOTE_CASES:
        got = ev.quote_found(quote, SRC)
        ok = got == expected
        print(f"{'OK  ' if ok else 'FAIL'} 引句｜{label}")
        fails += not ok
    return fails


def check_verify() -> int:
    sources = [{"title": "除濕機心得", "content": SRC}, {"title": "日立", "content": "推日立，除濕效率高，水箱也大，缺點是價格偏貴。"}]
    items = [
        ev.Evidence("很安靜", 1, "晚上開著睡覺完全不會吵"),          # 過
        ev.Evidence("日立效率高", 2, "推日立，除濕效率高"),           # 過
        ev.Evidence("編號放錯", 2, "晚上開著睡覺完全不會吵"),         # 引句在第 1 則，不在第 2 則
        ev.Evidence("超出範圍", 7, "晚上開著睡覺完全不會吵"),
        ev.Evidence("", 1, "晚上開著睡覺完全不會吵"),                # 沒有論點
    ]
    ok, bad = ev.verify(items, sources)
    passed = [e.point for e in ok] == ["很安靜", "日立效率高"] and len(bad) == 3
    print(f"{'OK  ' if passed else 'FAIL'} verify｜編號放錯、超出範圍、沒論點都不通過")
    return not passed


def check_write_messages() -> int:
    """寫作那一刀不可以帶工具回傳的原文，只能有已核對的論點。"""
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "除濕機推薦？"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "上萬字的貼文原文"},
    ]
    out = ev.write_messages(messages, [ev.Evidence("很安靜", 1, "晚上開著睡覺完全不會吵")],
                            [{"source": "dcard"}], None)
    roles = [m["role"] for m in out]
    ok = (roles == ["system", "user", "system"]
          and "上萬字" not in str(out) and "[1]（dcard）很安靜" in out[-1]["content"])
    print(f"{'OK  ' if ok else 'FAIL'} write_messages｜拿掉工具原文、只留已核對論點")
    return not ok


def check_live() -> None:
    """真打 LLM：拿 MySQL 裡一題真實答案的來源，走一次抽取＋核對，看通過率。"""
    import json

    from app import memory_store, tools, tracing

    conn = memory_store._connect()  # noqa: SLF001
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.sources, (SELECT u.content FROM message u WHERE u.conversation_id = "
                "a.conversation_id AND u.role='user' AND u.id < a.id ORDER BY u.id DESC LIMIT 1) "
                "FROM message a WHERE a.role='assistant' AND a.used_tools LIKE '%community_search%' "
                "AND JSON_LENGTH(a.sources) BETWEEN 10 AND 30 ORDER BY a.id DESC LIMIT 1")
            sources, question = cur.fetchone()
    finally:
        conn.close()
    posts = json.loads(sources) if isinstance(sources, str) else sources
    tool_text = "\n\n".join(f"[{i}]（{p.get('source', '')}）{p.get('title', '')}\n{p.get('content', '')}"
                            for i, p in enumerate(posts, 1))
    messages = [{"role": "system", "content": "你是熟悉鄉民討論的朋友。"},
                {"role": "user", "content": question},
                {"role": "system", "content": "（工具結果）\n" + tool_text}]
    items = ev.extract(messages, tools.TOOLS, None)
    ok, bad = ev.verify(items, posts)
    print(f"\n問題：{question}（{len(posts)} 則來源）")
    print(f"抽出 {len(items)} 條，引句對得上 {len(ok)} 條（{len(ok) / max(len(items), 1):.0%}）")
    for e in bad[:5]:
        print(f"  ✗ [{e.source}]「{e.quote[:40]}」")
    tracing.flush()


def main() -> int:
    fails = check_quotes() + check_verify() + check_write_messages()
    total = len(QUOTE_CASES) + 2
    print(f"\n{total - fails}/{total} 通過")
    if "--live" in sys.argv:
        check_live()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
