"""社群輿情智能問答 Demo UI：聊天介面 → 即時爬蟲 Agent 問答。

啟動： streamlit run ui/streamlit_app.py
（需先啟動 API： uvicorn app.api:app --port 8001）
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

# 讓 streamlit 能 import 到 app 套件
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8001")
ADMIN_API_URL = os.environ.get("ADMIN_API_URL", "http://localhost:8000")  # Django 後台（終端登入用）

# 對話持久化：以 sid 命名的 JSON 檔，存在這裡 → F5 重整後讀得回來
_SESSIONS_DIR = Path(__file__).parent / ".sessions"
_SESSIONS_DIR.mkdir(exist_ok=True)


def _session_file(sid: str) -> Path:
    return _SESSIONS_DIR / f"{sid}.json"


def _load_messages(sid: str) -> list:
    f = _session_file(sid)
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except (ValueError, OSError):
            return []
    return []


def _save_messages(sid: str, messages: list) -> None:
    try:
        _session_file(sid).write_text(json.dumps(messages, ensure_ascii=False), "utf-8")
    except OSError:
        pass  # 存檔失敗不該中斷對話


def _reset_conversation() -> None:
    """開一段全新對話：換 sid（寫進網址）、清空當前訊息。不刪任何舊檔。"""
    new_sid = uuid.uuid4().hex[:12]
    st.query_params["sid"] = new_sid
    st.session_state.session_id = new_sid
    st.session_state.messages = []


def _end_logout(api_url: str) -> None:
    """登出＝把對話生命週期收尾：請後端把整段對話摘要進長期記憶並軟刪原始紀錄，
    再清掉本地快取、開一段新的匿名對話（避免同瀏覽器下一位讀回這段對話）。"""
    token = st.session_state.get("token")
    sid = st.session_state.session_id
    if token:
        history = [{"role": m["role"], "content": m["content"]}
                   for m in st.session_state.get("messages", [])]
        try:
            requests.post(
                f"{api_url}/logout",
                json={"session_id": sid, "history": history},
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,  # 整段摘要含 LLM 呼叫，給寬裕些
            )
        except Exception:  # noqa: BLE001 — 後端登出失敗不該卡住前端登出
            pass
    _session_file(sid).unlink(missing_ok=True)  # 刪本地對話快取
    st.session_state.token = None
    st.session_state.user_name = None
    _reset_conversation()
    st.rerun()


def _end_auth(kind: str, username: str, password: str, display_name: str = "") -> None:
    """呼叫 Django 後台的終端登入/註冊；成功就把 token 存進 session_state。"""
    if not username or not password:
        st.warning("請輸入帳號與密碼")
        return
    endpoint = "register" if kind == "register" else "login"
    payload = {"username": username, "password": password}
    if kind == "register" and display_name:
        payload["display_name"] = display_name
    try:
        r = requests.post(f"{ADMIN_API_URL}/api/v1/end-auth/{endpoint}/", json=payload, timeout=10)
    except Exception as e:  # noqa: BLE001
        st.error(f"連不到後台（{ADMIN_API_URL}）：{e}")
        return
    if r.status_code in (200, 201):
        data = r.json()
        st.session_state.token = data["token"]
        st.session_state.user_name = data.get("display_name") or data.get("username")
        _reset_conversation()  # 登入＝開一段全新對話，乾淨綁定 end_user_id（不把匿名訊息算進來）
        st.rerun()
    else:
        try:
            st.error(r.json().get("detail", f"失敗（HTTP {r.status_code}）"))
        except Exception:  # noqa: BLE001
            st.error(f"失敗（HTTP {r.status_code}）")


@st.dialog("登入 / 註冊")
def _auth_dialog() -> None:
    """彈出式登入/註冊視窗（ChatGPT 風格：點底部帳號鈕才跳出）。"""
    st.caption("未登入＝匿名。登入後會套用你的個人偏好，對話也會歸到你名下。")
    _t_login, _t_reg = st.tabs(["登入", "註冊"])
    with _t_login:
        _lu = st.text_input("帳號", key="login_u")
        _lp = st.text_input("密碼", type="password", key="login_p")
        if st.button("登入", key="btn_login", type="primary", use_container_width=True):
            _end_auth("login", _lu, _lp)  # 成功會 st.rerun()，自動關閉彈窗
    with _t_reg:
        _ru = st.text_input("帳號", key="reg_u")
        _rp = st.text_input("密碼", type="password", key="reg_p")
        _rd = st.text_input("顯示名稱（選填）", key="reg_d")
        if st.button("註冊並登入", key="btn_reg", type="primary", use_container_width=True):
            _end_auth("register", _ru, _rp, _rd)


st.set_page_config(page_title="社群輿情智能問答", page_icon="📊")
st.title("📊 社群輿情智能問答 — 社群口碑")
st.caption("問鄉民口碑/時事類問題")
# Agent 會『同時』查 Dcard 口碑庫與即時爬 PTT、綜合兩邊回答；都沒有就用既有常識答。
# ── 狀態：sid 放網址(query param) → F5 重整網址不變、sid 不變 ──
sid = st.query_params.get("sid")
if not sid:
    sid = uuid.uuid4().hex[:12]
    st.query_params["sid"] = sid  # 寫進網址，之後重整就帶得回來
st.session_state.session_id = sid  # 給後端隔離 live 快取
# 首次載入（含重整後的新 session）：從磁碟讀回該 sid 的歷史
if "messages" not in st.session_state:
    st.session_state.messages = _load_messages(sid)  # [{role, content, used_tools?}]
st.session_state.setdefault("token", None)
st.session_state.setdefault("user_name", None)

# ── CSS：把 sidebar 的帳號區釘在最底部（ChatGPT 風格）──
# 註：靠 Streamlit 內部 DOM（stSidebarUserContent）做版面，若日後升級 Streamlit
#     改了結構，帳號區頂多退回「內容最下方」，功能不受影響。
st.markdown(
    """
    <style>
    section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"] {
        display: flex;
        flex-direction: column;
        min-height: calc(100vh - 4rem);
    }
    section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"]
        > div[data-testid="stVerticalBlock"] {
        flex: 1 1 auto;
    }
    section[data-testid="stSidebar"] .st-key-acct_box {
        margin-top: auto;           /* 推到底部 */
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.subheader("設定")
    api_url = st.text_input("API URL", API_URL)
    try:
        requests.get(f"{api_url}/health", timeout=5).raise_for_status()
        st.success("API 連線正常")
    except Exception:
        st.error("無法連到 API，請先啟動：\n`uvicorn app.api:app --port 8001`")
    st.caption(f"對話 ID：{st.session_state.session_id}")
    if st.button("🗑 清除對話", use_container_width=True):
        _session_file(sid).unlink(missing_ok=True)  # 刪掉磁碟存檔
        _reset_conversation()
        st.rerun()

    # ── 帳號區：釘在 sidebar 左下角（ChatGPT 風格）──
    acct_box = st.container(key="acct_box")
    with acct_box:
        st.divider()
        if st.session_state.get("token"):
            _name = st.session_state.get("user_name") or "使用者"
            with st.popover(f"👤 {_name}", use_container_width=True):
                st.caption(f"已登入：{_name}")
                if st.button("登出", use_container_width=True):
                    _end_logout(api_url)  # 摘要進長期記憶 → 軟刪這段對話 → 清本地 → 開新匿名對話
        else:
            if st.button("👤 登入 / 註冊", use_container_width=True):
                _auth_dialog()  # 點按鈕 → 跳出彈窗


def _render_answer(text: str) -> None:
    """渲染答案。半形 ~ 在 Streamlit(GFM Markdown)會被當成刪除線的成對定界符，
    像「80~120 萬」這種數字區間、或句尾語氣的 ~ 會兩兩配對、把中間整段文字劃掉。
    改成全形～（不是 Markdown 語法），保留區間/語氣原意又不會誤觸刪除線。"""
    st.write(text.replace("~", "～"))


def _answer_caption(sources: list) -> None:
    """燈號＝這次回答的來源：有撈到社群討論→綠燈（標各平台則數）；否則→黃燈（LLM 既有常識）。"""
    if not sources:
        st.caption("🟡 LLM 既有常識回答（Dcard / PTT 都沒有相關討論）")
        return
    n_dcard = sum(1 for s in sources if s.get("source") == "dcard")
    n_ptt = sum(1 for s in sources if s.get("source") == "ptt")
    parts = []
    if n_dcard:
        parts.append(f"Dcard 及時爬 {n_dcard} 則")
    if n_ptt:
        parts.append(f"PTT {n_ptt} 則")
    st.caption("🟢 來自社群討論：" + ("、".join(parts) or f"{len(sources)} 則"))


def _render_sources(sources: list) -> None:
    """來源依平台分組，做成『預設收合』的 expander（點標題才展開）；保留全域編號＝答案裡的 [n]。"""
    if not sources:
        return
    groups: dict[str, list] = {"dcard": [], "ptt": []}
    other: list = []
    for i, s in enumerate(sources):
        if not s.get("url"):
            continue
        line = f"{i + 1}. [{s.get('title') or s.get('url')}]({s['url']})"
        groups.get(s.get("source", ""), other).append(line)

    st.markdown("**來源：**")
    for label, lines in (("📘 Dcard 及時爬", groups["dcard"]),
                         ("📗 PTT", groups["ptt"]),
                         ("其他", other)):
        if lines:
            with st.expander(f"{label}（{len(lines)}）", expanded=False):  # 預設收合
                st.markdown("\n".join(lines))


# ── 重畫歷史對話 ──
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m["role"] == "assistant":
            _answer_caption(m.get("sources", []))
            _render_answer(m["content"])
            _render_sources(m.get("sources", []))
        else:
            st.write(m["content"])

# ── 新提問 ──
if prompt := st.chat_input("輸入問題，例如：遠距離戀愛可以維持嗎？藍芽耳機評價如何？"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    # 帶給後端的歷史：只送 role/content（不含本輪 user，agent 會自己加）
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages[:-1]
    ]

    with st.chat_message("assistant"):
        with st.spinner("Agent 思考中（請稍等）…"):
            try:
                resp = requests.post(
                    f"{api_url}/ask",
                    json={
                        "message": prompt,
                        "session_id": st.session_state.session_id,
                        "history": history,
                    },
                    headers=({"Authorization": f"Bearer {st.session_state.token}"}
                             if st.session_state.get("token") else {}),
                    timeout=300,  # live 爬蟲偏慢、agent 可能多輪；須 > 後端單輪 CRAWL_TIMEOUT
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                st.error(f"查詢失敗：{exc}")
                st.stop()

        _answer_caption(data.get("sources", []))
        _render_answer(data["answer"])
        _render_sources(data.get("sources", []))

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": data["answer"],
            "used_tools": data.get("used_tools", []),
            "sources": data.get("sources", []),
        }
    )
    _save_messages(sid, st.session_state.messages)  # 落地：F5 重整後讀得回
