"""使用者層語意記憶（輕量手刻版，非 mem0）。

每輪先用 LLM 從提問萃取「關於使用者本人的長期事實」（例：使用者是後端工程師、正在找工作），
把這條事實 embed 進 Qdrant 的 user_memory collection（payload 帶 end_user_id、原始 Q/A 備查）；
新問題進來時語意撈回該使用者的相關事實 → 注入 system prompt → 個人化、跨 session 記得。

為何存「事實」而非「答案結論」：本系統答案來自社群輿情，結論會過時；只記「關於這個人」的
穩定事實，避免把過時結論當記憶、也避免誘導系統不再即時查證。

定位（別跟另外兩個記憶混）：
- 這裡記「使用者這個人」（他關心/問過什麼）；
- vectorstore（dcard_insight）記「Dcard 貼文內容」；
- 方案 B（QdrantHotStore / crawl_agent_hot）記「爬回來的貼文」。
三者不同 collection、不同用途。

紀律：
- 只對登入使用者（end_user_id 不為 None）生效；匿名不留記憶。
- 全程 fail-safe：embed / Qdrant 任一步炸掉就當沒記憶，不影響回答。
- 走 Qdrant REST（requests），與 vectorstore.py 同一套，不引入 qdrant-client。
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

import requests

from .config import settings
from .llm import chat, embed

logger = logging.getLogger(__name__)

_VECTOR_SIZE = 1536  # text-embedding-3-small
_ensured = False

# 「meta 問題」偵測：使用者在問你記得他什麼 / 之前聊過什麼。
# 這類問題該『列出全部記憶』，而非語意搜（語意上它跟任何內容事實都不相關，會被門檻擋掉）。
_META_PATTERNS = [
    r"(之前|上次|先前|過去).{0,6}(討論|問|說|聊|提|講)",
    r"記得.{0,4}我",
    r"我.{0,6}說過",
    r"你知道.{0,3}(我|關於我)",
    r"我(們)?.{0,4}聊過",
    r"(關於我|我的記憶|我是誰|我的資料)",
]


def is_memory_query(text: str) -> bool:
    """是否為『你記得我什麼 / 之前聊過什麼』這類 meta 問題。"""
    t = (text or "").strip()
    return any(re.search(p, t) for p in _META_PATTERNS)


def _base() -> str:
    return settings.qdrant_url.rstrip("/")


def _ensure_collection() -> None:
    """首次呼叫時確保 user_memory collection 存在（1536 維 / Cosine）。"""
    global _ensured
    if _ensured:
        return
    url = f"{_base()}/collections/{settings.user_memory_collection}"
    r = requests.get(url, timeout=10)
    if r.status_code == 404:
        requests.put(
            url, json={"vectors": {"size": _VECTOR_SIZE, "distance": "Cosine"}}, timeout=15
        ).raise_for_status()
        # end_user_id 建 payload index（過濾用；失敗不致命，filter 仍可運作只是較慢）
        try:
            requests.put(f"{url}/index",
                         json={"field_name": "end_user_id", "field_schema": "integer"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    _ensured = True


def _extract_fact(question: str) -> str:
    """從使用者提問萃取一條『關於使用者本人』的長期事實；沒有就回空字串。fail-safe。"""
    msgs = [
        {"role": "system", "content": (
            "你從使用者的提問中，萃取『關於使用者本人』、可長期記住的事實，用於個人化記憶。\n"
            "規則：\n"
            "- 只抽關於這個人的穩定事實：身分 / 職業 / 處境 / 長期偏好 / 持續關心的主題領域。\n"
            "- 不要抽『世界現況、產品排名、時事結論』這種會過時的東西。\n"
            "- 不要抽一次性、沒有個人資訊的問題（例如純查資料、純常識）。\n"
            "- 用繁體中文、第三人稱一句話，開頭『使用者』。例：使用者是後端工程師，正在找新工作。\n"
            "- 若這題沒有值得長期記住的個人事實，只輸出 NONE。\n"
            "只輸出那一句（或 NONE），不要任何解釋。"
        )},
        {"role": "user", "content": question.strip()},
    ]
    try:
        out = chat(msgs, temperature=0.1).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("extract fact failed (ignored): %s", e)
        return ""
    if not out or out.upper().lstrip(" 「『\"").startswith("NONE"):
        return ""
    return out


def _store_fact(end_user_id: int, fact: str, question: str = "", answer: str = "",
                session_id: str = "", kind: str = "turn") -> None:
    """把一條事實 embed 後 upsert 進 user_memory。呼叫端負責 fail-safe 外層 try。

    kind：'turn'＝每輪即時萃取；'session_summary'＝登出時整段對話摘要（備查/分析用）。
    """
    point = {
        "id": uuid.uuid4().hex,
        "vector": embed(fact),
        "payload": {
            "end_user_id": int(end_user_id),
            "text": fact,                        # recall 注入的就是這條事實
            "question": (question or "").strip(),  # 原始問句（備查）
            "answer": (answer or "")[:1000],     # 原始答案（備查，截斷）
            "session_id": session_id,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    requests.put(
        f"{_base()}/collections/{settings.user_memory_collection}/points",
        json={"points": [point]}, timeout=15,
    ).raise_for_status()


def remember(end_user_id: int | None, question: str, answer: str = "",
             session_id: str = "") -> None:
    """萃取『使用者事實』後存進記憶。匿名 / 關閉 / 無事實 → 不存；fail-safe。

    存的是萃取出的事實 note（embed 用它），原始 Q/A 放 payload 備查、不參與檢索注入。
    """
    if not settings.user_memory_enabled or not end_user_id or not (question or "").strip():
        return
    try:
        fact = _extract_fact(question)
        if not fact:
            return  # 這題沒有值得長期記住的個人事實 → 不存，避免雜訊
        _ensure_collection()
        _store_fact(end_user_id, fact, question=question, answer=answer,
                    session_id=session_id, kind="turn")
    except Exception as e:  # noqa: BLE001
        logger.warning("remember failed (ignored): %s", e)


def _conversation_text(messages: list[dict], max_chars: int = 4000) -> str:
    """把前端帶來的對話（[{role, content}]）攤成純文字；取尾端保留最近脈絡。"""
    lines = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{'使用者' if role == 'user' else '助理'}：{content}")
    return "\n".join(lines)[-max_chars:]


def _extract_facts_from_conversation(transcript: str) -> list[str]:
    """從整段對話萃取最多 3 條『關於使用者本人』的長期事實；沒有就回 []。fail-safe。"""
    if not transcript.strip():
        return []
    msgs = [
        {"role": "system", "content": (
            "以下是一段使用者與助理的完整對話。請萃取『關於使用者本人』、可長期記住的事實，"
            "用於個人化記憶。\n"
            "規則：\n"
            "- 只抽關於這個人的穩定事實：身分 / 職業 / 處境 / 長期偏好 / 持續關心的主題領域。\n"
            "- 不要抽會過時的世界現況、產品排名、時事結論。\n"
            "- 每條一行，繁體中文、第三人稱、開頭『使用者』，最多 3 條。\n"
            "- 若整段對話沒有值得長期記住的個人事實，只輸出 NONE。\n"
            "只輸出事實行（或 NONE），不要編號、不要解釋。"
        )},
        {"role": "user", "content": transcript},
    ]
    try:
        out = chat(msgs, temperature=0.1).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize conversation failed (ignored): %s", e)
        return []
    if not out or out.upper().lstrip(" 「『\"").startswith("NONE"):
        return []
    facts = []
    for line in out.splitlines():
        s = line.strip().lstrip("-•*0123456789.、 ").strip()
        if s and not s.upper().startswith("NONE"):
            facts.append(s)
    return facts[:3]


def summarize_and_remember(end_user_id: int | None, messages: list[dict],
                           session_id: str = "") -> int:
    """登出時：對整段對話萃取 1–3 條長期事實寫入 user_memory。回傳寫入條數。

    每輪 remember() 已寫過即時事實；此處補一次整段脈絡摘要（kind='session_summary'）。
    匿名 / 關閉 / 無事實 → 0；全程 fail-safe，不影響登出流程。
    """
    if not settings.user_memory_enabled or not end_user_id or not messages:
        return 0
    try:
        facts = _extract_facts_from_conversation(_conversation_text(messages))
        if not facts:
            return 0
        _ensure_collection()
        stored = 0
        for fact in facts:
            try:
                _store_fact(end_user_id, fact, session_id=session_id, kind="session_summary")
                stored += 1
            except Exception as e:  # noqa: BLE001 — 單條失敗不影響其他
                logger.warning("store summary fact failed (ignored): %s", e)
        return stored
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize_and_remember failed (ignored): %s", e)
        return 0


def recall(end_user_id: int | None, query: str) -> list[str]:
    """語意撈回這位使用者最相關的記憶（過門檻、依分數高→低）。匿名 / 失敗 → []。"""
    if not settings.user_memory_enabled or not end_user_id or not (query or "").strip():
        return []
    try:
        _ensure_collection()
        body = {
            "vector": embed(query),
            "limit": settings.user_memory_top_k,
            "with_payload": True,
            "filter": {"must": [{"key": "end_user_id", "match": {"value": int(end_user_id)}}]},
        }
        r = requests.post(
            f"{_base()}/collections/{settings.user_memory_collection}/points/search",
            json=body, timeout=15,
        )
        r.raise_for_status()
        hits = r.json().get("result", []) or []
        thr = settings.user_memory_min_score
        out = []
        for h in hits:
            payload = h.get("payload") or {}
            if float(h.get("score", 0.0)) >= thr and payload.get("text"):
                out.append(payload["text"])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("recall failed (ignored): %s", e)
        return []


def list_memories(end_user_id: int | None, limit: int = 50) -> list[str]:
    """撈出這位使用者的全部記憶（依時間新→舊），供『meta 問題』列出用；非語意搜。"""
    if not settings.user_memory_enabled or not end_user_id:
        return []
    try:
        _ensure_collection()
        body = {
            "filter": {"must": [{"key": "end_user_id", "match": {"value": int(end_user_id)}}]},
            "limit": limit,
            "with_payload": True,
        }
        r = requests.post(
            f"{_base()}/collections/{settings.user_memory_collection}/points/scroll",
            json=body, timeout=15,
        )
        r.raise_for_status()
        points = r.json().get("result", {}).get("points", []) or []
        points.sort(key=lambda p: (p.get("payload") or {}).get("created_at", ""), reverse=True)
        out = []
        for p in points:
            t = (p.get("payload") or {}).get("text")
            if t:
                out.append(t)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("list_memories failed (ignored): %s", e)
        return []
