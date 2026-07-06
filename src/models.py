"""Pydantic 資料模型：一筆租屋物件的所有欄位。

所有欄位都允許 None，解析失敗時保持 None，不讓程式 crash。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ListingCard(BaseModel):
    """列表頁上的一張卡片（粗略資訊）。"""

    listing_id: str
    url: str
    title: Optional[str] = None
    price: Optional[int] = None
    layout: Optional[str] = None
    size_ping: Optional[float] = None
    floor: Optional[str] = None
    address: Optional[str] = None
    has_parking_keyword: bool = False
    raw_card_text: Optional[str] = None


class Listing(BaseModel):
    """詳細頁解析後的完整物件。"""

    listing_id: str
    url: str
    title: Optional[str] = None
    community_name: Optional[str] = None

    # 金額
    price: Optional[int] = None
    management_fee: Optional[int] = None
    parking_fee: Optional[int] = None
    total_monthly_cost: Optional[int] = None
    cost_confidence: str = "low"  # high / medium / low
    deposit: Optional[str] = None

    # 格局
    layout: Optional[str] = None
    rooms: Optional[int] = None
    living_rooms: Optional[int] = None
    bathrooms: Optional[int] = None
    is_suite: bool = False
    size_ping: Optional[float] = None
    floor: Optional[str] = None
    total_floors: Optional[int] = None

    # 位置
    address: Optional[str] = None
    section_area: Optional[str] = None
    life_circle_guess: str = "其他"
    life_circle_score: Optional[float] = None
    distance_to_taiyuan_note: Optional[str] = None

    # 車位
    has_parking: bool = False
    parking_type: str = "none"  # flat / mechanical / unknown / none
    parking_score: float = 0.0
    parking_included_in_rent: Optional[bool] = None

    # 條件
    has_elevator: Optional[bool] = None
    can_cook: Optional[bool] = None
    can_register_household: Optional[bool] = None
    can_tax_report: Optional[bool] = None
    owner_direct: Optional[bool] = None
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None  # 屋主 / 仲介 / 代理人
    available_now: Optional[bool] = None
    furniture_appliances: Optional[str] = None

    description: Optional[str] = None
    images_count: Optional[int] = None
    posted_time: Optional[str] = None
    updated_time: Optional[str] = None

    raw_text: Optional[str] = Field(default=None, repr=False)
    parse_status: str = "ok"  # ok / partial / failed
    parse_error: Optional[str] = None

    def to_row(self) -> dict:
        return self.model_dump()


# CSV 欄位順序（與規格書一致，加上解析輔助欄位）
LISTING_COLUMNS: list[str] = [
    "listing_id", "url", "title", "community_name",
    "price", "management_fee", "parking_fee", "total_monthly_cost",
    "cost_confidence", "deposit",
    "layout", "rooms", "living_rooms", "bathrooms", "is_suite",
    "size_ping", "floor", "total_floors",
    "address", "section_area", "life_circle_guess", "life_circle_score",
    "distance_to_taiyuan_note",
    "has_parking", "parking_type", "parking_score", "parking_included_in_rent",
    "has_elevator", "can_cook", "can_register_household", "can_tax_report",
    "owner_direct", "agent_name", "agent_type", "available_now",
    "furniture_appliances", "description", "images_count",
    "posted_time", "updated_time",
    "raw_text", "parse_status", "parse_error",
]
