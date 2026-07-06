"""物件存活狀態判斷（availability detection）。

detect_availability() 只吃純文字 + final_url + http_status，
不依賴 Playwright，方便 unit test。

狀態定義：
  active   物件頁正常存在，仍可出租
  rented   已出租 / 已成交 / 已租出
  removed  已下架 / 不存在 / 404
  expired  刊登過期
  blocked  CAPTCHA / 反爬 / 需登入
  error    連線錯誤、timeout（由呼叫端捕捉 exception 後設定）
  unknown  頁面載入但內容不足以判斷
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

ALL_STATUSES = ("active", "rented", "removed", "expired", "blocked", "error", "unknown")
UNAVAILABLE_STATUSES = ("rented", "removed", "expired")

RENTED_KEYWORDS = [
    "此房屋已出租", "物件已出租", "已被承租", "已出租", "已租出", "已成交",
]
REMOVED_KEYWORDS = [
    "此房屋不存在", "房屋不存在", "物件不存在", "查無此物件", "頁面不存在",
    "該房屋已下架", "物件已下架", "已下架", "找不到頁面",
]
EXPIRED_KEYWORDS = ["刊登已過期", "房屋已過期", "物件已過期"]
BLOCKED_KEYWORDS = [
    "captcha", "geetest", "驗證碼", "請完成驗證", "安全驗證", "滑動驗證",
    "登入後查看", "訪問過於頻繁", "access denied", "forbidden", "cloudflare",
    "存取被拒",
]

# 判斷「頁面確實還有物件細節」的訊號：租金 + 至少一項物件資訊
_PRICE_MARKER_RE = re.compile(r"\d[\d,]*\s*元\s*/\s*月")
_DETAIL_MARKERS = ["坪", "押金", "格局", "地址", "地 址", "樓層", "電梯", "房"]

# 詳細頁被 redirect 回首頁 / 列表頁，視為物件已不存在
_REDIRECT_TO_LIST_RE = re.compile(r"rent\.591\.com\.tw/?(?:\?[^/]*)?$|rent\.591\.com\.tw/list")


@dataclass
class AvailabilityResult:
    status: str
    reason: str
    matched_text: Optional[str] = None


def page_text_from_html(html: Optional[str]) -> str:
    """把 HTML 轉成純文字，供 detect_availability 使用。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _find_keyword(text: str, keywords: list[str]) -> Optional[str]:
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            return text[max(0, idx - 20): idx + len(kw) + 20]
    return None


def _has_detail_content(text: str) -> bool:
    if not _PRICE_MARKER_RE.search(text):
        return False
    return any(marker in text for marker in _DETAIL_MARKERS)


def detect_availability(
    page_text: str,
    final_url: str,
    http_status: Optional[int],
) -> AvailabilityResult:
    """判斷單一物件詳細頁的存活狀態。

    判斷順序：404 -> rented -> removed -> expired -> blocked -> active -> unknown。
    明確的下架/出租文字優先於 blocked（已出租頁也可能殘留登入元件文字）；
    blocked 只在頁面沒有完整物件細節時成立，避免誤判正常頁面上的
    隱藏登入視窗文字（例如「登入後查看」）。
    """
    text = page_text or ""
    lowered = text.lower()
    has_detail = _has_detail_content(text)

    if http_status in (404, 410):
        return AvailabilityResult("removed", "removed_or_404", f"http {http_status}")

    if final_url and _REDIRECT_TO_LIST_RE.search(final_url):
        return AvailabilityResult("removed", "removed_or_404", f"redirected to {final_url}")

    snippet = _find_keyword(text, RENTED_KEYWORDS)
    if snippet:
        return AvailabilityResult("rented", "rented_keyword", snippet)

    snippet = _find_keyword(text, REMOVED_KEYWORDS)
    if snippet:
        return AvailabilityResult("removed", "removed_or_404", snippet)
    if not has_detail and re.search(r"(?<!\d)404(?!\d)", text):
        return AvailabilityResult("removed", "removed_or_404", "404 in page text")

    snippet = _find_keyword(text, EXPIRED_KEYWORDS)
    if snippet:
        return AvailabilityResult("expired", "expired_keyword", snippet)

    if not has_detail:
        blocked = _find_keyword(lowered, BLOCKED_KEYWORDS)
        if blocked:
            return AvailabilityResult("blocked", "captcha_or_antibot", blocked)

    if has_detail:
        return AvailabilityResult("active", "detail_page_ok", None)

    return AvailabilityResult("unknown", "insufficient_content", text[:80] or "(empty page)")
