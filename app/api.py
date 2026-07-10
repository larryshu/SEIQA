"""FastAPI 入口：POST /ask 跑一輪 agent。對話歷史由前端帶（與 dcard_insight 同思路）。

另有 WS /ws/ask：同一個 agent loop，但把中間進度與逐字答案即時推給前端，且可中途取消。
/ask 與 /ws/ask 各自獨立，前者行為與加串流前完全相同（Streamlit 前端不受影響）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import requests
from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import agent, memory_store, progress, user_memory, user_preference
from .agent import run
from .auth import end_user_id_from_token
from .config import settings
from .config_repo import repo

logger = logging.getLogger(__name__)

app = FastAPI(title="社群輿情智能問答")

_DEMO_PAGE = Path(__file__).parent / "static" / "ws_demo.html"
_TERMINAL_EVENTS = ("done", "cancelled", "error")


class AskReq(BaseModel):
    message: str
    session_id: str = "default"
    history: list[dict] = []  # [{"role":"user"/"assistant","content":...}, ...]
    # 終端使用者身分不從 body 帶（不可信），改由 Authorization: Bearer <token> 驗證後取得


class Source(BaseModel):
    title: str
    url: str
    created_at: str = ""
    source: str = ""  # 平台標籤：'dcard' | 'ptt'（前端分流用）


class AskResp(BaseModel):
    answer: str
    used_tools: list[str]
    sources: list[Source] = []  # 實際抓到的來源（依 [n] 順序），前端渲染用


class LogoutReq(BaseModel):
    session_id: str = "default"
    history: list[dict] = []  # 前端帶整段對話（[{role, content}]），供登出摘要用


class LogoutResp(BaseModel):
    ok: bool = True
    summarized: int = 0     # 寫進 user_memory 的整段摘要事實條數
    inferred: int = 0       # 推論寫進 user_preference 的偏好條數
    deleted_rows: int = 0   # 軟刪的 conversation 列數


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/internal/reload-config")
def reload_config() -> dict:
    """清掉設定快取，下次請求重讀後台 MySQL（後台改設定後即時生效，供 agent 試跑用）。"""
    repo.reload()
    return {"reloaded": True}


@app.post("/ask", response_model=AskResp)
def ask(req: AskReq, authorization: str | None = Header(default=None)) -> AskResp:
    end_user_id = end_user_id_from_token(authorization)  # 驗證 token；沒帶/無效 → 匿名
    result = run(req.message, history=req.history, session_id=req.session_id,
                 end_user_id=end_user_id)
    sources = [
        Source(title=s.get("title", ""), url=s.get("url", ""),
               created_at=s.get("created_at", ""), source=s.get("source", ""))
        for s in result.get("sources", [])
        if s.get("url")
    ]
    # M4/M5：把這一輪寫進後台 MySQL（fail-safe，寫不進去不影響回應）
    memory_store.persist_turn(
        req.session_id, req.message, result["answer"],
        used_tools=result.get("used_tools"), sources=result.get("sources"),
        agent_id=(repo.get_active_agent() or {}).get("id"), end_user_id=end_user_id,
    )
    # 個人化長期記憶：萃取『使用者事實』後存進向量記憶（fail-safe；匿名/無事實自動略過）
    user_memory.remember(end_user_id, req.message, result["answer"], session_id=req.session_id)
    return AskResp(answer=result["answer"], used_tools=result["used_tools"], sources=sources)


@app.post("/logout", response_model=LogoutResp)
def logout(req: LogoutReq, authorization: str | None = Header(default=None)) -> LogoutResp:
    """登出：把整段對話摘要進長期記憶(user_memory)，再軟刪這段對話的原始紀錄。

    身分只認 Authorization 內的 token（不信任 body）；匿名 → 無事可做。全程 fail-safe。
    """
    end_user_id = end_user_id_from_token(authorization)
    if not end_user_id:
        return LogoutResp(ok=True)  # 匿名：沒有可綁定的記憶/紀錄
    # 1) 整段對話 → 長期記憶（每輪已即時寫過，這裡補一次整體脈絡摘要）
    summarized = user_memory.summarize_and_remember(end_user_id, req.history, req.session_id)
    # 2) 整段對話 → 推論『可執行的設定旋鈕』寫進 user_preference（白名單+保守+不覆寫人工設定）
    inferred = user_preference.infer_and_store(end_user_id, req.history, req.session_id)
    # 3) 軟刪這位使用者這段對話的原始紀錄（只鎖 sid + end_user_id）
    deleted_rows = memory_store.soft_delete_conversation(req.session_id, end_user_id)
    return LogoutResp(ok=True, summarized=summarized, inferred=inferred, deleted_rows=deleted_rows)


# ---------------------------------------------------------------------------
# WebSocket：即時進度 + 逐字串流 + 中途取消
# ---------------------------------------------------------------------------
@app.get("/demo", response_class=HTMLResponse)
def demo_page() -> str:
    """極簡 WebSocket demo 頁（Streamlit 之外的第二個前端，用來示範雙向即時互動）。"""
    return _DEMO_PAGE.read_text(encoding="utf-8")


class EndAuthReq(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""  # 只有註冊會用到


@app.post("/demo/auth/{kind}")
def demo_auth(kind: str, req: EndAuthReq) -> JSONResponse:
    """把 /demo 頁的終端登入／註冊轉發給 Django 後台，原樣回傳它的結果。

    為什麼要代理：demo 頁由 runtime（:8001）提供，Django 在 :8000。瀏覽器直接打過去是跨來源
    請求，而後台沒裝 django-cors-headers，會被同源政策擋掉。改由 runtime 在伺服器端轉發，
    瀏覽器全程只跟 :8001 說話。Streamlit 不會遇到這問題，是因為它的 requests.post 本來就跑在
    伺服器端——這裡只是把同一件事搬到 runtime 做。

    定義成 def（非 async def）：requests 是阻塞的，FastAPI 會把它丟到執行緒池，不卡事件迴圈。
    """
    if kind not in ("login", "register"):
        return JSONResponse({"detail": "未知的操作"}, status_code=404)
    if not req.username or not req.password:
        return JSONResponse({"detail": "請輸入帳號與密碼"}, status_code=400)

    payload: dict = {"username": req.username, "password": req.password}
    if kind == "register" and req.display_name:
        payload["display_name"] = req.display_name
    try:
        r = requests.post(f"{settings.admin_api_url}/api/v1/end-auth/{kind}/",
                          json=payload, timeout=10)
    except requests.RequestException as e:
        logger.warning("轉發 end-auth/%s 失敗：%s", kind, e)
        return JSONResponse({"detail": f"連不到後台（{settings.admin_api_url}）"}, status_code=502)
    try:
        body = r.json()
    except ValueError:
        body = {"detail": f"後台回應異常（HTTP {r.status_code}）"}
    return JSONResponse(body, status_code=r.status_code)


def _run_blocking(question: str, history: list[dict], session_id: str,
                  end_user_id: int | None, cancel_event: threading.Event,
                  emit: Callable[[dict], None]) -> None:
    """在 worker thread 跑 agent（阻塞：LLM + DrissionPage + requests 都是同步的）。

    所有結果都以事件送出，包含終結事件（done / cancelled / error）——/ws/ask 的排空迴圈
    靠它收工，所以這裡任何一條路徑都必須恰好送出一個終結事件。
    """
    try:
        with progress.session(emit, cancel_event):
            result = agent.run_streaming(question, history=history,
                                         session_id=session_id, end_user_id=end_user_id)
    except progress.Cancelled:
        emit({"type": "cancelled"})
        return
    except Exception as e:  # noqa: BLE001 — 任何失敗都要讓前端收得到終結事件
        logger.exception("/ws/ask 執行失敗")
        emit({"type": "error", "message": str(e)})
        return

    # 落地與長期記憶：與 /ask 完全相同（皆 fail-safe）。取消的那一輪不寫，因為沒有答案。
    sources = result.get("sources", [])
    memory_store.persist_turn(
        session_id, question, result["answer"],
        used_tools=result.get("used_tools"), sources=sources,
        agent_id=(repo.get_active_agent() or {}).get("id"), end_user_id=end_user_id,
    )
    user_memory.remember(end_user_id, question, result["answer"], session_id=session_id)
    emit({
        "type": "done",
        "answer": result["answer"],
        "used_tools": result.get("used_tools", []),
        "sources": [s for s in sources if s.get("url")],
        "light": "green" if sources else "yellow",  # 🟢 有社群來源／🟡 LLM 既有常識
    })


async def _stream_one(ws: WebSocket, msg: dict, end_user_id: int | None,
                      cancel_event: threading.Event) -> None:
    """跑一題：worker thread 發事件 → 這裡排空並送上 WebSocket。"""
    question = (msg.get("message") or "").strip()
    if not question:
        await ws.send_json({"type": "error", "message": "message 不可為空"})
        return

    loop = asyncio.get_running_loop()
    outbound: asyncio.Queue = asyncio.Queue()

    def emit(event: dict) -> None:
        """從 worker thread（含 fan-out 的子執行緒）餵事件回事件迴圈——唯一安全的橋。"""
        loop.call_soon_threadsafe(outbound.put_nowait, event)

    worker = asyncio.create_task(asyncio.to_thread(
        _run_blocking, question, msg.get("history") or [],
        msg.get("session_id") or "default", end_user_id, cancel_event, emit))
    try:
        while True:
            event = await outbound.get()
            await ws.send_json(event)
            if event["type"] in _TERMINAL_EVENTS:
                break
    except WebSocketDisconnect:
        cancel_event.set()  # 人都走了，別讓爬蟲白跑
    finally:
        # worker 一定會送終結事件後返回；被取消時受檢查點約束（最多再等一篇貼文）
        await worker


@app.websocket("/ws/ask")
async def ws_ask(ws: WebSocket) -> None:
    """一條連線可連續問多輪；生成中可送 {"type":"cancel"} 中止。

    瀏覽器的 WebSocket API 不能帶自訂 header，所以終端使用者 token 走 query string
    （?token=<jwt>）而不是 Authorization: Bearer——這是 WebSocket 認證的常見作法之一。
    """
    await ws.accept()
    end_user_id = end_user_id_from_token(ws.query_params.get("token"))

    inbound: asyncio.Queue = asyncio.Queue()
    running: dict[str, threading.Event | None] = {"cancel": None}

    async def reader() -> None:
        """整條連線只有這一個協程呼叫 receive()（Starlette 不允許並行 receive）。

        cancel 直接就地處理（要在問答進行中生效）；其餘訊息排進佇列給主迴圈。
        """
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "cancel":
                    event = running["cancel"]
                    if event is not None:
                        event.set()
                else:
                    await inbound.put(msg)
        except Exception:  # noqa: BLE001 — 斷線 / 壞 JSON：收掉連線並停掉在跑的查詢
            event = running["cancel"]
            if event is not None:
                event.set()
            await inbound.put(None)

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            msg = await inbound.get()
            if msg is None:  # reader 回報連線已斷
                break
            if msg.get("type") != "ask":
                continue
            running["cancel"] = threading.Event()
            try:
                await _stream_one(ws, msg, end_user_id, running["cancel"])
            finally:
                running["cancel"] = None
    finally:
        reader_task.cancel()
