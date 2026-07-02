"""使用者偏好『自動推論』：登出時從整段對話萃取可執行的設定旋鈕，寫入 MySQL user_preference。

定位（別跟 user_memory 混）：
- user_memory（Qdrant）：free-text『關於使用者本人』的長期事實，語意召回、個人化答案『內容』。
- user_preference（MySQL，本檔）：封閉 schema 的『設定旋鈕』（語氣/長度/語言/平台過濾），
  runtime 用『精確 key』讀出來，確定性地改變行為（agent._apply_pref_modifiers / sources._build_registry）。

為何要比 user_memory 保守很多：這裡寫錯會『靜默且確定性地』改變行為（例如把使用者隨口一句
「Dcard 有時候很亂」誤設成 excluded_platforms=[dcard]，等於默默關掉主要資料源）。所以四道護欄：
  1) 只收白名單 key、且值域受限（enum / 已知平台清單）；
  2) 過信心門檻（settings.pref_infer_min_confidence，預設 0.75）；
  3) 只在使用者『明確表達』偏好時才輸出（prompt 約束）；
  4) 人工設定（source='manual'）永不被推論覆寫（upsert 用 IF 守住）。

紀律：
- 只對登入使用者（end_user_id 不為 None）生效；匿名不推論。
- 全程 fail-safe：LLM / DB 任一步炸掉就當沒推論，不影響登出流程。
- 寫入走與 memory_store 同一個 crawl_rw 帳號（需授予 user_preference 的 INSERT/UPDATE）。
- model 這種高風險 key 刻意『不』自動推論（保持人工設定），這裡只推 UI/檢索層面的偏好。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import settings
from .llm import chat

logger = logging.getLogger(__name__)

try:
    import pymysql
except ImportError:  # 沒裝 driver 就等同停用
    pymysql = None


# ---- 封閉 schema：白名單 key + 值域驗證 --------------------------------------
# 每個 validator 接受 LLM 給的 value，回 (normalized_value, value_type) 或 None（不合法就丟）。
_TONES = {"friendly", "concise", "formal", "humorous", "professional", "neutral"}
_LENGTHS = {"short", "normal", "detailed"}
_LANGS = {"zh-TW", "zh-CN", "en", "ja"}


def _known_platforms() -> set[str]:
    """runtime 實際認得的平台 name（以 sources 的 adapter 為準）；取不到用保底集合。"""
    try:
        from .sources import _ADAPTERS
        return set(_ADAPTERS.keys())
    except Exception:  # noqa: BLE001
        return {"dcard", "ptt"}


def _v_enum(allowed: set[str]):
    def f(v):
        s = str(v).strip()
        return (s, "str") if s in allowed else None
    return f


def _v_platforms(v):
    """value 必須是字串陣列、元素都在已知平台內；去重保序後存成 json。空 → 視為不合法。"""
    if not isinstance(v, list):
        return None
    known = _known_platforms()
    seen: set[str] = set()
    names = []
    for x in v:
        n = str(x).strip()
        if n in known and n not in seen:
            seen.add(n)
            names.append(n)
    return (json.dumps(names, ensure_ascii=False), "json") if names else None


_ALLOWED = {
    "tone": _v_enum(_TONES),
    "answer_length": _v_enum(_LENGTHS),
    "language": _v_enum(_LANGS),
    "included_platforms": _v_platforms,
    "excluded_platforms": _v_platforms,
}


# ---- DB（crawl_rw，與 memory_store 同帳號）-----------------------------------
def _enabled() -> bool:
    return bool(pymysql and settings.db_host and settings.db_rw_user)


def _connect():
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port,
        user=settings.db_rw_user, password=settings.db_rw_password,
        database=settings.db_name, charset="utf8mb4",
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


# ---- LLM 推論 ---------------------------------------------------------------
def _transcript(messages: list[dict], max_chars: int = 4000) -> str:
    """把前端帶來的對話攤成純文字；取尾端保留最近脈絡。"""
    lines = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{'使用者' if role == 'user' else '助理'}：{content}")
    return "\n".join(lines)[-max_chars:]


def _parse_prefs(raw: str) -> list[dict]:
    """從 LLM 輸出解析 {"preferences":[...]}；容錯去掉 markdown 圍籬。失敗回 []。"""
    s = (raw or "").strip()
    if s.startswith("```"):  # 去掉 ```json ... ``` 圍籬
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    prefs = data.get("preferences") if isinstance(data, dict) else None
    return prefs if isinstance(prefs, list) else []


def _infer(transcript: str) -> list[dict]:
    """請 LLM 只從封閉清單輸出使用者『明確表達』的偏好。fail-safe，回候選 list。"""
    if not transcript.strip():
        return []
    msgs = [
        {"role": "system", "content": (
            "以下是一段使用者與助理的完整對話。請判斷使用者是否『明確表達』了對回答方式的偏好，"
            "只輸出下列封閉清單內的設定；沒有明確表達就不要猜。\n"
            "可用的 key 與值域（只能用這些，值必須完全吻合）：\n"
            "- tone：friendly | concise | formal | humorous | professional | neutral\n"
            "- answer_length：short | normal | detailed\n"
            "- language：zh-TW | zh-CN | en | ja\n"
            "- included_platforms：字串陣列，元素只能是 dcard / ptt（使用者要求只看這些平台）\n"
            "- excluded_platforms：字串陣列，元素只能是 dcard / ptt（使用者要求排除這些平台）\n"
            "規則：\n"
            "- 只在使用者『明確、直接』表達『長期』偏好時才輸出（例：『以後都請講重點就好』"
            "『以後都用英文回我』『我只想看 Dcard』）。語氣模糊、隨口抱怨都不算。\n"
            "- 一次性要求一律不要輸出：只要句子帶有『這題／這次／這一題／這一次／目前／暫時／"
            "先／只有這次』等指向『當下單次』的字眼，代表不是長期偏好，即使講了長度/語言也不要輸出。\n"
            "- confidence 為 0~1；只有 >= 0.75 才會被採用，不確定就給低分或不要輸出。\n"
            "- 嚴格輸出 JSON："
            "{\"preferences\":[{\"key\":..,\"value\":..,\"confidence\":..,\"evidence\":\"對話中的依據\"}]}\n"
            "- 沒有任何明確偏好時輸出 {\"preferences\":[]}。只輸出 JSON，不要解釋、不要 markdown。"
        )},
        {"role": "user", "content": transcript},
    ]
    try:
        out = chat(msgs, temperature=0.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("infer preferences failed (ignored): %s", e)
        return []
    return _parse_prefs(out)


def _validate(cands: list[dict], min_conf: float) -> list[dict]:
    """把候選過白名單 + 值域 + 信心門檻，回 [{key, value, value_type, confidence}]。"""
    out = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key", "")).strip()
        validator = _ALLOWED.get(key)
        if not validator:
            continue  # off-schema key 一律丟
        try:
            conf = float(c.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        norm = validator(c.get("value"))
        if not norm:
            continue  # 值域外 / 空 → 丟
        value, value_type = norm
        out.append({"key": key, "value": value, "value_type": value_type, "confidence": conf})
    return out


def _upsert(end_user_id: int, prefs: list[dict]) -> int:
    """upsert 進 user_preference；人工設定（source='manual'）永不被覆寫。回實際寫入條數。"""
    if not _enabled() or not prefs:
        return 0
    now = datetime.utcnow()  # 與 Django USE_TZ 的 UTC 儲存一致
    conn = None
    written = 0
    try:
        conn = _connect()
        with conn.cursor() as cur:
            for p in prefs:
                # ON DUPLICATE 時用 IF(source='manual', 舊值, 新值) 守住人工設定：
                # 既有列是 manual → 各欄維持原值（等於不動）；是 inferred → 用新值覆寫。
                cur.execute(
                    "INSERT INTO user_preference "
                    "(end_user_id, `key`, value, value_type, source, confidence, updated_at) "
                    "VALUES (%s,%s,%s,%s,'inferred',%s,%s) "
                    "ON DUPLICATE KEY UPDATE "
                    "value=IF(source='manual', value, VALUES(value)), "
                    "value_type=IF(source='manual', value_type, VALUES(value_type)), "
                    "confidence=IF(source='manual', confidence, VALUES(confidence)), "
                    "updated_at=IF(source='manual', updated_at, VALUES(updated_at))",
                    (int(end_user_id), p["key"], p["value"], p["value_type"],
                     p["confidence"], now),
                )
                written += 1
        conn.commit()
        return written
    except Exception as e:  # noqa: BLE001 — 寫入失敗只 log，不中斷登出
        logger.warning("upsert preferences failed (ignored): %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def infer_and_store(end_user_id: int | None, messages: list[dict], session_id: str = "") -> int:
    """登出時：從整段對話推論『可執行的設定旋鈕』寫入 user_preference。回寫入條數。

    匿名 / 關閉 / 無明確偏好 → 0；全程 fail-safe，不影響登出流程。
    """
    if not settings.pref_infer_enabled or not end_user_id or not messages:
        return 0
    try:
        cands = _infer(_transcript(messages))
        prefs = _validate(cands, settings.pref_infer_min_confidence)
        if not prefs:
            return 0
        return _upsert(int(end_user_id), prefs)
    except Exception as e:  # noqa: BLE001
        logger.warning("infer_and_store failed (ignored): %s", e)
        return 0
