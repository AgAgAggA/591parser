"""availability 偵測、狀態轉移、硬條件 priority 的 unit tests。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.availability import detect_availability
from src.score import DEFAULT_HARD_FILTER, DEFAULT_SCORING, hard_priority_row
from src.state import (
    ListingState,
    apply_status,
    compute_content_hash,
    mark_duplicates,
    mark_missing_from_list,
    mark_seen_in_list,
)

NOW = "2026-07-06 12:00:00"

ACTIVE_TEXT = "2房2廳1衛 21坪 租金 26,500元/月 地 址: 竹北市莊敬北路 車位 平面式"


# ---------------------------------------------------------------------------
# detect_availability（規格書測試案例）
# ---------------------------------------------------------------------------

class TestDetectAvailability:
    def test_removed_keyword(self):
        text = "很抱歉，該房屋已下架"
        assert detect_availability(text, "", 200).status == "removed"

    def test_rented_keyword(self):
        text = "此房屋已出租"
        assert detect_availability(text, "", 200).status == "rented"

    def test_active_detail_page(self):
        assert detect_availability(ACTIVE_TEXT, "", 200).status == "active"

    def test_blocked_captcha(self):
        text = "請完成安全驗證 captcha"
        assert detect_availability(text, "", 200).status == "blocked"

    def test_http_404_is_removed(self):
        result = detect_availability("whatever", "", 404)
        assert result.status == "removed"
        assert result.reason == "removed_or_404"

    def test_http_410_is_removed(self):
        assert detect_availability("", "", 410).status == "removed"

    def test_expired_keyword(self):
        assert detect_availability("此物件刊登已過期", "", 200).status == "expired"

    def test_redirect_to_list_is_removed(self):
        result = detect_availability(ACTIVE_TEXT, "https://rent.591.com.tw/list?kind=1", 200)
        assert result.status == "removed"

    def test_insufficient_content_is_unknown(self):
        assert detect_availability("竹北市 一些雜訊文字", "", 200).status == "unknown"

    def test_empty_page_is_unknown(self):
        assert detect_availability("", "", 200).status == "unknown"

    def test_rented_takes_priority_over_detail_content(self):
        # 已出租的頁面可能仍殘留租金/格局資訊
        text = ACTIVE_TEXT + " 此房屋已出租"
        assert detect_availability(text, "", 200).status == "rented"

    def test_login_wall_text_on_normal_page_not_blocked(self):
        # 正常物件頁常有隱藏的「登入後查看」電話元件，不可誤判 blocked
        text = ACTIVE_TEXT + " 登入後查看完整電話"
        assert detect_availability(text, "", 200).status == "active"

    def test_reason_strings(self):
        assert detect_availability("此房屋已出租", "", 200).reason == "rented_keyword"
        assert detect_availability("物件不存在", "", 200).reason == "removed_or_404"
        assert detect_availability("刊登已過期", "", 200).reason == "expired_keyword"
        assert detect_availability("請完成安全驗證", "", 200).reason == "captcha_or_antibot"
        assert detect_availability(ACTIVE_TEXT, "", 200).reason == "detail_page_ok"


# ---------------------------------------------------------------------------
# 狀態轉移
# ---------------------------------------------------------------------------

class TestStateTransitions:
    def _active_state(self) -> ListingState:
        return ListingState(listing_id="1", availability_status="active")

    def test_missing_from_list_not_removed_without_detail_check(self):
        old = ListingState(listing_id="1", availability_status="active")
        mark_missing_from_list(old)
        # 只要還沒有 detail check，不可以直接 removed
        assert old.availability_status == "active"
        assert old.seen_in_list_page_this_run is False
        assert old.missing_count == 1

    def test_active_to_rented_sets_unavailable_since(self):
        state = self._active_state()
        apply_status(state, "rented", "rented_keyword", NOW)
        assert state.availability_status == "rented"
        assert state.unavailable_since == NOW
        assert state.status_changed is True
        assert state.previous_status == "active"
        assert state.status_change_note == "active -> rented"

    def test_active_to_error_keeps_unavailable_since_empty(self):
        state = self._active_state()
        apply_status(state, "error", "timeout", NOW)
        assert state.availability_status == "error"
        assert state.unavailable_since is None

    def test_active_to_blocked_keeps_unavailable_since_empty(self):
        state = self._active_state()
        apply_status(state, "blocked", "captcha_or_antibot", NOW)
        assert state.unavailable_since is None

    def test_error_to_active_recovers(self):
        state = ListingState(listing_id="1", availability_status="error")
        apply_status(state, "active", "detail_page_ok", NOW)
        assert state.availability_status == "active"
        assert state.unavailable_since is None
        assert state.status_change_note == "error -> active"

    def test_rented_to_active_reactivates(self):
        state = ListingState(
            listing_id="1", availability_status="rented",
            unavailable_since="2026-07-01 00:00:00")
        apply_status(state, "active", "detail_page_ok", NOW)
        assert state.availability_status == "active"
        assert state.status_changed is True
        assert state.unavailable_since is None

    def test_active_to_active_no_change(self):
        state = self._active_state()
        apply_status(state, "active", "detail_page_ok", NOW)
        assert state.status_changed is False
        assert state.status_change_note is None

    def test_seen_in_list_resets_missing(self):
        state = self._active_state()
        state.missing_count = 3
        mark_seen_in_list(state, NOW)
        assert state.seen_in_list_page_this_run is True
        assert state.missing_count == 0
        assert state.seen_count == 1
        assert state.last_seen_at == NOW

    def test_content_hash_changes_with_price(self):
        h1 = compute_content_hash({"title": "a", "price": 30000})
        h2 = compute_content_hash({"title": "a", "price": 31000})
        assert h1 != h2


# ---------------------------------------------------------------------------
# 重複刊登
# ---------------------------------------------------------------------------

class TestDuplicates:
    def _state(self, listing_id: str, score: float, status: str = "active") -> ListingState:
        return ListingState(
            listing_id=listing_id, availability_status=status,
            community_name="某社區", price=30000.0, rooms=2, size_ping=20.0,
            score=score,
        )

    def test_marks_lower_score_as_duplicate(self):
        states = {"1": self._state("1", 80), "2": self._state("2", 70)}
        groups = mark_duplicates(states)
        assert groups == 1
        assert states["1"].is_duplicate is False
        assert states["2"].is_duplicate is True
        assert states["1"].duplicate_group == states["2"].duplicate_group

    def test_non_active_not_grouped(self):
        states = {"1": self._state("1", 80), "2": self._state("2", 70, status="rented")}
        assert mark_duplicates(states) == 0
        assert states["2"].is_duplicate is False


# ---------------------------------------------------------------------------
# state 層級預篩（stale check / refresh-details 不浪費請求）
# ---------------------------------------------------------------------------

class TestStatePrefilter:
    CONFIG = {"crawl": {"prefilter": {
        "enabled": True, "max_price": 32000, "skip_rooms_gte": 4, "skip_suite": True,
        "skip_keywords": ["樓中樓"],
    }}}

    def test_over_price_filtered(self):
        from src.pipeline import _state_prefiltered_out
        state = ListingState(listing_id="1", price=35000.0)
        assert _state_prefiltered_out(state, self.CONFIG) is True

    def test_loft_keyword_filtered(self):
        from src.pipeline import _state_prefiltered_out
        state = ListingState(listing_id="1", price=28000.0, title="漂亮樓中樓出租")
        assert _state_prefiltered_out(state, self.CONFIG) is True

    def test_normal_listing_kept(self):
        from src.pipeline import _state_prefiltered_out
        state = ListingState(listing_id="1", price=28000.0, title="兩房平車", rooms=2)
        assert _state_prefiltered_out(state, self.CONFIG) is False


# ---------------------------------------------------------------------------
# 硬條件 priority
# ---------------------------------------------------------------------------

class TestHardPriority:
    BASE = {
        "rooms": 2, "is_suite": False, "has_parking": True,
        "total_monthly_cost": 30000, "score": 85, "availability_status": "active",
    }

    def test_hard_pass_high_score_is_top(self):
        passed, priority = hard_priority_row(dict(self.BASE), DEFAULT_SCORING, DEFAULT_HARD_FILTER)
        assert passed is True
        assert priority == "優先約看"

    def test_no_parking_cannot_be_top(self):
        row = {**self.BASE, "has_parking": False}
        passed, priority = hard_priority_row(row, DEFAULT_SCORING, DEFAULT_HARD_FILTER)
        assert passed is False
        assert priority == "先跳過"

    def test_unknown_cost_at_most_backup(self):
        row = {**self.BASE, "total_monthly_cost": None}
        passed, priority = hard_priority_row(row, DEFAULT_SCORING, DEFAULT_HARD_FILTER)
        assert passed is False
        assert priority == "可備選"

    def test_non_active_never_top(self):
        row = {**self.BASE, "availability_status": "rented"}
        _, priority = hard_priority_row(row, DEFAULT_SCORING, DEFAULT_HARD_FILTER)
        assert priority == "先跳過"

    def test_suite_excluded(self):
        row = {**self.BASE, "is_suite": True}
        passed, priority = hard_priority_row(row, DEFAULT_SCORING, DEFAULT_HARD_FILTER)
        assert passed is False
        assert priority == "先跳過"
