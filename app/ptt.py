"""PTT 即時爬蟲（crawl_ptt 的實作）。

PTT web 無 Cloudflare、純伺服器渲染 HTML，只有部分看板有 18 禁年齡牆（帶 over18=1 cookie 即過）。
所以用 requests + BeautifulSoup 就夠，不需要 Playwright / curl_cffi / proxy。

策略（符合「符合的都抓、沒有預設篇數」）：
- 打某看板的站內搜尋 /bbs/<board>/search?q=...，翻頁把結果文章連結都收集起來；
- 逐篇進文章頁抓主文＋熱門推文；
- 用「時間預算」(PTT_TIME_BUDGET) 控總時長：到時間就停、回傳目前已抓到的全部，而非砍死在第 N 篇；
- 全程禮貌限速（PTT_MIN/MAX_DELAY）避免被 ban。

PTT 站內搜尋是「逐看板」的（沒有全站搜尋），故先用 LLM 從白名單挑一個最相關看板再搜。
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from . import llm, progress
from .config import settings
from .crawler import Post

log = logging.getLogger("ptt")

_BASE = "https://www.ptt.cc"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# 看板白名單（代碼 → 適用主題）；給 LLM 挑、也用來驗證它的回答
BOARDS: dict[str, str] = {
    "Gossiping": "八卦、時事、問卦、社會議題（最大綜合板，不確定時的預設）",
    "Stock": "股票、台股、投資",
    "MobileComm": "手機、通訊、行動裝置",
    "iOS": "iPhone、Apple、iOS",
    "Tech_Job": "科技業、工程師、工作、職場",
    "AI_Art": "AI、生成式 AI、AI 繪圖、ChatGPT／LLM／大型語言模型討論",
    "PC_Shopping": "電腦硬體、DIY、顯示卡、組裝、3C 開箱",
    "Boy-Girl": "感情、男女、交往、分手",
    "marriage": "婚姻、夫妻、家庭",
    "car": "汽車、買車、用車",
    "Lifeismoney": "省錢、優惠、信用卡、現金回饋",
    "movie": "電影、影評",
    "C_Chat": "動漫、遊戲、ACG、宅",
    "MakeUp": "美妝、化妝、保養",
    "e-shopping": "網路購物、電商、開箱",
    "NBA": "NBA、籃球",
    "Baseball": "棒球、中職、MLB",
    "Food": "美食、餐廳、小吃",
}
_DEFAULT_BOARD = "Gossiping"


# 純泛用限定詞：單獨拿去搜 PTT 標題會撈到成千上萬不相關文章（問 Kimi K3 卻回一堆
# 「iPhone 實際照片」）。prompt 已禁，但模型不一定聽——拿回後在程式層用這張表硬過濾兜底。
_FILLER_KEYWORDS: frozenset[str] = frozenset({
    "實際", "心得", "評價", "看法", "推薦", "意見", "感想", "體驗", "使用", "應用",
    "分享", "討論", "開箱", "比較", "選擇", "如何", "怎樣", "怎麼", "一般", "問題",
    "請問", "介紹", "情況", "狀況", "效果", "表現", "優缺點", "值得", "覺得",
})


def _clean_keywords(keywords: list[str]) -> list[str]:
    """剔掉純泛用限定詞（心得/實際/評價…），只留有主體的詞；去重保序。

    若整批都是泛用詞（模型完全沒給實體）就退回第一個，至少還有東西可搜、不致變空。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for k in keywords:
        if k and k not in _FILLER_KEYWORDS and k not in seen:
            seen.add(k)
            kept.append(k)
    return kept or keywords[:1]


def _plan_search(query: str) -> tuple[list[str], list[str]]:
    """用一次 LLM 呼叫決定 (看板清單 1~2 個, 多個單一關鍵詞)。

    PTT 站內搜尋是拿整串查詢字串比對『標題』，且空白分隔的多個詞是 AND——所以
    「外型 情緒穩定」要求標題同時含兩詞 → 幾乎 0 結果。正解是給『多個單一語詞』各搜一次再合併。

    兩個坑各有對策：
    - 看板可能選錯，或主題本來就分散在不只一個板（例：LLM 討論在 AI_Art 也可能在 Tech_Job）——
      故讓 LLM 回 1~2 個板都搜；上層再對「0 對題結果」退回 Gossiping 當第三層網。
    - 泛用限定詞（「看法」「心得」「實際」）單獨搜會拿到大量雜訊——prompt 先禁，
      拿回後再用 _clean_keywords 停用表硬過濾兜底。後端還有 embed cosine 二次過濾當保險。
    失敗退回 ([Gossiping], [原問句])。
    """
    listing = "\n".join(f"- {code}: {desc}" for code, desc in BOARDS.items())
    msgs = [
        {"role": "system", "content": (
            "你要幫使用者問題規劃 PTT 站內搜尋：(1) 從清單挑『1~2 個』最相關看板代碼，最相關的排第一；"
            "只有主題真的可能分散在兩個板才給第二個，否則給一個就好。"
            "(2) 給 1~3 個『單一關鍵詞』。注意：PTT 搜尋會把空白分隔的多個詞當 AND 去比對標題，"
            "所以每個關鍵詞必須是『單一語詞』（約 2~4 字、文章標題可能出現），"
            "不要把多個概念塞進同一個詞、不要整句問句、不要問號。\n"
            "但若『核心實體本身就含空格』（產品/型號/專有名詞，如『Kimi K3』『iPhone 16』），"
            "要當『一個』關鍵詞整串保留、不可拆成兩個——PTT 對含空格的單一關鍵詞是要求標題"
            "同時含這幾個字（AND），整串保留反而更精準；拆開各搜會混進不相關的東西。\n"
            "【最重要】先從問題找出『核心實體』（人名/機構/產品/AI 模型/地點/事件名稱），"
            "第一個關鍵詞必須就是這個核心實體；其他關鍵詞可以是它的同義變體或相關子項"
            "（例如核心實體是『福智』→ 可加『福智團』『福智基金會』）。\n"
            "【絕對禁止】只丟『看法/評價/心得/實際/推薦/意見/如何/怎樣』這種無主體的泛用限定詞——"
            "PTT 標題含這些字的文章成千上萬，搜這個等於什麼都沒過濾。\n"
            '只用 JSON 回：{"boards":["主板代碼","備板代碼(可省)"],"keywords":["詞1","詞2"]}\n看板清單：\n' + listing
        )},
        {"role": "user", "content": query},
    ]
    try:
        raw = llm.chat(msgs, temperature=0)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        raw_boards = data.get("boards", [])
        if isinstance(raw_boards, str):  # 模型偶爾回單一字串而非陣列
            raw_boards = [raw_boards]
        keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
    except Exception as e:  # noqa: BLE001
        log.warning("plan_search 失敗，退回 (%s, [原問句])：%s", _DEFAULT_BOARD, e)
        return [_DEFAULT_BOARD], [query]

    boards: list[str] = []  # 對回白名單（大小寫不敏感）、去重、上限 2
    for b in raw_boards:
        match = next((c for c in BOARDS if str(b).strip().lower() == c.lower()), None)
        if match and match not in boards:
            boards.append(match)
        if len(boards) >= 2:
            break
    return (boards or [_DEFAULT_BOARD]), (_clean_keywords(keywords) or [query])


class _Throttle:
    """禮貌限速：兩次請求間隔落在 [min, max]＋抖動。"""

    def __init__(self) -> None:
        self._last = 0.0

    def wait(self) -> None:
        gap = random.uniform(settings.ptt_min_delay, settings.ptt_max_delay)
        elapsed = time.monotonic() - self._last
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last = time.monotonic()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    s.cookies.set("over18", "1", domain=".ptt.cc")  # 過 18 禁年齡牆
    return s


_MAX_RETRIES = 3


def _get(sess: requests.Session, url: str, params: dict | None = None):
    """GET 帶退避重試（PTT 偶發 ConnectionReset / 暫時性錯誤就重試，不要一次失敗就放棄）。"""
    for attempt in range(_MAX_RETRIES):
        try:
            return sess.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            if attempt == _MAX_RETRIES - 1:
                log.warning("請求重試 %d 次仍失敗 %s：%s", _MAX_RETRIES, url, e)
                return None
            time.sleep(0.5 * (2 ** attempt) + random.random())
    return None


def _to_dt(ptt_time: str) -> str:
    """PTT 'Mon Jun 22 03:40:54 2026' → 'YYYY-MM-DD HH:MM:SS'；解析失敗回原字串。"""
    try:
        return datetime.strptime(ptt_time.strip(), "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ptt_time or ""


def _search_page_links(sess: requests.Session, board: str, query: str, page: int) -> list[str] | None:
    """抓一頁搜尋結果，回該頁文章相對連結清單；沒結果回 []、請求失敗回 None。"""
    r = _get(sess, f"{_BASE}/bbs/{board}/search", params={"q": query, "page": page})
    if r is None or r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    return [a["href"] for a in soup.select("div.r-ent div.title a[href]")]


def _parse_article(html: str) -> tuple[str, str, str]:
    """文章頁 → (標題, 主文＋熱門推文, created_at)。"""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main-content")
    if main is None:
        return "", "", ""

    title, created = "", ""
    for meta in main.select("div.article-metaline"):
        tag = meta.select_one("span.article-meta-tag")
        val = meta.select_one("span.article-meta-value")
        if not tag or not val:
            continue
        if "標題" in tag.get_text():
            title = val.get_text(strip=True)
        elif "時間" in tag.get_text():
            created = _to_dt(val.get_text(strip=True))

    # 先收推文，再把雜訊元素清掉留純主文
    pushes: list[str] = []
    for p in main.select("div.push"):
        ptag = p.select_one("span.push-tag")
        content = p.select_one("span.push-content")
        if content:
            mark = (ptag.get_text(strip=True) if ptag else "→")
            pushes.append(f"{mark} {content.get_text(strip=True).lstrip(': ').strip()}")

    for junk in main.select("div.article-metaline, div.article-metaline-right, div.push, span.f2"):
        junk.decompose()
    body = main.get_text()
    for marker in ("※ 發信站", "◆ From:", "--\n"):  # 砍簽名檔/發信站
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
    body = body.strip()

    if pushes:
        body += "\n— 熱門推文：" + " / ".join(pushes[:10])
    return title, body, created


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity；長度為零就回 0（fail-safe）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _rerank_posts_by_similarity(user_query: str, posts: list[Post],
                                min_score: float) -> list[Post]:
    """對抓到的 PTT posts 依語意相關度過濾——避免「看法/評價」單獨搜拿到的雜訊。

    分數 = cosine(embed(user_query), embed(title + body 前段))；保留 >= min_score
    並依分數重排。fail-safe：embed 失敗就回原 list（不擋整條爬蟲）。
    批次一次 embed 所有 posts，只多一次 API call。
    """
    if not posts or not (user_query or "").strip():
        return posts
    try:
        query_vec = llm.embed(user_query)
        # title + body 前 300 字（body 常含推文雜訊，多了反而稀釋主題訊號）
        texts = [((p.title or "") + " " + (p.content or "")[:300]).strip() or "空"
                 for p in posts]
        client = llm._client()  # noqa: SLF001 — 內部共用 client
        resp = client.embeddings.create(model=settings.embed_model, input=texts)
        vecs = [d.embedding for d in resp.data]
    except Exception as e:  # noqa: BLE001 — 過濾失敗就沿用原結果
        log.warning("PTT 語意過濾失敗（沿用原抓到的貼文）：%s", e)
        return posts

    scored = [(p, _cosine(query_vec, v)) for p, v in zip(posts, vecs)]
    kept = [(p, s) for p, s in scored if s >= min_score]
    kept.sort(key=lambda x: x[1], reverse=True)
    dropped = len(scored) - len(kept)
    log.info("PTT 語意過濾：%d 篇 → %d 篇（門檻 %.2f，丟 %d）",
             len(scored), len(kept), min_score, dropped)
    progress.emit("ptt_rerank", kept=len(kept), dropped=dropped,
                  threshold=min_score, before=len(scored))
    return [p for p, _ in kept]


def _crawl_boards(sess: requests.Session, th: "_Throttle", boards: list[str],
                  keywords: list[str], deadline: float, seen: set[str],
                  posts: list[Post]) -> None:
    """對每個 (看板 × 關鍵詞) 交錯翻搜尋頁、逐篇抓文，append 到 posts（就地累積）。

    交錯式：抓一頁搜尋結果就馬上抓那頁的文章再翻下一頁，讓時間預算真的花在抓文章上。
    seen 跨看板/關鍵詞以 url 去重；到 deadline 或被取消就停。
    """
    for b in boards:
        if time.monotonic() >= deadline:
            break
        for kw in keywords:  # 每個單詞各搜一次、合併（解決多詞 AND → 0 結果）
            progress.raise_if_cancelled()
            if time.monotonic() >= deadline:
                break
            page = 1
            while time.monotonic() < deadline:
                progress.raise_if_cancelled()
                th.wait()
                links = _search_page_links(sess, b, kw, page)
                if links is None or not links:  # 請求失敗或該詞沒有更多結果
                    break
                for href in links:
                    progress.raise_if_cancelled()  # 逐篇檢查點：停止最多再等一篇
                    if time.monotonic() >= deadline:
                        break
                    if href in seen:
                        continue
                    seen.add(href)
                    th.wait()
                    r = _get(sess, _BASE + href)
                    if r is None or r.status_code != 200:
                        continue
                    title, body, created = _parse_article(r.text)
                    if not body:  # 已刪文/解析不到內文就略過
                        continue
                    posts.append(Post(title=title, url=_BASE + href, content=body,
                                      created_at=created, source="ptt"))
                    progress.emit("crawl_progress", platform="ptt", done=len(posts))
                page += 1


def search(query: str, board: str | None = None, time_budget: int | None = None) -> list[Post]:
    """即時搜尋 PTT：挑看板→『邊翻搜尋頁、邊逐篇抓文章』，在時間預算內盡量抓。

    交錯式（streaming）：抓一頁搜尋結果就馬上抓那頁的文章，再翻下一頁——這樣預算會真的花在
    抓文章上，到時間就停、回已抓到的全部，而不是把預算耗在翻頁。

    收完後（如果 PTT_RERANK_ENABLED）用 embed cosine 對 title+body 前段做語意過濾，
    擋掉「看法/評價/心得」這類泛用限定詞單獨搜命中的雜訊。
    """
    budget = time_budget or settings.ptt_time_budget
    deadline = time.monotonic() + budget
    planned_boards, keywords = _plan_search(query)
    boards = [board] if board else planned_boards  # 呼叫端可指定看板；否則用規劃的 1~2 個
    progress.emit("crawl_plan", platform="ptt", board="、".join(boards), keywords=keywords)
    log.info("PTT 搜尋 boards=%r keywords=%r budget=%ds", boards, keywords, budget)

    sess = _session()
    th = _Throttle()
    posts: list[Post] = []
    seen: set[str] = set()  # 跨看板/關鍵詞以文章 url 去重
    try:
        _crawl_boards(sess, th, boards, keywords, deadline, seen, posts)
        if time.monotonic() >= deadline:
            progress.emit("crawl_budget", platform="ptt", done=len(posts))
        log.info("PTT 抓到 %d 篇（boards=%r, keywords=%r）", len(posts), boards, keywords)

        # 語意過濾：把「看法/評價」單獨搜命中的雜訊（例如問福智卻拿到周星馳的評價）擋掉。
        if settings.ptt_rerank_enabled and posts:
            posts = _rerank_posts_by_similarity(query, posts, settings.ptt_min_score)
            progress.emit("crawl_progress", platform="ptt", done=len(posts))

        # 保險：語意過濾後留 0 篇（多半是看板選錯）→ 退回 Gossiping（最大綜合板，話題多會被轉貼）再搜。
        if not posts and _DEFAULT_BOARD not in boards and time.monotonic() < deadline:
            log.info("PTT 主板 %r 無對題結果，退回 %s 再搜", boards, _DEFAULT_BOARD)
            progress.emit("crawl_plan", platform="ptt",
                          board=f"{_DEFAULT_BOARD}（退回）", keywords=keywords)
            _crawl_boards(sess, th, [_DEFAULT_BOARD], keywords, deadline, seen, posts)
            if settings.ptt_rerank_enabled and posts:
                posts = _rerank_posts_by_similarity(query, posts, settings.ptt_min_score)
            progress.emit("crawl_progress", platform="ptt", done=len(posts))
    finally:
        sess.close()
    return posts
