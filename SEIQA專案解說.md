# SEIQA 社群輿情智能問答 — 專案解說

> 本文以「一次真實請求的生命週期」為主軸，從**登入 → 對話 → Agent 回答 → 記憶 → 登出**逐段拆解：
> 每一段呼叫了哪些 API、用了哪些套件、程式在哪一支檔案、為什麼這樣設計。
> 所有內容皆對照 repo 現況（`app/`、`admin_backend/`、`ui/`），非規劃稿。

---

## 0. 一句話定位

使用者用自然語言問「鄉民口碑類」問題（例：遠距離戀愛能維持嗎？某支耳機評價如何？），
系統以 **LLM function calling** 自主決定要不要查社群，需要時**同時並行**去 Dcard（即時爬 + 向量庫 fallback）
與 PTT（即時爬）撈討論，消化後用「朋友口吻」回答並標註 `[n]` 來源；
過程支援 **WebSocket 逐字串流與中途取消**；使用者的長期記憶與偏好會跨 session 累積，登出時做摘要收尾。

---

## 1. 系統組成：三個行程、兩個前端

| # | 行程 | 技術 | 埠 | 職責 |
|---|------|------|----|------|
| 1 | **Runtime**（`app/`） | FastAPI + Uvicorn | 8001 | Agent loop、工具、爬蟲、記憶讀寫、WebSocket 串流 |
| 2 | **Admin 後台**（`admin_backend/`） | Django 5.1 + DRF + MySQL | 8000 | Agent/Skill/來源平台/系統設定 CRUD、終端使用者認證、對話檢視與清理、RBAC 稽核 |
| 3 | **Streamlit 前端**（`ui/streamlit_app.py`） | Streamlit | 8501 | 阻塞式聊天 UI（走 `POST /ask`） |
| 3' | **WebSocket Demo 前端**（`app/static/ws_demo.html`） | 原生 JS，由 runtime `GET /demo` 提供 | 8001 | 即時進度 + 逐字串流 + 停止鈕（走 `WS /ws/ask`） |

兩個前端**共用同一套後端語意**（相同的 history 格式、相同的落地與記憶行為），
差別只在傳輸方式：一個 request/response，一個雙向串流。

```
┌────────────┐   POST /ask (Bearer JWT)      ┌──────────────────────────────┐
│ Streamlit  │ ─────────────────────────────▶│                              │
│  :8501     │                                │   FastAPI Runtime  :8001     │
└────────────┘                                │  ┌────────────────────────┐  │
┌────────────┐   WS /ws/ask?token=JWT         │  │ agent loop (tool call) │  │
│ /demo 頁   │ ◀────────────────────────────▶ │  └───────┬────────────────┘  │
│  (瀏覽器)  │   事件：stage/token/done…       │          │ community_search   │
└────────────┘                                │   ┌──────┴───────┐           │
        │ POST /demo/auth/login（伺服器端轉發）│   │ 並行 fan-out │           │
        ▼                                     │   └──┬───────┬───┘           │
┌────────────────────────────┐                └──────┼───────┼───────────────┘
│ Django Admin/DRF   :8000   │  唯讀設定(crawl_ro)   │       │
│  MySQL: crawl_agent        │◀─────────────────────┘       │
│  end_user / agent / skill  │  對話落地(crawl_rw)           ▼
│  conversation / message    │                    ┌──────────────────┐
│  user_preference …         │                    │ Dcard 即時爬     │ DrissionPage→真實 Chrome
└────────────────────────────┘                    │ PTT  即時爬      │ requests + BeautifulSoup
            ▲                                     └──────────────────┘
            │ 偏好/設定                                     │ 失敗/空 → fallback
            │                                     ┌──────────────────┐
            └─────────────────────────────────────│ Qdrant  :7333    │
                              長期記憶/口碑庫      │ dcard_insight    │（唯讀向量庫）
                                                  │ user_memory      │（個人記憶）
                                                  └──────────────────┘
```

---

## 2. 技術棧與套件對照

### 2.1 Runtime（`requirements.txt`）

| 套件 | 用在哪 | 為什麼是它 |
|------|--------|-----------|
| `fastapi` / `uvicorn` | `app/api.py`：`/ask`、`/logout`、`/ws/ask`、`/demo` | 同一個 app 同時提供 REST 與 WebSocket；type hints + pydantic 自動驗證 |
| `websockets` | `WS /ws/ask` 的協定實作 | 精簡版 uvicorn 不含 WS 協定層，必須另裝 |
| `pydantic` | `AskReq/AskResp/LogoutReq/LogoutResp/Source` | request/response schema 與驗證 |
| `openai` | `app/llm.py` | 一套 SDK 同時支援 OpenAI、Azure OpenAI、任何 OpenAI 相容端點（vLLM），靠 `.env` 切換 |
| `requests` | `vectorstore.py`、`user_memory.py`（Qdrant REST）、`ptt.py`、`api.py`（轉發 Django） | Qdrant 只用到 search/upsert/scroll，直接打 REST 就夠，不必引入 `qdrant-client` |
| `beautifulsoup4` | `app/ptt.py` | PTT 是純伺服器渲染 HTML、無 Cloudflare，requests + bs4 即可 |
| `DrissionPage` | `app/dcard_live.py` | Dcard 前面有 Cloudflare Managed Challenge 且改用 `globalPaging` 端點，自行重放會 403；DrissionPage 驅動**真實 Chrome**（指紋乾淨）+ `page.listen` 攔截 SPA 自己發的回應 |
| `pymysql` | `config_repo.py`、`memory_store.py`、`user_preference.py` | runtime 直接讀寫後台 MySQL（純 SQL，不掛 ORM） |
| `pyjwt` | `app/auth.py` | 驗證 Django 簽發的終端使用者 JWT（HS256，共用 `TOKEN_SECRET`） |
| `python-dotenv` | `app/config.py` | 集中式 `Settings` dataclass，全部從 `.env` 讀 |
| `streamlit` | `ui/streamlit_app.py` | Demo UI |

標準庫的關鍵用法：`concurrent.futures.ThreadPoolExecutor`（並行 fan-out）、`threading.Event`（取消號誌）、
`contextvars`（跨層進度事件匯流排）、`asyncio.to_thread` / `loop.call_soon_threadsafe`（阻塞 worker ↔ 事件迴圈的橋）、
`abc.ABC`（來源 adapter 介面）、`typing.TypedDict`（`Post`）。

### 2.2 Admin 後台（`requirements-admin.txt`）

| 套件 | 用途 |
|------|------|
| `Django==5.1.15` | ORM、Admin、migration |
| `djangorestframework` | ViewSet / Router / Serializer / Permission |
| `djangorestframework-simplejwt` | **操作者**（後台管理員）的 JWT：access 60 分、refresh 7 天、rotate + blacklist |
| `mysqlclient` | MySQL driver |

> 兩個 venv 刻意分開（`.venv` / `.venv-admin`）：runtime 不需要 Django，後台不需要爬蟲相依。

---

## 3. 登入機制

### 3.1 兩種身分，兩套 JWT

| 身分 | 誰 | 端點 | 簽發者 | 驗證者 | 有效期 |
|------|----|------|--------|--------|--------|
| **操作者**（admin/editor/viewer） | 後台管理者 | `POST /api/v1/auth/login/`（simplejwt） | Django | Django（`JWTAuthentication`） | access 60 分 / refresh 7 天 |
| **終端使用者**（end user） | 聊天的人 | `POST /api/v1/end-auth/register/`、`/end-auth/login/` | Django（`accounts/views.py::issue_end_user_token`） | **FastAPI runtime**（`app/auth.py`） | 7 天 |

終端使用者的 token payload：

```python
{"end_user_id": 12, "username": "larry", "type": "end_user", "iat": ..., "exp": ...}
# HS256，密鑰 = settings.TOKEN_SECRET（Django 與 runtime 共用同一個 .env 值）
```

`EndUser` 是**自建的表**（`accounts/models.py`，`db_table = "end_user"`），不是 `auth_user`——
後台操作者沿用 Django 內建 `auth_user` + `Group`（RBAC），聊天使用者則是產品資料。
密碼用 Django hasher（`make_password` / `check_password`）存 `password_hash`。

### 3.2 runtime 這一側的驗證（`app/auth.py`）

```python
def end_user_id_from_token(authorization: str | None) -> int | None:
    # 沒帶 / 沒設密鑰 / 沒裝 jwt → None（匿名）
    # 解碼失敗（過期、簽章錯、格式錯）→ None（匿名）
    # payload["type"] != "end_user" → None（防止拿操作者 token 冒充）
```

**設計重點：身分絕不從 request body 帶。** `AskReq` 裡沒有 `end_user_id` 欄位——
body 是不可信的輸入，一律以 `Authorization: Bearer <token>` 驗證後取得。
任何驗證問題都**降級為匿名**而非丟 401：聊天功能照常可用，只是不留記憶（fail-safe）。

### 3.3 兩個前端的登入路徑差異（含一個真實的跨來源問題）

| 前端 | 登入呼叫 | 說明 |
|------|---------|------|
| Streamlit | `requests.post(f"{ADMIN_API_URL}/api/v1/end-auth/login/")` | Streamlit 的 `requests` 跑在**伺服器端**，沒有同源限制 |
| `/demo` 頁 | `fetch("/demo/auth/login")` → runtime 轉發到 Django | 頁面由 :8001 提供、Django 在 :8000，瀏覽器直接打是**跨來源請求**，而後台沒裝 `django-cors-headers` 會被同源政策擋掉 |

所以 runtime 多了一個代理端點 `POST /demo/auth/{kind}`（`api.py::demo_auth`）：
瀏覽器全程只跟 :8001 說話，由 runtime 在伺服器端 `requests.post` 轉發給 Django、**原樣回傳它的狀態碼與 body**。
該函式刻意宣告成 `def` 而非 `async def`——`requests` 是阻塞的，FastAPI 會自動把同步 handler 丟到執行緒池，不卡事件迴圈。

WebSocket 的認證另有一個現實限制：**瀏覽器的 WebSocket API 不能帶自訂 header**，
所以 token 走 query string（`ws://…/ws/ask?token=<jwt>`），由 `ws.query_params.get("token")` 取出後走同一支 `end_user_id_from_token()`。

### 3.4 登入後的一個產品決定

兩個前端在登入成功後都會**換一個新的 session id、清空當前訊息**（`_reset_conversation()` / `newSid()`）。
理由：登入前是匿名對話，若沿用同一段對話，匿名訊息會被算進這位使用者名下、也會被寫進他的長期記憶。
換 sid ＝ 乾淨綁定 `end_user_id`。

---

## 4. 對話與 Agent 回答

### 4.1 兩條入口，同一顆大腦

| 入口 | 檔案 | 行為 |
|------|------|------|
| `POST /ask` | `agent.run()` | 阻塞式，一次回完整答案。Streamlit 走這條 |
| `WS /ws/ask` | `agent.run_streaming()` | 同樣的 loop，但過程中推事件（含逐字 token）、可中途取消。`/demo` 走這條 |

兩者**共用 `_build_context()`**（prompt / 偏好 / 記憶組裝），確保行為一致；
但 loop 本身刻意不共用——串流與非串流的 LLM 呼叫語意有差（tool_calls 分片累積 vs 一次回全），
隔離開來才不會讓已驗證過的 `/ask` 被串流路徑的問題波及。

### 4.2 `_build_context()`：一輪對話的設定解析（`app/agent.py`）

```
1. repo.get_active_agent()      → 後台「啟用中」的 agent：system_prompt / model / temperature / max_tool_rounds
2. repo.get_user_preferences()  → 這位使用者的偏好（tone / answer_length / language / model / 平台過濾）
3. _apply_pref_modifiers()      → 把偏好貼成 system prompt 的附加指示
4. 記憶注入（僅登入使用者）：
     is_memory_query(問題)?
       是 → _apply_memory(list_memories(), meta=True)      # 「你記得我什麼」→ 列出全部記憶
       否 → _apply_memory(recall(uid, 問題))               # 語意撈回相關「原子事實」
            _apply_thread_context(recall_threads(uid, 問題))# 語意撈回相關「對話脈絡敘事」
5. messages = [system] + history（前端帶） + [本輪 user]
6. tools = repo.get_tools() or TOOLS（寫死 fallback）
```

**取值優先序：`user_preference` > `agent`（後台） > `system_setting` / `.env` 寫死值。**
每一層取不到就往下掉——後台 MySQL 掛掉，runtime 照樣用 `.env` 跑（`config_repo` 的 fail-safe）。

`ConfigRepository`（`app/config_repo.py`）的三個設計：
- **短 TTL 程序內快取**（預設 30 秒），加 per-user 偏好小快取，避免每個 request 打 DB；
- **失敗結果也快取**——DB 壞掉時不要每個 request 都重連一次壞掉的 DB；
- 提供 `POST /internal/reload-config` 立即清快取，後台改設定可即時生效。

### 4.3 Agent loop（LLM ↔ 工具）

```
LLM(messages, tools, tool_choice="auto")
  ├─ 沒有 tool_calls  → 這就是答案（🟡 常識題，不查社群）
  └─ 有 tool_calls    → 執行 community_search → 把結果以 role="tool" 塞回 messages
                        → 再問一次 LLM → 產出最終答案（🟢 有社群來源）
```

`MAX_TOOL_ROUNDS = 1`：一題查一次就夠——因為 `community_search` **內部已經並行查了兩個平台**，
不需要靠 LLM「記得」分別呼叫兩個工具。**「兩邊一定都查」是程式保證的，不是 prompt 保證的。**

只對 LLM 暴露**一個** skill（`app/tools.py::TOOLS`），function schema 的 `description` 就是觸發條件：
「需要鄉民口碑/心得/時事 → 用；純常識、定義、計算 → 不要用、直接答」。

### 4.4 `community_search`：並行 fan-out（`app/sources.py`）

這是整個系統的擴充點。抽象是一個 `Source` ABC：

```python
class Source(ABC):
    name: str
    def __init__(self, cfg: dict | None = None): ...   # cfg = 後台該平台的 source_config
    @abstractmethod
    def fetch(self, query: str) -> list[Post]: ...     # Post 已帶 source 平台標籤
```

| adapter | 類別 | 實作 |
|---------|------|------|
| `dcard`（`DCARD_MODE=live`） | `DcardLiveSource` | DrissionPage 全站文章搜尋 → 深挖內文 + 熱門留言；**失敗/沒撈到 → 自動 fallback 向量庫** |
| `dcard_vector` | `DcardSource` | Qdrant `dcard_insight` 口碑庫向量檢索 |
| `ptt` | `PttSource` | requests + bs4 站內搜尋，時間預算內逐篇抓 |

`_build_registry()` 依「後台啟用的平台 + 排序」組出 registry，再套使用者的 `included/excluded_platforms` 偏好；
DB 不可用就退回 `_DEFAULT_REGISTRY`。**新增一個平台 = 寫一個 adapter + 掛進 `_ADAPTERS`，agent / loop / prompt 全部不動。**

並行執行的細節：

```python
executor = ThreadPoolExecutor(max_workers=len(registry))
futures = [executor.submit(contextvars.copy_context().run, _safe_fetch, s, query) for s in registry]
for fut in futures:
    while True:
        progress.raise_if_cancelled()          # 等爬蟲時每 0.5 秒回頭看有沒有被取消
        try:
            results.extend(fut.result(timeout=0.5)); break
        except FutureTimeout:
            continue
finally:
    executor.shutdown(wait=False, cancel_futures=True)   # 取消時不等在途的爬蟲
```

三個踩過的坑寫在程式碼裡：
1. **先全部 submit 再依序收**——邊 submit 邊 `.result()` 會退化成序列執行；
2. **`contextvars` 不會自動流進 worker thread**，且同一個 Context 不能被兩條執行緒同時 `run`，所以每個 future 各複製一份，否則底層爬蟲的進度事件與取消檢查全都看不到訂閱者；
3. **DrissionPage 是阻塞的、無法從外部中斷**——取消時只能「不再等它」，那顆 Chrome 會自己跑到時間預算結束後收工，結果丟棄。

`_safe_fetch()` 是**單一來源的 fail-safe**：任一平台炸掉只少那一邊，不影響其他來源。

### 4.5 兩個爬蟲

**PTT（`app/ptt.py`）**——PTT web 無 Cloudflare、純 SSR HTML，只有部分板有 18 禁年齡牆（帶 `over18=1` cookie 即過），
所以 `requests` + `BeautifulSoup` 就夠。兩個真實限制決定了作法：
- PTT 站內搜尋是**逐看板**的（沒有全站搜尋）→ 先用一次 LLM 呼叫從**白名單看板**（16 個板）挑最相關的板；
- PTT 搜尋是拿整串字比對**標題**、空白分隔多詞是 **AND** → 「外型 情緒穩定」幾乎 0 結果。正解是讓 LLM 給**多個單一語詞**，各搜一次再合併。
- **時間預算**（`PTT_TIME_BUDGET`，預設 60 秒）而非固定篇數：到時就停、回目前抓到的全部。全程禮貌限速（0.5–1.0 秒隨機）避免被 ban。

**Dcard（`app/dcard_live.py`）**——DrissionPage 驅動真實 Chrome：
- **單例瀏覽器 + 一把 `threading.Lock`**：同時只跑一個 Dcard 爬蟲，過一次 Cloudflare 就重用、保溫；
- `page.listen` 攔截 SPA 自己發出的 API 回應，遞迴走訪任意 JSON 抽出貼文/留言（`_looks_like_post` / `_looks_like_comment`），不依賴 DOM 選擇器；
- 同樣有**時間預算**（`DCARD_TIME_BUDGET`，預設 100 秒，須 < 前端 300 秒 timeout）與逐篇檢查點；
- 任何例外一律吞掉回 `[]` 並收掉可能已壞的瀏覽器 session，交給上層 fallback 向量庫。

**向量庫（`app/vectorstore.py`）** 有兩個檢索品質設計：
- **多面向查詢改寫 + round-robin 合併**：單一稠密向量會被強勢詞綁架（「健身有助性生活品質？」整碗被端去健身版）。
  改成請 LLM 改寫出數條「鄉民實際用語」的查詢，各查一次，再**輪流各取一篇**合併——每個面向都有代表，不會被單一面向洗版。
- **分數門檻**（`SEARCH_MIN_SCORE=0.5`）：低於門檻視為不夠對題 → 回空 → 走🟡黃燈用常識答，
  而不是自信地引用一堆其實不相關的貼文。

### 4.6 幻覺防線

`tools.py::_community_search` 回給 LLM 的字串**明確標出這次哪些平台有/沒有資料**：

```
本次有撈到資料的平台：Dcard。（PTT 這次沒有撈到相關討論——回答時就只根據上面實際有的來源講，
不要假裝引用了它、也不要說它上面有什麼討論。）

[1]（Dcard）標題...
內文...
來源：https://...
```

前端據此顯示**燈號**：🟢 來自社群討論（標各平台則數）／🟡 LLM 既有常識（兩邊都沒撈到）。
`sources` 陣列的順序就是答案裡 `[n]` 的編號，前端依平台分組成可收合的來源清單。

---

## 5. WebSocket 即時問答（`WS /ws/ask`）

### 5.1 為什麼需要它

一次查詢可能跑到 100 秒以上（真實瀏覽器過 Cloudflare + 逐篇深挖）。
阻塞式 `/ask` 期間使用者只看得到一顆 spinner，也**無法中止**。
WebSocket 把「Agent 現在在做什麼」變成看得見的產品行為，並且能在生成中按停止。

### 5.2 事件協定

上行（client → server）：

| 訊息 | 內容 |
|------|------|
| 問一題 | `{"type":"ask","message":"…","session_id":"…","history":[{role,content},…]}` |
| 中途取消 | `{"type":"cancel"}` |

下行（server → client），由 `app/progress.py` 的匯流排從**任意深度**推出：

| 事件 | 發出者 | 內容 |
|------|--------|------|
| `stage` | `agent` | `planning`（判斷要不要查）/ `answering`（開始生成） |
| `search_start` | `sources` | 這次並行查哪些平台 |
| `source_start` / `source_done` / `source_error` | `sources._safe_fetch` | 各平台開始 / 抓到幾則 + 耗時 / 該平台失敗 |
| `source_fallback` | `sources.DcardLiveSource` | Dcard 即時爬失敗 → 退向量庫（**降級行為變成看得見的事**） |
| `crawl_plan` | `ptt` / `dcard_live` | LLM 挑的看板與關鍵詞 |
| `crawl_search` | `dcard_live` | 搜尋頁找到幾篇 |
| `crawl_progress` | `ptt` / `dcard_live` | 逐篇進度 |
| `crawl_budget` | `ptt` / `dcard_live` | 時間預算用完，回目前已抓到的 |
| `search_done` | `sources` | 合併總數 + 各平台則數 |
| `token` | `agent` | 逐字答案片段 |
| `answer_reset` | `agent` | 模型在決定用工具前先吐了幾個字 → 請前端把已印出的清掉（那些不是答案） |
| `tool_start` / `tool_done` | `agent` | 工具開始/結束（`/demo` 頁目前未渲染，保留給其他前端） |
| **`done` / `cancelled` / `error`** | `api` | **終結事件**：帶 `answer` / `used_tools` / `sources` / `light`（🟢🟡） |

### 5.3 執行緒模型

LLM SDK、DrissionPage、requests **全都是阻塞的**，不能直接跑在 asyncio 事件迴圈上：

```
事件迴圈（asyncio）                          worker thread（asyncio.to_thread）
──────────────────                          ──────────────────────────────
ws_ask()                                     _run_blocking()
 ├─ reader() 協程 ── 整條連線唯一呼叫          └─ with progress.session(emit, cancel_event):
 │   receive() 的地方（Starlette 不允許            agent.run_streaming()
 │   並行 receive）；cancel 就地處理                 └─ 底層任一層 progress.emit(...)
 │                                                      └─ loop.call_soon_threadsafe(queue.put_nowait, ev)
 └─ _stream_one() ── 排空 outbound queue ◀──────────────┘（唯一安全的跨執行緒橋）
      └─ await ws.send_json(ev)  直到收到終結事件
```

`_run_blocking()` 的**任何一條路徑都必須恰好送出一個終結事件**（`done` / `cancelled` / `error`），
因為 `_stream_one()` 的排空迴圈靠它收工。

### 5.4 取消機制：為什麼 `Cancelled` 繼承 `BaseException`

`progress.py` 用兩個 `contextvars`（emitter、cancel event）當匯流排。沒有訂閱者時 `emit()` 是 **no-op**——
所以 `/ask` 那條路徑**一個位元都沒變**，串流是純加法。

取消訊號是 `threading.Event`，各層在檢查點呼叫 `raise_if_cancelled()`：
- LLM 串流：**每收一片就檢查** → 按停止立刻中斷，且 `stream.close()` 會中止底層 HTTP 連線，不讓 LLM 繼續算完整段；
- fan-out：每 0.5 秒回頭檢查（＝使用者感受到的停止延遲上限）；
- 爬蟲：**逐篇檢查點** → 最多再等一篇。

```python
class Cancelled(BaseException):   # 刻意不是 Exception
```

理由很實際：底下每一層都有 fail-safe 的 `except Exception`，其中 `dcard_live.crawl()` 會把攔到的例外解讀成
「即時爬失敗 → 退回向量庫」。若取消是普通 `Exception`，**使用者按停止會被誤判成爬蟲掛了、進而觸發一次沒必要的向量庫 fallback**。
繼承 `BaseException` 才穿得過那些網子——標準庫的 `asyncio.CancelledError` 與 `KeyboardInterrupt` 正是基於同一個理由。

---

## 6. 記憶機制：四層，各司其職

| 層 | 存哪 | 存什麼 | 生命週期 | 檔案 |
|----|------|--------|----------|------|
| **短期對話** | **前端**（Streamlit：伺服器磁碟 `.sessions/{sid}.json`；demo 頁：`localStorage`） | 完整 `history`，每次請求原樣帶給後端 | 一段對話 | `ui/streamlit_app.py`、`app/static/ws_demo.html` |
| **對話落地** | MySQL `conversation` / `message` | 每輪 user + assistant 兩則訊息、`used_tools`、`sources` | 永久（可軟刪 / purge） | `app/memory_store.py` |
| **個人長期記憶** | Qdrant `user_memory` | 語意向量：**關於使用者本人的事實**、**對話脈絡敘事** | 跨 session | `app/user_memory.py` |
| **偏好旋鈕** | MySQL `user_preference` | 封閉 schema 的設定值（tone / length / language / 平台過濾） | 跨 session | `app/user_preference.py` |
| （來源資料） | Qdrant `dcard_insight` | Dcard 口碑庫，**唯讀**（由另一個專案批次建好） | — | `app/vectorstore.py` |
| （live 快取） | 記憶體（`SessionFreshStore`） | 本次爬回來的貼文，依 session 隔離 | 用完即丟 | `app/store.py` |

### 6.0 短期對話：後端是無狀態的，上下文由前端帶

「記得前面幾輪」這件事**不是後端記住的**——三個端點（`POST /ask`、`WS /ws/ask` 的 ask 訊息、`POST /logout`）
都吃同一個 `history` 欄位，前端每次把整段對話原樣送上來，`_build_context()` 直接攤進 LLM 的 messages：

```python
messages = [{"role": "system", "content": system_prompt}]
messages.extend(history or [])                        # ← 前端帶來的短期對話
messages.append({"role": "user", "content": user_message})
```

這是 Chat Completions 的標準用法（API 本身無狀態），好處是後端不必管 session 生命週期、多分頁併發、TTL。
代價是：**前端手上那份 history 一掉，上下文就斷了**——即使那些訊息早就被 `persist_turn()` 寫進 MySQL。

所以補上了一條**還原路徑**（`GET /conversation/{sid}`）：前端重整或換裝置時，
把落地在 `message` 表的內容讀回來重建 `history`。三個必要條件都寫在 SQL 裡：

```sql
SELECT m.role, m.content, m.sources FROM message m
JOIN conversation c ON m.conversation_id = c.id
WHERE c.sid=%s AND c.end_user_id=%s AND c.is_deleted=0   -- 綁身分、且不還原已軟刪的對話
ORDER BY m.created_at, m.id LIMIT %s
```

- **同時鎖 `sid` 與 `end_user_id`**：知道別人的 sid 也讀不到別人的對話；
- **濾掉 `is_deleted=1`**：登出時軟刪過的對話不該被還原回來；
- 用 `crawl_rw` 既有的 SELECT 權限，不必新增授權。

兩個前端的持久化策略因此變成：

| | Streamlit | `/demo` 頁 |
|---|---|---|
| sid | 寫進網址 query param | 寫進網址 query param（`window.history.replaceState`） |
| history | 伺服器磁碟 `.sessions/{sid}.json` | `localStorage`（本機備份）+ 開場向 `GET /conversation/{sid}` 要**權威版本** |
| token | `st.session_state`（重整即失效） | `localStorage`，開場用 `GET /me` 驗證是否過期（JWT 7 天） |

demo 頁開場順序：`/me` 驗 token → 連 WebSocket → `/conversation/{sid}` 還原；
後端沒有（匿名、DB 沒開、落地失敗）才退回 `localStorage` 那份。
登出時除了後端軟刪，也會清掉本機那份備份——否則同一台瀏覽器的下一位開同一個 sid 還讀得回來。

> 補充：送給後端的 `history` **只能有 `role` / `content`**，因為它會被原樣攤進 LLM 的 messages 陣列，
> 多帶 `sources` 之類的欄位會被 API 拒收。所以 demo 頁把「畫面用的 turns（含 sources）」與
> 「上線用的 chatHistory」分成兩個陣列維護。

### 6.1 為什麼記「事實」而不是「答案」

本系統的答案來自社群輿情，**結論會過時**。只記「關於這個人」的穩定事實，
才不會把過時結論當記憶、也不會誘導系統不再即時查證。這條紅線寫進了每一支萃取 prompt。

### 6.2 `user_memory`（Qdrant，1536 維 / Cosine，走 REST）

三種 `kind`，同一個 collection，用 payload filter 分流：

| kind | 何時寫 | 內容 | 怎麼檢索 |
|------|--------|------|---------|
| `turn` | **每輪**（`/ask` 與 `/ws/ask` 回答後） | LLM 從提問萃取一句「使用者是…」的原子事實；沒有值得記的就**不寫**（回 `NONE`） | `recall()`：語意搜，門檻 0.35，top 3 |
| `session_summary` | **登出時** | 整段對話萃取 0–3 條原子事實 | 同上（與 `turn` 一起撈） |
| `thread` | **登出時** | 整場對話的「有脈絡敘事」（5–8 句：使用者的處境/目標/提問走向 + **討論過的重點與結論梗概**） | `recall_threads()`：**用 headline 主題句 embed**，門檻 0.42（比事實高），top 2 |

`thread` 的兩個設計細節：
- **embed 主題句、存整段敘事**——整段敘事多主題會把向量稀釋、召回變糊；用一句主題化的名詞短語當檢索鍵才準。
- **敘事要把會過時的具體值抽象成主題層級**：寫「討論了薪資行情與談薪策略」而非「年薪 200 萬」；
  注入時也加註「若使用者要最新狀況，仍以本次查到的最新討論為準」，避免模型把舊梗概當成當下事實。

另外有一條 **meta 問題**的分支（`is_memory_query()`，正則比對「你記得我什麼 / 之前聊過什麼」）：
這類問題該**列出全部記憶**（`list_memories()` 走 Qdrant `scroll`），而不是語意搜——
因為它語意上跟任何內容事實都不相關，會被門檻擋掉。沒有記憶時 prompt 要求模型「誠實說還沒有並邀請他多聊聊」，
而不是說「我看不到對話紀錄」。

### 6.3 `user_preference`（MySQL）：為什麼要比記憶保守得多

兩者分工不同：

- `user_memory`（Qdrant）：free-text 事實，語意召回，個人化答案的**內容**；
- `user_preference`（MySQL）：封閉 schema 的**設定旋鈕**，runtime 用**精確 key** 讀出來，**確定性地改變行為**
  （`agent._apply_pref_modifiers()` 改 prompt、`sources._build_registry()` 過濾平台）。

寫錯 preference 會**靜默且確定性地**改變行為——例如把使用者隨口一句「Dcard 有時候很亂」誤設成
`excluded_platforms=[dcard]`，等於默默關掉主要資料源。所以有四道護欄：

1. **白名單 key + 值域驗證**（`_ALLOWED`）：`tone` / `answer_length` / `language` 是 enum，
   `included/excluded_platforms` 必須是已知平台名的陣列（以 `sources._ADAPTERS` 為準）。off-schema 一律丟棄；
2. **信心門檻**（`PREF_INFER_MIN_CONFIDENCE`，預設 0.75）；
3. **prompt 約束**：只在使用者「明確、直接」表達**長期**偏好時才輸出；帶有「這題／這次／暫時／先」等
   指向單次的字眼一律不輸出；
4. **人工設定永不被覆寫**——upsert 用 SQL 守住：

```sql
INSERT INTO user_preference (end_user_id, `key`, value, value_type, source, confidence, updated_at)
VALUES (%s,%s,%s,%s,'inferred',%s,%s)
ON DUPLICATE KEY UPDATE
  value      = IF(source='manual', value,      VALUES(value)),
  value_type = IF(source='manual', value_type, VALUES(value_type)),
  confidence = IF(source='manual', confidence, VALUES(confidence)),
  updated_at = IF(source='manual', updated_at, VALUES(updated_at))
```

`model` 這種高風險 key **刻意不自動推論**（保持人工設定），只推 UI / 檢索層面的偏好。

### 6.4 對話落地（`memory_store.persist_turn`）

純 SQL（pymysql），用**獨立的讀寫帳號 `crawl_rw`**（只在 `conversation` / `message` / `user_preference` 有 INSERT/UPDATE）：
依 `sid` 找/建 conversation → 插入 user + assistant 兩則 message → `message_count += 2`、更新 `last_active_at`。
時間一律 `datetime.utcnow()`，與 Django `USE_TZ=True` 的 UTC 儲存一致。

**全程 fail-safe**：落地失敗只 `logger.warning`，絕不中斷聊天回應。
`db_host` / `db_rw_user` 留空 ＝ 直接停用這個功能。

---

## 7. 登出機制（`POST /logout`）

登出不只是清 token，而是**把一段對話的生命週期收尾**。三個步驟，全程 fail-safe：

```python
end_user_id = end_user_id_from_token(authorization)   # 匿名 → 什麼都不做（沒有可綁定的記憶）
# 1) 整段對話 → 長期記憶（每輪已即時寫過 turn，這裡補整體脈絡）
summarized   = user_memory.summarize_and_remember(end_user_id, req.history, req.session_id)
# 2) 整段對話 → 推論可執行的設定旋鈕（白名單 + 信心門檻 + 不覆寫人工設定）
inferred     = user_preference.infer_and_store(end_user_id, req.history, req.session_id)
# 3) 軟刪這位使用者這段對話的原始紀錄
deleted_rows = memory_store.soft_delete_conversation(req.session_id, end_user_id)
```

第 1 步用**一次 LLM 呼叫同時產出兩種記憶**（`{"facts":[...], "thread":{"headline","narrative"}}`），
解析時容錯處理 markdown 圍籬與 `NONE`。

第 3 步的軟刪：

```sql
UPDATE conversation SET is_deleted=1, updated_at=%s
WHERE sid=%s AND end_user_id=%s AND is_deleted=0
```

- **只鎖 `sid` + `end_user_id`**，避免誤刪他人的對話；
- 用 UPDATE 而非 DELETE，`crawl_rw` 帳號**不需要 DELETE 權限**（最小權限原則）；
- 真正抹除交給後台 admin 的 `POST /conversations/purge/`（需 admin 角色）。

前端在收到回應後刪掉本地對話快取、清 token、開一段新的匿名對話——
避免同一台瀏覽器的下一位使用者讀回這段對話。

回應：`{"ok": true, "summarized": 2, "inferred": 1, "deleted_rows": 1}`

---

## 8. API 總表

### 8.1 Runtime（FastAPI，:8001）

| 方法 | 路徑 | 認證 | 用途 |
|------|------|------|------|
| GET | `/health` | — | 健康檢查（前端與 `start.ps1` 輪詢用） |
| POST | `/ask` | `Authorization: Bearer`（選填，無＝匿名） | 跑一輪 agent，回 `{answer, used_tools, sources[]}` |
| POST | `/logout` | 同上（匿名直接回 ok） | 摘要 → 偏好推論 → 軟刪，回 `{ok, summarized, inferred, deleted_rows}` |
| GET | `/me` | 同上 | token 還有效嗎？回 `{authenticated, end_user_id}`（前端重整後判斷要不要退回匿名） |
| GET | `/conversation/{sid}` | 同上（匿名回空） | 從 MySQL 讀回這段對話，供前端 F5／換裝置後還原上下文（鎖 sid + end_user_id + 未軟刪） |
| GET | `/conversations` | 同上（匿名回空） | 這位使用者未刪除的對話清單（新→舊），可做「我的對話」側欄 |
| WS | `/ws/ask?token=<jwt>` | query string token | 即時問答：多輪、逐字串流、可取消 |
| GET | `/demo` | — | WebSocket demo 頁（HTML） |
| POST | `/demo/auth/{login\|register}` | — | 代理轉發到 Django end-auth（解跨來源） |
| POST | `/internal/reload-config` | — | 清設定快取，下次請求重讀 MySQL |

### 8.2 Admin 後台（Django + DRF，:8000，前綴 `/api/v1/`）

| 模組 | 端點 | 說明 |
|------|------|------|
| 認證 | `POST auth/login/`、`auth/refresh/`、`auth/logout/`、`GET auth/me/` | 操作者 JWT（simplejwt；logout = blacklist refresh token） |
| 認證 | `POST end-auth/register/`、`POST end-auth/login/` | **終端使用者**（公開），成功回自簽的 7 天 JWT |
| 模組一 | `/agents/`（CRUD）<br>`POST /agents/{id}/activate/`<br>`POST /agents/{id}/test-run/` | agent 人設/模型/參數；activate 用 transaction 保證**全系統只有一個 is_active**；test-run 目前回 501（樁） |
| 模組一 | `/skills/`（CRUD） | function-calling 定義（name / description / json_schema） |
| 模組一 | `/source-platforms/`（CRUD）<br>`GET,PUT /source-platforms/{id}/configs/` | 來源平台啟用/排序；configs 以 key upsert（top_k / min_score / time_budget…） |
| 模組三 | `GET /conversations/`（分頁 50）<br>`GET /conversations/{id}/messages/`<br>`GET /conversations/{id}/export/`<br>`DELETE /conversations/{id}/`（軟刪）<br>`POST /conversations/purge/`（**admin**，硬刪已軟刪或已過期） | 對話檢視/匯出/清理 |
| 模組三 | `/memory-collections/`、`POST /{id}/sync/` | 打 Qdrant 更新 collection metadata（point_count / vector_size / status） |
| 模組四 | `/system-settings/`（CRUD，`lookup_field="key"`） | 全域設定 |
| 模組四 | `GET,PUT /end-users/{id}/preferences/` | 每使用者偏好（人工設定 → `source='manual'`） |

**RBAC**（`accounts/permissions.py`）：以 Django `Group` 實作三個角色。
`RoleBasedReadWrite`：安全方法（GET/HEAD/OPTIONS）需 viewer 以上；寫入需 editor 或 admin。
`purge` 另外要求 `IsAdminRole`。所有寫入動作經 `AuditLogMixin` 寫進 `audit_log`（actor / action / target / before-after diff / IP）。

---

## 9. 資料模型

### 9.1 MySQL（`crawl_agent`）— 由 Django ORM 管 migration，runtime 用純 SQL 讀寫

| 模組 | 表 | 重點欄位 |
|------|-----|---------|
| accounts | `end_user` | username(unique) / password_hash / status / last_login_at |
| accounts | `api_key` | 只存 hash + 顯示用 prefix，明碼僅產生當下顯示一次 |
| accounts | `audit_log` | actor / action / target_type / target_id / changes(JSON) / ip |
| agents | `agent` | system_prompt / model / temperature / max_tool_rounds / **is_active** |
| agents | `skill`、`agent_skill` | name / description / json_schema；M2M 帶 sort_order |
| agents | `source_platform`、`source_config` | name / adapter_key / kind / is_active / sort_order；參數 key-value typed |
| memory | `conversation` | sid(unique) / end_user / agent / message_count / last_active_at / expires_at / **is_deleted** |
| memory | `message` | role / content / used_tools(JSON) / sources(JSON) |
| memory | `memory_collection` | Qdrant collection 的 metadata 與統計 |
| preferences | `system_setting` | key(unique) / value / value_type / group_name / is_secret |
| preferences | `user_preference` | (end_user, key) unique / value_type / **source(manual\|inferred)** / confidence |

所有表都用 `db_table` 對齊規格命名（`docs/admin_backend_spec.md`），**FastAPI runtime 才能直接下 SQL 讀寫**。

**兩個 DB 帳號，最小權限**：`crawl_ro`（唯讀，讀設定）／`crawl_rw`（只在 conversation / message / user_preference 有 INSERT/UPDATE）。

一個做過的 SQL 優化（`memory/migrations/0002`）：對話列表查詢是
`filter(is_deleted=False).order_by(-last_active_at, -created_at)`，
建了複合索引 `conv_list_idx (is_deleted, -last_active_at, -created_at)`，
讓 MySQL 直接**走索引取序**，免去 `Using filesort` + 全表掃描（驗證腳本：`agents/management/commands/measure_sql.py`）。
DRF 那側也處理了 N+1：`prefetch_related("skills")` / `prefetch_related("configs")`。

### 9.2 Qdrant（:7333）

| collection | 用途 | 讀寫 |
|-----------|------|------|
| `dcard_insight` | Dcard 口碑庫（由另一個專案批次建好，chunk 存） | **唯讀**（以 url 去重，每篇只留最高分 chunk） |
| `user_memory` | 個人長期記憶（1536 維 / Cosine；payload index：`end_user_id`, `kind`） | 讀寫 |
| `crawl_agent_hot` | 方案 B 預留：爬回來的貼文持久化 | 未實作（`store.QdrantHotStore` 為樁） |

---

## 10. 功能 × API × 邏輯處理 對照總表

| 功能 | 觸發的 API / 端點 | 主要程式 | 邏輯處理 |
|------|------------------|---------|---------|
| 註冊 / 登入 | `POST /api/v1/end-auth/{register,login}/`（Streamlit 直打；demo 頁經 `POST /demo/auth/{kind}` 代理） | `accounts/views.py` | 驗證帳密（Django hasher）→ 更新 `last_login_at` → 簽 HS256 JWT（7 天，`type=end_user`）→ 前端存 token、**換新 sid 開乾淨對話** |
| 身分驗證 | 每個 `/ask`、`/logout`、`/ws/ask` | `app/auth.py` | Bearer / query token → `jwt.decode` → 檢查 `type=="end_user"` → 回 `end_user_id`；任何問題**降級為匿名**（不 401） |
| 設定解析 | （內部）`repo.get_active_agent()` / `get_tools()` / `get_enabled_sources()` / `get_user_preferences()` | `app/config_repo.py` | `crawl_ro` 唯讀 MySQL；30 秒 TTL 快取（含失敗結果）；DB 掛掉 → fallback `.env` 與寫死值 |
| 記憶注入 | （內部） | `app/user_memory.py` + `agent._build_context()` | meta 問題 → `scroll` 列出全部記憶；一般問題 → `recall()`（事實，門檻 0.35）+ `recall_threads()`（脈絡敘事，門檻 0.42）→ 貼進 system prompt |
| 偏好套用 | （內部） | `agent._apply_pref_modifiers()`、`sources._build_registry()` | tone / answer_length / language → 附加指示；included/excluded_platforms → **過濾 registry**（確定性地改行為） |
| Agent 決策 | LLM `chat.completions`（`tool_choice="auto"`） | `app/agent.py` + `app/llm.py` | 有 tool_calls → 執行工具再問一次（🟢）；無 → 直接答（🟡）。`max_tool_rounds=1` |
| 查社群 | tool `community_search` | `app/tools.py` → `app/sources.py` | `ThreadPoolExecutor` **並行 fan-out** 到啟用的 adapter；每個 source 各自 fail-safe；合併時標平台、明講哪些平台**沒有**資料（防幻覺） |
| Dcard 即時爬 | Dcard 網站（DrissionPage 驅動真實 Chrome） | `app/dcard_live.py` | LLM 抽關鍵字 → 全站文章搜尋 → 時間預算內深挖內文+熱門留言；單例瀏覽器 + Lock；**失敗/空 → fallback 向量庫** |
| Dcard 向量檢索 | Qdrant `POST /collections/dcard_insight/points/search` | `app/vectorstore.py` | LLM 多面向查詢改寫 → 各查一次 → **round-robin 合併** → url 去重 → 分數門檻 |
| PTT 即時爬 | `https://www.ptt.cc/bbs/<board>/search` | `app/ptt.py` | LLM 從 16 板白名單挑板 + 產多個單一關鍵詞（PTT 標題搜尋是 AND）→ requests + bs4 翻頁逐篇抓 → 時間預算 + 禮貌限速 |
| 逐字串流 | `WS /ws/ask` | `app/api.py`、`app/progress.py`、`llm.chat_with_tools_stream()` | worker thread 跑阻塞 agent → `contextvars` 匯流排 `emit()` → `call_soon_threadsafe` 進 asyncio queue → `ws.send_json()` |
| 中途取消 | `{"type":"cancel"}` | `app/progress.py` | `threading.Event` + 各層檢查點；`Cancelled(BaseException)` 穿過所有 `except Exception` 的 fail-safe 網 |
| 對話落地 | （內部）MySQL `crawl_rw` | `app/memory_store.py` | 依 sid 找/建 conversation → 插 user + assistant 兩則 message（含 used_tools / sources JSON）→ 失敗只 log |
| 對話還原 | `GET /me`、`GET /conversation/{sid}`、`GET /conversations` | `app/memory_store.py::load_history()` / `list_conversations()` | 前端開場：驗 token → 讀回這段對話重建 `history` 與畫面（含來源清單）。SQL 鎖 `sid + end_user_id + is_deleted=0`；讀不到就退回前端本機備份（fail-safe） |
| 每輪記憶 | Qdrant `PUT /collections/user_memory/points` | `app/user_memory.py::remember()` | LLM 萃取「關於使用者本人的事實」（沒有就回 NONE 不寫）→ embed → upsert（kind=`turn`） |
| 登出 | `POST /logout` | `app/api.py::logout` | ①整段摘要 → facts + thread 寫入 Qdrant ②偏好推論 → 白名單/門檻/不覆寫 manual → upsert MySQL ③軟刪 conversation（sid + end_user_id） |
| 後台管理 | `/api/v1/agents|skills|source-platforms|system-settings|conversations|...` | `admin_backend/*/views.py` | DRF ViewSet + RBAC（Group）+ AuditLog；改設定後 runtime 靠 TTL 或 `/internal/reload-config` 生效 |

---

## 11. 貫穿全案的設計原則

**1. 分層 fail-safe：每一層都能單獨壞掉，服務仍然回得了話。**

| 壞掉的東西 | 降級行為 |
|-----------|---------|
| 後台 MySQL 連不上 | `config_repo` 回 `None` → 用 `.env` 與程式內寫死值 |
| Dcard 即時爬失敗 / Cloudflare 擋 | `DcardLiveSource` → fallback 向量庫（並推 `source_fallback` 事件給前端） |
| 單一平台 fetch 炸掉 | `_safe_fetch` 只少那一邊，另一邊照回 |
| 兩邊都沒撈到 | 工具回「請用既有常識答並誠實說沒找到討論」→ 🟡 黃燈 |
| Qdrant / embed 失敗 | 記憶當作沒有，回答照常 |
| 對話落地失敗 | 只 log，不影響回應 |
| token 過期 / 無效 | 降級為匿名，聊天照常 |

**2. 保證放在程式裡，不放在 prompt 裡。**「一定要同時查兩個平台」是 fan-out 保證的；
「不能引用沒撈到的平台」是工具回傳字串明講的；「不能覆寫人工偏好」是 SQL 的 `IF(source='manual', …)` 保證的。

**3. 擴充點是介面，不是分支。** 加一個社群平台 ＝ 寫一個 `Source` adapter 掛進 `_ADAPTERS`；
換 live 資料的儲存方式 ＝ 換一個 `FreshStore` 實作。agent / loop / prompt 都不用動。

**4. 最小權限。** 兩個 DB 帳號（ro / rw）；rw 連 DELETE 權限都不給（軟刪用 UPDATE）；
身分只認簽章過的 token，不信任 request body。

---

## 12. 執行方式

```powershell
.\start.ps1        # 一鍵：Django(8000) + FastAPI(8001) + Streamlit(8501)，並輪詢 /health 就緒後自動開 /demo
```

分開跑：

```powershell
.venv-admin\Scripts\python.exe admin_backend\manage.py runserver 127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.api:app --reload --port 8001
.venv\Scripts\python.exe -m streamlit run ui\streamlit_app.py
```

關鍵環境變數（`.env`，完整清單見 `app/config.py`）：

| 群組 | 變數 |
|------|------|
| LLM | `LLM_API_KEY` / `CHAT_MODEL` / `EMBED_MODEL` / `LLM_BASE_URL` / `AZURE_OPENAI_ENDPOINT` |
| 檢索 | `QDRANT_URL` / `INSIGHT_COLLECTION` / `SEARCH_TOP_K` / `SEARCH_EXPAND_N` / `SEARCH_MIN_SCORE` |
| Dcard 即時爬 | `DCARD_MODE`(live\|vector) / `DCARD_TIME_BUDGET` / `DCARD_DEEP_MAX` / `DCARD_HEADLESS` / `DCARD_USER_DATA_DIR` |
| PTT | `PTT_TIME_BUDGET` / `PTT_MIN_DELAY` / `PTT_MAX_DELAY` |
| 記憶 | `USER_MEMORY_ENABLED` / `USER_MEMORY_TOP_K` / `USER_MEMORY_MIN_SCORE` / `USER_THREAD_*` / `PREF_INFER_*` |
| DB | `DB_HOST` / `DB_NAME` / `DB_USER`(ro) / `DB_RW_USER`(rw) / `CONFIG_CACHE_TTL` |
| 認證 | `TOKEN_SECRET`（Django 與 runtime **必須相同**） / `ADMIN_API_URL` |

---

## 13. 已知限制

- 對話上下文仍由**前端每輪帶上來**；`GET /conversation/{sid}` 只在**開場還原**時讀 DB，不是每輪都從 DB 重建 messages。
  這是刻意的（後端維持無狀態），代價是前端仍握有「送什麼上下文給 LLM」的權力。
- `POST /agents/{id}/test-run/` 目前是樁（回 501）——後台改完設定後，實際驗證要靠 `/internal/reload-config` + 前端試問。
- `store.QdrantHotStore`（方案 B：把爬回來的貼文持久化成 hot collection）尚未實作，目前 live 資料**用完即丟**（`SessionFreshStore`）。
- Dcard 即時爬依賴真實 Chrome 有頭模式才穩定過 Cloudflare（`DCARD_HEADLESS=0`），不適合無 GUI 的容器環境；
  該環境下應設 `DCARD_MODE=vector` 走向量庫。
- 後台未裝 `django-cors-headers`，跨來源前端需經 runtime 代理（現況：`/demo/auth/*`）。
