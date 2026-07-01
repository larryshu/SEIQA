"""FastAPI 入口：POST /ask 跑一輪 agent。對話歷史由前端帶（與 dcard_insight 同思路）。"""
from __future__ import annotations

from fastapi import FastAPI, Header
from pydantic import BaseModel

from . import memory_store, user_memory
from .agent import run
from .auth import end_user_id_from_token
from .config_repo import repo

app = FastAPI(title="社群輿情智能問答")


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
    # 2) 軟刪這位使用者這段對話的原始紀錄（只鎖 sid + end_user_id）
    deleted_rows = memory_store.soft_delete_conversation(req.session_id, end_user_id)
    return LogoutResp(ok=True, summarized=summarized, deleted_rows=deleted_rows)
