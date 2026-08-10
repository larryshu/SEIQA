"""把 Langfuse 需要的東西一次建好：模型定價、system prompt、評測資料集。

砍掉 Langfuse 的 volume 重建之後跑這一支就能復原（三件事都是冪等的，重跑只會更新不會重複）：

    .venv\\Scripts\\python.exe scripts\\langfuse_seed.py

為什麼定價要自己建：自架版內建的模型清單只有 87 筆舊模型，沒有 gpt-4.1，
text-embedding-3-small 雖有定義但沒有價格——不補的話 trace 上的成本全是 0。
詳見 docs/langfuse_observability_plan.md。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langfuse import get_client  # noqa: E402

from app.agent import SYSTEM_PROMPT  # noqa: E402
from app.config import settings  # noqa: E402

client = get_client()


# ---------------------------------------------------------------------------
# 1) 模型定價（OpenAI 官方牌價；鼎新代理的實際費率可能不同，這裡當估算基準）
# ---------------------------------------------------------------------------
# match_pattern 要吃得下 Azure 回傳的日期後綴：實際記錄到的是 gpt-4.1-2025-04-14，
# 不是 deployment 名 gpt-4.1。
MODELS = [
    {
        "model_name": "gpt-4.1",
        "match_pattern": r"(?i)^(gpt-4\.1)(-\d{4}-\d{2}-\d{2})?$",
        "unit": "TOKENS", "tokenizer_id": "openai",
        "input_price": 0.000002,    # $2 / 1M
        "output_price": 0.000008,   # $8 / 1M
    },
    {
        "model_name": "text-embedding-3-small",
        "match_pattern": r"(?i)^(text-embedding-3-small)$",
        "unit": "TOKENS", "tokenizer_id": "openai",
        "input_price": 0.00000002,  # $0.02 / 1M
    },
]


def seed_models() -> None:
    """建立／更新模型定價。

    API 沒有 upsert：同名再建會 400。所以先撈出專案自訂的模型，同名的先刪再建——
    這樣改了上面的價格重跑就會生效，而不是默默沿用舊價。
    （只碰 MODELS 裡列到的名字，不動 Langfuse 內建的那 87 筆。）
    """
    from langfuse.api.commons.errors.not_found_error import NotFoundError

    listed = client.api.models.list(limit=100).data
    for spec in MODELS:
        name = spec["model_name"]
        # 同一個名字可能同時有「內建」與「自訂」兩筆（text-embedding-3-small 就是這樣：
        # 內建那筆有定義卻沒有價格）。內建的刪不掉會回 404，跳過即可——自訂的優先生效。
        removed = 0
        for m in [x for x in listed if x.model_name == name]:
            try:
                client.api.models.delete(m.id)
                removed += 1
            except NotFoundError:
                pass  # 內建模型，不能刪也不需要刪
        m = client.api.models.create(**spec)
        action = "更新" if removed else "建立"
        print(f"  {action}定價 {m.model_name:<24} in={m.input_price} out={m.output_price}")


# ---------------------------------------------------------------------------
# 2) system prompt（來源仍以後台為優先，這裡是後台沒設時的那一格＋版本歷史）
# ---------------------------------------------------------------------------
def seed_prompt() -> None:
    """內容有變才建新版本。

    create_prompt 每呼叫一次就升一版，即使內容一模一樣——重跑幾次 seed 就會累積出
    一串沒有差異的版本，版本歷史（階段 3 想要的東西）反而變得沒法看。
    """
    name = settings.langfuse_prompt_name
    try:
        current = client.get_prompt(name, label=settings.langfuse_prompt_label,
                                    cache_ttl_seconds=0)
        if current.prompt == SYSTEM_PROMPT:
            print(f"  prompt {name} v{current.version} 內容未變，略過")
            return
    except Exception:  # noqa: BLE001 — 還沒有這個 prompt（第一次跑）→ 往下建
        pass
    p = client.create_prompt(
        name=name,
        prompt=SYSTEM_PROMPT,
        labels=[settings.langfuse_prompt_label],
        type="text",
        commit_message="從 app/agent.py 的 SYSTEM_PROMPT 匯入",
    )
    print(f"  prompt {p.name} v{p.version}（標籤：{settings.langfuse_prompt_label}）")


# ---------------------------------------------------------------------------
# 3) 路由評測資料集
# ---------------------------------------------------------------------------
# 評的是本專案最核心的一個判斷：**這題該不該去爬社群**。
# 判斷錯的兩種後果都很痛——該查沒查＝拿常識瞎掰（🟡 該是 🟢）；不該查卻查了＝
# 白等九十秒還可能撈回一堆雜訊。而且這個評測只需要「第一次 LLM 呼叫」就能驗，
# 不必真的跑爬蟲，所以跑一輪很便宜。
DATASET = "seiqa-routing"

ITEMS: list[tuple[str, str]] = [
    # --- 需要鄉民討論 → 應呼叫 community_search ---
    ("大家覺得台北那次颱風假放得合理嗎？", "search"),
    ("輝達進駐北士科，網友怎麼看？", "search"),
    ("福智教育園區的評價如何？", "search"),
    ("最近外送平台漲價，大家反應怎樣？", "search"),
    ("換工作到新創公司，過來人的心得如何？", "search"),
    ("大家都推薦哪家的除濕機？", "search"),
    # --- 常識／定義／計算 → 應直接回答 ---
    ("攝氏 100 度等於華氏幾度？", "direct"),
    ("HTTP 狀態碼 404 是什麼意思？", "direct"),
    ("幫我算 235 乘以 47", "direct"),
    ("Python 的 list 和 tuple 差在哪裡？", "direct"),
    ("一公斤等於幾公克？", "direct"),
    ("台灣最高的山是哪一座？", "direct"),
    # --- 邊界題：兩邊都說得通一點點，是實際會判錯的地方 ---
    # 沒有這些，正確率永遠是滿分，評測就偵測不到退步（第一次跑 baseline 就是 12/12）。
    # 挑選原則：正解要站得住腳。像「台積電股價多少」那種既非鄉民討論、模型也答不了的，
    # 正解本身就有爭議，放進來只會讓分數變得無法解讀。
    ("新竹有什麼推薦的餐廳？", "search"),          # 在地口碑，不是查得到的事實
    ("遠端工作大家實際適應得如何？", "search"),      # 問的是集體經驗
    ("ChatGPT 和 Claude 哪個比較好用？", "search"),  # 使用心得；模型很容易自己就答了
    ("勞基法的特休天數怎麼算？", "direct"),          # 法條事實，不需要鄉民意見
    ("什麼是 ETF？", "direct"),                     # 純定義
    ("第二次世界大戰是哪一年結束的？", "direct"),     # 史實
]


def _item_id(question: str) -> str:
    """由問題內容算出穩定的 item id（同一題重跑＝覆蓋，不是新增一筆）。

    這裡**不能用內建的 hash()**：Python 對字串的雜湊每個行程都會加隨機種子
    （PYTHONHASHSEED），重跑就會算出不同 id，資料集於是每跑一次就多長一份。
    第一次寫成 hash() 時就踩到了——18 題的資料集跑出 30 筆。
    """
    return "routing-" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:16]


def seed_dataset() -> None:
    client.create_dataset(
        name=DATASET,
        description="路由判斷：這題該不該呼叫 community_search 去爬社群討論",
    )
    wanted = {_item_id(q): (q, e) for q, e in ITEMS}

    # 先清掉不在 ITEMS 裡的舊項目：改過題目、或（像先前那樣）用不穩定 id 灌進來的重複，
    # 留著會讓正確率的分母悄悄變大，分數就不能跨 run 比較了。
    existing = client.api.dataset_items.list(dataset_name=DATASET, limit=100).data
    stale = [it for it in existing if it.id not in wanted]
    for it in stale:
        client.api.dataset_items.delete(it.id)

    for item_id, (question, expected) in wanted.items():
        client.create_dataset_item(
            dataset_name=DATASET,
            input={"question": question},
            expected_output=expected,
            id=item_id,
        )
    n_search = sum(1 for _, e in ITEMS if e == "search")
    removed = f"，清掉 {len(stale)} 筆舊項目" if stale else ""
    print(f"  資料集 {DATASET}：{len(ITEMS)} 題"
          f"（search {n_search} / direct {len(ITEMS) - n_search}）{removed}")


if __name__ == "__main__":
    print(f"Langfuse: {client.auth_check()} @ {settings.langfuse_prompt_name}")
    print("1) 模型定價")
    seed_models()
    print("2) system prompt")
    seed_prompt()
    print("3) 評測資料集")
    seed_dataset()
    client.flush()
    print("完成。")
