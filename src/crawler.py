"""Playwright 爬蟲：列表頁翻頁 + 詳細頁抓取。

原則：
- 每頁之間隨機延遲（預設 2-5 秒），不高頻請求。
- 偵測到 CAPTCHA / 阻擋頁立即停止，不嘗試繞過。
- 單一物件失敗只記 log，不中斷整批。
"""
from __future__ import annotations

import logging
import math
import queue
import random
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .models import Listing, ListingCard
from .parser_detail import parse_detail_page
from .parser_list import parse_list_page
from .utils import PROJECT_ROOT, log_failed_url, looks_blocked, parse_layout

logger = logging.getLogger(__name__)

DETAIL_URL_TEMPLATE = "https://rent.591.com.tw/{listing_id}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class CaptchaDetectedError(RuntimeError):
    """遇到 CAPTCHA 或反爬阻擋頁。"""


@dataclass
class FetchResult:
    """一次頁面抓取的完整結果，供 availability 判斷使用。"""

    html: str
    http_status: Optional[int]
    final_url: str


def _polite_delay(config: dict, kind: str = "detail") -> None:
    """禮貌延遲：detail 頁 2-5 秒、list 頁 3-6 秒（可由 config 調整）。"""
    crawl_cfg = config.get("crawl", {})
    if kind == "list":
        low = float(crawl_cfg.get("list_delay_min_seconds", 3.0))
        high = float(crawl_cfg.get("list_delay_max_seconds", 6.0))
    else:
        low = float(crawl_cfg.get("delay_min_seconds", 2.0))
        high = float(crawl_cfg.get("delay_max_seconds", 5.0))
    time.sleep(random.uniform(low, high))


@contextmanager
def browser_page(headless: bool = True, block_resources: bool = False) -> Iterator[Page]:
    """開一個設定好的 Playwright page（context manager）。

    block_resources=True 時擋掉圖片/影音/字型請求，大幅加速頁面載入
    （解析只需要 HTML 文字）。每個執行緒必須用自己的 browser_page，
    sync Playwright 物件不可跨執行緒共用。
    """
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-TW",
            viewport={"width": 1366, "height": 900},
        )
        if block_resources:
            def _block(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", _block)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def _with_page_param(url: str, page_no: int) -> str:
    """把 page=N 加進 URL query string。"""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_no)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _scroll_to_bottom(page: Page, max_rounds: int = 12, fast: bool = False) -> None:
    """往下捲動，讓 lazy-loaded 卡片載入。fast 模式用較短等待。"""
    low, high = (120, 280) if fast else (400, 900)
    previous_height = 0
    for _ in range(max_rounds):
        height = page.evaluate("document.body.scrollHeight")
        page.mouse.wheel(0, 2500 if fast else 1500)
        page.wait_for_timeout(random.randint(low, high))
        if height == previous_height:
            break
        previous_height = height


def _save_html(html: str, subdir: str, name: str) -> Optional[str]:
    try:
        directory = PROJECT_ROOT / "raw_pages" / subdir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)
    except OSError as exc:
        logger.warning("儲存 HTML 失敗 %s: %s", name, exc)
        return None


def _fetch_page(page: Page, url: str, timeout_ms: int, networkidle_ms: int = 1000) -> FetchResult:
    response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    if networkidle_ms > 0:
        try:
            # 內容是 SSR，domcontentloaded 後即可解析；networkidle 只等一小段，
            # 591 頁面有持續的背景請求，等太久只是浪費時間
            page.wait_for_load_state("networkidle", timeout=networkidle_ms)
        except PlaywrightTimeoutError:
            logger.debug("networkidle timeout，繼續解析: %s", url)
    return FetchResult(
        html=page.content(),
        http_status=response.status if response else None,
        final_url=page.url,
    )


def _fetch_page_html(page: Page, url: str, timeout_ms: int) -> str:
    return _fetch_page(page, url, timeout_ms).html


def fetch_with_retry(page: Page, url: str, timeout_ms: int, config: dict) -> FetchResult:
    """帶嚴格 retry policy 的抓取：只有 timeout / network error 可以重試。

    max_retries 預設 2、每次重試前等 retry_delay_seconds（預設 10 秒）。
    blocked / CAPTCHA 不在這裡處理（呼叫端偵測後不得重試）。
    """
    crawl_cfg = config.get("crawl", {})
    max_retries = int(crawl_cfg.get("max_retries", 2))
    retry_delay = float(crawl_cfg.get("retry_delay_seconds", 10))
    networkidle_ms = int(crawl_cfg.get("networkidle_wait_ms", 1000))

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _fetch_page(page, url, timeout_ms, networkidle_ms=networkidle_ms)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning("抓取失敗（%s），%d 秒後重試 %d/%d: %s",
                               type(exc).__name__, retry_delay, attempt + 1, max_retries, url)
                time.sleep(retry_delay)
    assert last_exc is not None
    raise last_exc


def _check_blocked(html: str, url: str) -> None:
    if looks_blocked(html):
        raise CaptchaDetectedError(
            f"疑似遇到 CAPTCHA 或反爬阻擋頁：{url}\n"
            "已停止爬取。請稍後再試、降低頻率，或改用瀏覽器手動確認。"
            "本工具不會嘗試繞過驗證機制。"
        )


_TOTAL_COUNT_RE = re.compile(r"已為你找到\s*([0-9,]+)\s*間")
_LIST_PAGE_SIZE = 30


def parse_total_count(html: str) -> Optional[int]:
    """從列表頁抓出「已為你找到 N 間」的總筆數。"""
    m = _TOTAL_COUNT_RE.search(html or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def crawl_list_pages_parallel(
    url: str,
    max_pages: int,
    save_html: bool,
    config: dict,
    headless: bool = True,
) -> list[ListingCard]:
    """多執行緒列表頁掃描：先抓第 1 頁取得總頁數，其餘頁面平行抓。

    每個 worker 有自己的 browser（sync Playwright 不可跨執行緒共用）。
    任一 worker 偵測到 blocked / CAPTCHA 就通知全部停止。
    """
    crawl_cfg = config.get("crawl", {})
    timeout_ms = int(crawl_cfg.get("page_timeout_ms", 30000))
    workers = max(1, int(crawl_cfg.get("parallel_workers", 8)))

    cards_by_id: dict[str, ListingCard] = {}
    lock = threading.Lock()
    stop = threading.Event()
    blocked_errors: list[str] = []

    def _collect(html: str, page_no: int) -> int:
        if save_html:
            _save_html(html, "list", f"page_{page_no:03d}")
        cards = parse_list_page(html)
        with lock:
            new = 0
            for card in cards:
                if card.listing_id not in cards_by_id:
                    cards_by_id[card.listing_id] = card
                    new += 1
        return new

    # 第 1 頁：拿總筆數決定要抓幾頁
    with browser_page(headless=headless, block_resources=True) as page:
        first = _fetch_page(page, _with_page_param(url, 1), timeout_ms)
        _check_blocked(first.html, url)
        _scroll_to_bottom(page, fast=True)
        html = page.content()
        _collect(html, 1)
        total_count = parse_total_count(html)

    if total_count:
        total_pages = min(max_pages, math.ceil(total_count / _LIST_PAGE_SIZE) + 1)
    else:
        total_pages = max_pages
    logger.info("列表總筆數 %s，計畫掃 %d 頁（%d workers）",
                total_count or "未知", total_pages, workers)

    page_queue: "queue.Queue[int]" = queue.Queue()
    for page_no in range(2, total_pages + 1):
        page_queue.put(page_no)

    def worker() -> None:
        if page_queue.empty():
            return
        try:
            with browser_page(headless=headless, block_resources=True) as wpage:
                while not stop.is_set():
                    try:
                        page_no = page_queue.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        fetched = _fetch_page(wpage, _with_page_param(url, page_no), timeout_ms)
                        _check_blocked(fetched.html, url)
                        _scroll_to_bottom(wpage, fast=True)
                        new = _collect(wpage.content(), page_no)
                        logger.info("列表頁 %d/%d：新物件 %d 筆", page_no, total_pages, new)
                    except CaptchaDetectedError as exc:
                        blocked_errors.append(str(exc))
                        stop.set()
                        return
                    except Exception as exc:  # noqa: BLE001 - 單頁失敗不中斷
                        logger.warning("列表頁 %d 失敗：%s", page_no, exc)
                    time.sleep(random.uniform(0.3, 1.0))
        except Exception as exc:  # noqa: BLE001 - worker 啟動失敗
            logger.error("列表 worker 啟動失敗：%s", exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, max(1, total_pages - 1)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if blocked_errors:
        raise CaptchaDetectedError(blocked_errors[0])
    logger.info("列表頁平行掃描完成：共 %d 筆不重複物件", len(cards_by_id))
    return list(cards_by_id.values())


def run_parallel_workers(
    items: list[Any],
    handler: Callable[[Page, Any], bool],
    config: dict,
    headless: bool = True,
    name: str = "detail",
) -> bool:
    """通用多執行緒 worker pool：每個 worker 自帶 browser，從佇列取 item 執行。

    handler(page, item) 回傳 False 表示要全部停止（例如 blocked）。
    回傳是否有 worker 觸發停止。
    """
    crawl_cfg = config.get("crawl", {})
    workers = max(1, int(crawl_cfg.get("parallel_workers", 8)))
    delay_low = float(crawl_cfg.get("parallel_delay_min_seconds", 0.4))
    delay_high = float(crawl_cfg.get("parallel_delay_max_seconds", 1.2))

    item_queue: "queue.Queue[Any]" = queue.Queue()
    for item in items:
        item_queue.put(item)
    stop = threading.Event()

    def worker(worker_no: int) -> None:
        try:
            with browser_page(headless=headless, block_resources=True) as page:
                while not stop.is_set():
                    try:
                        item = item_queue.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        keep_going = handler(page, item)
                    except Exception as exc:  # noqa: BLE001 - handler 已各自處理，保底
                        logger.error("%s worker %d 未預期錯誤：%s", name, worker_no, exc)
                        keep_going = True
                    if not keep_going:
                        stop.set()
                        return
                    time.sleep(random.uniform(delay_low, delay_high))
        except Exception as exc:  # noqa: BLE001
            logger.error("%s worker %d 啟動失敗：%s", name, worker_no, exc)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(min(workers, max(1, len(items))))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stop.is_set()


def crawl_list_pages(
    page: Page,
    url: str,
    max_pages: int,
    save_html: bool,
    config: dict,
) -> list[ListingCard]:
    """逐頁掃描列表頁，回傳去重後的卡片清單。"""
    timeout_ms = int(config.get("crawl", {}).get("page_timeout_ms", 30000))
    cards_by_id: dict[str, ListingCard] = {}

    for page_no in range(1, max_pages + 1):
        page_url = _with_page_param(url, page_no)
        logger.info("列表頁 %d/%d: %s", page_no, max_pages, page_url)
        try:
            html = _fetch_page_html(page, page_url, timeout_ms)
        except PlaywrightTimeoutError as exc:
            logger.error("列表頁載入逾時，停止翻頁: %s", exc)
            break

        _check_blocked(html, page_url)
        _scroll_to_bottom(page)
        html = page.content()

        if save_html:
            _save_html(html, "list", f"page_{page_no:03d}")

        cards = parse_list_page(html)
        new_ids = [c.listing_id for c in cards if c.listing_id not in cards_by_id]
        for card in cards:
            cards_by_id.setdefault(card.listing_id, card)

        logger.info("本頁解析到 %d 筆，其中新物件 %d 筆（累計 %d 筆）",
                    len(cards), len(new_ids), len(cards_by_id))

        if not cards:
            logger.info("本頁沒有任何物件，視為最後一頁，停止翻頁")
            break
        if not new_ids and page_no > 1:
            logger.info("本頁沒有新物件（可能已到底或重複頁），停止翻頁")
            break

        _polite_delay(config, kind="list")

    return list(cards_by_id.values())


def prefilter_cards(cards: list[ListingCard], config: dict) -> list[ListingCard]:
    """爬前預篩：只跳過「從列表卡片就能確定」不符條件的物件。

    卡片上看不出來的資訊（車位型式、管理費）一律保留給詳細頁判斷。
    """
    cfg = config.get("crawl", {}).get("prefilter", {})
    if not cfg.get("enabled", False):
        return cards

    max_price = cfg.get("max_price")
    skip_rooms_gte = cfg.get("skip_rooms_gte")
    skip_suite = cfg.get("skip_suite", False)
    skip_keywords = cfg.get("skip_keywords") or []

    kept: list[ListingCard] = []
    skipped = {"price": 0, "rooms": 0, "suite": 0, "keyword": 0}
    for card in cards:
        if max_price and card.price and card.price > max_price:
            skipped["price"] += 1
            continue
        layout_info = parse_layout(card.layout or card.raw_card_text)
        if skip_rooms_gte and layout_info["rooms"] and layout_info["rooms"] >= skip_rooms_gte:
            skipped["rooms"] += 1
            continue
        if skip_suite and layout_info["is_suite"]:
            skipped["suite"] += 1
            continue
        blob = " ".join(filter(None, [card.title, card.layout, card.raw_card_text]))
        if skip_keywords and any(kw in blob for kw in skip_keywords):
            skipped["keyword"] += 1
            continue
        kept.append(card)

    logger.info("預篩：%d -> %d 筆（跳過 租金超標 %d、房數過多 %d、套雅房 %d、關鍵字 %d）",
                len(cards), len(kept), skipped["price"], skipped["rooms"],
                skipped["suite"], skipped["keyword"])
    return kept


def crawl_detail_pages(
    page: Page,
    cards: list[ListingCard],
    save_html: bool,
    config: dict,
) -> list[Listing]:
    """逐一抓取詳細頁並解析，單筆失敗不中斷。"""
    timeout_ms = int(config.get("crawl", {}).get("page_timeout_ms", 30000))
    life_circles = config.get("life_circles")
    listings: list[Listing] = []

    for index, card in enumerate(cards, start=1):
        detail_url = card.url or DETAIL_URL_TEMPLATE.format(listing_id=card.listing_id)
        logger.info("詳細頁 %d/%d: %s", index, len(cards), detail_url)
        html_path: Optional[str] = None
        try:
            html = _fetch_page_html(page, detail_url, timeout_ms)
            _check_blocked(html, detail_url)
            if save_html:
                html_path = _save_html(html, "detail", card.listing_id)
            listing = parse_detail_page(
                html, listing_id=card.listing_id, url=detail_url,
                card=card, life_circles=life_circles,
            )
            listings.append(listing)
        except CaptchaDetectedError:
            raise
        except Exception as exc:  # noqa: BLE001 - 單筆失敗不可中斷整批
            logger.exception("詳細頁解析失敗: %s", detail_url)
            log_failed_url(card.listing_id, detail_url, str(exc), html_path)
            listings.append(Listing(
                listing_id=card.listing_id,
                url=detail_url,
                title=card.title,
                price=card.price,
                raw_text=card.raw_card_text,
                parse_status="failed",
                parse_error=str(exc)[:500],
            ))
        _polite_delay(config, kind="detail")

    return listings


def crawl(
    url: str,
    max_pages: int = 30,
    headless: bool = True,
    save_html: bool = False,
    config: Optional[dict] = None,
    known_ids: Optional[set[str]] = None,
) -> list[Listing]:
    """完整流程：列表頁 -> 去重 -> 預篩 -> 詳細頁。回傳 Listing 清單。

    known_ids：已抓過的 listing_id（增量模式），這些物件不再進詳細頁。
    """
    config = config or {}
    with browser_page(headless=headless) as page:
        cards = crawl_list_pages(page, url, max_pages, save_html, config)
        logger.info("列表頁掃描完成，共 %d 筆不重複物件", len(cards))
        cards = prefilter_cards(cards, config)
        if known_ids:
            before = len(cards)
            cards = [c for c in cards if c.listing_id not in known_ids]
            logger.info("增量模式：跳過已抓過的 %d 筆，剩 %d 筆", before - len(cards), len(cards))
        listings = crawl_detail_pages(page, cards, save_html, config)
    return listings
