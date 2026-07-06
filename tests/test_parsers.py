"""金額、格局、車位、生活圈解析的 unit tests。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.crawler import prefilter_cards
from src.models import ListingCard
from src.score import DEFAULT_SCORING, cost_score, layout_score, parking_score, score_row
from src.utils import (
    compute_total_cost,
    guess_life_circle,
    parse_fee,
    parse_floor,
    parse_layout,
    parse_money,
    parse_parking,
    parse_rent,
    parse_size_ping,
    parking_included_in_rent,
)


# ---------------------------------------------------------------------------
# 金額
# ---------------------------------------------------------------------------

class TestMoney:
    def test_parse_rent_with_comma(self):
        assert parse_rent("30,000 元/月") == 30000

    def test_parse_rent_no_comma(self):
        assert parse_rent("租金 28500元/月 押金兩個月") == 28500

    def test_parse_rent_none(self):
        assert parse_rent(None) is None
        assert parse_rent("面議") is None

    def test_parse_money(self):
        assert parse_money("另計 3,000 元") == 3000

    def test_fee_included(self):
        amount, status = parse_fee("含管理費")
        assert amount == 0
        assert status == "included"

    def test_fee_extra_with_amount(self):
        amount, status = parse_fee("另計 3,000 元")
        assert amount == 3000
        assert status == "extra"

    def test_fee_extra_unknown_amount(self):
        amount, status = parse_fee("車位另計")
        assert amount is None
        assert status == "unknown"

    def test_fee_none(self):
        amount, status = parse_fee("無")
        assert amount == 0
        assert status == "none"

    def test_total_cost_high_confidence(self):
        total, confidence = compute_total_cost(30000, 2000, "extra", 0, "included", True)
        assert total == 32000
        assert confidence == "high"

    def test_total_cost_medium_confidence(self):
        total, confidence = compute_total_cost(30000, None, "unknown", 0, "included", True)
        assert total == 30000
        assert confidence == "medium"

    def test_total_cost_low_confidence_no_rent(self):
        total, confidence = compute_total_cost(None, 2000, "extra", 0, "none", False)
        assert total is None
        assert confidence == "low"

    def test_total_cost_no_parking_ignores_parking_fee(self):
        total, confidence = compute_total_cost(30000, 0, "included", None, "unknown", False)
        assert total == 30000
        assert confidence == "high"


# ---------------------------------------------------------------------------
# 格局
# ---------------------------------------------------------------------------

class TestLayout:
    def test_full_layout(self):
        info = parse_layout("2房2廳1衛")
        assert info["rooms"] == 2
        assert info["living_rooms"] == 2
        assert info["bathrooms"] == 1
        assert info["layout"] == "2房2廳1衛"
        assert info["is_suite"] is False

    def test_rooms_only(self):
        info = parse_layout("3房")
        assert info["rooms"] == 3
        assert info["living_rooms"] is None

    def test_rooms_and_living(self):
        info = parse_layout("2房2廳")
        assert info["rooms"] == 2
        assert info["living_rooms"] == 2
        assert info["bathrooms"] is None

    def test_suite(self):
        info = parse_layout("獨立套房，近台元")
        assert info["is_suite"] is True

    def test_empty(self):
        info = parse_layout(None)
        assert info["rooms"] is None
        assert info["is_suite"] is False

    def test_size_ping(self):
        assert parse_size_ping("25.5坪 電梯大樓") == 25.5

    def test_floor(self):
        floor, total = parse_floor("樓層：5F/12F")
        assert floor == "5"
        assert total == 12


# ---------------------------------------------------------------------------
# 車位
# ---------------------------------------------------------------------------

class TestParking:
    @pytest.mark.parametrize("text", ["平面車位", "坡道平面", "B1平車", "B2平車", "含汽車位"])
    def test_flat(self, text):
        has_parking, parking_type = parse_parking(text)
        assert has_parking is True
        assert parking_type == "flat"

    @pytest.mark.parametrize("text", ["機械車位", "升降車位"])
    def test_mechanical(self, text):
        has_parking, parking_type = parse_parking(text)
        assert has_parking is True
        assert parking_type == "mechanical"

    @pytest.mark.parametrize("text", ["有車位", "車位另計", "可租車位"])
    def test_unknown(self, text):
        has_parking, parking_type = parse_parking(text)
        assert has_parking is True
        assert parking_type == "unknown"

    def test_no_parking(self):
        assert parse_parking("無車位，近市場") == (False, "none")
        assert parse_parking("採光好 兩房一廳") == (False, "none")

    def test_included_in_rent(self):
        assert parking_included_in_rent("租金含車位") is True
        assert parking_included_in_rent("車位另計 2000") is False
        assert parking_included_in_rent("兩房一廳") is None


# ---------------------------------------------------------------------------
# 生活圈
# ---------------------------------------------------------------------------

class TestLifeCircle:
    def test_taiyuan(self):
        assert guess_life_circle("近台元科技園區，福興一路") == "台元"

    def test_xiansan(self):
        assert guess_life_circle("十興國小旁 國賓大悅") == "縣三"

    def test_hsr(self):
        assert guess_life_circle("高鐵特區 嘉豐五路") == "高鐵/嘉豐"

    def test_other(self):
        assert guess_life_circle("竹北市某處") == "其他"
        assert guess_life_circle(None) == "其他"

    def test_priority_order(self):
        # 同時命中台元與高鐵時，優先判給排序在前的台元
        assert guess_life_circle("台元 高鐵") == "台元"


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------

class TestScore:
    def test_cost_brackets(self):
        assert cost_score(28000, DEFAULT_SCORING) == 20
        assert cost_score(30000, DEFAULT_SCORING) == 18
        assert cost_score(35000, DEFAULT_SCORING) == 15
        assert cost_score(37000, DEFAULT_SCORING) == 10
        assert cost_score(40000, DEFAULT_SCORING) == 5
        assert cost_score(None, DEFAULT_SCORING) == 8

    def test_parking_scores(self):
        assert parking_score("flat", DEFAULT_SCORING) == 15
        assert parking_score("unknown", DEFAULT_SCORING) == 9
        assert parking_score("mechanical", DEFAULT_SCORING) == 6
        assert parking_score("none", DEFAULT_SCORING) == 0

    def test_layout_scores(self):
        assert layout_score(2, False, DEFAULT_SCORING) == 10
        assert layout_score(3, False, DEFAULT_SCORING) == 9
        assert layout_score(1, False, DEFAULT_SCORING) == 5
        assert layout_score(5, False, DEFAULT_SCORING) == 3
        assert layout_score(2, True, DEFAULT_SCORING) == 0  # 套房排除

    def test_score_row_top_candidate(self):
        row = {
            "total_monthly_cost": 28000,
            "life_circle_guess": "台元",
            "parking_type": "flat",
            "rooms": 2,
            "is_suite": False,
            "has_elevator": True,
            "can_cook": True,
            "furniture_appliances": "冰箱、洗衣機",
            "available_now": True,
            "owner_direct": True,
            "can_register_household": True,
            "can_tax_report": True,
        }
        result = score_row(row, DEFAULT_SCORING)
        assert result["score"] >= 80
        assert result["priority"] == "優先約看"

    def test_score_row_over_budget(self):
        row = {
            "total_monthly_cost": 39000,
            "life_circle_guess": "台元",
            "parking_type": "flat",
            "rooms": 2,
            "is_suite": False,
        }
        result = score_row(row, DEFAULT_SCORING)
        assert result["over_budget_flag"] is True
        assert result["priority"] == "先跳過"


# ---------------------------------------------------------------------------
# 爬前預篩
# ---------------------------------------------------------------------------

class TestPrefilter:
    CONFIG = {"crawl": {"prefilter": {
        "enabled": True, "max_price": 40000, "skip_rooms_gte": 4, "skip_suite": True,
        "skip_keywords": ["樓中樓"],
    }}}

    @staticmethod
    def _card(listing_id: str, **kwargs) -> ListingCard:
        return ListingCard(listing_id=listing_id, url=f"https://rent.591.com.tw/{listing_id}", **kwargs)

    def test_keeps_matching_cards(self):
        cards = [self._card("1", price=30000, layout="2房2廳")]
        assert len(prefilter_cards(cards, self.CONFIG)) == 1

    def test_skips_over_price(self):
        cards = [self._card("1", price=45000, layout="2房")]
        assert prefilter_cards(cards, self.CONFIG) == []

    def test_skips_big_layout(self):
        cards = [self._card("1", price=30000, layout="4房2廳")]
        assert prefilter_cards(cards, self.CONFIG) == []

    def test_skips_suite(self):
        cards = [self._card("1", price=15000, raw_card_text="獨立套房 近台元")]
        assert prefilter_cards(cards, self.CONFIG) == []

    def test_skips_keyword_loft(self):
        cards = [self._card("1", price=30000, title="精美樓中樓 高鐵特區")]
        assert prefilter_cards(cards, self.CONFIG) == []

    def test_keeps_unknown_fields(self):
        # 卡片上沒有價格或房數資訊時不能亂砍
        cards = [self._card("1")]
        assert len(prefilter_cards(cards, self.CONFIG)) == 1

    def test_disabled_passthrough(self):
        cards = [self._card("1", price=99999, layout="9房")]
        config = {"crawl": {"prefilter": {"enabled": False}}}
        assert len(prefilter_cards(cards, config)) == 1
