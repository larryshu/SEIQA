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

import json
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
        # end_user_id / kind 建 payload index（過濾用；失敗不致命，filter 仍可運作只是較慢）
        try:
            requests.put(f"{url}/index",
                         json={"field_name": "end_user_id", "field_schema": "integer"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            requests.put(f"{url}/index",
                         json={"field_name": "kind", "field_schema": "keyword"}, timeout=10)
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
                session_id: str = "", kind: str = "turn",
                embed_text: str = "", headline: str = "") -> None:
    """把一條記憶 embed 後 upsert 進 user_memory。呼叫端負責 fail-safe 外層 try。

    kind：'turn'＝每輪即時萃取；'session_summary'＝登出時整段對話的原子事實；
          'thread'＝登出時整場對話的『有脈絡敘事』（headline 檢索、narrative 重載）。
    embed_text：拿去 embed 的字串；空＝用 fact 本身。thread 傳 headline——用主題句當檢索鍵，
                避免整段敘事多主題把向量稀釋、召回變糊。
    headline：thread 的主題句（也存進 payload，供 meta 列表/備查）。
    """
    point = {
        "id": uuid.uuid4().hex,
        "vector": embed(embed_text or fact),
        "payload": {
            "end_user_id": int(end_user_id),
            "text": fact,                        # recall 注入的就是這段（事實或敘事）
            "question": (question or "").strip(),  # 原始問句（備查）
            "answer": (answer or "")[:1000],     # 原始答案（備查，截斷）
            "session_id": session_id,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    if headline:
        point["payload"]["headline"] = headline
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


def _parse_summary(raw: str) -> dict:
    """解析 LLM 的 {facts, thread}；容錯去 markdown 圍籬、去 NONE；缺欄位回空結構。"""
    empty = {"facts": [], "thread": {"headline": "", "narrative": ""}}
    s = (raw or "").strip()
    if s.startswith("```"):  # 去掉 ```json ... ``` 圍籬
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty
    raw_facts = data.get("facts")
    facts = ([str(f).strip() for f in raw_facts if str(f).strip()][:3]
             if isinstance(raw_facts, list) else [])
    thread = data.get("thread") if isinstance(data.get("thread"), dict) else {}
    headline = str(thread.get("headline", "") or "").strip()
    narrative = str(thread.get("narrative", "") or "").strip()
    if headline.upper() == "NONE":
        headline = ""
    if narrative.upper() == "NONE":
        narrative = ""
    return {"facts": facts, "thread": {"headline": headline, "narrative": narrative}}


def _summarize_conversation(transcript: str) -> dict:
    """從整段對話一次產出 {facts:[...], thread:{headline, narrative}}。fail-safe，失敗回空結構。

    facts：0–3 條『關於使用者本人』的穩定原子事實（點狀召回用，同舊行為）。
    thread：一筆有脈絡的敘事——headline 供檢索、narrative 供重載。只記使用者的處境/目標/
            提問走向與穩定取捨傾向，不記會過時的世界結論（紅線）。
    """
    empty = {"facts": [], "thread": {"headline": "", "narrative": ""}}
    if not transcript.strip():
        return empty
    msgs = [
        {"role": "system", "content": (
            "以下是一段使用者與助理的完整對話。請一次產出兩種長期記憶，嚴格輸出 JSON。\n"
            "1) facts（維持嚴格）：關於『使用者本人』、可長期記住的穩定事實（身分／職業／處境／"
            "長期偏好／持續關心的主題），第三人稱、開頭『使用者』，最多 3 條；沒有就給 []。"
            "facts 絕不要寫世界結論、社群給的答案、會過時的資訊。\n"
            "2) thread：把這場對話（不論單輪或多輪）濃縮成一段『有脈絡的敘事』，供日後相關問題"
            "重新載入背景。\n"
            "   - headline：一句主題化的名詞短語（利於未來相關問題語意命中）。\n"
            "   - narrative：第三人稱、繁體中文，寫成一段較完整的敘事（約 5–8 句），含兩部分——\n"
            "     (a) 使用者的處境、目標、關注點、提問走向與顯露的取捨傾向；\n"
            "     (b) 這場對話『討論到的重點與結論梗概』：給了哪些建議、社群的共識或主要說法是"
            "什麼，讓日後能喚回討論過的內容。\n"
            "   但 narrative 要把『會快速過時的具體值』抽象成主題層級：寫『討論了薪資行情與談薪"
            "策略』而非『年薪 200 萬』；寫『比較了幾款保濕品的口碑』而非『X 牌目前排第一』。"
            "較常青的通則建議可保留（例：減脂靠熱量赤字＋規律運動、重訓保肌、睡眠充足）。\n"
            "若整段對話沒有值得長期記住的個人脈絡（純查資料／純常識／純計算），"
            "facts 給 []、thread 兩欄給空字串。\n"
            "嚴格輸出 JSON：{\"facts\":[..],\"thread\":{\"headline\":..,\"narrative\":..}}；"
            "只輸出 JSON，不要 markdown、不要解釋。"
        )},
        {"role": "user", "content": transcript},
    ]
    try:
        out = chat(msgs, temperature=0.1)
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize conversation failed (ignored): %s", e)
        return empty
    return _parse_summary(out)


def summarize_and_remember(end_user_id: int | None, messages: list[dict],
                           session_id: str = "") -> int:
    """登出時：對整段對話一次產出並存兩種記憶，回傳寫入總條數（facts + thread）。

    - facts（kind='session_summary'）：0–3 條原子事實，補一次整段脈絡的點狀事實。
    - thread（kind='thread'）：一筆有脈絡敘事，headline 檢索 / narrative 重載（相關問題時載回）。
    每輪 remember() 已寫過即時事實；此處補整段。匿名 / 關閉 / 無內容 → 0；全程 fail-safe。
    """
    if not settings.user_memory_enabled or not end_user_id or not messages:
        return 0
    try:
        summary = _summarize_conversation(_conversation_text(messages))
        facts = summary.get("facts") or []
        thread = summary.get("thread") or {}
        headline = (thread.get("headline") or "").strip()
        narrative = (thread.get("narrative") or "").strip()
        if not facts and not (headline and narrative):
            return 0  # 這段對話沒有值得長期記住的個人脈絡 → 不存，避免雜訊
        _ensure_collection()
        stored = 0
        for fact in facts:
            try:
                _store_fact(end_user_id, fact, session_id=session_id, kind="session_summary")
                stored += 1
            except Exception as e:  # noqa: BLE001 — 單條失敗不影響其他
                logger.warning("store summary fact failed (ignored): %s", e)
        if settings.user_thread_enabled and headline and narrative:
            try:  # thread：embed 主題句、text 存敘事（recall_threads 注入的就是敘事）
                _store_fact(end_user_id, narrative, session_id=session_id, kind="thread",
                            embed_text=headline, headline=headline)
                stored += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("store thread failed (ignored): %s", e)
        return stored
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize_and_remember failed (ignored): %s", e)
        return 0


def recall(end_user_id: int | None, query: str) -> list[str]:
    """語意撈回這位使用者最相關的『原子事實』（kind turn/session_summary，過門檻）。匿名 / 失敗 → []。

    刻意排除 kind='thread'：脈絡敘事走 recall_threads() 另一條（不同門檻、不同注入區塊）。
    """
    if not settings.user_memory_enabled or not end_user_id or not (query or "").strip():
        return []
    try:
        _ensure_collection()
        body = {
            "vector": embed(query),
            "limit": settings.user_memory_top_k,
            "with_payload": True,
            "filter": {"must": [
                {"key": "end_user_id", "match": {"value": int(end_user_id)}},
                {"key": "kind", "match": {"any": ["turn", "session_summary"]}},
            ]},
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


def recall_threads(end_user_id: int | None, query: str) -> list[str]:
    """語意撈回這位使用者最相關的『對話脈絡』(kind='thread')。回 narrative 清單（截長度）。

    用主題句 embed 過的 thread 向量比對；門檻比事實高（敘事要夠對題才值得整段注入 context）。
    匿名 / 關閉 / 失敗 → []。
    """
    if (not settings.user_memory_enabled or not settings.user_thread_enabled
            or not end_user_id or not (query or "").strip()):
        return []
    try:
        _ensure_collection()
        body = {
            "vector": embed(query),
            "limit": settings.user_thread_top_k,
            "with_payload": True,
            "filter": {"must": [
                {"key": "end_user_id", "match": {"value": int(end_user_id)}},
                {"key": "kind", "match": {"value": "thread"}},
            ]},
        }
        r = requests.post(
            f"{_base()}/collections/{settings.user_memory_collection}/points/search",
            json=body, timeout=15,
        )
        r.raise_for_status()
        hits = r.json().get("result", []) or []
        thr = settings.user_thread_min_score
        cap = settings.user_thread_max_chars
        out = []
        for h in hits:
            payload = h.get("payload") or {}
            if float(h.get("score", 0.0)) >= thr and payload.get("text"):
                out.append(payload["text"][:cap])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("recall_threads failed (ignored): %s", e)
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
            payload = p.get("payload") or {}
            # thread 列主題句（headline）較精簡好讀；其餘 kind 列事實本文（text）
            t = payload.get("headline") if payload.get("kind") == "thread" else payload.get("text")
            if t:
                out.append(t)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("list_memories failed (ignored): %s", e)
        return []
