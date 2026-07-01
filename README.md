# 社群輿情智能問答

> 專案資料夾 / 技術識別名仍為 `Crawl_Agent`（DB、collection、路徑沿用）。

tool-calling 的 **多平台社群口碑問答 Agent**。借鑑 Hermes Agent「LLM 用 tool calling 自己決定何時呼叫外部工具」的模式，但用既有 Azure OpenAI 技術棧原生實作（不引入 Hermes 平台）。

> 你問「遠距離戀愛可以維持嗎？」→ Agent 判斷需要鄉民口碑 → 呼叫 `community_search` →
> **同時**查 Dcard 口碑庫（向量庫）＋ 即時爬 PTT → 綜合兩邊、帶分平台出處回答。
> 問「一年有幾個月？」→ 判斷不需查 → 直接用常識回答（🟡 黃燈）。

## 系統組成（三個服務）

| 服務 | 埠 | 角色 |
|---|---|---|
| FastAPI runtime（`app/`）| 8001 | 跑 agent、`/ask` 問答、對話落地 MySQL、個人化長期記憶 |
| Streamlit 聊天前端（`ui/`）| 8501 | 終端使用者問答、🟢/🟡 燈號、登入 / 登出 |
| Django + DRF 後台（`admin_backend/`）| 8000 | 設定 / 帳戶 / 對話管理，**MySQL schema 唯一擁有者** |

- **一鍵起全部**：專案資料夾執行 `.\start.ps1`（同時起 8000/8001/8501；關掉前端會一併收掉另外兩個）。
- runtime 用**唯讀帳號**讀後台設定（人設 prompt / 模型 / 平台開關 / 檢索門檻…），改設定不必改程式碼；後台掛掉時 runtime 自動 fallback 到 `.env`／預設值照常跑。
- 後台完整規格（資料表、API、RBAC、記憶三層、登入/登出流程）見 [`docs/admin_backend_spec.md`](docs/admin_backend_spec.md)。

## 架構：一個 skill，多個來源 adapter（registry + 並行 fan-out）

對 LLM **只暴露一個工具** `community_search`；底下掛一個 **來源 registry**，每個平台是一個 adapter，把該平台包成統一的 `fetch(query) -> list[Post]`。查詢時**並行 fan-out** 到所有 adapter、合併結果（每篇帶 `source` 平台標籤）。

**加新平台＝在 `sources.py` 多寫一個 adapter、加進 `REGISTRY` 即可——agent / loop / prompt 全部不動。** 這也保證「每次兩邊都查」是程式層保證的，不靠 LLM 自己記得同時叫多個工具。

目前的 adapter：

| adapter | 平台 | 方式 | 反爬 |
|---|---|---|---|
| `DcardSource` | Dcard | 查向量庫 `dcard_insight`（語意檢索，唯讀） | 無（離線已建庫） |
| `PttSource` | PTT | 即時爬站內搜尋（時間預算內邊翻邊抓） | 無 Cloudflare，帶 `over18` cookie 即可 |

> Dcard 為何不即時爬：站內搜尋會被 Cloudflare 阻擋，故改查已建好的向量庫。即時爬程式碼保留在 `crawler.py` / `tools.py` 的 `_crawl_dcard`（已停用）。

## 設計決策

- **Dcard＝向量庫（唯讀）＋多面向檢索**：問句 embedding 後查 `dcard_insight`（3529 筆、1536 維 / Cosine）。先請 LLM 改寫成數條「鄉民用詞」面向查詢（`SEARCH_EXPAND_N`），各查一次後 **round-robin 合併**，避免單一稠密向量被強勢詞綁架；低於門檻（`SEARCH_MIN_SCORE`）視為不夠對題。
- **PTT＝即時爬＋時間預算**：LLM 一次決定（看板 + 多個『單一關鍵詞』）——整句問句搜不到、且 PTT 多詞是 AND 比對標題（「外型 情緒穩定」→ 0 筆），故抽成多個單詞各搜一次再合併。翻搜尋頁『邊翻邊抓』，到 `PTT_TIME_BUDGET`（預設 60s）就停、回已抓到的全部（符合的都抓、不設固定篇數）；全程禮貌限速避免被 ban。
- **檢索快取＝方案 A（預設）**：查到的社群資料只進**當次 session 記憶體**（`SessionFreshStore`），用完即丟。業務只認 `FreshStore` 抽象。（完整記憶設計見下方「三層記憶」）
- **燈號＝來源透明**：回答若**有撈到社群討論**→ 🟢 綠燈＋標各平台則數（Dcard X 則 / PTT Y 則）＋來源 `[n]`；**都沒撈到（或不需查）**→ 🟡 黃燈，誠實標示為 LLM 既有常識。
- **fail-safe**：任一平台（embed/Qdrant/PTT）失敗只少那一邊，不影響其他來源；兩邊都空就退回常識回答。
- **工具＝skill**：`tools.py` 的 `description` 寫清楚「何時該用」＝觸發條件，等同 Hermes skill 的 front-matter trigger。

## 三層記憶（各司其職）

| 層 | 存哪 | 存什麼 | 生命週期 |
|---|---|---|---|
| 檢索快取（方案 A）| 程序記憶體 `SessionFreshStore` | 當次查到的社群貼文 | 當次 session，用完即丟 |
| 對話紀錄 | 後台 MySQL `conversation` / `message` | 每輪 Q/A（`memory_store.persist_turn`）| 落地保存、可在後台檢視；登出軟刪 |
| 使用者長期記憶 | Qdrant `user_memory` collection | LLM 萃取的「關於使用者本人的長期事實」（`user_memory.py`）| 跨 session 保留，**僅登入者** |

- **只有登入使用者**才有長期記憶與對話歸戶；匿名照常能問答，但不留記憶。全程 fail-safe：記憶那層炸掉也不影響回答。
- 為何長期記憶存「事實」而非「答案」：答案來自會過時的社群輿情，只記穩定的「關於這個人」的事實（例：使用者是後端工程師、正在找工作），避免把過時結論當記憶。
- **登入 / 登出**：Streamlit 呼叫 Django `end-auth` 拿 JWT，聊天時帶 `Authorization: Bearer`；runtime 用與 Django 共用的 `TOKEN_SECRET` 驗證取得 `end_user_id`。**登出**（`POST /logout`）會把整段對話摘要成長期事實寫入 `user_memory`，再**軟刪**這段對話原始紀錄。

## 結構

```
app/
  config.py       # .env 設定（LLM / Qdrant / 口碑庫 / PTT / MySQL / user_memory）
  llm.py          # LLM 客戶端：chat() / chat_with_tools() / embed() / expand_queries()
  vectorstore.py  # Dcard 口碑庫向量檢索（Qdrant REST，多面向 + 門檻）
  ptt.py          # PTT 即時爬蟲（requests + bs4，over18 + 時間預算 + 限速 + 重試）
  sources.py      # 來源 registry：Source 抽象 + DcardSource/PttSource + 並行 fan-out
  crawler.py      # Dcard 即時爬轉接層（已停用；Post 結構定義在此）
  store.py        # FreshStore 抽象 + SessionFreshStore(A) + QdrantHotStore(B 預留)
  tools.py        # 單一 skill：community_search（分派到 sources.community_search）
  agent.py        # 規劃→工具→行動 的多輪 loop
  config_repo.py  # ConfigRepository：唯讀讀後台 MySQL 設定（短 TTL 快取 + reload）
  memory_store.py # 每輪對話落地 MySQL（crawl_rw 帳號，fail-safe）
  user_memory.py  # 使用者長期語意記憶（Qdrant user_memory：萃取事實 → 注入 prompt）
  auth.py         # 驗證 Django 簽發的終端 token → end_user_id（失敗即匿名）
  api.py          # FastAPI：/ask、/logout、/health、/internal/reload-config
ui/
  streamlit_app.py  # 聊天前端（🟢/🟡 燈號 + 來源分組 📘Dcard / 📗PTT + 登入/登出）
admin_backend/    # Django + DRF 後台（MySQL schema 擁有者；規格見 docs/admin_backend_spec.md）
start.ps1         # 一鍵起 Django(8000) + FastAPI(8001) + Streamlit(8501)
```

## 前置

- **Qdrant 口碑庫**：`QDRANT_URL`（預設 `http://localhost:7333`）跑著、`dcard_insight` 已有資料、`.env` 的 `EMBED_MODEL` 與建庫時同一個模型（`text-embedding-3-small`，1536 維）。
- **PTT**：無需額外設定（公開站、`requests + bs4`）。

## 快速開始

**只跑問答 runtime（最小；不需 MySQL / 後台）**

```powershell
python -m venv .venv; .venv\Scripts\activate     # Windows
pip install -r requirements.txt
# 建立 .env（本專案無範本檔），至少填 LLM_API_KEY / Azure endpoint / QDRANT_URL（見下方參數）
uvicorn app.api:app --reload --port 8001
```

> `DB_HOST` 留空＝停用後台整合，runtime 全走 `.env`／預設值。

**完整系統（後台 + runtime + 前端，一鍵）**

需另備 MySQL（後台 schema）與第二個 venv `.venv-admin`（Django）：

```powershell
python -m venv .venv-admin
.venv-admin\Scripts\pip install -r requirements-admin.txt
.venv-admin\Scripts\python admin_backend\manage.py migrate   # 後台 seed / DB 帳號見 docs/admin_backend_spec.md §9、§11
.\start.ps1                                                  # 起 Django 8000 + FastAPI 8001 + Streamlit 8501
```

測試（三選一）：

**A. Swagger UI**：開 http://localhost:8001/docs → 點 `/ask`。

**B. curl**：
```bash
curl -X POST http://localhost:8001/ask -H "Content-Type: application/json" ^
  -d "{\"message\":\"遠距離戀愛可以維持嗎？\",\"session_id\":\"s1\"}"
```

**C. Streamlit 前端**（另開終端，API 要先在跑）：
```bash
streamlit run ui/streamlit_app.py
```
回覆上方標「🟢 來自社群討論：Dcard X 則 / PTT Y 則」或「🟡 LLM 既有常識回答」；最下面來源依平台分組顯示。

> 注意：含 PTT 即時爬，一題最久 ≈ `PTT_TIME_BUDGET` 秒（預設 60s），比純查向量庫慢。

## 相關 .env 參數

```
EMBED_MODEL=text-embedding-3-small   # 須與建 dcard_insight 時同一個模型
QDRANT_URL=http://localhost:7333
INSIGHT_COLLECTION=dcard_insight     # Dcard 口碑庫 collection
SEARCH_TOP_K=5                       # Dcard 向量檢索回傳幾則（url 去重後）
SEARCH_EXPAND_N=3                    # 多面向查詢改寫條數
SEARCH_MIN_SCORE=0.5                 # 相似度門檻（低於→不夠對題）
PTT_TIME_BUDGET=60                   # PTT 即時爬時間預算（秒）
PTT_MIN_DELAY=0.5 / PTT_MAX_DELAY=1.0  # PTT 禮貌限速

# --- 個人化長期記憶（僅登入者；fail-safe）---
USER_MEMORY_ENABLED=true             # 關掉即完全不啟用使用者長期記憶
USER_MEMORY_COLLECTION=user_memory   # Qdrant collection（首次自動建，1536 維）
USER_MEMORY_TOP_K=3                  # 每次撈回幾條使用者事實注入 prompt
USER_MEMORY_MIN_SCORE=0.35           # 記憶召回相似度門檻

# --- 後台 MySQL 整合（DB_HOST 留空＝停用整合，只跑 runtime）---
DB_HOST=127.0.0.1                    # 後台共用 MySQL
DB_NAME=crawl_agent
DB_USER=crawl_ro   / DB_PASSWORD=... # 唯讀帳號（讀設定 / 偏好）
DB_RW_USER=crawl_rw / DB_RW_PASSWORD=...  # 讀寫帳號（只寫 conversation / message）
CONFIG_CACHE_TTL=30                  # runtime 設定快取秒數
TOKEN_SECRET=...                     # 與 admin_backend/.env 同值：驗證終端登入 token
```

## 怎麼再加一個平台（如 Mobile01 / 巴哈 / LIHKG）

1. 寫一個 adapter：在 `sources.py` 新增一個 `Source` 子類，實作 `fetch(query) -> list[Post]`（Post 記得標 `source="平台名"`）。
2. 加進 `REGISTRY`。
3. （前端可選）在 UI 的來源分組加該平台的標籤。

agent / tools / prompt 都不用動——這就是 registry 的用意。各平台的反爬差異見備忘：Mobile01 是 Akamai（要 Playwright）、巴哈是 Cloudflare（可複用 Dcard 那套）、PTT/痞客邦最輕。
