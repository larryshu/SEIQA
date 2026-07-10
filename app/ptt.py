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


def _plan_search(query: str) -> tuple[str, list[str]]:
    """用一次 LLM 呼叫決定 (看板, 多個單一關鍵詞)。

    PTT 站內搜尋是拿整串查詢字串比對『標題』，且空白分隔的多個詞是 AND——所以
    「外型 情緒穩定」要求標題同時含兩詞 → 幾乎 0 結果。正解是給『多個單一語詞』各搜一次再合併
    （像「外型」「擇偶」各 20 筆）。失敗時退回 (Gossiping, [原問句])。
    """
    listing = "\n".join(f"- {code}: {desc}" for code, desc in BOARDS.items())
    msgs = [
        {"role": "system", "content": (
            "你要幫使用者問題規劃 PTT 站內搜尋：(1) 從清單挑一個最相關看板代碼；"
            "(2) 給 1~3 個『單一關鍵詞』。注意：PTT 搜尋會把空白分隔的多個詞當 AND 去比對標題，"
            "所以每個關鍵詞必須是『單一語詞』（約 2~4 字、文章標題可能出現），"
            "不要把多個概念塞進同一個詞、不要整句問句、不要問號。"
            "用不同單詞涵蓋問題的不同面向（例如『外型』『擇偶』『個性』）。"
            '只用 JSON 回：{"board":"看板代碼","keywords":["詞1","詞2"]}\n看板清單：\n' + listing
        )},
        {"role": "user", "content": query},
    ]
    try:
        raw = llm.chat(msgs, temperature=0)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        board_ans = str(data.get("board", "")).strip()
        keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
    except Exception as e:  # noqa: BLE001
        log.warning("plan_search 失敗，退回 (%s, [原問句])：%s", _DEFAULT_BOARD, e)
        return _DEFAULT_BOARD, [query]
    board = next((c for c in BOARDS if board_ans.lower() == c.lower()), _DEFAULT_BOARD)
    return board, (keywords or [query])


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


def search(query: str, board: str | None = None, time_budget: int | None = None) -> list[Post]:
    """即時搜尋 PTT：挑看板→『邊翻搜尋頁、邊逐篇抓文章』，在時間預算內盡量抓。

    交錯式（streaming）：抓一頁搜尋結果就馬上抓那頁的文章，再翻下一頁——這樣預算會真的花在
    抓文章上，到時間就停、回已抓到的全部，而不是把預算耗在翻頁。
    """
    budget = time_budget or settings.ptt_time_budget
    deadline = time.monotonic() + budget
    planned_board, keywords = _plan_search(query)
    chosen = board or planned_board  # 整句問句搜不到，一律用抽出的單一關鍵詞去搜
    progress.emit("crawl_plan", platform="ptt", board=chosen, keywords=keywords)
    log.info("PTT 搜尋 board=%s keywords=%r budget=%ds", chosen, keywords, budget)

    sess = _session()
    th = _Throttle()
    posts: list[Post] = []
    seen: set[str] = set()  # 跨關鍵詞以文章 url 去重
    try:
        for kw in keywords:  # 每個單詞各搜一次、合併（解決多詞 AND → 0 結果）
            progress.raise_if_cancelled()
            if time.monotonic() >= deadline:
                break
            page = 1
            while time.monotonic() < deadline:
                progress.raise_if_cancelled()
                th.wait()
                links = _search_page_links(sess, chosen, kw, page)
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
        if time.monotonic() >= deadline:
            progress.emit("crawl_budget", platform="ptt", done=len(posts))
        log.info("PTT 抓到 %d 篇（board=%s, keywords=%r）", len(posts), chosen, keywords)
    finally:
        sess.close()
    return posts
