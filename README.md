# 社群輿情智能問答 — 完整技術說明

> **一份文件看懂整個系統**：前台問答引擎（tool-calling Agent）＋ 後台管理系統（Django + DRF）＋ 三層記憶與偏好。
> 本檔由 [`README.md`](README.md)（runtime / agent）與 [`docs/admin_backend_spec.md`](docs/admin_backend_spec.md)（後台完整規格）融合的**導覽版**——著重架構與亮點，細節（完整欄位、API）指回 spec。

| 項目 | 內容 |
|---|---|
| 定位 | tool-calling 的**多平台社群口碑問答 Agent** + 可設定化後台 |
| 技術棧 | 前台 runtime：FastAPI · WebSocket · PyMySQL · PyJWT · DrissionPage（Dcard 反爬即時爬）· Qdrant · Azure OpenAI；後台：Django 5 · DRF · MySQL 8；前端：Streamlit（阻塞問答）＋ WebSocket demo 頁（即時進度 / 逐字串流 / 中途取消） |
| 狀態 | M0–M6 + 終端登入 + 使用者長期記憶/登出 + 偏好自動推論 + WebSocket 即時前端 **皆已實作（as-built）** |

---

## 目錄

1. [專案簡介與亮點](#1-專案簡介與亮點)
2. [系統組成（三個服務）與整體架構](#2-系統組成三個服務與整體架構)
3. [Agent 與檢索設計](#3-agent-與檢索設計)
4. [記憶（三層）與偏好](#4-記憶三層與偏好)
5. [後台管理系統（Django + DRF）](#5-後台管理系統django--drf)
6. [專案結構](#6-專案結構)
7. [快速開始](#7-快速開始)
8. [.env 參數](#8-env-參數)
9. [怎麼再加一個平台](#9-怎麼再加一個平台)
10. [開發里程碑（as-built）](#10-開發里程碑as-built)
<!-- 11. [本版不做與關鍵決議](#11-本版不做與關鍵決議) -->

---

## 1. 專案簡介與亮點

tool-calling 的**多平台社群口碑問答 Agent**。借鑑 Hermes Agent「LLM 用 tool calling 自己決定何時呼叫外部工具」的模式，但用既有 Azure OpenAI 技術棧原生實作（不引入 Hermes 平台）。

> 你問「遠距離戀愛可以維持嗎？」→ Agent 判斷需要鄉民口碑 → 呼叫 `community_search` →
> **同時即時爬 Dcard（DrissionPage 過 Cloudflare）＋ PTT** → 綜合兩邊、帶分平台出處回答。（Dcard 即時爬失敗自動退回向量庫 fallback）
> 問「一年有幾個月？」→ 判斷不需查 → 直接用常識回答（🟡 黃燈）。

### 技術亮點

- **原生 tool-calling Agent**：不靠框架，LLM 自行規劃是否查、查什麼；`community_search` 一個 skill 對外，內部並行 fan-out 多平台。
- **雙來源即時爬 + 反爬**：**Dcard 用 DrissionPage 驅動真實 Chrome 過 Cloudflare 即時爬**、PTT 用 requests 即時爬，**「兩邊都查」是程式層保證**；Dcard 另備離線向量庫當 fallback（即時爬失敗自動退回）。
- **Registry + adapter 擴充性**：加平台＝多寫一個 adapter，agent / prompt / loop 全不動；連 Dcard 的「即時爬↔向量庫」切換也靠這層乾淨接起來（`DCARD_MODE`）。
- **設定資料庫化**：prompt / 模型 / 平台開關 / 檢索門檻全搬進 MySQL，後台改設定**免改程式碼**；runtime 唯讀讀取 + 短 TTL 快取。
- **全域 fail-safe（分層）**：Dcard 即時爬掛了退向量庫、PTT 掛了只少 PTT、任一來源 embed/Qdrant/後台 DB 出事只降級不中斷；兩邊都空 → 誠實退回常識（🟡 黃燈），反幻覺。
- **WebSocket 即時前端**：`/ws/ask` 把 agent 的每一步（抽關鍵字、深挖第幾篇、**退回 fallback**）即時推給前端、答案逐字串流，並支援生成中取消。事件匯流排用 `contextvars` 實作，**`/ask` 與 Streamlit 行為完全不變**（見 [§3.3](#33-即時進度串流與取消websocketwsask)）。
- **三層記憶（長期層雙軌）+ 偏好自動推論**：短期檢索快取、中期對話落地、長期使用者記憶（**事實點狀召回 ＋ 脈絡敘事重載 / episodic 雙軌**）；登出時 LLM 另從對話**自動學習可執行偏好**（白名單 + 保守門檻 + 不覆寫人工設定）。
- **完整後台**：四模組（agent / 帳戶 / 記憶 / 偏好）、RBAC（admin/editor/viewer）、稽核、JWT 雙身分（操作者 vs 終端使用者）。
- **SQL 優化實績**：N+1 修正（31→2、21→2 查詢）、複合索引消除 filesort、對話列表分頁對齊索引。

---

## 2. 系統組成（三個服務）與整體架構

| 服務 | 埠 | 角色 |
|---|---|---|
| FastAPI runtime（`app/`）| 8001 | 跑 agent、`/ask`（阻塞）與 `/ws/ask`（WebSocket 即時串流）、對話落地 MySQL、個人化長期記憶與偏好；並自行提供 `/demo` 前端 |
| Streamlit 聊天前端（`ui/`）| 8501 | 終端使用者問答、🟢/🟡 燈號、登入 / 登出（走阻塞 `/ask`）|
| Django + DRF 後台（`admin_backend/`）| 8000 | 設定 / 帳戶 / 對話管理，**MySQL schema 唯一擁有者** |

> **三個服務、兩個終端前端**：Streamlit（`:8501`，阻塞問答）與 WebSocket demo 頁（`:8001/demo`，即時進度 + 串流 + 取消）。兩者共用同一個 agent，差別只在傳輸方式。

```
[Streamlit 聊天 UI]────POST /ask────┐
                                    ├──► [FastAPI runtime]（跑 agent；唯讀 MySQL + Qdrant）
[WebSocket demo 頁]───WS /ws/ask────┘         │
   （由 runtime 自己在 /demo 提供）            └─POST /demo/auth/*─┐（伺服器端轉發登入，避開 CORS）
                                                                   ▼
[後台 Web 介面]──────────────────────────► [Django + DRF]（寫設定/帳戶/對話；擁有 schema/migration）
                                                     │
                                           ┌─────────┴─────────┐
                                      [MySQL]              [Qdrant]
                                   關聯資料：設定、         向量本體：使用者長期記憶
                                   帳戶、對話、metadata     ＋ Dcard fallback 向量庫（唯讀）
```

**架構定案要點：**

1. **共用同一個 MySQL**：Django 寫設定，FastAPI runtime 直接讀同一個 DB。
2. **Schema 唯一擁有者是 Django**：所有表由 Django migration 建立與維護；**FastAPI 用唯讀帳號讀取，絕不建表**。
3. **MySQL 不存向量**：Dcard 口碑庫的向量本體留在 Qdrant；MySQL 只存 metadata / 統計（`memory_collection`）。
4. **帳戶涵蓋兩類人**：後台操作者（staff）+ 終端使用者，兩套身分互不混用。
5. **一鍵起全部**：`.\start.ps1` 同時起 8000/8001/8501，就緒後（輪詢 `/health`）自動開啟 `http://localhost:8001/demo`；Streamlit 以 `--server.headless` 啟動、不再自己彈瀏覽器，但 8501 仍可手動開。關掉前景視窗會一併收掉另外兩個。
6. **後台掛掉不影響問答**：runtime 讀不到 DB 時自動 fallback 到 `.env`／寫死預設值照常跑。
7. **兩個前端共用同一個 agent**：`/ask` 與 `/ws/ask` 都走 `app/agent.py`，共用 `_build_context()`（prompt / 偏好 / 記憶組裝）；差別只在前者阻塞回完整答案、後者邊跑邊推事件。

---

## 3. Agent 與檢索設計

### 3.1 一個 skill，多個來源 adapter（registry + 並行 fan-out）

對 LLM **只暴露一個工具** `community_search`；底下掛一個**來源 registry**，每個平台是一個 adapter，把該平台包成統一的 `fetch(query) -> list[Post]`。查詢時**並行 fan-out** 到所有 adapter、合併結果（每篇帶 `source` 平台標籤）。

**加新平台＝在 `sources.py` 多寫一個 adapter、加進 `REGISTRY` 即可——agent / loop / prompt 全部不動。** 這也保證「每次兩邊都查」是程式層保證的，不靠 LLM 自己記得同時叫多個工具。

| adapter | 平台 | 方式 | 反爬 |
|---|---|---|---|
| `DcardLiveSource` | Dcard | **DrissionPage 即時爬**：LLM 抽關鍵字 → 全站「文章」搜尋 → 時間預算內深挖內文/留言；失敗→退回向量庫 | Cloudflare：DrissionPage 驅動真實 Chrome + 持久設定檔養 `cf_clearance` |
| `DcardSource` | Dcard（fallback） | 查向量庫 `dcard_insight`（語意檢索，唯讀）；`DCARD_MODE=vector` 也可強制走這條 | 無（離線已建庫） |
| `PttSource` | PTT | 即時爬站內搜尋（時間預算內邊翻邊抓） | 無 Cloudflare，帶 `over18` cookie 即可 |

> **Dcard 的演進**：一開始因站內搜尋被 Cloudflare 擋，改用離線向量庫（穩定、唯讀）；後來用 DrissionPage 攻克 Cloudflare、升級成即時爬拿最新討論，**向量庫保留當 fallback**。`DCARD_MODE=live`（預設，即時爬＋fallback）／`vector`（純向量庫）。

### 3.2 設計決策

- **Dcard＝即時爬（DrissionPage）＋向量庫 fallback**：LLM 先把問句抽成關鍵字（不用選版），開全站「文章」搜尋 `/search/posts`；貼文改用 `globalPaging` 端點載入（自行重放會 403），故用 `page.listen` 攔截網頁自己發的回應。搜回相關文後，在 `DCARD_TIME_BUDGET`（預設 100s）內逐篇進頁抓內文＋熱門留言，到時就停、回已抓到的（單例瀏覽器 + 鎖、留言掃描上限控成本）。**即時爬失敗/沒結果 → 自動退回向量庫**（`dcard_insight` 3529 筆、多面向查詢改寫 `SEARCH_EXPAND_N` + round-robin + `SEARCH_MIN_SCORE` 門檻，避免單一稠密向量被強勢詞綁架）。
- **PTT＝即時爬＋時間預算**：LLM 一次決定（看板 + 多個『單一關鍵詞』）——PTT 多詞是 AND 比對標題（「外型 情緒穩定」→ 0 筆），故抽成多個單詞各搜再合併。翻搜尋頁『邊翻邊抓』，到 `PTT_TIME_BUDGET`（預設 60s）就停；全程禮貌限速避免被 ban。
- **檢索快取＝方案 A（預設）**：查到的社群資料只進**當次 session 記憶體**（`SessionFreshStore`），用完即丟。業務只認 `FreshStore` 抽象。
- **燈號＝來源透明**：有撈到社群討論 → 🟢 綠燈＋標各平台則數＋來源 `[n]`；都沒撈到（或不需查）→ 🟡 黃燈，誠實標為 LLM 既有常識。
- **fail-safe（分層）**：Dcard 即時爬失敗自動退向量庫；PTT 失敗只少 PTT；任一平台 embed/Qdrant 出事只影響那一邊；兩邊都空就退回常識回答。
- **工具＝skill**：`tools.py` 的 `description` 寫清楚「何時該用」＝觸發條件，等同 Hermes skill 的 trigger。

### 3.3 即時進度、串流與取消（WebSocket，`/ws/ask`）

`/ask` 是阻塞請求：Dcard 時間預算 100s、PTT 60s（並行），最壞情況使用者盯著 spinner 等近兩分鐘、毫無回饋。`/ws/ask` 就是為了解決這件事，而 `/ask` 原封不動保留。

- **事件匯流排 `app/progress.py`**：用 `contextvars` 傳遞 emitter 與取消號誌，而不是改函式簽章——所以 `Source.fetch()` 介面不變，README 承諾的「加平台＝只寫一個 adapter」依然成立。**沒有訂閱者時 `emit()` 是 no-op**，`/ask` 行為一個位元都沒變。fan-out 的每個 future 各複製一份 `Context`（同一個 `Context` 不能被兩條執行緒同時 `run`）。
- **事件流**：`stage` → `crawl_plan` → `crawl_search` → `crawl_progress`（就地更新）→ `source_fallback` / `source_error` → `source_done` → `search_done` → `token`（逐字）→ `done` / `cancelled` / `error`。其中 **`source_fallback` 讓原本只寫進後端 log 的分層降級，變成使用者看得見的產品行為**。
- **逐字串流**：`llm.chat_with_tools_stream()`。串流時 `tool_calls` 的 `arguments` 是一片一片吐出的（`name`/`id` 只在首片），依 `delta.index` 分槽累積才拼得回來。
- **取消**：`progress.Cancelled` 刻意繼承 **`BaseException` 而非 `Exception`**。底層到處是 fail-safe 的 `except Exception`，其中 `dcard_live.crawl()` 會把攔到的例外解讀成「即時爬失敗 → 退回向量庫」；若取消是普通 `Exception`，使用者按停止會誤觸一次沒必要的 fallback。（標準庫的 `asyncio.CancelledError` 基於同一理由這樣設計。）fan-out 用 `fut.result(timeout=0.5)` 輪詢而非死等，前端 0.5 秒內收到 `cancelled`。
  > **已知限制**：DrissionPage 是阻塞的、無法從外部中斷，取消只是「不再等它」——那顆 Chrome 會握著單例鎖跑到時間預算結束。所以取消後立刻再問一題，Dcard 那條會排隊（PTT 不受影響）。
- **認證是 per-connection 的**：瀏覽器的 WebSocket API 不能帶自訂 header，所以 token 走 query string（`/ws/ask?token=<jwt>`），而且**握手時只讀一次**——登入 / 登出後必須斷線重連。這與 HTTP 每個 request 都能換 header 不同。
- **登入代理 `/demo/auth/{login,register}`**：demo 頁由 runtime（:8001）提供、Django 在 :8000，瀏覽器直接打是跨來源請求，而後台沒裝 `django-cors-headers`。改由 runtime 在伺服器端轉發，瀏覽器全程同源。Streamlit 不會遇到這問題，因為它的 `requests.post` 本來就跑在伺服器端。
- **併發模型**：整條連線只有**一個 reader 協程**呼叫 `receive()`（Starlette 不允許並行 receive），`cancel` 就地處理、其餘訊息排進佇列；agent 跑在 `asyncio.to_thread` 的 worker thread，事件經 `loop.call_soon_threadsafe` 回到事件迴圈。

---

## 4. 記憶（三層）與偏好

**記憶分三層（短 / 中 / 長期）；長期層再分「事實」與「脈絡」雙軌。偏好不是記憶、而是「指令性設定」，故另計。**

| 記憶層 | 存哪 | 存什麼 | 生命週期 |
|---|---|---|---|
| **短期**・檢索快取（方案 A）| 程序記憶體 `SessionFreshStore` | 當次查到的社群貼文 | 當次 session，用完即丟 |
| **中期**・對話紀錄 | 後台 MySQL `conversation` / `message` | 每輪 Q/A（`memory_store.persist_turn`）| 落地保存、可在後台檢視；**前端可用 `GET /conversation/{sid}` 讀回**；登出軟刪 |
| **長期・事實**（atomic）| Qdrant `user_memory`（`kind=turn/session_summary`）| LLM 萃取「關於使用者本人的穩定事實」；**點狀語意召回**（`recall`，門檻 0.35）| 跨 session 保留，**僅登入者** |
| **長期・脈絡**（thread / episodic）| Qdrant `user_memory`（`kind=thread`）| 登出時把整場對話濃縮成**一筆有脈絡敘事**（headline 檢索／narrative 重載）；相關問題**整段重載**（`recall_threads`，門檻 0.42）| 跨 session 保留，**僅登入者** |

> **偏好（`user_preference`，另計，不併入記憶層數）**：LLM 從對話推論的「可執行設定旋鈕」（語氣/長度/語言/model/平台過濾），登出寫入 MySQL、跨 session；runtime 用精確 key **確定性改變行為**。與上面「描述性記憶」是兩回事。

- **記憶 vs 偏好**：`user_memory`（Qdrant）記「描述性」自由文字（事實／脈絡）、語意召回個人化**內容**；`user_preference`（MySQL）記「指令性」設定旋鈕、runtime 用精確 key **確定性改變行為**。兩者寫入路徑獨立。
- **事實記憶 vs 脈絡記憶**：同一個 `user_memory` collection 用 `kind` 分兩軌——**事實**（原子、第三人稱一句話，點狀召回填個人化背景）與 **脈絡（thread / episodic）**（整場對話的敘事梗概，headline 當檢索鍵、narrative 重載，讓相關新問題能喚回上次討論到哪）；thread 走 `recall_threads` 注入獨立的「先前相關對話的脈絡」區塊，門檻（0.42）比事實（0.35）高，避免鬆散舊脈絡灌爆 prompt。
- **偏好推論比記憶保守**（因為會確定性且靜默地改變行為，例如誤設 `excluded_platforms` 會默默關掉資料源）：只收白名單 key、值域受限、過信心門檻（`PREF_INFER_MIN_CONFIDENCE`，預設 0.75），且 `source='manual'` 的人工設定**永不被覆寫**。
- **取值優先序**（runtime 解析）：`user_preference` > `agent` > `system_setting`。
- **只有登入使用者**才有長期記憶與對話歸戶；匿名照常能問答但不留記憶。全程 fail-safe：記憶那層炸掉也不影響回答。
- **短期對話（上下文）由前端帶、後端無狀態**：`/ask`、`/ws/ask`、`/logout` 都吃同一個 `history` 欄位，`_build_context()` 直接把它攤進 LLM 的 messages。好處是後端不必管 session 生命週期；代價是前端那份 history 一掉，上下文就斷了——即使訊息早已落地 MySQL。故補上**還原路徑** `GET /conversation/{sid}`（另有 `GET /conversations` 列出使用者的對話、`GET /me` 驗 token 是否過期）：SQL 同時鎖 `sid + end_user_id + is_deleted=0`，所以知道別人的 sid 也讀不到、登出軟刪過的對話也不會被還原。**只在開場還原時讀 DB，不是每輪都從 DB 重建 messages**（後端維持無狀態）。

  | | Streamlit | `/demo` 頁 |
  |---|---|---|
  | sid | 網址 query param | 網址 query param |
  | history | 伺服器磁碟 `.sessions/{sid}.json` | `localStorage`（備份）+ 開場向 `GET /conversation/{sid}` 要權威版本 |
  | token | `st.session_state`（重整即失效） | `localStorage`，開場用 `GET /me` 驗證是否過期 |

**登入 / 登出流程（as-built）**：Streamlit 向 Django `end-auth` 拿 JWT（共用 `TOKEN_SECRET`、HS256），聊天帶 `Authorization: Bearer`；runtime 驗證取 `end_user_id`（失敗即匿名）。**登出**（`POST /logout`）把整段對話 ①摘要成長期事實**＋一筆脈絡敘事（thread）**寫入 `user_memory`、②推論可執行偏好寫入 `user_preference`、③軟刪原始對話，回 `{ok, summarized, inferred, deleted_rows}`（`summarized` 含事實與 thread 條數）。

> **WebSocket demo 頁的登入路徑不同**：瀏覽器不能跨來源打 Django，改打 runtime 的 `POST /demo/auth/{login,register}` 由伺服器端轉發（見 [§3.3](#33-即時進度串流與取消websocketwsask)）；拿到 token 後**重新握手** WebSocket。登出仍是同源的 `POST /logout`，走完全相同的摘要 / 偏好推論 / 軟刪流程。

> 詳細機制（每輪萃取、meta 問題列表、payload 備查、四道護欄）見 [`admin_backend_spec.md` §8.1–8.3](docs/admin_backend_spec.md#81-終端登入與-token-驗證as-built)。

---

## 5. 後台管理系統（Django + DRF）

### 5.1 四模組總覽與邊界

| 模組 | Django app | 職責 | 取代原本哪段死碼 |
|---|---|---|---|
| 一、Skill / Agent | `agents` | agent 人設/模型/參數、skill 定義、來源平台與其參數 | `agent.py` 常數、`tools.py` TOOLS、`sources.py` REGISTRY |
| 二、帳戶 | `accounts` | 操作者 RBAC、終端使用者、API 金鑰、稽核 | （原本完全沒有） |
| 三、記憶 | `memory` | 對話落地 MySQL、檢視 Qdrant 口碑庫 metadata | `ui/.sessions/*.json`、`store.py`（部分） |
| 四、偏好 | `preferences` | 全域系統設定（取代 `.env`）、per-user 偏好 | `config.py` settings |

- **模組一 = 結構**（有哪些 agent / skill / 平台、各自功能參數）；**模組四 = 偏好層**（全域預設 + 每使用者覆寫）。
- **runtime 取值優先序**：`user_preference` > `agent` 設定 > `system_setting` 全域預設。

### 5.2 資料模型 ERD

```mermaid
%%{init: {"theme": "default", "themeVariables": {"fontSize": "22px"}, "er": {"useMaxWidth": false, "entityPadding": 18, "minEntityWidth": 140}}}%%
erDiagram
    auth_user ||--o{ audit_log : "操作"
    auth_user ||--o{ api_key : "擁有"
    end_user ||--o{ conversation : "擁有"
    end_user ||--o{ user_preference : "設定"
    agent ||--o{ agent_skill : ""
    skill ||--o{ agent_skill : ""
    agent ||--o{ conversation : "使用"
    source_platform ||--o{ source_config : "參數"
    conversation ||--o{ message : "包含"

    auth_user {
        bigint id PK
        varchar username
        bool is_staff
    }
    end_user {
        bigint id PK
        varchar username
        varchar status
    }
    agent {
        bigint id PK
        varchar name
        text system_prompt
        bool is_active
    }
    skill {
        bigint id PK
        varchar name
        json json_schema
    }
    agent_skill {
        bigint id PK
        bigint agent_id FK
        bigint skill_id FK
    }
    source_platform {
        bigint id PK
        varchar name
        bool is_active
    }
    source_config {
        bigint id PK
        bigint platform_id FK
        varchar key
    }
    conversation {
        bigint id PK
        varchar sid
        bigint end_user_id FK
        datetime expires_at
    }
    message {
        bigint id PK
        bigint conversation_id FK
        varchar role
        mediumtext content
    }
    memory_collection {
        bigint id PK
        varchar name
        int point_count
    }
    system_setting {
        bigint id PK
        varchar key
        varchar value
    }
    user_preference {
        bigint id PK
        bigint end_user_id FK
        varchar key
    }
    api_key {
        bigint id PK
        varchar key_hash
    }
    audit_log {
        bigint id PK
        bigint actor_id FK
        varchar action
    }
```

**關聯機制（FK 存 PK）**：`audit_log.actor_id`/`api_key.owner_user_id`→`auth_user`、`conversation.end_user_id`→`end_user`、`conversation.agent_id`→`agent`、`message.conversation_id`→`conversation`、`user_preference.end_user_id`→`end_user`、`source_config.platform_id`→`source_platform`；`agent ↔ skill` 為多對多，透過中間表 `agent_skill`。`memory_collection`、`system_setting` 為全域層級、刻意不連 FK。

### 5.3 資料表一覽（14 張；完整欄位型別/限制見 [spec §6](docs/admin_backend_spec.md#6-資料表欄位定義)）

| 模組 | 表 | 職責重點 |
|---|---|---|
| 帳戶 | `auth_user`（Django 內建）| 後台操作者 + RBAC（用 Group） |
| 帳戶 | `end_user` | 終端使用者（帳密 / SSO 欄位、狀態） |
| 帳戶 | `api_key` | runtime/外部呼叫金鑰（只存 hash） |
| 帳戶 | `audit_log` | 後台寫入稽核（actor / action / before-after diff） |
| Agent | `agent` | 人設 prompt / model / temperature / max_tool_rounds（`is_active` 唯一啟用） |
| Agent | `skill` | function-calling schema + 給 LLM 的觸發條件 |
| Agent | `agent_skill` | agent ↔ skill 多對多中間表 |
| Agent | `source_platform` | 來源平台（adapter_key / kind / 啟用開關 / 順序） |
| Agent | `source_config` | 每平台參數 key-value（top_k / min_score / expand_n…） |
| 記憶 | `conversation` | 對話（sid / 歸戶 / 軟刪；`conv_list_idx` 複合索引對齊列表查詢） |
| 記憶 | `message` | 每則訊息（role / content / used_tools / sources JSON） |
| 記憶 | `memory_collection` | Qdrant collection metadata（point_count / status，檢視用） |
| 偏好 | `system_setting` | 全域業務設定 key-value（取代 `.env`） |
| 偏好 | `user_preference` | 每使用者偏好；含 `source`(manual/inferred) + `confidence`，manual 不被推論覆寫 |

### 5.4 DRF API（完整方法/路徑/權限見 [spec §7](docs/admin_backend_spec.md#7-drf-api-endpoint-清單)）

- Base path `/api/v1/`；**runtime 不走 API、直接讀 DB**。權限分 `admin` / `editor` / `viewer`。
- **認證（雙身分）**：`/auth/*` 給後台操作者（Django `auth_user`）、`/end-auth/*` 給終端使用者（`end_user`，回 JWT 供 Streamlit），兩套互不混用。
- **四模組 CRUD（as-built）**：
  - Agent：`/agents/`（+`activate`；`test-run` 目前回 501 樁）、`/skills/`、`/source-platforms/`（+`configs`）
  - 帳戶：`/auth/*`（操作者 JWT：login / refresh / logout / me）、`/end-auth/{register,login}/`（終端使用者）
  - 記憶：`/conversations/`（列表分頁、`messages`、`export`、`purge`）、`/memory-collections/`（+`sync`）
  - 偏好：`/system-settings/`、`/end-users/{id}/preferences/`

> ⚠️ **spec §7.3 與實作的落差**：規格列的帳戶管理 REST API（`/operators/`、`/roles/`、`/end-users/` CRUD、`/api-keys/`、`/audit-logs/`）**尚未實作成 DRF endpoint**。這些管理動作目前一律走 **Django Admin**（`accounts/admin.py` 已註冊 `EndUser` / `ApiKey` / `AuditLog`，含批次動作），符合 spec §14 決議四「後台前端先用 Django Admin 過渡」。`accounts` 的 DRF 路由**只有**認證那兩組。

### 5.5 Runtime 整合：FastAPI 如何讀 MySQL

新增一層 **`ConfigRepository`**：唯讀帳號讀設定 + 程序內快取（30–60 秒 TTL）或 `/internal/reload-config` 顯式重載，避免每個 request 查 DB。原本寫死的東西改讀自 DB：

| 原本的死碼 | 改讀自 |
|---|---|
| `agent.py` `SYSTEM_PROMPT` / `MAX_TOOL_ROUNDS` | `agent`（`is_active=1`） |
| `tools.py` `TOOLS` | `skill` + `agent_skill` |
| `sources.py` `REGISTRY` 啟用與順序 | `source_platform` |
| 各檢索參數（top_k / min_score / expand_n / PTT 預算） | `source_config` |
| `config.py` 業務設定（model / 門檻 / 逾時） | `system_setting` |
| 每使用者語氣 / 平台過濾 / 答案長度 | `user_preference` |

> **`.env` 仍保留**啟動前需要且機密的東西：MySQL 連線、LLM API Key、Qdrant URL、記憶/偏好開關。**業務設定才搬進 `system_setting`。**

### 5.6 權限與 DB 帳號（最小權限）

**RBAC（Django Group）**：`admin`（全部）、`editor`（agent/skill/source/setting CRUD + 終端使用者 + 記憶管理，不碰操作者/金鑰）、`viewer`（全唯讀）。後台寫入一律記 `audit_log`。

**DB 帳號**：`root`（Django 管 schema、寫 `end_user`）、`crawl_ro`（runtime 唯讀讀設定/偏好）、`crawl_rw`（runtime 寫 `conversation`/`message`；**偏好自動推論**另需 `user_preference` 的 **SELECT/INSERT/UPDATE**——SELECT 是因 upsert 守衛 `IF(source='manual', …)` 要讀既有 `source` 欄）：

```sql
GRANT SELECT, INSERT, UPDATE ON <db>.conversation     TO 'crawl_rw'@'<host>';
GRANT SELECT, INSERT, UPDATE ON <db>.message          TO 'crawl_rw'@'<host>';
GRANT SELECT, INSERT, UPDATE ON <db>.user_preference  TO 'crawl_rw'@'<host>';
```

> 資料遷移/初始 seed（system_setting / source_platform / agent / skill / memory_collection / 對話遷移）見 [spec §9](docs/admin_backend_spec.md#9-資料遷移與初始化)。

---

## 6. 專案結構

```
SEIQA/
├─ app/                     # FastAPI runtime（agent + 唯讀 MySQL 讀取層）
│   config.py               # .env 設定（LLM / Qdrant / Dcard 即時爬 / PTT / MySQL / 記憶 / 偏好）
│   llm.py                  # LLM 客戶端：chat() / chat_with_tools() / embed() / expand_queries()
│   dcard_live.py           # Dcard 即時爬（DrissionPage 過 Cloudflare：全站文章搜尋 + 深挖內文/留言 + 時間預算）
│   vectorstore.py          # Dcard fallback 向量檢索（Qdrant REST，多面向 + 門檻；即時爬失敗時用）
│   ptt.py                  # PTT 即時爬蟲（requests + bs4，over18 + 時間預算 + 限速 + 重試）
│   sources.py              # 來源 registry：Source 抽象 + DcardLiveSource(→向量 fallback)/PttSource + 並行 fan-out
│   store.py                # FreshStore 抽象 + SessionFreshStore(A) + QdrantHotStore(B 預留)
│   tools.py                # 單一 skill：community_search
│   agent.py                # 規劃→工具→行動 的多輪 loop
│   config_repo.py          # ConfigRepository：唯讀讀後台 MySQL 設定（短 TTL 快取 + reload）
│   memory_store.py         # 對話落地 MySQL（persist_turn / soft_delete）+ 讀回歷史（load_history / list_conversations）
│   user_memory.py          # 使用者長期語意記憶（Qdrant：萃取事實 → 注入 prompt）
│   user_preference.py      # 使用者偏好自動推論（登出時萃取設定旋鈕 → MySQL user_preference）
│   auth.py                 # 驗證 Django 簽發的終端 token → end_user_id（失敗即匿名）
│   progress.py             # 即時事件匯流排 + 取消號誌（contextvars；無訂閱者時 no-op）
│   api.py                  # FastAPI：/ask、/ws/ask、/logout、/me、/conversation/{sid}、/conversations、
│                           #          /demo、/demo/auth/*、/health、/internal/reload-config
│   static/ws_demo.html     # WebSocket 前端：即時進度 + 逐字串流 + 停止鈕 + 登入/登出 + F5 還原對話
├─ ui/streamlit_app.py      # Streamlit 聊天前端（🟢/🟡 燈號 + 來源分組 + 登入/登出；走阻塞 /ask）
├─ admin_backend/           # Django + DRF 後台（accounts / agents / memory / preferences）
├─ docs/admin_backend_spec.md   # 後台完整規格（本檔的來源之一）
├─ .venv/ /.venv-admin/     # runtime 環境 / Django 後台環境
└─ start.ps1                # 一鍵起 Django(8000) + FastAPI(8001) + Streamlit(8501)，並自動開 /demo
```

---

## 7. 快速開始

**前置**：
- **Dcard 即時爬（`DCARD_MODE=live`，預設）**：本機裝有 Google Chrome；設 `DCARD_USER_DATA_DIR` 持久設定檔養 `cf_clearance` 過 Cloudflare（首次可能需在彈出視窗手動點一次盾）。需有桌面環境（有頭瀏覽器）。
- **Dcard fallback / `DCARD_MODE=vector`**：Qdrant 跑著、`dcard_insight` 已有資料、`EMBED_MODEL` 與建庫時同一模型（`text-embedding-3-small`，1536 維）。
- PTT 免設定。

**只跑問答 runtime（最小；不需 MySQL / 後台）**

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
# 建立 .env（無範本檔），至少填 LLM_API_KEY / Azure endpoint / QDRANT_URL
uvicorn app.api:app --reload --port 8001
```

> `DB_HOST` 留空＝停用後台整合，runtime 全走 `.env`／預設值。

**完整系統（後台 + runtime + 前端，一鍵）**

```powershell
python -m venv .venv-admin
.venv-admin\Scripts\pip install -r requirements-admin.txt
.venv-admin\Scripts\python admin_backend\manage.py migrate
.\start.ps1                        # Django 8000 + FastAPI 8001 + Streamlit 8501，並自動開 /demo
```

**兩個前端**：

| 前端 | 網址 | 特性 |
|---|---|---|
| WebSocket demo（`start.ps1` 自動開） | http://localhost:8001/demo | agent 即時進度、逐字串流、**中途可停止**、來源依平台分組收合、**F5／換裝置可還原對話**（登入者從 MySQL 讀回） |
| Streamlit | http://localhost:8501 | 阻塞問答（送出後等完整答案），需手動開啟；F5 從伺服器磁碟 `.sessions/` 讀回 |

**測試**：Swagger UI（http://localhost:8001/docs，注意 WebSocket 不會出現在 OpenAPI）／ curl `POST /ask` ／ 兩個前端。回覆上方標「🟢 Dcard 及時爬 X 則 / PTT Y 則」或「🟡 LLM 既有常識」。兩來源並行，一題最久 ≈ `max(DCARD_TIME_BUDGET, PTT_TIME_BUDGET)` 秒（非相加）——這也正是 `/demo` 要即時推進度的原因。

---

## 8. .env 參數

```
EMBED_MODEL=text-embedding-3-small   # 須與建 dcard_insight 時同一個模型
QDRANT_URL=http://localhost:7333
INSIGHT_COLLECTION=dcard_insight     # Dcard fallback 向量庫 collection

# --- Dcard 即時爬（DrissionPage）---
DCARD_MODE=live                      # live=即時爬（失敗自動退向量庫）／vector=純向量庫
DCARD_TIME_BUDGET=100                # 即時爬時間預算（秒），到時回已抓到的
DCARD_DEEP_MAX=18                    # 最多深挖幾篇（進頁抓內文+留言）
DCARD_MAX_COMMENTS=20 / DCARD_COMMENT_SCAN_MAX=50  # 每篇取前幾則留言 / 掃描上限（控成本）
DCARD_HEADLESS=0                     # 有頭才過得了 Cloudflare（須桌面環境）
DCARD_USER_DATA_DIR=...              # 持久 Chrome 設定檔：養 cf_clearance 跨次重用
# DCARD_COOKIE / DCARD_USER_AGENT    # 替代方案：貼同一瀏覽器的 cookie+UA

# --- Dcard fallback 向量檢索（DCARD_MODE=vector 或即時爬失敗時）---
SEARCH_TOP_K=5 / SEARCH_EXPAND_N=3 / SEARCH_MIN_SCORE=0.5   # 則數 / 改寫條數 / 門檻
PTT_TIME_BUDGET=60                   # PTT 即時爬時間預算（秒）
PTT_MIN_DELAY=0.5 / PTT_MAX_DELAY=1.0  # PTT 禮貌限速

# --- 個人化長期記憶：事實（僅登入者；fail-safe）---
USER_MEMORY_ENABLED=true / USER_MEMORY_COLLECTION=user_memory
USER_MEMORY_TOP_K=3 / USER_MEMORY_MIN_SCORE=0.35            # 撈回條數 / 召回門檻

# --- 脈絡記憶（thread / episodic；登出存整場敘事，相關問題重載；僅登入者）---
USER_THREAD_ENABLED=true            # 關掉即不存/不重載脈絡敘事
USER_THREAD_TOP_K=2 / USER_THREAD_MIN_SCORE=0.42           # 重載筆數 / 召回門檻（比事實高）
USER_THREAD_MAX_CHARS=1200          # 單筆敘事注入 prompt 的長度上限

# --- 使用者偏好自動推論（登出時萃取設定旋鈕 → user_preference；比長期記憶保守）---
PREF_INFER_ENABLED=true              # 關掉即不自動推論偏好
PREF_INFER_MIN_CONFIDENCE=0.75       # 只有信心 >= 此值才寫入（越高越保守）

# --- 後台 MySQL 整合（DB_HOST 留空＝停用整合，只跑 runtime）---
DB_HOST=127.0.0.1 / DB_NAME=crawl_agent
DB_USER=crawl_ro   / DB_PASSWORD=...      # 唯讀帳號（讀設定 / 偏好）
DB_RW_USER=crawl_rw / DB_RW_PASSWORD=...  # 讀寫帳號（寫 conversation / message / user_preference）
CONFIG_CACHE_TTL=30                  # runtime 設定快取秒數
TOKEN_SECRET=...                     # 與 admin_backend/.env 同值：驗證終端登入 token
ADMIN_API_URL=http://localhost:8000  # /demo 頁的登入/註冊由 runtime 伺服器端轉發到這裡（避開 CORS）
```

---

## 9. 怎麼再加一個平台（如 Mobile01 / 巴哈 / LIHKG）

1. 寫一個 adapter：在 `sources.py` 新增 `Source` 子類，實作 `fetch(query) -> list[Post]`（Post 標 `source="平台名"`）。
2. 加進 `REGISTRY`（或在後台 `source_platform` 開一筆，設 `adapter_key`、`is_active`）。
3. （前端可選）在 UI 的來源分組加該平台標籤。

agent / tools / prompt 都不用動——這就是 registry 的用意。

---

## 10. 開發里程碑（as-built）

| 里程碑 | 內容 |
|---|---|
| M0–M2 | Django 骨架 + MySQL；模組二 auth/RBAC；模組一 agent/skill/source CRUD + seed |
| M3 | FastAPI 讀取層（`ConfigRepository`）+ 快取 → runtime 吃 DB 設定 |
| M4–M5 | 模組三對話落地 + 檢視 API；模組四 system_setting + user_preference + 取值優先序 |
| M6 | 後台前端（先用 Django Admin / DRF browsable API） |
| 追加 | 終端登入（Django 發 token / runtime 驗）、使用者長期記憶、登出摘要、**偏好自動推論** |
| 追加 | **WebSocket 即時前端**：`progress.py` 事件匯流排、`/ws/ask` 逐字串流、中途取消、`/demo` 頁與登入代理 |
| 追加 | **對話還原**：`GET /me`、`GET /conversation/{sid}`、`GET /conversations`（`memory_store.load_history`）；`/demo` 頁 sid 進網址 + `localStorage` 備份 → F5／換裝置接得回上下文（落地的 `message` 表從此不再是唯寫） |

> ✅ **as-built**：M0–M6 與個人化記憶／偏好推論皆通過真實 LLM 端到端驗證（後台改 prompt→reload→回答變、`community_search` 雙來源、對話落地、登入後偏好過濾平台 + 對話歸戶、登出摘要與偏好推論）。
>
> ✅ **WebSocket 路徑**：事件流、逐字串流、取消（0.16s 內收到 `cancelled`）、同連線多輪、登入代理與 `/ask` 不回歸，皆以自動化測試對真實 uvicorn 驗證過（agent 以 stub 替換）。
>
> ✅ **`chat_with_tools_stream()` 已對真實 Azure OpenAI 驗證**（`stream=True` + tools）：常識題正確不呼叫工具並逐字串流；需查題在串流中組出 `community_search` 的 tool_call，分片累積出的 `arguments` 為合法 JSON。長答案實測 305 片 token 於 1.67s 內陸續到達（確為串流、非一次回傳）。備註：此端點首片延遲約 7s（模型/gateway 起始等待，非程式問題）——正好凸顯進度推播的價值。

---

<!-- ## 11. 本版不做與關鍵決議 -->

<!-- **Out of Scope**：方案 B「越用越強」累積記憶（`QdrantHotStore` 預留）、從後台重建 Qdrant 向量庫、多租戶、新平台 adapter 實作（schema 已預留）。 -->

<!-- **關鍵決議**：① 終端登入用自管帳密（SSO 為 drop-in）；② 對話歷史收斂到 DB 單一真相（先只寫、後改讀，避免雙真相漂移）；③ 設定生效用短 TTL 快取 + 重載端點；④ 後台前端先用 Django Admin 過渡、DRF API 同步建；⑤ 入口 Django 8000 + Streamlit 側邊欄連結。 -->

> 完整規格（欄位、API、決議脈絡）見 [`docs/admin_backend_spec.md`](docs/admin_backend_spec.md)。
