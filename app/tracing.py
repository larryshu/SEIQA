"""Langfuse 埋點的薄封裝：`@observe` 裝飾器 + 幾個 fail-safe 的補值函式。

為什麼不直接 `from langfuse import observe`：

1. **fail-safe 收在一個地方。** 本專案處處都是「觀測/加分功能壞掉不可以弄死問答」，
   埋點更該如此。SDK 在沒設金鑰時本來就會自己停用，但 `update_current_span` 這類
   補值呼叫仍可能拋（例如當下沒有作用中的 span），所以統一在這裡吞掉並只記 log。
2. **語意類型集中管理。** v4 的 `as_type` 支援 agent / tool / retriever / embedding 等，
   UI 會依類型給不同圖示與分類；散落各檔容易寫得不一致。

注意 v4 與 v3 的 API 差異（升級時容易踩）：
  - 沒有 `update_current_trace()`，trace 層級的 user_id / session_id 改用
    `propagate_attributes()` context manager，且**必須在 root span 建立時就進入**，
    否則先前建立的 span 不會被歸戶，成本/用量的分組統計就漏掉那一段。
  - trace 的 input/output 用 `set_current_trace_io()`。

**下面那行 `from .config import settings` 不能刪，即使看起來沒用到。**
langfuse 的 client 是單例：第一次呼叫 `get_client()` 時讀環境變數建好就快取起來，若那時
`.env` 還沒載入，會建出一個「沒有金鑰、永久停用」的 client，之後補載入也救不回來——
症狀是全部埋點靜默失效，只在 stderr 留一行 Authentication error。
匯入 config 會觸發它的 `load_dotenv()`，保證任何進入點（api、scripts、測試）都安全。
（單純在頂層 import langfuse 本身沒問題，有問題的是 get_client() 的呼叫時機。）
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from langfuse import get_client, observe, propagate_attributes

from .config import settings  # noqa: F401 — 只為觸發 load_dotenv，見上方說明

logger = logging.getLogger(__name__)

__all__ = ["observe", "trace_context", "set_span", "set_trace_io", "flush"]


@contextmanager
def trace_context(session_id: str = "", end_user_id: int | None = None,
                  trace_name: str = ""):
    """把 session / 使用者掛到這個 context 內建立的所有 span（trace 層級歸戶）。

    要在 root span 一建立就進入——見模組 docstring。end_user_id 是 int，這裡轉成字串；
    匿名（None）就不帶 user_id，Langfuse 上會顯示為未歸戶的 trace。
    """
    try:
        with propagate_attributes(
            session_id=session_id or None,
            user_id=str(end_user_id) if end_user_id else None,
            trace_name=trace_name or None,
        ):
            yield
    except Exception as e:  # noqa: BLE001 — 埋點壞掉不可以影響問答
        logger.warning("trace_context 失效（略過歸戶）：%s", e)
        yield


def set_span(**kwargs: Any) -> None:
    """補值到目前的 span（name / input / output / metadata / level…）。失敗只記 log。

    用在「輸入參數太大或不可序列化，改用 capture_input=False 再手動挑重點記」的場合，
    例如 tools.dispatch 的 sources 是整份貼文清單、relevance.rerank 的 text_of 是 lambda。
    """
    try:
        get_client().update_current_span(**kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("set_span 失敗（略過）：%s", e)


def set_trace_io(input: Any = None, output: Any = None) -> None:  # noqa: A002
    """設定整個 trace 的輸入／輸出（Tracing 列表的 Input/Output 欄位直接看得到問題與答案）。

    **不要改用 `set_current_trace_io()`**：那支在 v4 已廢棄，呼叫時只印一行
    DeprecationWarning、不拋例外，但也不寫入任何東西——第一版就是這樣寫的，結果列表頁的
    Input/Output 整欄空白，而錯誤被吞在 debug 等級所以完全沒察覺。

    v4 的模型裡「trace 的 input/output」就是**根 observation 自己的 input/output**，
    所以正確作法是對當前 span 補值。本函式只該從 trace 根呼叫（ask / ws_ask）。
    """
    try:
        get_client().update_current_span(input=input, output=output)
    except Exception as e:  # noqa: BLE001
        logger.debug("set_trace_io 失敗（略過）：%s", e)


def score(name: str, value: float, comment: str | None = None) -> None:
    """對目前的 trace 打一個分數（Langfuse 的 Scores 頁與 Dashboard 都吃這個）。

    給 audit.py 用：把「答案有沒有違反反幻覺規定」變成可以畫成趨勢線的數字。
    """
    try:
        get_client().score_current_trace(name=name, value=value, comment=comment,
                                         data_type="NUMERIC")
    except Exception as e:  # noqa: BLE001
        logger.debug("score 失敗（略過）：%s", e)


def get_prompt(name: str, fallback: str, label: str = "production",
               ttl_seconds: int = 60) -> tuple[str, str]:
    """從 Langfuse 拉 prompt，回 (內容, 版本標記)。連不上就回 (fallback, "fallback")。

    版本標記會被寫進 span metadata——「這個答案是哪一版 prompt 生出來的」必須看得到，
    否則改完 prompt 之後回頭看舊 trace 會對不起來，評測分數也失去意義。

    SDK 的 get_prompt 自帶 fallback 參數（拉不到就用它），所以 Langfuse 掛掉時
    這裡仍回得了話——與本專案其他外部相依一樣，降級但不中斷。
    """
    try:
        p = get_client().get_prompt(name, label=label, fallback=fallback,
                                    cache_ttl_seconds=ttl_seconds)
        # 走 fallback 時 SDK 給的 version 是 0，用它區分「真的拉到」與「降級」
        version = getattr(p, "version", 0)
        if not version:
            return fallback, "fallback"
        return p.prompt, f"{name}:v{version}"   # 沒有變數要代入，取原文即可
    except Exception as e:  # noqa: BLE001 — 拉不到 prompt 不可以讓問答掛掉
        logger.warning("Langfuse prompt 取用失敗（改用內建值）：%s", e)
        return fallback, "fallback"


def flush() -> None:
    """把佇列中的事件送出。長駐服務不需要（背景會自己送），寫測試/腳本時才用得到。"""
    try:
        get_client().flush()
    except Exception as e:  # noqa: BLE001
        logger.debug("flush 失敗（略過）：%s", e)
