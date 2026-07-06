"""Persistent listing state（SQLite）。

每個 listing 保留跨執行的歷史狀態：first_seen / last_seen / availability /
seen_count / missing_count / content_hash 等。物件即使 removed / rented
也「永不刪除」，供之後分析流動速度、重複刊登、重新上架。

狀態轉移規則（apply_status）：
  active -> rented/removed/expired : 設 unavailable_since = now
  active -> error/blocked          : 保留舊資料，不設 unavailable_since
  error/blocked -> active          : recover，清除 unavailable_since
  rented/removed/expired -> active : reactivate，status_changed = True
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .availability import UNAVAILABLE_STATUSES
from .utils import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "data/listings_state.sqlite"

# 進 stale check 的舊狀態（之前有效或不確定，這次列表沒看到就要確認）
STALE_CHECK_STATUSES = ("active", "unknown", "error")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ListingState(BaseModel):
    """一筆 listing 的完整持久化狀態。"""

    listing_id: str
    url: str = ""

    # 物件摘要（供 CSV / report 直接使用）
    title: Optional[str] = None
    community_name: Optional[str] = None
    address: Optional[str] = None
    layout: Optional[str] = None
    rooms: Optional[int] = None
    is_suite: Optional[bool] = None
    size_ping: Optional[float] = None
    floor: Optional[str] = None
    total_floors: Optional[int] = None
    price: Optional[float] = None
    management_fee: Optional[float] = None
    parking_fee: Optional[float] = None
    total_monthly_cost: Optional[float] = None
    cost_confidence: Optional[str] = None
    has_parking: Optional[bool] = None
    parking_type: Optional[str] = None
    life_circle_guess: Optional[str] = None
    owner_direct: Optional[bool] = None
    agent_type: Optional[str] = None
    score: Optional[float] = None
    priority: Optional[str] = None
    hard_pass: bool = False

    # 存活狀態
    availability_status: str = "unknown"
    availability_reason: str = ""
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    unavailable_since: Optional[str] = None
    seen_count: int = 0
    missing_count: int = 0
    last_successful_parse_at: Optional[str] = None
    last_parse_error: Optional[str] = None
    last_raw_status_text: Optional[str] = None
    last_http_status: Optional[int] = None
    last_redirect_url: Optional[str] = None
    content_hash: Optional[str] = None

    # 本輪執行旗標
    seen_in_list_page_this_run: bool = False
    detail_checked_this_run: bool = False
    new_this_run: bool = False
    status_changed: bool = False
    previous_status: Optional[str] = None
    status_change_note: Optional[str] = None

    # 重複刊登
    is_duplicate: bool = False
    duplicate_group: Optional[str] = None

    # 完整解析結果（description、電梯、家具等，供 report 顯示）
    payload: dict[str, Any] = Field(default_factory=dict)


# 輸出 CSV 的欄位順序
STATE_COLUMNS: list[str] = [
    "listing_id", "url", "title", "community_name", "address",
    "layout", "rooms", "is_suite", "size_ping", "floor", "total_floors",
    "price", "management_fee", "parking_fee", "total_monthly_cost", "cost_confidence",
    "has_parking", "parking_type", "life_circle_guess",
    "owner_direct", "agent_type", "score", "priority", "hard_pass",
    "availability_status", "availability_reason",
    "first_seen_at", "last_seen_at", "last_checked_at", "unavailable_since",
    "seen_in_list_page_this_run", "detail_checked_this_run", "new_this_run",
    "status_changed", "previous_status", "status_change_note",
    "seen_count", "missing_count",
    "last_successful_parse_at", "last_parse_error", "last_raw_status_text",
    "last_http_status", "last_redirect_url", "content_hash",
    "is_duplicate", "duplicate_group",
]


# ---------------------------------------------------------------------------
# 狀態轉移
# ---------------------------------------------------------------------------

def apply_status(
    state: ListingState,
    new_status: str,
    reason: str,
    now: str,
    raw_status_text: Optional[str] = None,
    http_status: Optional[int] = None,
    redirect_url: Optional[str] = None,
) -> ListingState:
    """套用一次 availability 判斷結果，處理狀態轉移與時間戳。"""
    old_status = state.availability_status
    state.last_checked_at = now
    if raw_status_text is not None:
        state.last_raw_status_text = raw_status_text[:200]
    if http_status is not None:
        state.last_http_status = http_status
    if redirect_url is not None:
        state.last_redirect_url = redirect_url

    if new_status != old_status:
        state.status_changed = True
        state.previous_status = old_status
        state.status_change_note = f"{old_status} -> {new_status}"

    state.availability_status = new_status
    state.availability_reason = reason

    if new_status in UNAVAILABLE_STATUSES:
        if not state.unavailable_since:
            state.unavailable_since = now
    elif new_status == "active":
        # recover / reactivate：物件確認仍有效
        state.unavailable_since = None
    # error / blocked / unknown：保留舊 unavailable_since，不做任何猜測
    return state


def mark_seen_in_list(state: ListingState, now: str) -> None:
    state.seen_in_list_page_this_run = True
    state.last_seen_at = now
    state.seen_count += 1
    state.missing_count = 0


def mark_missing_from_list(state: ListingState) -> None:
    """本次列表頁沒看到：只累計 missing，不改 availability_status。

    絕對不可以只因列表缺席就標 removed，要等 detail check 確認。
    """
    state.seen_in_list_page_this_run = False
    state.missing_count += 1


def reset_run_flags(state: ListingState) -> None:
    state.seen_in_list_page_this_run = False
    state.detail_checked_this_run = False
    state.new_this_run = False
    state.status_changed = False
    state.previous_status = None
    state.status_change_note = None


def compute_content_hash(row: dict[str, Any]) -> str:
    """物件核心內容的指紋，變動時可偵測改價/改內容。"""
    key = "|".join(str(row.get(k) or "") for k in (
        "title", "price", "layout", "size_ping", "address", "management_fee", "parking_fee",
    ))
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def update_from_parsed_listing(state: ListingState, row: dict[str, Any], now: str) -> None:
    """detail parse 成功後，把解析結果寫回 state。"""
    state.detail_checked_this_run = True
    state.last_successful_parse_at = now
    state.last_parse_error = None
    state.content_hash = compute_content_hash(row)
    state.payload = {k: v for k, v in row.items() if k != "raw_text"}

    def _num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    state.url = str(row.get("url") or state.url)
    state.title = row.get("title") or state.title
    state.community_name = row.get("community_name") or state.community_name
    state.address = row.get("address") or state.address
    state.layout = row.get("layout") or state.layout
    state.rooms = int(row["rooms"]) if row.get("rooms") not in (None, "") else state.rooms
    state.is_suite = bool(row.get("is_suite")) if row.get("is_suite") is not None else state.is_suite
    state.size_ping = _num(row.get("size_ping")) or state.size_ping
    state.floor = str(row.get("floor")) if row.get("floor") not in (None, "") else state.floor
    state.total_floors = int(row["total_floors"]) if row.get("total_floors") not in (None, "") else state.total_floors
    state.price = _num(row.get("price")) or state.price
    state.management_fee = _num(row.get("management_fee")) if row.get("management_fee") is not None else state.management_fee
    state.parking_fee = _num(row.get("parking_fee")) if row.get("parking_fee") is not None else state.parking_fee
    state.total_monthly_cost = _num(row.get("total_monthly_cost")) or state.total_monthly_cost
    state.cost_confidence = row.get("cost_confidence") or state.cost_confidence
    state.has_parking = bool(row.get("has_parking")) if row.get("has_parking") is not None else state.has_parking
    state.parking_type = row.get("parking_type") or state.parking_type
    state.life_circle_guess = row.get("life_circle_guess") or state.life_circle_guess
    state.owner_direct = row.get("owner_direct") if row.get("owner_direct") is not None else state.owner_direct
    state.agent_type = row.get("agent_type") or state.agent_type


def is_stale_check_candidate(state: ListingState) -> bool:
    """需要 detail availability check 的舊物件。

    條件：之前狀態是 active/unknown/error、本次列表頁沒看到、
    missing_count >= 1，且不是「因預篩而從未檢查」的物件。
    """
    return (
        state.availability_status in STALE_CHECK_STATUSES
        and not state.seen_in_list_page_this_run
        and state.missing_count >= 1
        and state.availability_reason != "prefiltered_not_checked"
    )


# ---------------------------------------------------------------------------
# 重複刊登偵測
# ---------------------------------------------------------------------------

def _duplicate_key(state: ListingState) -> Optional[str]:
    """同社區(或地址) + 同租金 + 同房數 + 相近坪數 -> 視為同一物件的重複刊登。"""
    if state.price is None or state.rooms is None:
        return None
    place = (state.community_name or state.address or "").strip()
    if not place:
        return None
    size = round(state.size_ping) if state.size_ping else 0
    return f"{place}|{int(state.price)}|{state.rooms}|{size}"


def mark_duplicates(states: dict[str, ListingState]) -> int:
    """在 active 物件中標記重複刊登，回傳重複群組數。

    每組保留分數最高（同分取最早看到）的一筆，其餘 is_duplicate = True。
    """
    groups: dict[str, list[ListingState]] = {}
    for state in states.values():
        state.is_duplicate = False
        state.duplicate_group = None
        if state.availability_status != "active":
            continue
        key = _duplicate_key(state)
        if key:
            groups.setdefault(key, []).append(state)

    dup_groups = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        dup_groups += 1
        members.sort(key=lambda s: (-(s.score or 0), s.first_seen_at or "9999"))
        for member in members:
            member.duplicate_group = key
        for member in members[1:]:
            member.is_duplicate = True
    return dup_groups


# ---------------------------------------------------------------------------
# SQLite 存取
# ---------------------------------------------------------------------------

class StateStore:
    """SQLite-backed state store。一個 table，payload 存 JSON。"""

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH):
        p = Path(path)
        self.path = p if p.is_absolute() else PROJECT_ROOT / p
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        columns = ", ".join(f"{c} TEXT" for c in STATE_COLUMNS if c != "listing_id")
        with self._connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS listings (listing_id TEXT PRIMARY KEY, {columns}, payload_json TEXT)"
            )

    def load(self) -> dict[str, ListingState]:
        states: dict[str, ListingState] = {}
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM listings"):
                data = dict(row)
                payload_json = data.pop("payload_json", None)
                parsed: dict[str, Any] = {}
                for key, value in data.items():
                    if value is None:
                        continue
                    parsed[key] = value
                try:
                    payload = json.loads(payload_json) if payload_json else {}
                except json.JSONDecodeError:
                    payload = {}
                try:
                    states[str(data["listing_id"])] = ListingState(**{**_coerce_types(parsed), "payload": payload})
                except Exception as exc:  # noqa: BLE001 - 單筆壞資料不可讓整個 state 掛掉
                    logger.warning("讀取 state 失敗 listing_id=%s: %s", data.get("listing_id"), exc)
        logger.info("載入 state：%d 筆（%s）", len(states), self.path)
        return states

    def save(self, states: dict[str, ListingState]) -> None:
        rows = []
        for state in states.values():
            data = state.model_dump()
            payload = data.pop("payload", {})
            values = [_to_db(data.get(c)) for c in STATE_COLUMNS]
            values.append(json.dumps(payload, ensure_ascii=False, default=str))
            rows.append(values)
        placeholders = ", ".join("?" for _ in range(len(STATE_COLUMNS) + 1))
        columns = ", ".join(STATE_COLUMNS + ["payload_json"])
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO listings ({columns}) VALUES ({placeholders})", rows
            )
        logger.info("儲存 state：%d 筆 -> %s", len(states), self.path)


_BOOL_FIELDS = {
    "is_suite", "has_parking", "owner_direct", "hard_pass",
    "seen_in_list_page_this_run", "detail_checked_this_run", "new_this_run",
    "status_changed", "is_duplicate",
}
_INT_FIELDS = {"rooms", "total_floors", "seen_count", "missing_count", "last_http_status"}
_FLOAT_FIELDS = {"size_ping", "price", "management_fee", "parking_fee", "total_monthly_cost", "score"}


def _to_db(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def _coerce_types(data: dict[str, Any]) -> dict[str, Any]:
    """SQLite TEXT 欄位讀回來時轉回正確型別。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value is None or value == "":
            continue
        try:
            if key in _BOOL_FIELDS:
                out[key] = str(value).strip().lower() in ("1", "true", "yes")
            elif key in _INT_FIELDS:
                out[key] = int(float(value))
            elif key in _FLOAT_FIELDS:
                out[key] = float(value)
            else:
                out[key] = value
        except (TypeError, ValueError):
            out[key] = None
    return out
