"""ConfigRepository：runtime 從後台 MySQL 唯讀讀取設定（M3）。

設計重點：
- 短 TTL 程序內快取（settings.config_cache_ttl，預設 30 秒）；reload() 可即時清快取。
- fail-safe：任何 DB 失敗（沒設 DB、連不上、表不存在）一律回 None／預設，呼叫端 fall back 到
  .env/寫死值——後台掛了 runtime 照樣能跑。失敗結果也照樣快取，避免每個 request 重連壞掉的 DB。
- 唯讀：用 crawl_ro 帳號，只 SELECT。

讀的內容（對應 docs/admin_backend_spec.md §8）：
  agent（is_active=1） / 該 agent 的 skills → tools / 啟用的 source_platform + source_config / system_setting
"""
from __future__ import annotations

import json
import logging
import threading
import time

from .config import settings

logger = logging.getLogger(__name__)

try:
    import pymysql
    import pymysql.cursors
except ImportError:  # 沒裝 driver 就等同停用 → 全走 fallback
    pymysql = None

_CASTERS = {
    "int": int,
    "float": float,
    "bool": lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"),
    "json": json.loads,
    "str": str,
}


def _cast(value, value_type: str):
    try:
        return _CASTERS.get(value_type, str)(value)
    except (ValueError, TypeError):
        return value


class ConfigRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._loaded = False           # 是否已嘗試載入（區分「沒載過」與「載到 None」）
        self._cache_at = 0.0
        self._user_cache: dict[int, tuple[dict, float]] = {}  # end_user_id -> (prefs, ts)

    def _enabled(self) -> bool:
        return bool(pymysql and settings.db_host and settings.db_user)

    def _connect(self):
        return pymysql.connect(
            host=settings.db_host, port=settings.db_port,
            user=settings.db_user, password=settings.db_password,
            database=settings.db_name, charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3, read_timeout=5,
        )

    def _load(self) -> dict | None:
        """一次把需要的設定全讀進來組成 snapshot。任何問題回 None。"""
        if not self._enabled():
            return None
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            logger.warning("DB connect failed, using fallback: %s", e)
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, system_prompt, model, temperature, max_tool_rounds "
                    "FROM agent WHERE is_active=1 LIMIT 1"
                )
                agent = cur.fetchone()

                tools: list[dict] = []
                if agent:
                    cur.execute(
                        "SELECT s.name, s.description, s.json_schema "
                        "FROM skill s JOIN agent_skill a ON a.skill_id = s.id "
                        "WHERE a.agent_id = %s AND s.is_active = 1 ORDER BY a.sort_order",
                        (agent["id"],),
                    )
                    for row in cur.fetchall():
                        schema = row["json_schema"]
                        if isinstance(schema, str):
                            schema = json.loads(schema or "{}")
                        tools.append({
                            "type": "function",
                            "function": {
                                "name": row["name"],
                                "description": row["description"],
                                "parameters": schema or {},
                            },
                        })

                cur.execute(
                    "SELECT id, name, adapter_key, kind FROM source_platform "
                    "WHERE is_active = 1 ORDER BY sort_order"
                )
                sources = cur.fetchall()
                for s in sources:
                    cur.execute(
                        "SELECT `key`, value, value_type FROM source_config WHERE platform_id = %s",
                        (s["id"],),
                    )
                    s["configs"] = {r["key"]: _cast(r["value"], r["value_type"]) for r in cur.fetchall()}

                cur.execute("SELECT `key`, value, value_type FROM system_setting")
                sys_settings = {r["key"]: _cast(r["value"], r["value_type"]) for r in cur.fetchall()}

            if agent and agent.get("temperature") is not None:
                agent["temperature"] = float(agent["temperature"])
            return {"agent": agent, "tools": tools, "sources": sources, "settings": sys_settings}
        except Exception as e:  # noqa: BLE001
            logger.warning("DB read failed, using fallback: %s", e)
            return None
        finally:
            conn.close()

    def _snapshot(self) -> dict | None:
        now = time.monotonic()
        with self._lock:
            if self._loaded and (now - self._cache_at) < settings.config_cache_ttl:
                return self._cache
            self._cache = self._load()
            self._loaded = True
            self._cache_at = now
            return self._cache

    def reload(self) -> None:
        """清快取（含 per-user 偏好），下次取值重讀 DB（後台改設定後即時生效用）。"""
        with self._lock:
            self._cache = None
            self._loaded = False
            self._cache_at = 0.0
            self._user_cache.clear()

    # ---- public：取不到一律回 None/default，讓呼叫端 fallback ----
    def get_active_agent(self) -> dict | None:
        snap = self._snapshot()
        return snap["agent"] if snap else None

    def get_tools(self) -> list[dict] | None:
        snap = self._snapshot()
        return (snap["tools"] or None) if snap else None

    def get_enabled_sources(self) -> list[dict] | None:
        snap = self._snapshot()
        return (snap["sources"] or None) if snap else None

    def get_setting(self, key: str, default=None):
        snap = self._snapshot()
        return snap["settings"].get(key, default) if snap else default

    def get_user_preferences(self, end_user_id: int | None) -> dict:
        """取某終端使用者的偏好（已 typed）。沒 id / DB 失敗 → 回 {}（呼叫端 fallback）。

        per-user 小快取（同 TTL）；avoid 每個 request 都查 user_preference。
        """
        if not end_user_id or not self._enabled():
            return {}
        now = time.monotonic()
        with self._lock:
            cached = self._user_cache.get(end_user_id)
            if cached and (now - cached[1]) < settings.config_cache_ttl:
                return cached[0]
        prefs = self._load_user_prefs(end_user_id)
        with self._lock:
            self._user_cache[end_user_id] = (prefs, now)
        return prefs

    def _load_user_prefs(self, end_user_id: int) -> dict:
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            logger.warning("user prefs connect failed: %s", e)
            return {}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT `key`, value, value_type FROM user_preference WHERE end_user_id = %s",
                    (end_user_id,),
                )
                return {r["key"]: _cast(r["value"], r["value_type"]) for r in cur.fetchall()}
        except Exception as e:  # noqa: BLE001
            logger.warning("user prefs read failed: %s", e)
            return {}
        finally:
            conn.close()


# 單例：整個 app 共用一份快取
repo = ConfigRepository()
