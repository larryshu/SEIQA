"""Threads 即時爬蟲（ThreadsSource 的實作）。

免登入：Threads 對 crawler UA 會直接吐出含 `thread_items` 的 SSR JSON，而且搜尋頁
`/search/?q=` 與貼文頁的結構相同，所以同一個 parser 兩邊通用。純 httpx、不必開瀏覽器
（比 Dcard 的 DrissionPage 輕得多，也沒有 Cloudflare 要過）。

平台特性（2026-08-07 實測），這五點決定了整個抓取策略：

1. **搜尋只吃「單一詞」**。任何含空白的 query 一律回 0 筆——不管編碼成 `+` 還是 `%20`，
   連「台灣 美食」都是 0。也不是子字串比對：「AI越獄」0 筆，但「越獄」51 筆。
   → 跟 PTT 殊途同歸（那邊是多詞 AND 比對標題），**只能拆成單詞各搜一次再合併**；
   「A 且 B」這種交集在 Threads 上查不到，別指望用組合詞收斂主題。
2. **搜尋是全球的，沒有地區／語言過濾**。搜「OpenAI」回 49 筆裡 `zh_TW` 只有 1 筆；
   搜「輝達」回 51 筆裡有 41 筆。→ 必須自己濾非中文，否則「台灣網友輿論」會被英文貼文稀釋。
3. **`lang` 欄位大量從缺**（那 49 篇有 47 篇是 None）→ 語言判斷不能只靠它，要用字元兜底。
4. 綜合 1~3：**Threads 是「實體驅動」平台**——問題核心是專有名詞（慈濟、輝達、館長）時
   表現很好；是組合概念（AI 越獄 ∩ 資安）時幾乎撈不到。故抽詞 prompt 只要核心實體。
5. SSR 沒有 `end_cursor`，單次上限就是幾十筆；貼文頁首屏也只給前 10~16 則回覆。對即時
   問答夠用（10 篇 × 10~16 則 ≈ 100~160 則回覆），不值得為此做捲動分頁。

**回覆的處理**：Threads 內部不分主文與留言，兩者都是 post。若原樣回傳，一則兩行的回覆
會各佔一個 `[n]`，前端來源清單爆量、`stance_breakdown` 也會被單一熱門串灌爆。故在本檔
收斂成「一則主文 = 一個 Post，回覆折進 content」，與 PTT 的「熱門推文」、Dcard 的
「熱門留言」完全同構——粒度對齊後，前端與統計都不必為 Threads 特別處理。

**已知風險**：crawler UA 是本檔能免登入運作的關鍵，也是最脆弱的一環（Meta 改掉 SSR
就整條失效）。全程 fail-safe 回空，由 sources._safe_fetch 保證「只少這一邊」。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from . import llm, progress, relevance
from .config import settings
from .crawler import Post

log = logging.getLogger("threads")

_SEARCH_URL = "https://www.threads.com/search/"
# 一般瀏覽器 UA 拿到的是空殼（內容靠前端 GraphQL 補），crawler UA 才會拿到 SSR payload
_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# 搜尋模式：recent 精準但只有最近 1~2 天約 20 筆；broad 涵蓋近三週約 60 筆但夾帶語意相關。
# 預設 broad——即時問答沒有「每天跑一次累積時間窗」的機會，一次就要拿到夠用的量，
# 精準度改由後面的語意過濾把關（這與原 threads_watch 專案的建議相反，理由見上方 4.）。
_MODES: dict[str, dict[str, str]] = {"recent": {"serp_type": "default"}, "broad": {}}

_TAIPEI = timezone(timedelta(hours=8))
_MAX_RETRIES = 3

_SJS_RE = re.compile(r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', re.DOTALL)
_MARKER = "thread_items"

_CJK = re.compile(r"[一-鿿]")
_KANA = re.compile(r"[぀-ヿ]")     # 日文假名
_HANGUL = re.compile(r"[가-힯]")   # 韓文諺文
_MIN_CJK_CHARS = 4   # lang 從缺時，本文至少要有這麼多漢字才當成中文貼文
_MIN_TEXT_LEN = 15   # 太短的貼文（「Tq chatgpt 💪」）沒有輿情價值，直接丟


@dataclass
class _Item:
    """Threads 原始 post 收斂後的中間形態（尚未變成 crawler.Post）。"""

    pk: str
    code: str
    username: str
    text: str
    taken_at: int
    like_count: int
    reply_count: int
    is_reply: bool
    lang: str | None

    @property
    def permalink(self) -> str:
        return f"https://www.threads.com/@{self.username}/post/{self.code}"


# ---------------------------------------------------------------- 抓取

class _Throttle:
    """禮貌限速：兩次請求間隔 min_interval 起跳、加隨機抖動。

    這層完全不帶登入憑證，沒有帳號停權風險；最壞情況是 IP 被限流——所以間隔可以比
    原 threads_watch 的 3 秒短（要在時間預算內展開 10 篇），但不要再往下調。
    """

    def __init__(self) -> None:
        self._last = 0.0

    def wait(self) -> None:
        gap = settings.threads_min_interval + random.uniform(0, settings.threads_jitter)
        elapsed = time.monotonic() - self._last
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last = time.monotonic()


def _session() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=settings.threads_request_timeout,
    )


def _get(client: httpx.Client, url: str) -> str | None:
    """GET 帶退避重試；429/5xx 重試，其他狀況一律回 None（上層當作這頁沒有資料）。"""
    for attempt in range(_MAX_RETRIES):
        try:
            r = client.get(url)
        except httpx.HTTPError as e:
            log.warning("第 %d 次請求 %s 失敗：%s", attempt + 1, url, e)
        else:
            if r.status_code == 200:
                return r.text
            if r.status_code not in (429, 500, 502, 503, 504):
                log.warning("HTTP %d：%s", r.status_code, url)
                return None
            log.warning("第 %d 次請求 %s 得到 %d，退避中", attempt + 1, url, r.status_code)
        if attempt < _MAX_RETRIES - 1:
            time.sleep(min(2 ** attempt + random.random(), 10))
    return None


# ---------------------------------------------------------------- 解析

def _iter_sjs_blobs(html: str) -> Iterator[Any]:
    """走訪 HTML 內所有含 thread_items 的 data-sjs JSON blob。"""
    for raw in _SJS_RE.findall(html):
        if _MARKER not in raw:   # 先用字串過濾，省掉一堆無謂的 JSON parse
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _walk_thread_items(node: Any) -> Iterator[list]:
    """遞迴找出所有 thread_items 陣列。

    刻意不寫死 JSON 路徑：那條路徑巢狀極深且會隨 Threads 改版變動，遞迴找標記
    在改版時比較不會整個爛掉。
    """
    if isinstance(node, dict):
        items = node.get(_MARKER)
        if isinstance(items, list):
            yield items
        for value in node.values():
            yield from _walk_thread_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_thread_items(value)


def _normalize(raw: dict) -> _Item | None:
    """Threads 原始 post 物件 → _Item；缺關鍵欄位就丟掉。"""
    pk, code = raw.get("pk") or raw.get("id"), raw.get("code")
    user = raw.get("user") or {}
    if not pk or not code or not user.get("username"):
        return None
    info = raw.get("text_post_app_info") or {}
    return _Item(
        pk=str(pk),
        code=str(code),
        username=user["username"],
        text=((raw.get("caption") or {}).get("text") or "").strip(),
        taken_at=int(raw.get("taken_at") or 0),
        like_count=int(raw.get("like_count") or 0),
        reply_count=int(info.get("direct_reply_count") or 0),
        is_reply=bool(info.get("is_reply")),
        lang=raw.get("detected_language") or raw.get("original_lang_for_translations"),
    )


def _parse(html: str) -> list[_Item]:
    """解析任一含 thread_items 的頁面（搜尋頁／貼文頁通用），以 pk 去重。"""
    seen: dict[str, _Item] = {}
    for blob in _iter_sjs_blobs(html):
        for items in _walk_thread_items(blob):
            for item in items:
                raw = item.get("post") if isinstance(item, dict) else None
                if not isinstance(raw, dict):
                    continue
                got = _normalize(raw)
                if got and got.pk not in seen:
                    seen[got.pk] = got
    return list(seen.values())


# ---------------------------------------------------------------- 過濾

def _is_zh(item: _Item) -> bool:
    """這則是不是中文貼文。

    Threads 搜尋沒有地區過濾，不濾的話「台灣網友怎麼看」會混進一半英文貼文。
    lang 欄位實測大量從缺（49 篇有 47 篇是 None），所以：
      1. lang 明確標 zh* → 是；
      2. 出現日文假名或韓文諺文 → 不是（中日韓共用漢字，只數漢字會把日韓貼文誤收）；
      3. 其餘看漢字數量。
    """
    if (item.lang or "").lower().startswith("zh"):
        return True
    if _KANA.search(item.text) or _HANGUL.search(item.text):
        return False
    return len(_CJK.findall(item.text)) >= _MIN_CJK_CHARS


def _keep(item: _Item) -> bool:
    """搜尋結果的初步過濾：只要主文、要有實質內容、（選配）要中文。"""
    if item.is_reply or len(item.text) < _MIN_TEXT_LEN:
        return False
    return _is_zh(item) if settings.threads_lang_filter else True


# ---------------------------------------------------------------- 關鍵字規劃

def _plan_keywords(query: str) -> tuple[list[str], list[str]]:
    """用一次 LLM 呼叫決定要搜哪幾個『單一詞』，以及其中哪些是『具體實體』。

    回 (要搜的關鍵詞, 其中屬於具體實體的那些)。失敗回 ([原問句], [])。

    prompt 的重點與 PTT 那版不同：PTT 是多詞 AND 比對標題，所以要「短詞」；Threads 是
    根本不接受多詞（含空白一律 0 筆），而且沒有地區過濾、單詞會撈回全世界的東西。

    為什麼要多標一個 entity 旗標：關鍵詞單獨命中算不算數，取決於這個詞夠不夠specific。
    問「輝達進駐北士科」時只提到「北士科」的房產文是對題的（北士科是地名，本身就是實體）；
    問「OpenAI 越獄」時只提到「越獄」的貼文卻是貓咪、影集、電視盒（越獄是歧義普通名詞）。
    這個判斷程式很難做、模型很好做，所以在抽詞時就一併問出來，交給 search() 的字面過濾用。
    """
    msgs = [
        {"role": "system", "content": (
            "你要幫使用者問題規劃 Threads 站內搜尋，給 1~3 個關鍵詞。\n"
            "【硬性限制】Threads 搜尋只接受『單一詞』：關鍵詞裡**絕對不能有空白**，"
            "有空白就會查到 0 筆。也不能是整句問句。所以不要把兩個概念合成一個詞"
            "（「OpenAI 越獄」「AI 資安」都是 0 筆），要拆成分開的關鍵詞。\n"
            "【最重要】第一個關鍵詞必須是問題的『核心實體』——人名／機構／品牌／產品／"
            "AI 模型／地名／事件名稱（例如「慈濟」「輝達」「館長」「OpenAI」「北士科」）。"
            "其餘可以是它的同義變體或別名（「輝達」→「NVIDIA」；「OpenAI」→「ChatGPT」）、"
            "或問題裡另一個具體對象。\n"
            "【絕對禁止】只給『看法／評價／心得／問題／推薦』這種沒有主體的泛用詞——"
            "Threads 是全球平台又不做交集查詢，搜這種詞撈回來的是完全不相干的東西。\n"
            "【每個關鍵詞都要標 entity】entity=true 代表它是『具體到足以單獨指認這個話題』的"
            "專有名詞（人名／機構／品牌／產品／地名／事件名）；entity=false 代表它是普通名詞或"
            "有其他常見意思的詞——例如「越獄」除了 AI 越獄，更常指貓狗跑出籠、影集、手機刷機；"
            "「資安」「開箱」「解封」也是這種。標 false 的詞我們仍然會拿去搜（可以擴大召回），"
            "但只命中它的貼文不會被採信。\n"
            "【專有名詞正規化】用貼文裡最可能出現的寫法：「open ai」要寫成「OpenAI」（不留空白）。\n"
            '只用 JSON 回：{"keywords":[{"term":"詞1","entity":true},{"term":"詞2","entity":false}]}'
        )},
        {"role": "user", "content": query},
    ]
    try:
        raw = llm.chat(msgs, temperature=0)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        flags: dict[str, bool] = {}
        for k in data.get("keywords", []):
            # 容錯：模型偶爾會退化成舊格式的純字串陣列
            term = str(k.get("term", "") if isinstance(k, dict) else k).strip()
            if term:
                flags[term] = bool(k.get("entity", True)) if isinstance(k, dict) else True
    except Exception as e:  # noqa: BLE001
        log.warning("plan_keywords 失敗，退回原問句：%s", e)
        return [query], []

    # 含空白的詞在 Threads 保證 0 筆——模型若沒聽話，就地拆成各自獨立的關鍵詞救回來，
    # 而不是照樣送出去換一個必然的空結果（拆出來的片段沿用原本那個詞的 entity 標記）。
    split: dict[str, bool] = {}
    for term, is_entity in flags.items():
        for part in (term.split() if any(c.isspace() for c in term) else [term]):
            split[part] = is_entity

    keywords = relevance.clean_keywords(list(split))[:3]
    if not keywords:
        return [query], []
    return keywords, [k for k in keywords if split.get(k)]


# ---------------------------------------------------------------- 組裝

def _to_dt(taken_at: int) -> str:
    """Threads 的 taken_at 是 unix epoch（UTC）→ 台北時間 'YYYY-MM-DD HH:MM:SS'。"""
    if not taken_at:
        return ""
    return datetime.fromtimestamp(taken_at, tz=_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def _title_of(item: _Item) -> str:
    """Threads 沒有標題，但前端來源清單、立場統計的項目都靠 title 顯示。

    用「@作者：本文首行前 30 字」合成——比只放 @作者 有資訊量，也不會像整段本文那樣
    把來源清單撐爆。
    """
    first = next((ln.strip() for ln in item.text.splitlines() if ln.strip()), "")
    return f"@{item.username}：{first[:30]}" if first else f"@{item.username} 的貼文"


def _to_post(item: _Item, replies: list[str]) -> Post:
    body = item.text
    if replies:
        body += "\n— 熱門回覆：" + " / ".join(replies)
    return Post(title=_title_of(item), url=item.permalink, content=body,
                created_at=_to_dt(item.taken_at), source="threads")


# ---------------------------------------------------------------- 主流程

def _search_keyword(client: httpx.Client, th: _Throttle, keyword: str) -> list[_Item]:
    """搜一個關鍵詞，回該詞的搜尋結果（未過濾）。"""
    params = _MODES.get(settings.threads_mode, _MODES["broad"])
    url = _SEARCH_URL + "?q=" + quote(keyword)
    for key, value in params.items():
        url += f"&{key}={value}"
    th.wait()
    html = _get(client, url)
    if not html:
        return []
    items = _parse(html)
    log.info("Threads 搜尋「%s」→ %d 筆", keyword, len(items))
    return items


def _expand(client: httpx.Client, th: _Throttle, item: _Item) -> list[str]:
    """進貼文頁把回覆抓回來，依讚數取前 N 則的文字。失敗回 []（該篇就只有主文）。"""
    th.wait()
    html = _get(client, item.permalink)
    if not html:
        return []
    replies = [i for i in _parse(html) if i.pk != item.pk and i.text]
    replies.sort(key=lambda i: i.like_count, reverse=True)
    return [" ".join(r.text.split()) for r in replies[:settings.threads_max_replies]]


def search(query: str, time_budget: int | None = None) -> list[Post]:
    """即時搜尋 Threads：抽核心實體 → 各詞搜一次合併 → 語言/語意過濾 → 展開熱門篇的回覆。

    時間預算內做完就回；到時間就停、回目前已組好的（比照 PTT / Dcard）。
    三個平台是並行的，總時長取 max()，所以這裡的預算不會加到使用者的等待時間上。
    """
    budget = time_budget or settings.threads_time_budget
    deadline = time.monotonic() + budget
    keywords, entities = _plan_keywords(query)
    progress.emit("crawl_plan", platform="threads", keywords=keywords)
    log.info("Threads 搜尋 keywords=%r（實體 %r）mode=%s budget=%ds",
             keywords, entities, settings.threads_mode, budget)

    client = _session()
    th = _Throttle()
    try:
        # 1) 各關鍵詞搜一次、以 pk 跨關鍵詞去重（Threads 不支援交集，只能各搜再合併）
        merged: dict[str, _Item] = {}
        for kw in keywords:
            progress.raise_if_cancelled()
            if time.monotonic() >= deadline:
                break
            for item in _search_keyword(client, th, kw):
                merged.setdefault(item.pk, item)

        # 2) 初步過濾：丟掉回覆、太短的、非中文的
        items = [i for i in merged.values() if _keep(i)]

        # 2.5) strict：本文必須真的提到某個『具體實體』關鍵詞。這道擋的是另外兩道都擋不掉的：
        #      - 字面重疊：搜「輝達」撈回森田輝、搜「北士科」撈回北科大（broad 模式會夾帶
        #        字面沒命中但被平台判定相關的貼文），它們的 cosine 分數還偏高，拉門檻沒用；
        #      - 同字不同義：問「OpenAI 越獄」時，只命中「越獄」的是貓咪跑出籠、越獄風雲
        #        影集、安博盒子刷機——字面檢查放行（「越獄」真的有），語意檢查也放行
        #        （對「越獄」的相似度本來就高）。只有「這篇有沒有提到 OpenAI」問得出真相。
        #      所以只有 entity 詞能單獨讓一篇貼文過關；非 entity 詞照樣拿去搜（擴大召回），
        #      但不能單獨採信。模型一個 entity 都沒標時退回「任一關鍵詞」，不要整批砍光。
        if settings.threads_strict:
            required = [k.lower() for k in (entities or keywords)]
            hit = [i for i in items if any(k in i.text.lower() for k in required)]
            log.info("Threads 字面過濾（須含 %s）：%d 篇 → %d 篇",
                     "／".join(entities or keywords), len(items), len(hit))
            items = hit
        log.info("Threads 合併 %d 筆 → 過濾後 %d 筆", len(merged), len(items))
        progress.emit("crawl_search", platform="threads", found=len(items))
        if not items:
            return []

        # 3) 語意過濾：這是 Threads 唯一的相關性機制——平台只能單詞查、又不做交集，
        #    「越獄」撈回來的是寵物與美劇，得靠這關對回原問句才留得下對題的。
        if settings.threads_rerank_enabled:
            items = relevance.rerank(query, items, settings.threads_min_score,
                                     lambda i: i.text[:300], platform="threads")
            progress.emit("crawl_search", platform="threads", found=len(items))
        items = items[:settings.threads_max_posts]
        if not items:
            return []

        # 4) 展開回覆：只花在「有討論量」的貼文上（依回覆數挑），到時間預算就停。
        #    expand_max 是上限不是保證——候選不足時有幾篇展幾篇，不硬湊。
        targets = sorted((i for i in items if i.reply_count >= settings.threads_min_replies),
                         key=lambda i: i.reply_count, reverse=True)[:settings.threads_expand_max]
        replies_of: dict[str, list[str]] = {}
        for n, item in enumerate(targets, 1):
            progress.raise_if_cancelled()   # 逐篇檢查點：停止最多再等一篇
            if time.monotonic() >= deadline:
                log.info("Threads 到時間預算，已展開 %d/%d 篇就停", n - 1, len(targets))
                progress.emit("crawl_budget", platform="threads", done=n - 1)
                break
            replies_of[item.pk] = _expand(client, th, item)
            progress.emit("crawl_progress", platform="threads", done=n, total=len(targets))

        # 5) 依相關度順序輸出（回覆折進 content，粒度與 Dcard / PTT 對齊）
        posts = [_to_post(i, replies_of.get(i.pk, [])) for i in items]
        log.info("Threads 回傳 %d 篇（展開 %d 篇回覆）", len(posts), len(replies_of))
        return posts
    finally:
        client.close()
