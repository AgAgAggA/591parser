"""State-based crawl pipeline：列表掃描 -> detail 解析 -> stale check -> 打分 -> 輸出。

核心原則：
- 列表頁缺席「絕不」直接標 removed，一定要 detail page 確認。
- blocked / CAPTCHA：立即停止本輪 detail 檢查、儲存 state、印 warning，不重試不繞過。
- 物件永不從 state 刪除，rented/removed/expired 只改狀態。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
from playwright.sync_api import Page

from .availability import AvailabilityResult, detect_availability, page_text_from_html
from .crawler import (
    DETAIL_URL_TEMPLATE,
    CaptchaDetectedError,
    _save_html,
    crawl_list_pages_parallel,
    fetch_with_retry,
    prefilter_cards,
    run_parallel_workers,
)
from .models import ListingCard
from .parser_detail import parse_detail_page
from .score import apply_hard_priority, score_dataframe, to_bool, to_number
from .state import (
    ListingState,
    StateStore,
    apply_status,
    is_stale_check_candidate,
    mark_duplicates,
    mark_missing_from_list,
    mark_seen_in_list,
    now_str,
    reset_run_flags,
    update_from_parsed_listing,
)
from .utils import log_failed_url

logger = logging.getLogger(__name__)


def _days_since(timestamp: Optional[str]) -> float:
    """距離某個 'YYYY-MM-DD HH:MM:SS' 時間戳過了幾天；無法解析回傳極大值。"""
    from datetime import datetime
    if not timestamp:
        return 99999.0
    try:
        then = datetime.strptime(str(timestamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 99999.0
    return (datetime.now() - then).total_seconds() / 86400


def _state_prefiltered_out(state: ListingState, config: dict) -> bool:
    """對既有 state 套用與列表預篩相同的排除規則（租金上限、關鍵字）。

    不符找房條件的物件不值得花 detail 請求確認存活，直接跳過。
    """
    cfg = config.get("crawl", {}).get("prefilter", {})
    if not cfg.get("enabled", False):
        return False
    max_price = cfg.get("max_price")
    if max_price and state.price and state.price > float(max_price):
        return True
    skip_rooms_gte = cfg.get("skip_rooms_gte")
    if skip_rooms_gte and state.rooms and state.rooms >= int(skip_rooms_gte):
        return True
    if cfg.get("skip_suite") and state.is_suite:
        return True
    keywords = cfg.get("skip_keywords") or []
    blob = " ".join(filter(None, [state.title, state.layout]))
    return any(kw in blob for kw in keywords)


@dataclass
class RunReport:
    """一輪執行的 delta 統計。"""

    run_timestamp: str
    list_seen: int = 0
    previously_active_missing: int = 0
    detail_checked: int = 0
    stale_checked: int = 0
    new_listings: int = 0
    recovered: int = 0
    blocked_hit: bool = False
    duplicate_group_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    status_changed: int = 0
    top_candidates: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 單一物件 detail 檢查
# ---------------------------------------------------------------------------

def check_detail(
    page: Page,
    state: ListingState,
    config: dict,
    save_html: bool,
    life_circles: Optional[list] = None,
    card: Optional[ListingCard] = None,
) -> AvailabilityResult:
    """抓一個 detail page，判斷 availability 並更新 state。

    回傳 AvailabilityResult；連線失敗（retry 用盡）時回傳 error 狀態。
    """
    now = now_str()
    url = state.url or DETAIL_URL_TEMPLATE.format(listing_id=state.listing_id)
    timeout_ms = int(config.get("crawl", {}).get("page_timeout_ms", 30000))

    try:
        fetched = fetch_with_retry(page, url, timeout_ms, config)
    except Exception as exc:  # noqa: BLE001 - timeout/network：標 error，不 crash
        result = AvailabilityResult("error", str(exc)[:200])
        apply_status(state, "error", result.reason, now)
        state.last_parse_error = str(exc)[:500]
        log_failed_url(state.listing_id, url, str(exc))
        return result

    html_path = _save_html(fetched.html, "detail", state.listing_id) if save_html else None
    page_text = page_text_from_html(fetched.html)
    result = detect_availability(page_text, fetched.final_url, fetched.http_status)

    apply_status(
        state, result.status, result.reason, now,
        raw_status_text=result.matched_text,
        http_status=fetched.http_status,
        redirect_url=fetched.final_url if fetched.final_url != url else None,
    )
    state.detail_checked_this_run = True

    if result.status == "active":
        try:
            listing = parse_detail_page(
                fetched.html, listing_id=state.listing_id, url=url,
                card=card, life_circles=life_circles,
            )
            update_from_parsed_listing(state, listing.to_row(), now)
        except Exception as exc:  # noqa: BLE001 - 解析失敗不可讓整輪掛掉
            logger.exception("詳細頁解析失敗（availability 仍為 active）: %s", url)
            state.last_parse_error = str(exc)[:500]
            log_failed_url(state.listing_id, url, str(exc), html_path)
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_pipeline(
    url: str,
    max_pages: int = 30,
    headless: bool = True,
    save_html: bool = False,
    refresh_stale: bool = True,
    refresh_details: bool = True,
    only_active: bool = False,
    max_stale_checks: int = 200,
    stop_on_blocked: bool = True,
    state_path: Optional[str] = None,
    config: Optional[dict] = None,
    skip_list_crawl: bool = False,
    stale_missing_days: int = 0,
) -> tuple[dict[str, ListingState], RunReport]:
    """完整 state-based pipeline。回傳 (states, run report)。

    skip_list_crawl=True 時跳過列表頁（refresh-details / check-stale 專用）。
    """
    config = config or {}
    state_cfg = config.get("state", {})
    store = StateStore(state_path or state_cfg.get("path", "data/listings_state.sqlite"))
    states = store.load()
    life_circles = config.get("life_circles")

    run_ts = now_str()
    report = RunReport(run_timestamp=run_ts)

    # Step 1: 重置本輪旗標（歷史欄位保留）。
    # refresh-details / check-stale 等不掃列表的模式保留上一輪的
    # seen/missing 旗標，才能對「上輪缺席」的物件做 stale check。
    if not skip_list_crawl:
        for state in states.values():
            reset_run_flags(state)

    report_lock = threading.Lock()

    try:
        detail_queue: list[tuple[ListingState, Optional[ListingCard]]] = []

        if not skip_list_crawl:
            # Step 2: 列表頁 crawl（多執行緒，先抓第 1 頁取總頁數）
            cards = crawl_list_pages_parallel(url, max_pages, save_html, config, headless)
            report.list_seen = len(cards)
            logger.info("列表頁掃描完成：本輪看到 %d 筆", len(cards))

            seen_ids = set()
            for card in cards:
                seen_ids.add(card.listing_id)
                state = states.get(card.listing_id)
                if state is None:
                    state = ListingState(
                        listing_id=card.listing_id,
                        url=card.url,
                        title=card.title,
                        price=float(card.price) if card.price else None,
                        first_seen_at=run_ts,
                        new_this_run=True,
                    )
                    states[card.listing_id] = state
                    report.new_listings += 1
                mark_seen_in_list(state, run_ts)

            # 本輪沒看到的舊物件：只累計 missing，不改狀態
            for listing_id, state in states.items():
                if listing_id not in seen_ids:
                    mark_missing_from_list(state)
                    if state.availability_status == "active":
                        report.previously_active_missing += 1

            # Step 3: detail 解析佇列（預篩不符者不進 detail）
            kept_cards = prefilter_cards(cards, config)
            kept_ids = {c.listing_id for c in kept_cards}
            for card in cards:
                state = states[card.listing_id]
                if card.listing_id not in kept_ids:
                    if not state.last_successful_parse_at:
                        state.availability_reason = "prefiltered_not_checked"
                    continue
                if only_active and state.availability_status not in ("active", "unknown"):
                    continue
                detail_queue.append((state, card))
        elif refresh_details:
            # refresh-details 模式：不掃列表，直接重新檢查既有物件
            for state in states.values():
                if only_active and state.availability_status != "active":
                    continue
                if not only_active and state.availability_reason == "prefiltered_not_checked":
                    continue
                if _state_prefiltered_out(state, config):
                    continue
                detail_queue.append((state, None))

        # 多執行緒 detail handler：每個 state 只會被一個 worker 處理，
        # check_detail 只改自己的 state；report 計數用 lock 保護。
        def _detail_handler(page: Page, item: tuple[ListingState, Optional[ListingCard]]) -> bool:
            state, card = item
            result = check_detail(page, state, config, save_html, life_circles, card)
            with report_lock:
                report.detail_checked += 1
                done = report.detail_checked
            if done % 20 == 0:
                logger.info("Detail 進度：%d/%d", done, len(detail_queue))
            if result.status == "blocked":
                with report_lock:
                    report.blocked_hit = True
                logger.warning(
                    "偵測到 blocked / CAPTCHA（%s），停止本輪 detail 檢查並儲存 state。"
                    "請稍後再試，不要嘗試繞過驗證。", result.reason)
                return not stop_on_blocked
            return True

        if refresh_details and detail_queue:
            logger.info("Detail 檢查佇列：%d 筆（多執行緒）", len(detail_queue))
            run_parallel_workers(detail_queue, _detail_handler, config, headless, name="detail")

        # Step 4: stale listing check —— 之前 active/unknown/error、
        # 這次（或上輪）列表沒看到的物件，逐一進 detail 確認
        # （絕不憑列表缺席下架）
        if refresh_stale and not report.blocked_hit:
            candidates = [
                s for s in states.values()
                if is_stale_check_candidate(s) and not _state_prefiltered_out(s, config)
            ]
            if stale_missing_days > 0:
                candidates = [
                    s for s in candidates
                    if _days_since(s.last_seen_at) >= stale_missing_days
                ]
            # 優先檢查之前是 active 的（對決策影響最大），再依最久未檢查排序
            candidates.sort(key=lambda s: (
                0 if s.availability_status == "active" else 1,
                s.last_checked_at or "",
            ))
            if max_stale_checks and len(candidates) > max_stale_checks:
                logger.info("stale 候選 %d 筆，僅檢查前 %d 筆（--max-stale-checks）",
                            len(candidates), max_stale_checks)
                candidates = candidates[:max_stale_checks]

            def _stale_handler(page: Page, state: ListingState) -> bool:
                result = check_detail(page, state, config, save_html, life_circles)
                with report_lock:
                    report.stale_checked += 1
                    done = report.stale_checked
                if done % 20 == 0:
                    logger.info("Stale 進度：%d/%d", done, len(candidates))
                if result.status == "blocked":
                    with report_lock:
                        report.blocked_hit = True
                    logger.warning("Stale check 遇到 blocked，停止並儲存 state。")
                    return not stop_on_blocked
                return True

            if candidates:
                logger.info("Stale check：%d 筆（多執行緒）", len(candidates))
                run_parallel_workers(candidates, _stale_handler, config, headless, name="stale")
    except CaptchaDetectedError as exc:
        # 列表頁層級的 CAPTCHA / 阻擋：停止本輪、保留 state，不重試不繞過
        report.blocked_hit = True
        logger.warning("%s", exc)
    finally:
        # Step 5: 打分 + 重複標記 + 統計，無論中途是否 blocked 都要儲存 state
        _rescore_states(states, config)
        report.duplicate_group_count = mark_duplicates(states)
        _finalize_report(states, report, config)
        store.save(states)

    return states, report


def _rescore_states(states: dict[str, ListingState], config: dict) -> None:
    """對所有有解析資料的物件重新打分，並套用硬條件 priority。"""
    rows = []
    for state in states.values():
        if not state.payload:
            # 從未成功解析：沒有分數，priority 一律先跳過
            state.score = None
            state.priority = "先跳過"
            state.hard_pass = False
            continue
        row = dict(state.payload)
        row["listing_id"] = state.listing_id
        row["availability_status"] = state.availability_status
        rows.append(row)
    if not rows:
        return

    df = pd.DataFrame(rows)
    scored = score_dataframe(df, config)
    for row in scored.to_dict(orient="records"):
        state = states.get(str(row.get("listing_id")))
        if state is None:
            continue
        state.score = to_number(row.get("score"))
        state.priority = str(row.get("priority"))
        state.hard_pass = bool(to_bool(row.get("hard_pass")))
        # 分數寫回 payload，report 才有分項分數可顯示
        for key in ("score", "priority", "hard_pass", "location_score", "cost_score",
                    "parking_score", "condition_score", "commute_score",
                    "layout_score", "landlord_contract_score"):
            state.payload[key] = row.get(key)


def _finalize_report(states: dict[str, ListingState], report: RunReport, config: dict) -> None:
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state.availability_status] = counts.get(state.availability_status, 0) + 1
        if state.status_changed:
            report.status_changed += 1
            report.changes.append({
                "listing_id": state.listing_id,
                "old_status": state.previous_status,
                "new_status": state.availability_status,
                "title": state.title,
                "url": state.url,
                "reason": state.availability_reason,
            })
        if state.status_changed and state.availability_status == "active" \
                and state.previous_status in ("error", "blocked", "rented", "removed", "expired", "unknown"):
            report.recovered += 1
    report.status_counts = counts
    report.top_candidates = sum(1 for s in states.values() if is_top_candidate(s, config))


def is_top_candidate(state: ListingState, config: dict) -> bool:
    """top_candidates 條件：active + 2/3 房 + 非套房 + 有車位 + 月付 <= 36000。"""
    hard_cfg = config.get("hard_filter", {})
    max_cost = float(hard_cfg.get("max_total_monthly_cost", 36000))
    allowed_rooms = set(hard_cfg.get("allowed_rooms", [2, 3]))
    return (
        state.availability_status == "active"
        and state.rooms is not None and state.rooms in allowed_rooms
        and not state.is_suite
        and bool(state.has_parking)
        and state.total_monthly_cost is not None
        and state.total_monthly_cost <= max_cost
    )


# ---------------------------------------------------------------------------
# QA summary
# ---------------------------------------------------------------------------

def qa_summary(states: dict[str, ListingState], config: dict) -> dict[str, Any]:
    """輸出 QA 統計與 debug 用的問題 listing_id 清單。"""
    from .availability import UNAVAILABLE_STATUSES
    from .utils import DEFAULT_LIFE_CIRCLES, MECHANICAL_PARKING_KEYWORDS

    circles = config.get("life_circles") or DEFAULT_LIFE_CIRCLES
    huaxing_keywords = next(
        (c.get("keywords", []) for c in circles if c.get("name") == "華興/市公所"), [])

    huaxing_bad, mech_bad, mgmt_bad = [], [], []
    for state in states.values():
        address = state.address or ""
        if state.life_circle_guess == "台元" and any(kw in address for kw in huaxing_keywords):
            huaxing_bad.append(state.listing_id)

        blob = " ".join(str(state.payload.get(k) or "") for k in ("title", "description", "layout"))
        if state.parking_type == "flat" and any(kw in blob for kw in MECHANICAL_PARKING_KEYWORDS):
            mech_bad.append(state.listing_id)
        if state.payload and "含管理費" in blob and state.management_fee is None:
            mgmt_bad.append(state.listing_id)

    return {
        "total_state_count": len(states),
        "active_count": sum(1 for s in states.values() if s.availability_status == "active"),
        "unavailable_count": sum(1 for s in states.values() if s.availability_status in UNAVAILABLE_STATUSES),
        "new_this_run_count": sum(1 for s in states.values() if s.new_this_run),
        "missing_from_list_count": sum(
            1 for s in states.values()
            if not s.seen_in_list_page_this_run
            and s.availability_status in ("active", "unknown", "error", "blocked")),
        "stale_checked_count": sum(
            1 for s in states.values()
            if s.detail_checked_this_run and not s.seen_in_list_page_this_run),
        "status_changed_count": sum(1 for s in states.values() if s.status_changed),
        "priority_top_count": sum(1 for s in states.values() if s.priority == "優先約看"),
        "backup_count": sum(1 for s in states.values() if s.priority == "可備選"),
        "skip_count": sum(1 for s in states.values() if s.priority == "先跳過"),
        "huaxing_mislabeled_as_taiyuan_count": len(huaxing_bad),
        "mechanical_displayed_as_flat_count": len(mech_bad),
        "rent_includes_management_but_unknown_fee_count": len(mgmt_bad),
        "duplicate_group_count": len({
            s.duplicate_group for s in states.values() if s.duplicate_group}),
        "_debug_huaxing_ids": huaxing_bad[:20],
        "_debug_mechanical_ids": mech_bad[:20],
        "_debug_mgmt_ids": mgmt_bad[:20],
    }
