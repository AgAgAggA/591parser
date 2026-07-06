"""列表頁解析：從搜尋結果 HTML 抓出每一筆物件卡片。

策略：不依賴特定 class 名稱（591 前端常改版），
改成掃描所有指向詳細頁的連結（URL 內含數字 id），
再往上找卡片容器，用 regex 從卡片文字抽粗略欄位。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from .models import ListingCard
from .utils import parse_layout, parse_parking, parse_rent, parse_size_ping, truncate

logger = logging.getLogger(__name__)

# 詳細頁連結格式，例如 https://rent.591.com.tw/18123456 或 /rent-detail-18123456.html
_DETAIL_HREF_RE = re.compile(
    r"(?:rent\.591\.com\.tw/|^/)(?:rent-detail-)?(\d{6,10})(?:\.html)?(?:[/?#]|$)"
)

_FLOOR_RE = re.compile(r"(\d+F/\d+F|\d+樓/\d+樓|B\d+F?/\d+F?)")
_ADDRESS_RE = re.compile(r"((?:新竹[縣市])?竹北市?[\u4e00-\u9fff\dA-Za-z]{0,20}(?:路|街|大道|巷)?[\u4e00-\u9fff\d-]{0,10})")


def _extract_listing_id(href: str) -> Optional[str]:
    m = _DETAIL_HREF_RE.search(href)
    return m.group(1) if m else None


def _find_card_container(anchor: Tag) -> Tag:
    """從連結往上找卡片容器：文字量合理（有價格資訊）的最近祖先。"""
    node: Optional[Tag] = anchor
    best = anchor
    for _ in range(6):
        if node is None or not isinstance(node, Tag):
            break
        text = node.get_text(" ", strip=True)
        if "元" in text and 20 < len(text) < 1200:
            best = node
        if len(text) >= 1200:
            break
        node = node.parent
    return best


def _extract_title(anchor: Tag, card_text: str) -> Optional[str]:
    text = anchor.get_text(" ", strip=True)
    if text and len(text) > 4:
        return text[:100]
    img = anchor.find("img")
    if isinstance(img, Tag) and img.get("alt"):
        return str(img["alt"])[:100]
    first_line = card_text.split(" ")[0] if card_text else None
    return first_line[:100] if first_line else None


def parse_list_page(html: str) -> list[ListingCard]:
    """解析一頁列表 HTML，回傳去重後的卡片。"""
    soup = BeautifulSoup(html, "lxml")
    cards: dict[str, ListingCard] = {}

    for anchor in soup.find_all("a", href=True):
        listing_id = _extract_listing_id(str(anchor["href"]))
        if not listing_id or listing_id in cards:
            continue

        container = _find_card_container(anchor)
        card_text = container.get_text(" ", strip=True)

        layout_info = parse_layout(card_text)
        has_parking, _ = parse_parking(card_text)

        floor_match = _FLOOR_RE.search(card_text)
        address_match = _ADDRESS_RE.search(card_text)

        href = str(anchor["href"])
        url = href if href.startswith("http") else f"https://rent.591.com.tw/{listing_id}"

        cards[listing_id] = ListingCard(
            listing_id=listing_id,
            url=url,
            title=_extract_title(anchor, card_text),
            price=parse_rent(card_text),
            layout=layout_info["layout"],
            size_ping=parse_size_ping(card_text),
            floor=floor_match.group(1) if floor_match else None,
            address=address_match.group(1) if address_match else None,
            has_parking_keyword=has_parking,
            raw_card_text=truncate(card_text, 1500),
        )

    logger.debug("列表頁解析出 %d 筆卡片", len(cards))
    return list(cards.values())
