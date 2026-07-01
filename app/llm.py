"""薄薄一層 LLM 客戶端：chat()（純文字）+ chat_with_tools()（function calling）。
靠 .env 在 OpenAI / 相容端點（vLLM）與 Azure OpenAI 之間切換——與 dcard_insight 同概念。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from .config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client():
    if settings.azure_endpoint:
        from openai import AzureOpenAI

        return AzureOpenAI(
            api_key=settings.api_key,
            azure_endpoint=settings.azure_endpoint,
            api_version=settings.azure_api_version,
        )
    from openai import OpenAI

    kwargs = {"api_key": settings.api_key}
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    return OpenAI(**kwargs)


def chat(messages: list[dict], temperature: float = 0.2) -> str:
    """純文字補全：不掛工具。"""
    resp = _client().chat.completions.create(
        model=settings.chat_model, temperature=temperature, messages=messages
    )
    return resp.choices[0].message.content or ""


def embed(text: str) -> list[float]:
    """把文字轉成向量（查 Dcard 口碑庫前用）。

    Azure 走 deployment 名稱、OpenAI 走模型名稱——兩者都用 settings.embed_model，
    須與建 dcard_insight 時同一個模型（text-embedding-3-small，1536 維），否則檢索不準。
    """
    resp = _client().embeddings.create(model=settings.embed_model, input=text)
    return resp.data[0].embedding


def expand_queries(question: str, n: int = 3) -> list[str]:
    """把口語問句改寫成 n 條『鄉民用詞』的檢索字串，涵蓋不同面向／同義說法。失敗回 []。

    解決單一稠密向量被強勢詞綁架的問題：例如「健身有助性生活品質？」會被「健身」整碗
    端去健身版；改寫出「做愛 性慾 親密」這種鄉民實際用語，才撈得到對題的感情版討論。
    """
    msgs = [
        {"role": "system", "content": (
            "你是 Dcard 站內搜尋的查詢改寫器。把使用者問題改寫成幾條適合語意檢索的查詢字串，"
            "用鄉民實際會在貼文裡打的口語詞（例如不要寫『性生活品質』，要寫『做愛 性慾 親密』）。"
            "關鍵規則：每一條只聚焦『一個』主題面向，**不要每條都塞同一個詞**；"
            "若問題把兩件事連在一起（例如『A 是否有助於 B』），請分別產生『只談 A』與『只談 B』"
            "的查詢，讓兩個主題各自都能撈到對題的討論，不要每條都同時出現 A 和 B。"
            f"只輸出 {n} 行查詢字串，一行一條，不要編號、不要引號、不要任何解釋。"
        )},
        {"role": "user", "content": question},
    ]
    try:
        text = chat(msgs, temperature=0.3)
    except Exception as e:  # noqa: BLE001 — 改寫失敗就退回只用原問句（上層 fail-safe）
        logger.warning("expand_queries failed, using original query only: %s", e)
        return []
    lines = [ln.strip(" -•·\t").strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln][:n]


def chat_with_tools(messages: list[dict], tools: list[dict], temperature: float = 0.2,
                    model: str | None = None):
    """掛上工具的補全：回傳 message 物件，呼叫端自行判斷有沒有 tool_calls。

    model 留空＝用 settings.chat_model（.env）；後台啟用的 agent 會帶入自己的 model（M3）。
    """
    resp = _client().chat.completions.create(
        model=model or settings.chat_model,
        temperature=temperature,
        messages=messages,
        tools=tools,
        tool_choice="auto",
    )
    return resp.choices[0].message
