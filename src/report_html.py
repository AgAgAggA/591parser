"""HTML 報告輸出：把 state/scored 資料轉成單一檔案、可離線開啟的互動卡片頁。

特色：
- 純靜態單檔（inline CSS/JS），瀏覽器直接開啟即可
- 預設只顯示 availability_status == "active" 的物件（hideUnavailable = true）
- 預設隱藏重複刊登（is_duplicate）
- 篩選 chips：有效/下架、本輪新物件、狀態變更、車位型式、2-3 房、
  屋主直租、費用已確認、總月付上限
- 非 active 卡片變灰，且不顯示「優先約看」
- 標題與卡片可連到 591 物件頁（https://rent.591.com.tw/{listing_id}）
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .score import to_bool, to_number
from .utils import PROJECT_ROOT

logger = logging.getLogger(__name__)

LISTING_URL_BASE = "https://rent.591.com.tw"

PARKING_LABELS = {"flat": "平面車位", "mechanical": "機械車位", "unknown": "車位(型式未確認)", "none": "無車位"}
PRIORITY_CLASSES = {"優先約看": "p-top", "可備選": "p-backup", "先跳過": "p-skip"}
CONFIDENCE_LABELS = {"high": "費用已確認", "medium": "一項費用未知", "low": "費用不完整"}
AVAILABILITY_LABELS = {
    "active": "有效", "rented": "已出租", "removed": "已下架", "expired": "已過期",
    "blocked": "被阻擋", "error": "錯誤", "unknown": "未知",
}

# 排序選單：(key, 顯示名稱, data 屬性, 預設方向)
SORT_OPTIONS = [
    ("score_desc", "總分（高→低）", "score", -1),
    ("cost_asc", "總月付（低→高）", "cost", 1),
    ("price_asc", "租金（低→高）", "price", 1),
    ("size_desc", "坪數（大→小）", "size", -1),
]

COST_CAP_OPTIONS = ["28000", "30000", "33000", "36000"]


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def listing_url(row: dict) -> str:
    """優先用 listing_id 組出乾淨的 591 連結，退回原始 url 欄位。"""
    listing_id = str(row.get("listing_id") or "").strip()
    if listing_id and listing_id.lower() not in ("nan", "none"):
        return f"{LISTING_URL_BASE}/{listing_id}"
    return str(row.get("url") or "").strip()


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or str(value) in ("", "nan", "None")


def _text(value, fallback: str = "—") -> str:
    return fallback if _missing(value) else html.escape(str(value))


def _money(value, fallback: str = "—") -> str:
    num = to_number(value)
    return fallback if num is None else f"{num:,.0f}"


def _bool_attr(value, default: bool = False) -> str:
    b = to_bool(value)
    return "true" if (default if b is None else b) else "false"


def _dt_short(value) -> str:
    """'2026-07-06 01:18:58' -> '2026-07-06 01:18'"""
    if _missing(value):
        return "—"
    text = str(value)
    return html.escape(text[:16] if len(text) >= 16 else text)


def _availability(row: dict) -> str:
    status = str(row.get("availability_status") or "").strip().lower()
    return status if status in AVAILABILITY_LABELS else "active"


def _feature_badges(row: dict) -> str:
    """條件徽章：只顯示為 True 的正面條件，未知不顯示。"""
    features = [
        ("has_elevator", "電梯"),
        ("can_cook", "可開伙"),
        ("available_now", "即可入住"),
        ("owner_direct", "屋主直租"),
        ("can_tax_report", "可報稅"),
        ("can_register_household", "可遷戶籍"),
        ("parking_included_in_rent", "租金含車位"),
    ]
    tags = [f'<span class="tag">{label}</span>' for key, label in features if to_bool(row.get(key)) is True]
    if not _missing(row.get("furniture_appliances")):
        tags.append('<span class="tag">附家具家電</span>')
    return "".join(tags)


def _cost_breakdown(row: dict) -> str:
    parts = [f"租金 {_money(row.get('price'), '?')}"]
    mgmt = to_number(row.get("management_fee"))
    if mgmt is None:
        parts.append("管理費 ?")
    elif mgmt > 0:
        parts.append(f"管理費 {mgmt:,.0f}")
    if to_bool(row.get("has_parking")):
        park = to_number(row.get("parking_fee"))
        if park is None:
            parts.append("車位費 ?")
        elif park > 0:
            parts.append(f"車位費 {park:,.0f}")
    return " + ".join(parts)


def _status_strip(row: dict, availability: str) -> str:
    """卡片上的存活狀態列：狀態 / 本輪看到 / 檢查時間 / 狀態變更。"""
    label = AVAILABILITY_LABELS.get(availability, availability)
    seen = to_bool(row.get("seen_in_list_page_this_run"))
    seen_text = "—" if seen is None else ("是" if seen else "否")
    bits = [
        f'<span class="avail avail-{availability}">狀態：{label}</span>',
        f"<span>本輪看到：{seen_text}</span>",
        f"<span>最後檢查：{_dt_short(row.get('last_checked_at'))}</span>",
        f"<span>第一次看到：{_dt_short(row.get('first_seen_at'))}</span>",
    ]
    note = row.get("status_change_note")
    if not _missing(note) and to_bool(row.get("status_changed")):
        bits.append(f'<span class="change-note">狀態變更：{html.escape(str(note))}</span>')
    if to_bool(row.get("is_duplicate")):
        bits.append('<span class="dup-note">疑似重複刊登</span>')
    if to_bool(row.get("new_this_run")):
        bits.append('<span class="new-note">本輪新物件</span>')
    return f'<div class="status-strip">{"".join(bits)}</div>'


def _card_html(row: dict) -> str:
    url = listing_url(row)
    availability = _availability(row)
    is_active = availability == "active"

    # 非 active 一律不可顯示「優先約看」，卡片以狀態標籤取代 priority 徽章
    priority = str(row.get("priority") or "")
    if not is_active:
        priority = "先跳過"
    p_class = PRIORITY_CLASSES.get(priority, "p-skip")
    badge_html = (
        f'<span class="badge {p_class}">{_text(priority, "未分級")}</span>' if is_active
        else f'<span class="badge b-unavail">{AVAILABILITY_LABELS.get(availability, availability)}</span>'
    )

    score = to_number(row.get("score")) or 0
    life_circle = str(row.get("life_circle_guess") or "其他")
    cost = to_number(row.get("total_monthly_cost"))
    confidence = str(row.get("cost_confidence") or "")
    rooms = to_number(row.get("rooms"))

    cost_class = ""
    if is_active and cost is not None:
        cost_class = "cost-good" if cost <= 36000 else ("cost-bad" if cost > 38000 else "")

    search_blob = html.escape(
        " ".join(
            str(row.get(k) or "")
            for k in ("title", "address", "community_name", "life_circle_guess", "layout", "description")
        ).lower()
    )

    meta_items = [
        ("格局", _text(row.get("layout"))),
        ("坪數", f"{_money(row.get('size_ping'))} 坪" if not _missing(row.get("size_ping")) else "—"),
        ("樓層", (f"{_text(row.get('floor'))}F/{_money(row.get('total_floors'))}F"
                  if not _missing(row.get("floor")) and not _missing(row.get("total_floors")) else _text(row.get("floor")))),
        ("社區", _text(row.get("community_name"))),
        ("地址", _text(row.get("address"))),
        ("車位", _text(PARKING_LABELS.get(str(row.get("parking_type")), row.get("parking_type")))),
        ("刊登者", _text(row.get("agent_type"))),
        ("押金", _text(row.get("deposit"))),
    ]
    meta_html = "".join(
        f'<div class="meta-item"><span class="meta-label">{label}</span><span class="meta-value">{value}</span></div>'
        for label, value in meta_items
    )

    desc = str(row.get("description") or "").strip()
    desc_html = f'<p class="desc">{html.escape(desc[:160])}{"…" if len(desc) > 160 else ""}</p>' if desc else ""

    distance = _text(row.get("distance_to_taiyuan_note"), "")
    posted = _text(row.get("posted_time"), "")
    footer_bits = [b for b in (distance, f"刊登：{posted}" if posted else "", f"ID {_text(row.get('listing_id'))}") if b]

    conf_label = CONFIDENCE_LABELS.get(confidence, "")
    conf_html = f'<span class="conf conf-{html.escape(confidence)}">{conf_label}</span>' if conf_label else ""

    pct = max(0, min(100, float(score)))
    title = _text(row.get("title"), "(無標題)")
    title_html = (
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    )
    open_btn = (
        f'<a class="open-btn" href="{html.escape(url)}" target="_blank" rel="noopener">在 591 開啟 ↗</a>' if url else ""
    )

    data_attrs = " ".join([
        f'data-priority="{html.escape(priority)}"',
        f'data-circle="{html.escape(life_circle)}"',
        f'data-search="{search_blob}"',
        f'data-score="{score}"',
        f'data-cost="{cost if cost is not None else 999999999}"',
        f'data-price="{to_number(row.get("price")) or 999999999}"',
        f'data-size="{to_number(row.get("size_ping")) or 0}"',
        f'data-availability="{availability}"',
        f'data-seen-this-run="{_bool_attr(row.get("seen_in_list_page_this_run"), default=True)}"',
        f'data-status-changed="{_bool_attr(row.get("status_changed"))}"',
        f'data-new-this-run="{_bool_attr(row.get("new_this_run"))}"',
        f'data-parking="{html.escape(str(row.get("parking_type") or "none"))}"',
        f'data-rooms="{int(rooms) if rooms is not None else ""}"',
        f'data-owner-direct="{_bool_attr(row.get("owner_direct"))}"',
        f'data-cost-confidence="{html.escape(confidence or "low")}"',
        f'data-duplicate="{_bool_attr(row.get("is_duplicate"))}"',
        f'data-total-cost="{int(cost) if cost is not None else ""}"',
        f'data-hard-pass="{_bool_attr(row.get("hard_pass"))}"',
    ])

    unavail_class = "" if is_active else " unavail"
    return f"""<article class="listing {p_class}-border{unavail_class}" {data_attrs}>
  {_status_strip(row, availability)}
  <div class="listing-head">
    {badge_html}
    <span class="circle-chip">{html.escape(life_circle)}</span>
    <div class="score-ring" style="--pct:{pct}"><span>{score:g}</span></div>
  </div>
  <h2 class="listing-title">{title_html}</h2>
  <div class="price-row {cost_class}">
    <span class="price-main">{_money(cost, "月付 ?")}</span><span class="price-unit">元/月</span>{conf_html}
    <div class="price-breakdown">{html.escape(_cost_breakdown(row))}</div>
  </div>
  <div class="meta-grid">{meta_html}</div>
  <div class="tags">{_feature_badges(row)}</div>
  {desc_html}
  <div class="listing-footer"><span>{html.escape("　·　".join(footer_bits))}</span>{open_btn}</div>
</article>"""


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([None] * len(df), index=df.index)


def _summary_html(df: pd.DataFrame) -> str:
    availability = _col(df, "availability_status").fillna("active").astype(str)
    active_mask = availability == "active"
    unavailable_mask = availability.isin(["rented", "removed", "expired"])

    bools = {
        name: _col(df, name).map(lambda v: to_bool(v) is True)
        for name in ("new_this_run", "status_changed", "hard_pass", "seen_in_list_page_this_run")
    }
    missing_unconfirmed = int((
        ~bools["seen_in_list_page_this_run"]
        & availability.isin(["active", "unknown", "error", "blocked"])
    ).sum()) if "seen_in_list_page_this_run" in df.columns else 0

    parking = _col(df, "parking_type").astype(str)
    # 總月付中位數只用 active 物件計算，避免下架物件干擾行情判斷
    active_costs = pd.to_numeric(_col(df, "total_monthly_cost")[active_mask], errors="coerce").dropna()
    median_cost = f"{active_costs.median():,.0f}" if not active_costs.empty else "—"

    priority = _col(df, "priority").astype(str)
    top_count = int(((priority == "優先約看") & active_mask).sum())

    cards = [
        ("物件總數", str(len(df))),
        ("有效物件", str(int(active_mask.sum()))),
        ("本輪新物件", str(int(bools["new_this_run"].sum()))),
        ("消失待確認", str(missing_unconfirmed)),
        ("已出租/下架", str(int(unavailable_mask.sum()))),
        ("狀態變更", str(int(bools["status_changed"].sum()))),
        ("硬條件通過", str(int((bools["hard_pass"] & active_mask).sum()))),
        ("優先約看", str(top_count)),
        ("平面車位", str(int(((parking == "flat") & active_mask).sum()))),
        ("機械車位", str(int(((parking == "mechanical") & active_mask).sum()))),
        ("月付中位數 (active)", median_cost),
    ]
    return "".join(
        f'<div class="card"><div class="card-num">{value}</div><div class="card-label">{label}</div></div>'
        for label, value in cards
    )


def _filter_buttons(df: pd.DataFrame) -> tuple[str, str]:
    priorities = [p for p in ("優先約看", "可備選", "先跳過") if "priority" in df.columns and (df["priority"] == p).any()]
    circles = sorted(_col(df, "life_circle_guess").dropna().unique()) if "life_circle_guess" in df.columns else []
    p_html = '<button class="chip active" data-filter-priority="">全部</button>' + "".join(
        f'<button class="chip" data-filter-priority="{html.escape(p)}">{html.escape(p)}</button>' for p in priorities
    )
    c_html = '<button class="chip active" data-filter-circle="">全部</button>' + "".join(
        f'<button class="chip" data-filter-circle="{html.escape(str(c))}">{html.escape(str(c))}</button>' for c in circles
    )
    return p_html, c_html


def render_html_report(df: pd.DataFrame, title: str = "591 竹北租屋報告") -> str:
    """把 DataFrame 渲染成完整 HTML 字串（卡片版面，預設只顯示 active）。"""
    if "score" in df.columns:
        df = df.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)

    cards = "\n".join(_card_html(row) for row in df.to_dict(orient="records"))
    priority_chips, circle_chips = _filter_buttons(df)
    sort_options = "".join(
        f'<option value="{key}" data-attr="{attr}" data-dir="{direction}">{label}</option>'
        for key, label, attr, direction in SORT_OPTIONS
    )
    cost_cap_options = '<option value="">全部</option>' + "".join(
        f'<option value="{cap}">{int(cap):,} 以下</option>' for cap in COST_CAP_OPTIONS
    )
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --bg: #f4f6fa; --panel: #ffffff; --ink: #1f2430; --muted: #6b7280;
  --brand: #4472c4; --line: #e5e7eb;
  --good: #c6efce; --good-ink: #1e6b34; --warn: #ffeb9c; --warn-ink: #7a5b00;
  --bad: #ffc7ce; --bad-ink: #9c1f2e; --skip: #eceef2; --skip-ink: #6b7280;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", system-ui, sans-serif;
  font-size: 14px;
}}
header {{
  background: linear-gradient(135deg, #3b5fa8, #4472c4 60%, #5b8bd6);
  color: #fff; padding: 28px 32px 40px;
}}
header h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 700; }}
header .sub {{ opacity: .85; font-size: 13px; }}
header .sub a {{ color: #dce8ff; }}
main {{ max-width: 1480px; margin: 0 auto; padding: 0 24px 48px; }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: -26px 0 18px; }}
.card {{
  background: var(--panel); border-radius: 12px; padding: 12px 18px; min-width: 118px;
  box-shadow: 0 4px 14px rgba(31,36,48,.08);
}}
.card-num {{ font-size: 22px; font-weight: 700; color: var(--brand); }}
.card-label {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
.toolbar {{
  background: var(--panel); border-radius: 12px; padding: 12px 18px; margin-bottom: 18px;
  box-shadow: 0 2px 8px rgba(31,36,48,.05); display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center;
  position: sticky; top: 0; z-index: 10;
}}
.toolbar .group {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
.toolbar .group-label {{ color: var(--muted); font-size: 12px; margin-right: 2px; }}
.chip {{
  border: 1px solid var(--line); background: #fff; border-radius: 999px;
  padding: 4px 12px; font-size: 13px; cursor: pointer; color: var(--ink);
}}
.chip.active {{ background: var(--brand); border-color: var(--brand); color: #fff; }}
#search {{ border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; font-size: 13px; min-width: 200px; }}
select {{ border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; font-size: 13px; background: #fff; }}
#count {{ color: var(--muted); font-size: 12px; margin-left: auto; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }}
.listing {{
  background: var(--panel); border-radius: 14px; padding: 14px 18px 14px;
  box-shadow: 0 2px 8px rgba(31,36,48,.06); display: flex; flex-direction: column; gap: 10px;
  border-top: 4px solid var(--skip);
  transition: transform .12s ease, box-shadow .12s ease;
}}
.listing:hover {{ transform: translateY(-2px); box-shadow: 0 8px 20px rgba(31,36,48,.12); }}
.listing.unavail {{ background: #f1f2f5; filter: grayscale(.55); opacity: .8; }}
.p-top-border {{ border-top-color: #46a35e; }}
.p-backup-border {{ border-top-color: #e0b400; }}
.p-skip-border {{ border-top-color: #c3c8d1; }}
.status-strip {{
  display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 11px; color: var(--muted);
  padding-bottom: 6px; border-bottom: 1px dashed var(--line);
}}
.avail {{ font-weight: 700; border-radius: 999px; padding: 1px 8px; }}
.avail-active {{ background: var(--good); color: var(--good-ink); }}
.avail-rented, .avail-removed, .avail-expired {{ background: var(--bad); color: var(--bad-ink); }}
.avail-blocked, .avail-error, .avail-unknown {{ background: var(--warn); color: var(--warn-ink); }}
.change-note {{ color: var(--bad-ink); font-weight: 700; }}
.dup-note {{ color: #8b5cf6; font-weight: 700; }}
.new-note {{ color: var(--good-ink); font-weight: 700; }}
.listing-head {{ display: flex; align-items: center; gap: 8px; }}
.badge {{ border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 600; }}
.p-top {{ background: var(--good); color: var(--good-ink); }}
.p-backup {{ background: var(--warn); color: var(--warn-ink); }}
.p-skip {{ background: var(--skip); color: var(--skip-ink); }}
.b-unavail {{ background: #d7d9de; color: #4b5563; }}
.circle-chip {{
  background: #eef3fc; color: #2d5aa8; border-radius: 999px; padding: 3px 10px; font-size: 12px;
}}
.score-ring {{
  margin-left: auto; width: 44px; height: 44px; border-radius: 50%;
  background: conic-gradient(var(--brand) calc(var(--pct) * 1%), var(--line) 0);
  display: grid; place-items: center; flex-shrink: 0;
}}
.score-ring span {{
  width: 34px; height: 34px; border-radius: 50%; background: var(--panel);
  display: grid; place-items: center; font-weight: 700; font-size: 13px; color: var(--brand);
}}
.listing-title {{ margin: 0; font-size: 15px; line-height: 1.45; font-weight: 700; }}
.listing-title a {{ color: #1a56b0; text-decoration: none; }}
.listing-title a:hover {{ text-decoration: underline; }}
.price-row {{ border-radius: 10px; padding: 8px 12px; background: #f7f8fb; }}
.price-row.cost-good {{ background: var(--good); }}
.price-row.cost-good .price-main, .price-row.cost-good .price-breakdown {{ color: var(--good-ink); }}
.price-row.cost-bad {{ background: var(--bad); }}
.price-row.cost-bad .price-main, .price-row.cost-bad .price-breakdown {{ color: var(--bad-ink); }}
.price-main {{ font-size: 20px; font-weight: 800; }}
.price-unit {{ font-size: 12px; color: var(--muted); margin: 0 6px 0 3px; }}
.conf {{ font-size: 11px; border-radius: 999px; padding: 2px 8px; vertical-align: 2px; }}
.conf-high {{ background: #e2f3e6; color: var(--good-ink); }}
.conf-medium {{ background: #fdf3d0; color: var(--warn-ink); }}
.conf-low {{ background: #fde3e6; color: var(--bad-ink); }}
.price-breakdown {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
.meta-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; }}
.meta-item {{ display: flex; gap: 6px; font-size: 13px; min-width: 0; }}
.meta-label {{ color: var(--muted); flex-shrink: 0; }}
.meta-value {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.tag {{
  background: #eef3fc; color: #2d5aa8; border-radius: 6px; padding: 2px 8px; font-size: 12px;
}}
.desc {{
  margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.6;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}}
.listing-footer {{
  margin-top: auto; padding-top: 8px; border-top: 1px dashed var(--line);
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  color: var(--muted); font-size: 11.5px;
}}
.open-btn {{
  flex-shrink: 0; background: var(--brand); color: #fff; text-decoration: none;
  border-radius: 8px; padding: 6px 12px; font-size: 12px; font-weight: 600;
}}
.open-btn:hover {{ background: #35599c; }}
footer.page {{ color: var(--muted); font-size: 12px; margin-top: 20px; text-align: center; }}
#filters-toggle {{ display: none; }}
/* ---- 手機版 ---- */
@media (max-width: 640px) {{
  body {{ font-size: 15px; }}
  header {{ padding: 16px 14px 30px; }}
  header h1 {{ font-size: 18px; }}
  main {{ padding: 0 10px 32px; }}
  .cards {{ gap: 8px; margin-top: -22px; }}
  .card {{ padding: 10px 12px; min-width: 0; flex: 1 1 calc(33% - 8px); }}
  .card-num {{ font-size: 18px; }}
  .toolbar {{ padding: 10px 12px; gap: 8px 12px; }}
  /* 篩選預設收合，點「篩選」展開，避免佔滿整個螢幕 */
  #filters-toggle {{ display: inline-block; font-weight: 600; }}
  .toolbar .group.collapsible {{ display: none; width: 100%; }}
  .toolbar.filters-open .group.collapsible {{ display: flex; }}
  .chip {{ padding: 7px 13px; }}   /* 加大觸控目標 */
  #search {{ min-width: 0; flex: 1; }}
  #count {{ width: 100%; margin-left: 0; text-align: right; }}
  .grid {{ grid-template-columns: 1fr; gap: 12px; }}
  .meta-grid {{ grid-template-columns: 1fr; }}
  .meta-value {{ white-space: normal; }}
  .status-strip {{ font-size: 12px; }}
}}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="sub">資料來源：<a href="https://rent.591.com.tw/list?kind=1&amp;region=5&amp;section=54" target="_blank" rel="noopener">591 租屋網（竹北・整層住家）</a>　·　<strong>報告產生時間：{generated}</strong>　·　預設只顯示有效物件並隱藏重複刊登</div>
</header>
<main>
  <div class="cards">{_summary_html(df)}</div>
  <div class="toolbar" id="toolbar">
    <button class="chip" id="filters-toggle">☰ 篩選</button>
    <div class="group"><span class="group-label">狀態</span>
      <button class="chip active" data-filter-availability="active">只看有效物件</button>
      <button class="chip" data-filter-availability="">顯示已下架/已出租</button>
    </div>
    <div class="group collapsible"><span class="group-label">本輪</span>
      <button class="chip toggle" data-toggle="newOnly">只看本輪新物件</button>
      <button class="chip toggle" data-toggle="changedOnly">只看狀態變更</button>
    </div>
    <div class="group collapsible"><span class="group-label">車位</span>
      <button class="chip toggle" data-toggle="flatOnly">只看平面車位</button>
      <button class="chip toggle" data-toggle="hideMech">隱藏機械車位</button>
    </div>
    <div class="group collapsible"><span class="group-label">條件</span>
      <button class="chip toggle" data-toggle="rooms23">只看 2–3 房</button>
      <button class="chip toggle" data-toggle="ownerOnly">只看屋主直租</button>
      <button class="chip toggle" data-toggle="confirmedOnly">只看費用已確認</button>
      <button class="chip toggle active" data-toggle="hideDup">隱藏重複刊登</button>
    </div>
    <div class="group collapsible"><span class="group-label">總月付上限</span><select id="costcap">{cost_cap_options}</select></div>
    <div class="group collapsible"><span class="group-label">優先級</span>{priority_chips}</div>
    <div class="group collapsible"><span class="group-label">生活圈</span>{circle_chips}</div>
    <div class="group"><span class="group-label">排序</span><select id="sort">{sort_options}</select></div>
    <div class="group"><input id="search" type="search" placeholder="搜尋標題 / 地址 / 社區 / 描述…"></div>
    <span id="count"></span>
  </div>
  <div class="grid" id="grid">
{cards}
  </div>
  <footer class="page">點擊標題或「在 591 開啟」按鈕可前往 591 物件頁　·　報告產生時間：{generated}　·　由 591parser 產生，僅供個人找房參考</footer>
</main>
<script>
(function () {{
  // hideUnavailable = true：預設 availability 篩選為 "active"
  var state = {{
    priority: "", circle: "", q: "", availability: "active", costCap: null,
    newOnly: false, changedOnly: false, flatOnly: false, hideMech: false,
    rooms23: false, ownerOnly: false, confirmedOnly: false, hideDup: true
  }};
  var grid = document.getElementById("grid");
  var items = Array.prototype.slice.call(grid.children);

  function visible(el) {{
    var d = el.dataset;
    if (state.availability && d.availability !== state.availability) return false;
    if (state.priority && d.priority !== state.priority) return false;
    if (state.circle && d.circle !== state.circle) return false;
    if (state.q && d.search.indexOf(state.q) === -1) return false;
    if (state.newOnly && d.newThisRun !== "true") return false;
    if (state.changedOnly && d.statusChanged !== "true") return false;
    if (state.flatOnly && d.parking !== "flat") return false;
    if (state.hideMech && d.parking === "mechanical") return false;
    if (state.rooms23 && d.rooms !== "2" && d.rooms !== "3") return false;
    if (state.ownerOnly && d.ownerDirect !== "true") return false;
    if (state.confirmedOnly && d.costConfidence !== "high") return false;
    if (state.hideDup && d.duplicate === "true") return false;
    if (state.costCap !== null) {{
      var cost = parseInt(d.totalCost, 10);
      if (isNaN(cost) || cost > state.costCap) return false;
    }}
    return true;
  }}

  function apply() {{
    var shown = 0;
    items.forEach(function (el) {{
      var ok = visible(el);
      el.style.display = ok ? "" : "none";
      if (ok) shown++;
    }});
    document.getElementById("count").textContent = "顯示 " + shown + " / " + items.length + " 筆";
  }}

  function bindChips(attr, key) {{
    document.querySelectorAll("[" + attr + "]").forEach(function (btn) {{
      btn.addEventListener("click", function () {{
        state[key] = btn.getAttribute(attr);
        btn.parentNode.querySelectorAll(".chip").forEach(function (b) {{ b.classList.remove("active"); }});
        btn.classList.add("active");
        apply();
      }});
    }});
  }}
  bindChips("data-filter-priority", "priority");
  bindChips("data-filter-circle", "circle");
  bindChips("data-filter-availability", "availability");

  document.querySelectorAll("[data-toggle]").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      var key = btn.getAttribute("data-toggle");
      state[key] = !state[key];
      btn.classList.toggle("active", state[key]);
      apply();
    }});
  }});

  document.getElementById("filters-toggle").addEventListener("click", function () {{
    document.getElementById("toolbar").classList.toggle("filters-open");
  }});

  document.getElementById("costcap").addEventListener("change", function (e) {{
    state.costCap = e.target.value ? parseInt(e.target.value, 10) : null;
    apply();
  }});

  document.getElementById("search").addEventListener("input", function (e) {{
    state.q = e.target.value.trim().toLowerCase();
    apply();
  }});

  document.getElementById("sort").addEventListener("change", function (e) {{
    var opt = e.target.selectedOptions[0];
    var attr = opt.dataset.attr, dir = +opt.dataset.dir;
    items.sort(function (a, b) {{
      return (parseFloat(a.dataset[attr]) - parseFloat(b.dataset[attr])) * dir;
    }});
    items.forEach(function (el) {{ grid.appendChild(el); }});
  }});

  apply();
}})();
</script>
</body>
</html>
"""


def write_html_report(df: pd.DataFrame, path: str | Path, title: str = "591 竹北租屋報告") -> Path:
    """輸出 HTML 報告檔，回傳實際路徑。"""
    out = _resolve(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html_report(df, title=title), encoding="utf-8")
    logger.info("已輸出 HTML 報告 %d 筆 -> %s", len(df), out)
    return out
