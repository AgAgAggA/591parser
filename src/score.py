"""房源打分：對每筆物件計算 0-100 分與 priority 分級。

所有權重與級距都從 config.yaml 讀取，程式內只保留同樣數值的預設值。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_SCORING: dict[str, Any] = {
    "weights": {
        "location_score": 25, "cost_score": 20, "parking_score": 15,
        "condition_score": 15, "commute_score": 10, "layout_score": 10,
        "landlord_contract_score": 5,
    },
    "cost_brackets": [
        {"max": 29000, "score": 20},
        {"max": 33000, "score": 18},
        {"max": 36000, "score": 15},
        {"max": 38000, "score": 10},
        {"max": None, "score": 5},
    ],
    "cost_unknown_score": 8,
    "parking_scores": {"flat": 15, "unknown": 9, "mechanical": 6, "none": 0},
    "layout_scores": {
        "suite": 0, "rooms_1": 5, "rooms_2": 10, "rooms_3": 9,
        "rooms_4_plus": 3, "unknown": 4,
    },
    "location_scores": {
        "台元": 25, "縣三": 24, "遠百/勝利": 23,
        "高鐵/嘉豐": 20, "華興/市公所": 16, "其他": 10,
    },
    "commute_scores": {
        "台元": 10, "縣三": 9, "遠百/勝利": 8,
        "高鐵/嘉豐": 6, "華興/市公所": 5, "其他": 3,
    },
    "condition_points": {
        "has_elevator": 5, "can_cook": 4, "has_furniture": 4, "available_now": 2,
    },
    "landlord_points": {
        "owner_direct": 2, "can_register_household": 1.5, "can_tax_report": 1.5,
    },
    "priority": {"top": 80, "backup": 65},
    "low_priority_cost_threshold": 38000,
}

# 硬條件（priority 不能只看 score，要先通過這些條件）
DEFAULT_HARD_FILTER: dict[str, Any] = {
    "allowed_rooms": [2, 3],
    "require_parking": True,
    "exclude_suite": True,
    "max_total_monthly_cost": 36000,   # hard_pass 的月付上限
    "soft_max_cost": 38000,            # 費用未知或略超時最多只能是「可備選」
}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() in ("", "nan", "None"):
        return True
    return False


def to_bool(value: Any) -> Optional[bool]:
    """CSV round-trip 後布林值可能變字串，這裡統一轉回來。"""
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "1", "yes")


def to_number(value: Any) -> Optional[float]:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_scoring_config(config: dict) -> dict[str, Any]:
    merged = dict(DEFAULT_SCORING)
    merged.update(config.get("scoring", {}))
    return merged


# ---------------------------------------------------------------------------
# 各分項
# ---------------------------------------------------------------------------

def cost_score(total_cost: Optional[float], scoring: dict) -> float:
    if total_cost is None:
        return float(scoring["cost_unknown_score"])
    for bracket in scoring["cost_brackets"]:
        if bracket["max"] is None or total_cost <= bracket["max"]:
            return float(bracket["score"])
    return float(scoring["cost_brackets"][-1]["score"])


def parking_score(parking_type: Optional[str], scoring: dict) -> float:
    key = (parking_type or "none").strip().lower()
    return float(scoring["parking_scores"].get(key, 0))


def layout_score(rooms: Optional[float], is_suite: Optional[bool], scoring: dict) -> float:
    table = scoring["layout_scores"]
    if is_suite:
        return float(table["suite"])
    if rooms is None:
        return float(table["unknown"])
    rooms = int(rooms)
    if rooms >= 4:
        return float(table["rooms_4_plus"])
    return float(table.get(f"rooms_{rooms}", table["unknown"]))


def location_score(life_circle: Optional[str], scoring: dict) -> float:
    table = scoring["location_scores"]
    return float(table.get(life_circle or "其他", table.get("其他", 10)))


def commute_score(life_circle: Optional[str], scoring: dict) -> float:
    table = scoring["commute_scores"]
    return float(table.get(life_circle or "其他", table.get("其他", 3)))


def condition_score(row: dict, scoring: dict) -> float:
    points = scoring["condition_points"]
    cap = float(scoring["weights"]["condition_score"])
    total = 0.0
    if to_bool(row.get("has_elevator")):
        total += points["has_elevator"]
    if to_bool(row.get("can_cook")):
        total += points["can_cook"]
    if not _is_missing(row.get("furniture_appliances")):
        total += points["has_furniture"]
    if to_bool(row.get("available_now")):
        total += points["available_now"]
    return min(total, cap)


def landlord_contract_score(row: dict, scoring: dict) -> float:
    points = scoring["landlord_points"]
    cap = float(scoring["weights"]["landlord_contract_score"])
    total = 0.0
    if to_bool(row.get("owner_direct")):
        total += points["owner_direct"]
    if to_bool(row.get("can_register_household")):
        total += points["can_register_household"]
    if to_bool(row.get("can_tax_report")):
        total += points["can_tax_report"]
    return min(total, cap)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def score_row(row: dict, scoring: dict) -> dict[str, float | str | bool]:
    """對單筆物件計算所有分項與總分。"""
    total_cost = to_number(row.get("total_monthly_cost"))
    life_circle = row.get("life_circle_guess") if not _is_missing(row.get("life_circle_guess")) else "其他"

    scores = {
        "location_score": location_score(life_circle, scoring),
        "cost_score": cost_score(total_cost, scoring),
        "parking_score": parking_score(row.get("parking_type"), scoring),
        "condition_score": condition_score(row, scoring),
        "commute_score": commute_score(life_circle, scoring),
        "layout_score": layout_score(to_number(row.get("rooms")), to_bool(row.get("is_suite")), scoring),
        "landlord_contract_score": landlord_contract_score(row, scoring),
    }
    total = round(sum(scores.values()), 1)

    threshold = float(scoring["low_priority_cost_threshold"])
    over_budget = total_cost is not None and total_cost > threshold

    top = float(scoring["priority"]["top"])
    backup = float(scoring["priority"]["backup"])
    if over_budget:
        priority = "先跳過"
    elif total >= top:
        priority = "優先約看"
    elif total >= backup:
        priority = "可備選"
    else:
        priority = "先跳過"

    return {**scores, "score": total, "priority": priority, "over_budget_flag": over_budget}


def get_hard_filter_config(config: dict) -> dict[str, Any]:
    merged = dict(DEFAULT_HARD_FILTER)
    merged.update(config.get("hard_filter", {}))
    return merged


def hard_pass_row(row: dict, hard_cfg: dict) -> bool:
    """硬條件：2-3 房、非套雅房、有車位、總月付已知且 <= 上限。"""
    rooms = to_number(row.get("rooms"))
    if rooms is None or int(rooms) not in set(hard_cfg["allowed_rooms"]):
        return False
    if hard_cfg.get("exclude_suite", True) and to_bool(row.get("is_suite")):
        return False
    if hard_cfg.get("require_parking", True) and not to_bool(row.get("has_parking")):
        return False
    cost = to_number(row.get("total_monthly_cost"))
    return cost is not None and cost <= float(hard_cfg["max_total_monthly_cost"])


def _soft_pass_row(row: dict, hard_cfg: dict) -> bool:
    """軟通過：格局/車位符合，但費用未知或略超上限（最多列為可備選）。"""
    rooms = to_number(row.get("rooms"))
    if rooms is None or int(rooms) not in set(hard_cfg["allowed_rooms"]):
        return False
    if hard_cfg.get("exclude_suite", True) and to_bool(row.get("is_suite")):
        return False
    if hard_cfg.get("require_parking", True) and not to_bool(row.get("has_parking")):
        return False
    cost = to_number(row.get("total_monthly_cost"))
    return cost is None or cost <= float(hard_cfg.get("soft_max_cost", 38000))


def hard_priority_row(row: dict, scoring: dict, hard_cfg: dict) -> tuple[bool, str]:
    """回傳 (hard_pass, priority)。priority 由硬條件 + score 共同決定：

    - 優先約看：必須通過全部硬條件，且 score >= top 門檻
    - 可備選：通過硬條件且 score >= backup；或軟通過（費用未知/略超）且 score >= top
    - 其餘：先跳過
    - 非 active 物件一律先跳過
    """
    availability = str(row.get("availability_status") or "active")
    score = to_number(row.get("score")) or 0
    top = float(scoring["priority"]["top"])
    backup = float(scoring["priority"]["backup"])

    passed = hard_pass_row(row, hard_cfg)
    if availability != "active":
        return passed, "先跳過"
    if passed and score >= top:
        return passed, "優先約看"
    if passed and score >= backup:
        return passed, "可備選"
    if _soft_pass_row(row, hard_cfg) and score >= top:
        return passed, "可備選"
    return passed, "先跳過"


def apply_hard_priority(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """對已打分的 DataFrame 套用硬條件 priority，新增 hard_pass 欄位。"""
    scoring = get_scoring_config(config)
    hard_cfg = get_hard_filter_config(config)
    results = [hard_priority_row(row, scoring, hard_cfg) for row in df.to_dict(orient="records")]
    df = df.copy()
    df["hard_pass"] = [r[0] for r in results]
    df["priority"] = [r[1] for r in results]
    return df


def score_dataframe(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """對整個 DataFrame 打分，回傳含分數欄位的新 DataFrame（依 score 降冪排序）。"""
    scoring = get_scoring_config(config)
    results = [score_row(row, scoring) for row in df.to_dict(orient="records")]
    scores_df = pd.DataFrame(results, index=df.index)
    # 解析階段可能已有 parking_score 等同名欄位，以打分結果為準避免欄位重複
    base_df = df.drop(columns=[c for c in scores_df.columns if c in df.columns])
    out = pd.concat([base_df, scores_df], axis=1)
    out = apply_hard_priority(out, config)  # priority 以硬條件為準，不只看 score
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    logger.info("打分完成：%d 筆，優先約看 %d、可備選 %d",
                len(out),
                int((out["priority"] == "優先約看").sum()),
                int((out["priority"] == "可備選").sum()))
    return out
