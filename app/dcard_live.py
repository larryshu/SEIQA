"""Dcard 即時爬（DrissionPage 版）。移植自 test_Drissonpage/dcard_drission.py，改為：

- 吃 app.config.settings（不再 import 它自己的 config.py，避免與 app/config.py 撞名）；
- 全站『文章』搜尋（不切版）：直接開 /search/posts?query=<關鍵字>；
- 單例瀏覽器 + 一把鎖：同時只跑一個 Dcard 爬蟲（過一次 Cloudflare 就重用、保溫）；
- 時間預算（deadline）：到時間就停、回已抓到的，不卡死在第 N 篇（比照 PTT / crawler.py）；
- 輸出統一 Post（source="dcard"），供 sources.py fan-out 與前端 [n] 引用。

為什麼用 DrissionPage：Dcard 前面擋 Cloudflare Managed Challenge，且改用 globalPaging 端點載入
貼文（自行重放會 403）。DrissionPage 驅動真實 Chrome（指紋乾淨、有頭多半自動過盾）+ page.listen
攔截 SPA 自己發出的回應，是目前最穩的作法。DrissionPage 採 lazy import：沒裝也不影響 app 啟動，
只有真的走即時爬時才需要（失敗會 fallback 向量庫）。
"""
from __future__ import annotations

import atexit
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import quote

from . import llm, progress
from .config import settings
from .crawler import Post

log = logging.getLogger("dcard.live")

_BASE = "https://www.dcard.tw"
_POST_URL = _BASE + "/f/{alias}/p/{post_id}"
_SEARCH_URL = _BASE + "/search/posts?query={q}"
# Cloudflare 挑戰頁的標題特徵（中英）；標題不含這些字＝已進入真實頁。
_CHALLENGE_HINTS = ("請稍候", "請稍後", "just a moment", "moment", "attention required", "%")
_TAIPEI = timezone(timedelta(hours=8))
_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")  # 純網址/貼圖留言（Dcard 留言常是 sticker 圖）


# --- JSON 結構判斷 / 遞迴抽取（比對 Dcard 回應結構）------------------------
def _looks_like_post(o: dict[str, Any]) -> bool:
    """像不像一篇貼文：有 id + title，且帶 excerpt / commentCount / content。"""
    return (
        o.get("id") is not None
        and o.get("title") is not None
        and ("excerpt" in o or "commentCount" in o or "content" in o)
    )


def _looks_like_comment(o: dict[str, Any]) -> bool:
    """像不像一則留言：有內容 + 樓層（floor），且非貼文（無 title）。"""
    return "content" in o and "floor" in o and "title" not in o


def _extract(obj: Any, pred: Callable[[dict], bool], found: dict[str, Any]) -> None:
    """遞迴走訪任意 JSON，把符合 pred 的 dict 收進 found（以 id/floor 去重、保序）。"""
    if isinstance(obj, dict):
        if pred(obj):
            key = str(obj.get("id") or obj.get("floor"))
            found.setdefault(key, obj)
            return
        for v in obj.values():
            _extract(v, pred, found)
    elif isinstance(obj, list):
        for v in obj:
            _extract(v, pred, found)


def _strip_tags(s: Any) -> str:
    """去掉搜尋結果標題/摘要裡的高亮標籤（<em>…</em> 等），還原乾淨文字。"""
    return re.sub(r"<[^>]+>", "", s) if isinstance(s, str) else ""


def _slim_post(p: dict[str, Any]) -> dict[str, Any]:
    """貼文原始 dict → 常用欄位（保留 id / forumAlias，之後深挖內文要用）。"""
    alias = p.get("forumAlias") or ""
    return {
        "id": p.get("id"),
        "forumAlias": alias,
        "title": _strip_tags(p.get("title")),
        "excerpt": _strip_tags(p.get("excerpt")),
        "likeCount": p.get("likeCount"),
        "commentCount": p.get("commentCount"),
        "createdAt": p.get("createdAt"),
        "url": _POST_URL.format(alias=alias or "all", post_id=p.get("id")),
    }


def _to_dt(iso: Any) -> str:
    """Dcard createdAt（ISO8601, UTC）→ 台北時間 'YYYY-MM-DD HH:MM:SS'；解析失敗回原字串。"""
    if not isinstance(iso, str) or not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(_TAIPEI)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso


# --- 瀏覽器 session（單例，過一次 Cloudflare 即可重用）----------------------
class DcardCrawler:
    """單一有頭瀏覽器 session。支援 with 語法。DrissionPage 在此 lazy import。"""

    def __init__(self) -> None:
        from DrissionPage import ChromiumOptions, ChromiumPage  # lazy：沒裝也不擋 app 啟動

        co = ChromiumOptions()
        co.headless(settings.dcard_headless)
        # 不加 --disable-blink-features=AutomationControlled：新版 Chrome 會跳「不受支援的命令列
        # 標幟」警告列，那本身就是自動化破綻。DrissionPage 預設啟動就無 webdriver 特徵。
        co.set_argument("--lang=zh-TW")
        if settings.dcard_user_agent:
            co.set_user_agent(settings.dcard_user_agent)
        if settings.dcard_user_data_dir:
            # 持久化設定檔：Cloudflare 通行 cookie / 登入態跨次保留（越養越不易被盾）
            co.set_user_data_path(settings.dcard_user_data_dir)
        co.auto_port()  # 自動挑空閒除錯埠，避免和已開著的 Chrome 撞埠
        self.page = ChromiumPage(co)
        self._inject_cookies()
        log.info("Dcard 瀏覽器啟動（headless=%s, profile=%s）",
                 settings.dcard_headless, settings.dcard_user_data_dir or "(臨時)")

    def _inject_cookies(self) -> None:
        """把 settings.dcard_cookie 的整串 Cookie 注入 .dcard.tw（含 cf_clearance 即可免互動盾）。"""
        if not settings.dcard_cookie:
            return
        if not settings.dcard_user_agent:
            log.warning("設了 DCARD_COOKIE 但未設 DCARD_USER_AGENT！cf_clearance 綁 UA，兩者須同一瀏覽器。")
        cookies = []
        for part in settings.dcard_cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                cookies.append({"name": name.strip(), "value": value.strip(),
                                "domain": ".dcard.tw", "path": "/"})
        if not cookies:
            return
        try:
            self.page.set.cookies(cookies)
            names = {c["name"] for c in cookies}
            log.info("已注入 %d 個 cookie%s", len(cookies),
                     "（含 cf_clearance）" if "cf_clearance" in names else "（未見 cf_clearance）")
        except Exception as e:  # noqa: BLE001
            log.warning("注入 cookie 失敗：%s", e)

    def __enter__(self) -> "DcardCrawler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.page.quit()
        except Exception as e:  # noqa: BLE001 — 收尾噪音，資料早已抓到
            log.warning("關閉 Dcard 瀏覽器例外（忽略）：%s", e)

    # --- 對外高階介面 ---------------------------------------------------
    def search_global(self, query: str, max_posts: int) -> list[dict[str, Any]]:
        """全站『文章』搜尋（不限看板）：開 /search/posts?query=<q>，依相關度回傳貼文 meta。

        這個 URL 本身即『文章』分頁、全站範圍、預設排序『最相關』——等同手動「打字搜尋 →
        點文章分頁」，不需要先切到某個版。
        """
        page = self.page
        page.listen.start(["search", "globalPaging", "/posts"])  # 廣攔搜尋/貼文回應
        try:
            page.get(_SEARCH_URL.format(q=quote(query)), timeout=settings.dcard_request_timeout)
            if not self._wait_cloudflare():
                log.error("搜尋頁過 Cloudflare 失敗：%s", query)
                return []

            collected: dict[str, Any] = {}
            self._extract_next_data(_looks_like_post, collected)  # 首屏 SSR 搜尋結果
            stale = 0
            while len(collected) < max_posts and stale < settings.dcard_scroll_stale_limit:
                before = len(collected)
                page.scroll.to_bottom()
                page.wait(random.uniform(settings.dcard_min_delay, settings.dcard_max_delay))
                self._drain(_looks_like_post, collected)
                stale = stale + 1 if len(collected) == before else 0

            posts = [_slim_post(p) for p in list(collected.values())[:max_posts]]
            log.info("全站搜尋『%s』：抽到 %d 篇、回傳 %d 篇", query, len(collected), len(posts))
            return posts
        finally:
            page.listen.stop()

    def fetch_post(self, alias: str, post_id: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """開單篇貼文頁，回傳（完整貼文含內文, 留言清單）。"""
        page = self.page
        page.listen.start(["/comments", "/posts/"])  # 攔截留言/貼文回應
        try:
            page.get(_POST_URL.format(alias=alias or "all", post_id=post_id),
                     timeout=settings.dcard_request_timeout)
            if not self._wait_cloudflare():
                log.warning("貼文 %s 過 Cloudflare 失敗", post_id)
                return None, []

            full: dict[str, Any] = {}
            self._extract_next_data(
                lambda o: _looks_like_post(o) and str(o.get("id")) == str(post_id), full)
            post = full.get(str(post_id))

            comments: dict[str, Any] = {}
            self._extract_next_data(_looks_like_comment, comments)  # 首屏留言
            # 掃到 scan_max 則就停（只取前 N 熱門，不必為一篇熱門文捲完數百則而吃光預算）；
            # 或連續 stale 次捲動都沒新留言（＝這篇留言不多、已到底）也停。
            stale = 0
            while len(comments) < settings.dcard_comment_scan_max and stale < settings.dcard_comment_stale_limit:
                before = len(comments)
                page.scroll.to_bottom()
                page.wait(random.uniform(settings.dcard_min_delay, settings.dcard_max_delay))
                self._drain(_looks_like_comment, comments)
                stale = stale + 1 if len(comments) == before else 0

            log.info("貼文 %s：內文=%s，留言 %d 則", post_id, "有" if post else "無", len(comments))
            return post, list(comments.values())
        finally:
            page.listen.stop()

    # --- 內部工具 -------------------------------------------------------
    def _drain(self, pred: Callable[[dict], bool], found: dict[str, Any]) -> None:
        """把目前 listen 佇列裡攔到的回應全部讀出並抽取（趁分頁還開著讀）。"""
        timeout = settings.dcard_min_delay  # 第一次多等一下讓回應進來；之後只快掃已排隊的
        while True:
            pkt = self.page.listen.wait(count=1, timeout=timeout, fit_count=False, raise_err=False)
            if not pkt:
                break
            packets: Iterable = pkt if isinstance(pkt, list) else [pkt]
            for p in packets:
                try:
                    _extract(p.response.body, pred, found)
                except Exception:  # noqa: BLE001 — 該回應非 JSON / 已失效，略過
                    pass
            timeout = 0.6

    def _extract_next_data(self, pred: Callable[[dict], bool], found: dict[str, Any]) -> None:
        """從 SSR 的 __NEXT_DATA__ 補資料（首屏貼文 / 留言常在這裡）。"""
        try:
            raw = self.page.run_js(
                "var e=document.getElementById('__NEXT_DATA__');return e?e.textContent:null;")
            if raw:
                _extract(json.loads(raw), pred, found)
        except Exception:  # noqa: BLE001
            pass

    def _wait_cloudflare(self) -> bool:
        """等 Cloudflare 挑戰解開。優先靠 cf_clearance（cookie / 持久設定檔）免盾；真跳盾盡力點+等手動。"""
        deadline = time.monotonic() + settings.dcard_cf_timeout
        attempts = 0
        warned = False
        while time.monotonic() < deadline:
            if self.page.title and not self._is_challenged():
                return True  # 標題已載入且無挑戰特徵＝進到真實頁
            if attempts < 2:  # 盡力座標點兩次（reputation 好時偶爾能過）
                self._solve_turnstile()
                attempts += 1
                self.page.wait(3)
                continue
            if not warned:
                warned = True
                log.warning(
                    "遇到 Cloudflare 互動盾。設好 DCARD_USER_DATA_DIR 養出 cf_clearance（或貼 "
                    "DCARD_COOKIE+DCARD_USER_AGENT）通常不會走到這；否則請在彈出視窗手動點『驗證您是"
                    "人類』，程式會自動接續（最多等 %d 秒）。", settings.dcard_cf_timeout)
            self.page.wait(1.5)
        ok = bool(self.page.title) and not self._is_challenged()
        if not ok:
            log.warning("Cloudflare 仍未通過（cf_clearance 可能過期/未養成，或本機 IP 目前被盯）。")
        return ok

    def _is_challenged(self) -> bool:
        """目前頁面是否卡在 Cloudflare 挑戰：看標題特徵，或有 challenges.cloudflare.com iframe。"""
        title = (self.page.title or "").lower()
        if any(h in title for h in _CHALLENGE_HINTS):
            return True
        try:
            return bool(self.page.ele(
                'xpath://iframe[contains(@src,"challenges.cloudflare.com")]', timeout=0.5))
        except Exception:  # noqa: BLE001
            return False

    def _solve_turnstile(self) -> bool:
        """（盡力而為）對 Turnstile 勾選框座標派送真實滑鼠點擊。真正可靠的是養出 cf_clearance。"""
        try:
            rect = self.page.run_js(r"""
                const sels=['.dcard_captcha .main-wrapper','.main-wrapper','.cf-turnstile',
                            '[class*="turnstile"]','.dcard_captcha'];
                for(const s of sels){const el=document.querySelector(s);
                  if(el){const r=el.getBoundingClientRect();
                    if(r.width>0&&r.height>0)
                      return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height});}}
                return null;""")
        except Exception:  # noqa: BLE001
            rect = None
        if not rect:
            return False
        r = json.loads(rect)
        x = int(r["x"]) + 28                    # 勾選框約在容器左緣 +28px
        y = int(r["y"]) + int(r["h"] // 2)      # 垂直置中
        try:
            p = self.page
            p.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x - 40, y=y + 6, button="none", buttons=0)
            p.wait(0.12)
            p.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none", buttons=0)
            p.wait(0.15)
            p.run_cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
            p.wait(0.07)
            p.run_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
            return True
        except Exception:  # noqa: BLE001
            return False


# --- 單例瀏覽器 + 鎖（fan-out 是多執行緒，Dcard 這條要序列化）----------------
_lock = threading.Lock()
_singleton: DcardCrawler | None = None


def _get_crawler() -> DcardCrawler:
    global _singleton
    if _singleton is None:
        _singleton = DcardCrawler()
    return _singleton


def _reset_crawler() -> None:
    """瀏覽器可能已壞（崩潰/連線掉）→ 收掉單例，下次呼叫重啟。"""
    global _singleton
    if _singleton is not None:
        try:
            _singleton.close()
        except Exception:  # noqa: BLE001
            pass
        _singleton = None


def shutdown() -> None:
    """行程要結束了 → 收掉那顆 Chrome。

    平常「爬完不關」是刻意的：單例保溫，過一次 Cloudflare 就重用那個 session（關掉再開
    等於每題重過一次互動盾，慢且更容易被盯上）。但**沒有人在行程結束時收尾**——Chrome 是
    DrissionPage 啟動的獨立子行程，uvicorn 死了它不會跟著死，每次重啟就多留一顆孤兒
    （`--reload` 存一次檔就堆一顆，因為 auto_port 每次挑新埠、不會接管既有那顆）。

    正常 quit() 還有一個好處：Chrome 會把 profile（含 cf_clearance）好好寫回磁碟，
    下次啟動反而更容易直接過盾——被硬殺的 Chrome 可能來不及 flush。
    """
    if _singleton is None:
        return
    log.info("關機：收掉 Dcard 瀏覽器")
    _reset_crawler()


# 保險絲：正常退出（Ctrl+C、uvicorn 收工）時一定收掉；沒開過瀏覽器就是 no-op。
# 註：硬殺（kill -9 / 工作管理員結束處理程序）救不了，那是必然的。
atexit.register(shutdown)


# --- 關鍵字抽取（LLM）------------------------------------------------------
def _extract_keywords(query: str) -> list[str]:
    """把口語問句濃縮成 1~3 條 Dcard 全站『文章』搜尋查詢詞（不用選版）。失敗回 [原問句]。"""
    msgs = [
        {"role": "system", "content": (
            "你要把使用者問題轉成 Dcard 站內『文章』搜尋的查詢詞。Dcard 是全文檢索（比對標題+內文），"
            "請給 1~3 條精簡查詢詞，每條是 2~8 字的名詞短語、涵蓋問題不同面向，"
            "不要問號、不要整句問句、不要編號、不要引號。只輸出查詢詞，一行一條。"
            "例如問「請問目前軟體工程師年薪範圍是?」可輸出：軟體工程師 年薪 / 工程師 薪水 待遇。"
        )},
        {"role": "user", "content": query},
    ]
    try:
        text = llm.chat(msgs, temperature=0)
    except Exception as e:  # noqa: BLE001 — 抽取失敗就退回原問句（上層仍會搜）
        log.warning("關鍵字抽取失敗，改用原問句：%s", e)
        return [query]
    lines = [ln.strip(" -•·\t").strip() for ln in text.splitlines()]
    kws = [ln for ln in lines if ln][:3]
    return kws or [query]


# --- 留言清洗 / 轉 Post -----------------------------------------------------
def _is_text_comment(content: Any) -> bool:
    """是否為『有文字』的留言：排除空白與純網址/貼圖。"""
    return isinstance(content, str) and bool(content.strip()) and not _URL_ONLY.match(content)


def _to_post(meta: dict[str, Any], full: dict[str, Any] | None,
             comments: list[dict[str, Any]]) -> Post | None:
    """meta（搜尋結果）+ full（貼文內文）+ comments → 統一 Post；沒內文也沒摘要就跳過。"""
    body = _strip_tags((full or {}).get("content")) or meta.get("excerpt") or ""
    body = body.strip()
    if not body:
        return None
    # 留言：濾掉貼圖/純網址 → 依讚數由高到低取前 N → 附在內文後（比照 PTT「熱門推文」）
    texts = [c for c in comments if _is_text_comment(c.get("content"))]
    texts.sort(key=lambda c: c.get("likeCount") or 0, reverse=True)
    top = [_strip_tags(c["content"]).strip() for c in texts[:settings.dcard_max_comments]]
    top = [t for t in top if t]
    if top:
        body += "\n— 熱門留言：" + " / ".join(top)
    return Post(
        title=_strip_tags(meta.get("title")) or "",
        url=meta.get("url") or "",
        content=body,
        created_at=_to_dt(meta.get("createdAt") or (full or {}).get("createdAt")),
        source="dcard",
    )


def _run(keywords: list[str], deep_max: int, deadline: float) -> list[Post]:
    """實際爬：全站搜尋（多關鍵詞合併去重）→ 時間預算內逐篇深挖內文+留言。

    搜尋與逐篇深挖的迴圈頂端都設了取消檢查點：使用者按停止時，最多再等一篇貼文的時間。
    """
    crawler = _get_crawler()

    # 1) 全站搜尋，多關鍵詞合併、以 post id 去重、保序（≈相關度）
    metas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kw in keywords:
        progress.raise_if_cancelled()
        if time.monotonic() >= deadline or len(metas) >= deep_max:
            break
        for m in crawler.search_global(kw, max_posts=deep_max):
            key = str(m.get("id"))
            if not m.get("id") or key in seen:
                continue
            seen.add(key)
            metas.append(m)
    progress.emit("crawl_search", platform="dcard", found=len(metas))

    # 2) 逐篇深挖（時間預算內；到時間就回目前已抓到的）
    posts: list[Post] = []
    planned = metas[:deep_max]
    for i, m in enumerate(planned):
        progress.raise_if_cancelled()
        if time.monotonic() >= deadline:
            log.info("Dcard 到時間預算，已深挖 %d 篇就停", len(posts))
            progress.emit("crawl_budget", platform="dcard", done=len(posts))
            break
        if i:  # 每篇之間停頓，降低『操作頻率過快』觸發 Cloudflare 互動盾
            crawler.page.wait(random.uniform(settings.dcard_post_delay_min, settings.dcard_post_delay_max))
        full, comments = crawler.fetch_post(m["forumAlias"], m["id"])
        post = _to_post(m, full, comments)
        if post:
            posts.append(post)
        progress.emit("crawl_progress", platform="dcard",
                      done=len(posts), total=len(planned))
    log.info("Dcard 即時爬完成：搜到 %d 篇、深挖回 %d 篇", len(metas), len(posts))
    return posts


def crawl(query: str, max_posts: int | None = None, time_budget: int | None = None) -> list[Post]:
    """即時爬 Dcard（對外入口）：LLM 抽關鍵字 → 全站『文章』搜尋 → 時間預算內深挖內文+留言。

    單例瀏覽器 + 鎖：同時只跑一個 Dcard 爬蟲。任何例外都吞掉回 []（上層 DcardLiveSource
    會 fallback 到向量庫），並收掉可能已壞的瀏覽器 session 讓下次重啟。
    """
    deep_max = max_posts or settings.dcard_deep_max
    budget = time_budget or settings.dcard_time_budget
    deadline = time.monotonic() + budget
    keywords = _extract_keywords(query)
    progress.emit("crawl_plan", platform="dcard", keywords=keywords)
    log.info("Dcard 即時爬：keywords=%r deep_max=%d budget=%ds", keywords, deep_max, budget)
    with _lock:
        try:
            return _run(keywords, deep_max, deadline)
        except Exception as e:  # noqa: BLE001 — live 爬蟲什麼都可能炸，一律 fail-safe
            # 取消（progress.Cancelled）是 BaseException，刻意不被這裡攔下：
            # 使用者按停止不該被當成「爬蟲掛了」而觸發一次沒必要的向量庫 fallback。
            log.warning("Dcard 即時爬失敗（將 fallback 向量庫）：%s", e)
            _reset_crawler()
            return []
