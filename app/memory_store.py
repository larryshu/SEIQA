"""把每輪對話寫進後台 MySQL（M4 步驟一：runtime 只寫、前端不動）。

用獨立的 read-write 帳號 crawl_rw（只在 conversation/message 有 INSERT/UPDATE 權限）。
全程 fail-safe：任何寫入失敗都只 log，不影響聊天回應。db_host / db_rw_user 留空＝停用。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import settings

logger = logging.getLogger(__name__)

try:
    import pymysql
except ImportError:
    pymysql = None


def _enabled() -> bool:
    return bool(pymysql and settings.db_host and settings.db_rw_user)


def _connect():
    return pymysql.connect(
        host=settings.db_host, port=settings.db_port,
        user=settings.db_rw_user, password=settings.db_rw_password,
        database=settings.db_name, charset="utf8mb4",
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


def persist_turn(session_id: str, user_message: str, answer: str,
                 used_tools: list | None = None, sources: list | None = None,
                 agent_id: int | None = None, end_user_id: int | None = None) -> None:
    """寫入一輪對話：依 sid 找/建 conversation → 插入 user + assistant 兩則訊息。"""
    if not _enabled():
        return
    now = datetime.utcnow()  # 與 Django USE_TZ 的 UTC 儲存一致
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM conversation WHERE sid=%s", (session_id,))
            row = cur.fetchone()
            if row:
                conv_id = row[0]
                cur.execute(
                    "UPDATE conversation SET message_count=message_count+2, "
                    "last_active_at=%s, updated_at=%s WHERE id=%s",
                    (now, now, conv_id),
                )
            else:
                cur.execute(
                    "INSERT INTO conversation "
                    "(sid, end_user_id, agent_id, title, message_count, created_at, updated_at, "
                    "last_active_at, is_deleted) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)",
                    (session_id, end_user_id, agent_id, (user_message or "")[:255], 2, now, now, now),
                )
                conv_id = cur.lastrowid
            cur.execute(
                "INSERT INTO message (conversation_id, role, content, used_tools, sources, created_at) "
                "VALUES (%s,'user',%s,NULL,NULL,%s)",
                (conv_id, user_message or "", now),
            )
            cur.execute(
                "INSERT INTO message (conversation_id, role, content, used_tools, sources, created_at) "
                "VALUES (%s,'assistant',%s,%s,%s,%s)",
                (conv_id, answer or "",
                 json.dumps(used_tools or [], ensure_ascii=False),
                 json.dumps(sources or [], ensure_ascii=False), now),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001 — 落地失敗絕不可中斷聊天
        logger.warning("persist failed (ignored): %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def load_history(session_id: str, end_user_id: int | None, limit: int = 200) -> list[dict]:
    """讀回這位使用者這段對話的訊息（供前端重整後還原上下文）。

    只鎖 sid + end_user_id + is_deleted=0：知道別人的 sid 也讀不到別人的對話，
    登出時軟刪掉的對話也不會被還原。用 crawl_rw 現有的 SELECT 權限即可。
    回 [{role, content, sources}]（依時間排序），任何失敗都回 [] —— 還原是加值功能，不該擋住聊天。
    """
    if not _enabled() or not session_id or not end_user_id:
        return []
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.role, m.content, m.sources FROM message m "
                "JOIN conversation c ON m.conversation_id = c.id "
                "WHERE c.sid=%s AND c.end_user_id=%s AND c.is_deleted=0 "
                "ORDER BY m.created_at, m.id LIMIT %s",
                (session_id, int(end_user_id), int(limit)),
            )
            rows = cur.fetchall()
        return [
            {"role": role, "content": content or "", "sources": _loads(sources)}
            for role, content, sources in rows
            if role in ("user", "assistant")
        ]
    except Exception as e:  # noqa: BLE001 — 讀不回來就當沒有歷史，前端照樣能開新對話
        logger.warning("load history failed (ignored): %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def list_conversations(end_user_id: int | None, limit: int = 30) -> list[dict]:
    """列出這位使用者未刪除的對話（新→舊），供前端做「我的對話」清單、跨裝置接續。"""
    if not _enabled() or not end_user_id:
        return []
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sid, title, message_count, last_active_at FROM conversation "
                "WHERE end_user_id=%s AND is_deleted=0 "
                "ORDER BY last_active_at DESC, id DESC LIMIT %s",
                (int(end_user_id), int(limit)),
            )
            rows = cur.fetchall()
        return [
            {"sid": sid, "title": title or "", "message_count": count or 0,
             "last_active_at": last_active.isoformat() if last_active else ""}
            for sid, title, count, last_active in rows
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("list conversations failed (ignored): %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _loads(raw) -> list:
    """message.sources 是 JSON 欄；driver 可能回字串或已解析好的 list。壞資料一律當空。"""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def soft_delete_conversation(session_id: str, end_user_id: int | None) -> int:
    """登出時軟刪『這位使用者這段對話』（UPDATE is_deleted=1）。回傳受影響列數。

    只鎖 sid + end_user_id，避免誤刪他人；用 crawl_rw 現有 UPDATE 權限即可（免加 DELETE 授權）。
    真正抹除交給後台 admin purge。全程 fail-safe。
    """
    if not _enabled() or not session_id or not end_user_id:
        return 0
    now = datetime.utcnow()
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            affected = cur.execute(
                "UPDATE conversation SET is_deleted=1, updated_at=%s "
                "WHERE sid=%s AND end_user_id=%s AND is_deleted=0",
                (now, session_id, int(end_user_id)),
            )
        conn.commit()
        return affected or 0
    except Exception as e:  # noqa: BLE001 — 刪除失敗只 log，不中斷登出
        logger.warning("soft delete failed (ignored): %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
