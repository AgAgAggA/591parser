"""詳細頁解析：把單一物件頁面 HTML 轉成 Listing。

策略：先試常見 selector（h1 標題、dl/dt 資訊欄位），
任何 selector 失敗都 fallback 到整頁文字 + regex。
任一欄位解析失敗填 None，絕不 raise（除非整頁完全無法讀）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag

from .models import Listing, ListingCard
from .score import DEFAULT_SCORING, parking_score as compute_parking_score
from .utils import (
    compute_total_cost,
    distance_to_taiyuan_note,
    guess_life_circle_layered,
    parse_bool_yes_no,
    parse_fee,
    parse_floor,
    parse_layout,
    parse_money,
    parse_parking,
    parse_rent,
    parse_size_ping,
    parking_included_in_rent,
    truncate,
)

logger = logging.getLogger(__name__)

_FURNITURE_KEYWORDS = [
    "冰箱", "洗衣機", "電視", "冷氣", "熱水器", "床", "衣櫃", "沙發",
    "桌椅", "書桌", "微波爐", "烘衣機", "洗碗機", "廚具", "窗簾",
]


def _full_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "header", "nav", "footer"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


# 內容區之後的雜訊區塊（推薦物件、檢舉、footer）
_CONTENT_END_MARKERS = ["熱門社區推薦", "檢舉此房屋", "591提醒您", "同租金、同鄉鎮"]


def _content_slice(text: str, listing_id: str) -> str:
    """把整頁文字裁切到物件本身的內容區。

    591 頁面上方導覽列含「獨立套房/雅房/591房屋交易」等字樣，
    下方又有推薦物件（別的物件的房數與價格），直接用整頁文字
    解析會嚴重誤判，所以用物件編號 R{id} 與推薦區標記裁切。
    """
    start = 0
    marker = f"R{listing_id}"
    idx = text.find(marker)
    if idx >= 0:
        start = idx
    else:
        for fallback in ("位置與周邊", "屋況介紹"):
            idx = text.find(fallback)
            if idx >= 0:
                start = idx
                break

    end = len(text)
    for marker in _CONTENT_END_MARKERS:
        idx = text.find(marker, start)
        if 0 <= idx < end:
            end = idx
    return text[start:end]


def _extract_title(soup: BeautifulSoup, text: str) -> Optional[str]:
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        title = h1.get_text(" ", strip=True)
        if title:
            return title[:150]
    if soup.title and soup.title.string:
        # 去掉 591 頁面標題的尾綴
        return re.split(r"[|｜-]\s*591", str(soup.title.string))[0].strip()[:150] or None
    return None


def _extract_field(text: str, label: str, max_len: int = 30) -> Optional[str]:
    """從整頁文字抓 '標籤：值' 或 '標籤 值' 形式的欄位。"""
    m = re.search(rf"{label}\s*[:：]?\s*([^\s，,。；;]{{1,{max_len}}})", text)
    return m.group(1) if m else None


def _extract_address(text: str) -> Optional[str]:
    # 「地 址: 竹北市勝利八街二段」是詳細頁最準確的來源
    m = re.search(r"地\s*址\s*[:：]\s*((?:新竹[縣市])?竹北市?[^\s，,。]{2,25})", text)
    if m:
        return m.group(1)
    m = re.search(
        r"((?:新竹[縣市])?竹北市[\u4e00-\u9fff\dA-Za-z]{1,15}(?:路|街|大道|巷)[\u4e00-\u9fff\d之-]{0,15}(?:號|段)?)",
        text,
    )
    if m:
        return m.group(1)
    m = re.search(r"(新竹[縣市]竹北市[\u4e00-\u9fff\d]{2,20})", text)
    return m.group(1) if m else None


def _extract_deposit(text: str) -> Optional[str]:
    m = re.search(r"押金\s*[:：]?\s*(面議|[一二三四五六七八九十\d]+個月|[\d,]+\s*元?)", text)
    return m.group(1).strip() if m else None


def _extract_dates(text: str) -> tuple[Optional[str], Optional[str]]:
    posted, updated = None, None
    m = re.search(r"(?:刊登|發布|發佈)[^\d]{0,6}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
    if m:
        posted = m.group(1)
    if not posted:
        # 591 常見寫法：'此房屋在4天前發佈'
        m = re.search(r"(\d+\s*(?:分鐘|小時|天|週|個月)前)\s*發[佈布]", text)
        if m:
            posted = m.group(1)
    m = re.search(r"更新[^\d]{0,6}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
    if m:
        updated = m.group(1)
    if not updated:
        # '(12分鐘內更新)'
        m = re.search(r"(\d+\s*(?:分鐘|小時|天|週|個月)內?)\s*更新", text)
        if m:
            updated = m.group(1)
    return posted, updated


def _extract_agent(text: str) -> tuple[Optional[bool], Optional[str], Optional[str]]:
    """回傳 (owner_direct, agent_name, agent_type)。"""
    owner_direct: Optional[bool] = None
    agent_type: Optional[str] = None
    if re.search(r"屋主(?:直租|刊登|自租)|房東(?:直租|刊登)", text):
        owner_direct, agent_type = True, "屋主"
    elif "仲介" in text:
        owner_direct, agent_type = False, "仲介"
    elif "代理人" in text:
        owner_direct, agent_type = False, "代理人"
    m = re.search(r"(?:聯絡人|經紀人)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z]{2,10})", text)
    agent_name = m.group(1) if m else None
    return owner_direct, agent_name, agent_type


def _extract_furniture(text: str) -> Optional[str]:
    found = [kw for kw in _FURNITURE_KEYWORDS if kw in text]
    return "、".join(found) if found else None


def _extract_description(soup: BeautifulSoup, text: str) -> Optional[str]:
    # 常見的描述容器 class 關鍵字
    for pattern in ["description", "house-detail", "detail-info", "article"]:
        node = soup.find(class_=re.compile(pattern, re.I))
        if isinstance(node, Tag):
            desc = node.get_text(" ", strip=True)
            if len(desc) > 30:
                return truncate(desc, 3000)
    # fallback：找 '屋況說明' 或 '房屋介紹' 之後的一段文字
    m = re.search(r"(?:屋況說明|房屋介紹|物件說明)\s*[:：]?\s*(.{30,2000})", text)
    return truncate(m.group(1), 3000) if m else None


def _count_images(soup: BeautifulSoup) -> Optional[int]:
    imgs = [
        img for img in soup.find_all("img")
        if isinstance(img, Tag) and re.search(r"591|house|photo|img\d", str(img.get("src", "")), re.I)
    ]
    return len(imgs) if imgs else None


def _fee_contexts(text: str, label: str) -> list[str]:
    """抓標籤附近的所有文字片段給 parse_fee 用（例如 '管理費 另計 3,000 元'）。

    把「免/無/不含/含」等前綴一併帶入片段，讓 parse_fee 能判斷
    '免管理費' 這種寫法，而不是誤抓後面的其他金額。
    """
    return [
        (m.group(1) or "") + " " + m.group(2)
        for m in re.finditer(rf"(免|無|不含|含)?{label}\s*[:：】]?\s*([^\n]{{0,25}})", text)
    ]


def _parse_fee_best(text: str, labels: list[str]) -> tuple[Optional[int], str]:
    """對多個標籤與多次出現逐一嘗試，回傳第一個能確定的結果。"""
    fallback: tuple[Optional[int], str] = (None, "unknown")
    for label in labels:
        for context in _fee_contexts(text, label):
            amount, status = parse_fee(context)
            if status != "unknown":
                return amount, status
    return fallback


def parse_detail_page(
    html: str,
    listing_id: str,
    url: str,
    card: Optional[ListingCard] = None,
    life_circles: Optional[list[dict[str, Any]]] = None,
) -> Listing:
    """解析詳細頁 HTML，欄位盡量填，失敗填 None。"""
    soup = BeautifulSoup(html, "lxml")
    full_text = _full_text(soup)
    # 只在物件內容區解析，避免導覽列（套房/雅房分類連結）與推薦物件干擾
    text = _content_slice(full_text, listing_id)
    errors: list[str] = []

    def safe(fn, default=None, name: str = ""):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 單欄位失敗不可中斷
            errors.append(f"{name or fn.__name__}: {exc}")
            return default

    title = safe(lambda: _extract_title(soup, text), name="title") or (card.title if card else None)

    # --- 金額 ---
    price = safe(lambda: parse_rent(text), name="price") or (card.price if card else None)
    mgmt_fee, mgmt_status = safe(
        lambda: _parse_fee_best(text, ["管理費"]),
        default=(None, "unknown"), name="mgmt_fee")
    # 「含管理費 / 租金含管理費」寫在描述裡時，不可顯示費用未知
    if mgmt_status == "unknown" and re.search(r"含管理費|管理費已?含|租金含管理", text):
        mgmt_fee, mgmt_status = 0, "included"

    # --- 車位 ---
    has_parking, parking_type = safe(lambda: parse_parking(text), default=(False, "none"), name="parking")
    if not has_parking and card and card.has_parking_keyword:
        has_parking, parking_type = True, "unknown"
    parking_fee, parking_status = (
        safe(lambda: _parse_fee_best(text, ["車位租金", "車位費用?"]),
             default=(None, "unknown"), name="parking_fee")
        if has_parking else (0, "none"))
    included = parking_included_in_rent(text)
    if included is True:
        parking_fee, parking_status = 0, "included"

    total_cost, cost_confidence = compute_total_cost(
        price, mgmt_fee, mgmt_status, parking_fee, parking_status, has_parking,
    )

    # --- 格局 / 坪數 / 樓層 ---
    layout_basis = " ".join(filter(None, [title, text]))
    layout_info = safe(lambda: parse_layout(layout_basis), default=parse_layout(None), name="layout")
    size_ping = safe(lambda: parse_size_ping(text), name="size") or (card.size_ping if card else None)
    floor, total_floors = safe(lambda: parse_floor(text), default=(None, None), name="floor")

    # --- 位置 / 生活圈 ---
    # 分層判斷：地址 > 標題 > 內文。避免描述中的「x分鐘到台元」把
    # 華興/市公所等其他生活圈的物件誤判成台元。
    address = safe(lambda: _extract_address(text), name="address") or (card.address if card else None)
    life_circle = guess_life_circle_layered(address, title, text[:3000], life_circles)

    # --- 條件 ---
    posted_time, updated_time = safe(lambda: _extract_dates(full_text), default=(None, None), name="dates")
    owner_direct, agent_name, agent_type = safe(
        lambda: _extract_agent(text), default=(None, None, None), name="agent")

    listing = Listing(
        listing_id=listing_id,
        url=url,
        title=title,
        community_name=safe(lambda: _extract_field(text, "社區"), name="community"),
        price=price,
        management_fee=mgmt_fee,
        parking_fee=parking_fee if has_parking else None,
        total_monthly_cost=total_cost,
        cost_confidence=cost_confidence,
        deposit=safe(lambda: _extract_deposit(text), name="deposit"),
        layout=layout_info["layout"] or (card.layout if card else None),
        rooms=layout_info["rooms"],
        living_rooms=layout_info["living_rooms"],
        bathrooms=layout_info["bathrooms"],
        is_suite=layout_info["is_suite"],
        size_ping=size_ping,
        floor=floor or (card.floor if card else None),
        total_floors=total_floors,
        address=address,
        section_area="竹北市",
        life_circle_guess=life_circle,
        distance_to_taiyuan_note=distance_to_taiyuan_note(life_circle),
        has_parking=has_parking,
        parking_type=parking_type,
        parking_score=compute_parking_score(parking_type, DEFAULT_SCORING),
        parking_included_in_rent=included,
        has_elevator=parse_bool_yes_no(text, "有電梯", "無電梯"),
        can_cook=parse_bool_yes_no(text, "可開伙", "不可開伙"),
        can_register_household=parse_bool_yes_no(text, "可遷入戶籍", "不可遷入戶籍"),
        can_tax_report=parse_bool_yes_no(text, "可報稅", "不可報稅"),
        owner_direct=owner_direct,
        agent_name=agent_name,
        agent_type=agent_type,
        available_now=any(kw in text for kw in ("隨時入住", "即可入住", "隨時可遷入", "可隨時遷入")) or None,
        furniture_appliances=safe(lambda: _extract_furniture(text), name="furniture"),
        description=safe(lambda: _extract_description(soup, text), name="description"),
        images_count=safe(lambda: _count_images(soup), name="images"),
        posted_time=posted_time,
        updated_time=updated_time,
        raw_text=truncate(text),
        parse_status="partial" if errors else "ok",
        parse_error="; ".join(errors)[:500] if errors else None,
    )
    return listing
