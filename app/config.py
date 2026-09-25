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

    # ---- 使用者脈絡記憶（thread / episodic；登出時把整場對話存成有脈絡敘事，相關問題重載）----
    # 與原子事實並存：事實管點狀召回，thread 管脈絡重載。只記使用者處境/目標/提問走向，不記世界結論。
    user_thread_enabled: bool = os.environ.get(
        "USER_THREAD_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    user_thread_top_k: int = _int("USER_THREAD_TOP_K", 2)
    # 門檻比事實（0.35）高：整段敘事要夠對題才注入，避免鬆散舊脈絡灌爆 system prompt
    user_thread_min_score: float = _float("USER_THREAD_MIN_SCORE", 0.42)
    user_thread_max_chars: int = _int("USER_THREAD_MAX_CHARS", 1200)  # 注入單筆敘事長度上限（含討論重點梗概故拉高）
    # 命中脈絡時，是否請模型『開場先回顧一兩句』而非只當背景默讀（溫故知新；主體仍以本次查到的為準）。
    # 與 user_thread_enabled 不同層級：那個關掉整條脈絡軌，這個只關『說出來』、保留背景影響。
    thread_recap_enabled: bool = os.environ.get(
        "THREAD_RECAP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

    # ---- 使用者偏好自動推論（登出時從對話萃取設定旋鈕 → user_preference；比 user_memory 保守）----
    pref_infer_enabled: bool = os.environ.get(
        "PREF_INFER_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    pref_infer_min_confidence: float = _float("PREF_INFER_MIN_CONFIDENCE", 0.75)

    # ---- 答案稽核違規時自動重寫（agent._audit_retry；只用在非串流的 /ask）----
    # 只在違規時多一次 LLM 呼叫，正常答案零成本，所以預設開。
    audit_retry_enabled: bool = os.environ.get(
        "AUDIT_RETRY_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

    # ---- 跨平台重排（reranker.py：三平台合併後依相關度重排、只留前 N 則）----
    # 預設開，依據是 dataset 評測 A/B（9 題，2026-09-24）：faithfulness 100% → 100%、
    # 送進回答模型的字數 −64%、耗時 +3%；論點數 −30%，人工比對確認少掉的是離題內容，
    # 答案沒有變單薄。要關就設 RERANK_ENABLED=false。
    rerank_enabled: bool = os.environ.get(
        "RERANK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    rerank_backend: str = os.environ.get("RERANK_BACKEND", "llm").strip().lower()  # llm | cross_encoder
    rerank_model: str = os.environ.get("RERANK_MODEL", "").strip()  # 留空＝CHAT_MODEL；可指定便宜模型
    rerank_top_n: int = _int("RERANK_TOP_N", 30)                    # 最多留幾則；不超過這數量就不重排
    rerank_min_score: float = _float("RERANK_MIN_SCORE", 2.0)       # 0～3 分，低於此分不補位
    rerank_per_platform_min: int = _int("RERANK_PER_PLATFORM_MIN", 3)  # 每個平台至少保留幾則（>0 分）

    # ---- 忠實度放行關卡（agent._faithfulness_gate；只用在非串流的 /ask）----
    # 答案送出前先讓 judge 核對引用，不符的句子請模型修正、仍不過就刪句。
    # 每題多一次 judge 呼叫（約 5～10 秒），所以預設關，A/B 確認後再開。
    faith_gate_enabled: bool = os.environ.get(
        "FAITH_GATE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    # 串流版（/ws/ask，/demo 前端用的就是這條）也套用：查到來源後的答案先扣住，跑完稽核重寫
    # 與上面的關卡才分段送出。第一個字會晚約十秒出現，所以與 FAITH_GATE_ENABLED 分開控制。
    faith_gate_stream: bool = os.environ.get(
        "FAITH_GATE_STREAM", "false").strip().lower() in ("1", "true", "yes", "on")
    # 要處理哪些判定：contradicted＝跟原文相反（最嚴重）；加上 not_found＝原文沒提到也處理。
    faith_gate_verdicts: tuple[str, ...] = tuple(
        v.strip() for v in os.environ.get("FAITH_GATE_VERDICTS", "contradicted").split(",")
        if v.strip())

    # ---- 忠實度量測（faithfulness.py：被引用的原文是否支持論點）----
    # 線上抽樣率：0＝關閉。每抽中一題多一次 LLM 呼叫（背景執行，不影響回應時間），開多少由人決定。
    faith_sample_rate: float = _float("FAITH_SAMPLE_RATE", 0.0)
    # 交給 judge 的原文長度：總預算平均分給被引用的那幾則（單則上限 8000），但每則至少 FAITH_SOURCE_CHARS。
    # 引用少就讀長一點（避免論點在後段被誤判 not_found），引用多就維持短版（避免 prompt 暴增）。
    faith_source_chars: int = _int("FAITH_SOURCE_CHARS", 1500)
    faith_source_budget: int = _int("FAITH_SOURCE_BUDGET", 30000)
    # NLI 初篩（選配，需另裝 transformers + torch）：留空＝不用，全交 LLM。
    # 建議 MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7（多語、含中文）。
    faith_nli_model: str = os.environ.get("FAITH_NLI_MODEL", "").strip()
    # NLI 蘊含機率 >= 此值才直接放行；訂得高是因為 NLI 讀不懂反諷，只讓它放行「很有把握」的。
    faith_nli_pass: float = _float("FAITH_NLI_PASS", 0.9)

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

    # ---- Dcard 即時爬（DrissionPage / DcardLiveSource）----
    # DCARD_MODE=live → 走 DrissionPage 即時爬（全站『文章』搜尋），失敗/空自動 fallback 向量庫；
    # DCARD_MODE=vector → 走 Dcard 口碑庫向量檢索（原本行為，程式碼保留不動）。
    dcard_mode: str = os.environ.get("DCARD_MODE", "live").strip().lower()
    dcard_time_budget: int = _int("DCARD_TIME_BUDGET", 200)   # 秒；即時爬硬上限，到時回已抓到的（須 < 前端 300s）
    dcard_deep_max: int = _int("DCARD_DEEP_MAX", 18)          # 最多深挖幾篇（進頁抓內文+留言）
    dcard_max_comments: int = _int("DCARD_MAX_COMMENTS", 20)  # 每篇留言取前幾則（依讚數，濾掉貼圖/純網址後）
    dcard_headless: bool = os.environ.get(
        "DCARD_HEADLESS", "0").strip().lower() in ("1", "true", "yes", "on")  # 有頭才過得了 Cloudflare
    dcard_cookie: str = os.environ.get("DCARD_COOKIE", "").strip()            # 選填：cf_clearance 等整串 cookie
    dcard_user_agent: str = os.environ.get("DCARD_USER_AGENT", "").strip()    # 選填：須與 cookie 同一瀏覽器
    dcard_user_data_dir: str = os.environ.get("DCARD_USER_DATA_DIR", "").strip()  # 持久設定檔：養 cf_clearance 跨次重用
    dcard_cf_timeout: int = _int("DCARD_CF_TIMEOUT", 90)         # 秒；等 Cloudflare 挑戰解開逾時
    dcard_request_timeout: int = _int("DCARD_REQUEST_TIMEOUT", 25)  # 秒；等單一頁面/回應逾時
    dcard_min_delay: float = _float("DCARD_MIN_DELAY", 1.0)      # 秒；捲動/翻頁間隔下限
    dcard_max_delay: float = _float("DCARD_MAX_DELAY", 2.0)      # 秒；捲動/翻頁間隔上限
    dcard_post_delay_min: float = _float("DCARD_POST_DELAY_MIN", 1.5)  # 秒；每篇貼文之間停頓下限（降 CF 觸發）
    dcard_post_delay_max: float = _float("DCARD_POST_DELAY_MAX", 3.0)  # 秒；每篇貼文之間停頓上限
    dcard_scroll_stale_limit: int = _int("DCARD_SCROLL_STALE_LIMIT", 5)   # 搜尋頁連續幾次捲動無新資料就停
    dcard_comment_stale_limit: int = _int("DCARD_COMMENT_STALE_LIMIT", 2)  # 留言連續幾次捲動無新就停（壓延遲）
    # 每篇最多掃描幾則留言就停：熱門文動輒數百則，但我們只取前 dcard_max_comments 則熱門，
    # 掃到這個量已足以涵蓋高讚留言，不必捲完全部（否則一篇熱門文就吃光整個時間預算）。
    dcard_comment_scan_max: int = _int("DCARD_COMMENT_SCAN_MAX", 80)

    # Dcard 即時爬：搜到結果後對每篇 title+excerpt 做 embed，跟原問題比 cosine 相似度，
    # 過門檻才進深挖。避免 Dcard 全文檢索的部分匹配 fallback
    # （例如搜「福智教育園區」拿回一堆「幼兒園評價」）。
    dcard_live_rerank_enabled: bool = os.environ.get(
        "DCARD_LIVE_RERANK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    dcard_live_min_score: float = _float("DCARD_LIVE_MIN_SCORE", 0.5)

    # PTT 即時爬：抓完後對每篇 title+body 前段做 embed，跟原問題比 cosine 相似度，
    # 過門檻才收。避免 LLM 抽關鍵字時把「看法/評價/心得」這種無主體泛詞單獨搜、
    # 撈回一堆與問題無關的貼文（如問「福智教育看法」卻拿到「周星馳的評價」）。
    #
    # 門檻從 0.5 下修到 0.30，因為 0.5 從來沒有被真正執行過：舊版 rerank 用 p.title 存取
    # Post，但 Post 是 TypedDict（＝dict），屬性存取必定拋 AttributeError 並被 except 吞掉、
    # 回傳未過濾的原清單。修好後實測「輝達進駐北士科」49 篇的分數是
    # 最高 0.448 / 中位 0.356 / 最低 0.179——沿用 0.5 會一篇不留（那 49 篇全是對題的輝達文）。
    # 0.30 保留 42 篇，同時仍能擋掉真正的雜訊（周星馳那類離題文分數約 0.1~0.2）。
    ptt_rerank_enabled: bool = os.environ.get(
        "PTT_RERANK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    ptt_min_score: float = _float("PTT_MIN_SCORE", 0.30)

    # ---- Threads 即時爬（httpx 讀 SSR JSON，免登入、不必開瀏覽器；ThreadsSource）----
    # 預算 90 秒看起來很長，但三平台是並行的、總時長取 max()，而 Dcard 就要 200 秒——
    # 所以這條放到 90 秒完全不會增加使用者感受到的等待。
    threads_time_budget: int = _int("THREADS_TIME_BUDGET", 90)
    # broad（無參數，約 60 筆／近三週）｜recent（serp_type=default，約 20 筆／最近 1~2 天）。
    # 預設 broad：即時問答沒有「每天累積時間窗」的機會，一次就要夠量，精準度交給語意過濾。
    threads_mode: str = os.environ.get("THREADS_MODE", "broad").strip().lower()
    threads_max_posts: int = _int("THREADS_MAX_POSTS", 20)     # 最終回傳上限（避免壓過其他平台）
    # strict：只留本文真的含關鍵字的貼文。broad 模式會夾帶「字面沒命中但被平台判定相關」的
    # 貼文，實測搜「輝達／北士科」會混進森田輝（日本藝人）、北科大——它們靠單字重疊擠進來，
    # 語意分數還偏高，拉門檻擋不掉，只有字面檢查擋得住。輿情統計要的是乾淨樣本，故預設開。
    threads_strict: bool = os.environ.get(
        "THREADS_STRICT", "true").strip().lower() in ("1", "true", "yes", "on")
    threads_expand_max: int = _int("THREADS_EXPAND_MAX", 10)   # 最多展開幾篇的回覆串（上限非保證）
    threads_min_replies: int = _int("THREADS_MIN_REPLIES", 1)  # 至少幾則回覆才值得展開
    threads_max_replies: int = _int("THREADS_MAX_REPLIES", 15)  # 每篇取前幾則回覆（依讚數）
    # 禮貌限速：不帶登入憑證，最壞是 IP 被限流。原 threads_watch 預設 3 秒，這裡壓到 1.5
    # 才能在預算內展開 10 篇；不要再往下調。
    threads_min_interval: float = _float("THREADS_MIN_INTERVAL", 1.5)
    threads_jitter: float = _float("THREADS_JITTER", 1.0)
    threads_request_timeout: int = _int("THREADS_REQUEST_TIMEOUT", 20)
    # 語言過濾：Threads 搜尋是全球的（搜 OpenAI 回 49 筆裡 zh_TW 只有 1 筆），
    # 不濾的話「台灣網友怎麼看」會混進一半英文貼文。
    threads_lang_filter: bool = os.environ.get(
        "THREADS_LANG_FILTER", "true").strip().lower() in ("1", "true", "yes", "on")
    # 語意過濾：Threads 只能單詞查、又不做交集（「OpenAI」「越獄」只能各搜再合併），
    # 所以這關不是保險而是唯一的相關性機制。
    # 門檻 0.30 是實測訂的，不能沿用 PTT/Dcard 的 0.5：Threads 貼文又短又破碎（常只有一兩句
    # 加表情符號），短文本對長問句的 cosine 天生偏低。實測「輝達進駐北士科」那題 44 篇的
    # 分數是 最高 0.407 / 中位 0.278 / 最低 0.111——用 0.45 會一篇都不留，0.30 留 16 篇。
    threads_rerank_enabled: bool = os.environ.get(
        "THREADS_RERANK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    threads_min_score: float = _float("THREADS_MIN_SCORE", 0.30)

    # ---- 立場統計（stance_breakdown）----
    # 追問「畫圖」時沿用上一輪貼文的語意門檻：同一場對話問過多個話題時，「上一輪」不保證
    # 就是使用者要統計的那一輪。實測兩批真實貼文對議題句「慈濟被詐騙10億這件事」的分數——
    # 對題 0.387~0.719、離題（輝達）0.163~0.355，中間有空隙，取 0.35 對題全留、離題幾乎全丟。
    # （比各平台爬蟲的門檻高，因為 issue 是『陳述句』，embedding 品質比問句好得多。）
    stance_reuse_min_score: float = _float("STANCE_REUSE_MIN_SCORE", 0.35)

    # ---- 追問建議（follow-up；當輪答完後產生，前端點了填入輸入框可改再送）----
    suggest_enabled: bool = os.environ.get(
        "SUGGEST_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    suggest_n: int = _int("SUGGEST_N", 4)                            # 產幾題
    suggest_temperature: float = _float("SUGGEST_TEMPERATURE", 0.4)  # 要一點變化但別發散
    # 個人化：把本輪已撈回的使用者記憶（agent 那次 recall 的結果，不另外搜）交給建議器選面向。
    # 只影響「挑哪個角度」，不准把記憶內容寫進題目——chip 是螢幕上常駐可見的 UI，複述記憶
    # 等於把個人事實攤在畫面上（截圖／共用螢幕會外洩）。匿名使用者無記憶 → 自動退回通用行為。
    suggest_personalize: bool = os.environ.get(
        "SUGGEST_PERSONALIZE", "true").strip().lower() in ("1", "true", "yes", "on")
    suggest_personalize_max: int = _int("SUGGEST_PERSONALIZE_MAX", 1)  # 至多幾題個人化（其餘保持通用）

    # ---- Langfuse prompt 管理（可觀測性平台那邊改 prompt，不必改 code 重啟）----
    # 取值優先序刻意是「後台 agent > Langfuse > 本檔寫死值」：後台的 prompt 管理是既有的
    # 產品功能（M3），Langfuse 不該把它蓋掉，只補上『後台沒設時』那一格，並帶來版本歷史
    # 與 diff。關掉（或 Langfuse 連不上）就完全退回原本行為。
    langfuse_prompt_enabled: bool = os.environ.get(
        "LANGFUSE_PROMPT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    langfuse_prompt_name: str = os.environ.get("LANGFUSE_PROMPT_NAME", "seiqa-system").strip()
    # 用哪個標籤的版本：production＝Langfuse UI 上標記為正式的那一版（可隨時回滾）
    langfuse_prompt_label: str = os.environ.get("LANGFUSE_PROMPT_LABEL", "production").strip()
    # 本地快取秒數：避免每題都打一次 Langfuse。改了 prompt 最多等這麼久才生效。
    langfuse_prompt_ttl: int = _int("LANGFUSE_PROMPT_TTL", 60)

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
    # Django 後台位址：/demo 頁的登入/註冊由 runtime 伺服器端轉發過去（見 api.py demo_auth）
    admin_api_url: str = os.environ.get("ADMIN_API_URL", "http://localhost:8000").strip()


settings = Settings()
