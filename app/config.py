"""集中設定：全部從 .env / 環境變數讀，沿用 dcard_insight（諸葛記憶）的 PROVIDER 風格。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# override=True：.env 內容覆蓋既有環境變數，確保改 .env 重啟後一定吃到新值
# （否則舊的環境變數會卡住，例如 uvicorn reload 後仍沿用舊 CRAWL_TIMEOUT）
load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # ---- LLM ----
    api_key: str = os.environ.get("LLM_API_KEY", "").strip()
    chat_model: str = os.environ.get("CHAT_MODEL", "gpt-4.1").strip()
    embed_model: str = os.environ.get("EMBED_MODEL", "text-embedding-3-small").strip()
    base_url: str = os.environ.get("LLM_BASE_URL", "").strip()
    azure_endpoint: str = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    azure_api_version: str = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview").strip()

    # ---- 即時爬蟲 ----
    crawler_path: str = os.environ.get("CRAWLER_PATH", "").strip()
    crawl_max_posts: int = _int("CRAWL_MAX_POSTS", 5)  # live 逐篇進頁慢＋多開頁易觸發 CF 盾，抓少而精
    crawl_timeout: int = _int("CRAWL_TIMEOUT", 30)  # 秒；live 抓取硬上限，避免拖死對話

    # ---- FreshStore：session（方案 A）｜qdrant（方案 B）----
    fresh_store: str = os.environ.get("FRESH_STORE", "session").strip().lower()
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://localhost:7333").strip()
    hot_collection: str = os.environ.get("HOT_COLLECTION", "crawl_agent_hot").strip()

    # ---- 使用者層語意記憶（個人化；僅登入使用者生效）----
    user_memory_enabled: bool = os.environ.get(
        "USER_MEMORY_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    user_memory_collection: str = os.environ.get("USER_MEMORY_COLLECTION", "user_memory").strip()
    user_memory_top_k: int = _int("USER_MEMORY_TOP_K", 3)
    user_memory_min_score: float = _float("USER_MEMORY_MIN_SCORE", 0.35)

    # ---- Dcard 口碑庫（唯讀查詢；資料由 dcard_insight 專案批次建好，這裡只查不寫）----
    insight_collection: str = os.environ.get("INSIGHT_COLLECTION", "dcard_insight").strip()
    search_top_k: int = _int("SEARCH_TOP_K", 5)  # 向量檢索回傳幾則（去重後的貼文數）
    # 多面向查詢改寫的查詢條數（除原問句外，請 LLM 另外改寫幾條鄉民用詞）
    search_expand_n: int = _int("SEARCH_EXPAND_N", 3)
    # 相似度門檻（Cosine）：合併後低於此分視為不夠對題 → 當沒命中、走黃燈用常識答
    search_min_score: float = _float("SEARCH_MIN_SCORE", 0.5)

    # ---- PTT 即時爬蟲（crawl_ptt / PttSource）----
    ptt_time_budget: int = _int("PTT_TIME_BUDGET", 60)   # 秒；翻搜尋頁逐篇抓的時間預算，到時就停回已抓到的
    ptt_min_delay: float = _float("PTT_MIN_DELAY", 0.5)  # 秒；禮貌限速下限（避免被 ban）
    ptt_max_delay: float = _float("PTT_MAX_DELAY", 1.0)  # 秒；禮貌限速上限

    # ---- 後台共用 MySQL（M3：唯讀讀設定；db_host 留空＝停用，全走上面的 .env/寫死值）----
    db_host: str = os.environ.get("DB_HOST", "").strip()
    db_port: int = _int("DB_PORT", 3306)
    db_name: str = os.environ.get("DB_NAME", "crawl_agent").strip()
    db_user: str = os.environ.get("DB_USER", "").strip()
    db_password: str = os.environ.get("DB_PASSWORD", "")
    config_cache_ttl: int = _int("CONFIG_CACHE_TTL", 30)  # 設定快取 TTL（秒）
    # 對話落地用的讀寫帳號（M4）
    db_rw_user: str = os.environ.get("DB_RW_USER", "").strip()
    db_rw_password: str = os.environ.get("DB_RW_PASSWORD", "")
    # 終端使用者登入 token 簽章密鑰（與 Django 共用，驗證 /ask 帶的 token）
    token_secret: str = os.environ.get("TOKEN_SECRET", "").strip()


settings = Settings()
