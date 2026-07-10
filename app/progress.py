"""即時進度事件匯流排 + 取消號誌（給 /ws/ask 用；/ask 那條路徑完全不受影響）。

**為什麼用 contextvars 而不是把 emit 當參數一路傳下去**
事件的源頭在最底層（爬蟲逐篇迴圈、fan-out 的 adapter），要傳到那裡得改
`Source.fetch()` 的簽章——那正是 README 承諾「加平台＝只寫一個 adapter、agent/loop/prompt
全不動」的介面。用 contextvars 就能讓任何一層想發事件時直接 `progress.emit(...)`，
沒有訂閱者時它是 no-op，既有呼叫路徑（/ask）行為一個位元都不變。

**為什麼 Cancelled 繼承 BaseException 而不是 Exception**
底下每一層都有 fail-safe 的 `except Exception`，其中 `dcard_live.crawl()` 會把攔到的例外
解讀成「即時爬失敗 → 退回向量庫」。若取消是普通 Exception，使用者按下停止會被誤判成爬蟲
掛掉、進而觸發一次沒必要的向量庫 fallback。繼承 BaseException 才穿得過那些網子——
標準庫的 asyncio.CancelledError 與 KeyboardInterrupt 正是基於同一個理由這樣設計。

**執行緒**
emit 會從 fan-out 的 worker thread 被呼叫，所以註冊進來的 callback 必須是 thread-safe 的
（實際上是 asyncio 事件迴圈的 call_soon_threadsafe）。取消號誌用 threading.Event。
跨執行緒的 contextvars 不會自動繼承，由 sources.py 在 submit 時各複製一份 Context。
"""
from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Cancelled(BaseException):
    """使用者按下「停止」。刻意繼承 BaseException，見模組 docstring。"""


# 沒有訂閱者（例如 /ask）時兩者都是 None → emit 是 no-op、永遠不會被取消
_emitter: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "seiqa_progress_emitter", default=None)
_cancel: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "seiqa_progress_cancel", default=None)


def emit(event: str, **data) -> None:
    """推一個進度事件給訂閱者。永不拋例外——推播壞掉不該影響問答本身。"""
    fn = _emitter.get()
    if fn is None:
        return
    try:
        fn({"type": event, **data})
    except Exception as e:  # noqa: BLE001 — 推播是附加功能，失敗只記 log
        logger.warning("progress.emit(%s) 失敗（忽略）：%s", event, e)


def is_cancelled() -> bool:
    ev = _cancel.get()
    return ev is not None and ev.is_set()


def raise_if_cancelled() -> None:
    """在長迴圈的檢查點呼叫。取消 → 拋 Cancelled，一路穿過 fail-safe 直到 /ws/ask 處理。"""
    if is_cancelled():
        raise Cancelled()


@contextmanager
def session(on_event: Callable[[dict], None],
            cancel_event: threading.Event) -> Iterator[None]:
    """在這個 with 區塊內，底下任何一層的 emit()/raise_if_cancelled() 都會生效。"""
    tok_emit = _emitter.set(on_event)
    tok_cancel = _cancel.set(cancel_event)
    try:
        yield
    finally:
        _emitter.reset(tok_emit)
        _cancel.reset(tok_cancel)
