"""薄薄一層 LLM 客戶端：chat()（純文字）+ chat_with_tools()（function calling）
+ chat_with_tools_stream()（同上但逐字串流，給 /ws/ask 用）。
靠 .env 在 OpenAI / 相容端點（vLLM）與 Azure OpenAI 之間切換——與 dcard_insight 同概念。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from functools import lru_cache

from . import progress
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


def _merge_tool_call_deltas(acc: dict[int, dict], deltas) -> None:
    """把串流回來的 tool_calls 分片依 index 合併成完整的 tool call。

    串流時 function.arguments 是一段一段（有時一個字元）吐出來的，name/id 只在第一片出現，
    所以要以 delta.index 當槽位累積字串，不能直接覆寫。
    """
    for d in deltas:
        slot = acc.setdefault(d.index, {"id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}})
        if d.id:
            slot["id"] = d.id
        if d.function is None:
            continue
        if d.function.name:
            slot["function"]["name"] = d.function.name
        if d.function.arguments:
            slot["function"]["arguments"] += d.function.arguments


def chat_with_tools_stream(messages: list[dict], tools: list[dict], temperature: float = 0.2,
                           model: str | None = None) -> Iterator[tuple[str, object]]:
    """chat_with_tools 的串流版。逐一 yield ('token', 文字片段)，最後 yield ('message', dict)。

    最後那個 message dict 的形狀與非串流版的 message.model_dump(exclude_none=True) 對齊
    （content 為空時不放，才能安全塞回 messages 陣列），供 agent loop 繼續跑工具。

    每收一片就檢查取消 → 使用者在「生成中」按停止能立刻中斷；退出時關掉 stream，
    底層 HTTP 連線隨之中止，不會讓 LLM 繼續算完整段。
    """
    stream = _client().chat.completions.create(
        model=model or settings.chat_model,
        temperature=temperature,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        stream=True,
    )
    content_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    try:
        for chunk in stream:
            progress.raise_if_cancelled()
            if not chunk.choices:  # Azure 首片常是空 choices（內容過濾 metadata）
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield ("token", delta.content)
            if delta.tool_calls:
                _merge_tool_call_deltas(tool_acc, delta.tool_calls)
    finally:
        stream.close()

    msg: dict = {"role": "assistant"}
    content = "".join(content_parts)
    if content:
        msg["content"] = content
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    if "content" not in msg and "tool_calls" not in msg:
        msg["content"] = ""  # 模型什麼都沒回：給個空字串，上層照常收斂
    yield ("message", msg)
