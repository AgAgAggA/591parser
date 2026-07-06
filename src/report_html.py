"""HTML 報告輸出：single-file static HTML（GitHub Pages 直接可開）。

架構：
- 只輸出一個 HTML 檔：CSS inline 於 <style>、JS inline 於 <script>
- listings 資料以 JSON 嵌在 <script id="listings-data" type="application/json">
- 卡片由前端 JS 從嵌入的 JSON 渲染（不 fetch 任何本機 JSON、不依賴
  localhost 或 backend server），手機 Safari / Chrome 可直接開啟
- 預設只顯示 availability_status == "active" 且隱藏重複刊登
- 篩選 chips：有效/下架、本輪新物件、狀態變更、車位型式、2-3 房、
  屋主直租、費用已確認、總月付上限；非 active 卡片變灰、不顯示「優先約看」
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .score import to_bool, to_number
from .utils import PROJECT_ROOT

logger = logging.getLogger(__name__)

LISTING_URL_BASE = "https://rent.591.com.tw"

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

# 嵌入 JSON 的欄位（前端卡片渲染所需的全部資料）
_STR_FIELDS = [
    "listing_id", "title", "community_name", "address", "layout", "floor",
    "parking_type", "life_circle_guess", "agent_type", "deposit", "description",
    "distance_to_taiyuan_note", "posted_time", "priority", "cost_confidence",
    "availability_status", "first_seen_at", "last_checked_at", "status_change_note",
    "furniture_appliances",
]
_NUM_FIELDS = [
    "price", "management_fee", "parking_fee", "total_monthly_cost",
    "size_ping", "total_floors", "score", "rooms",
]
_BOOL_FIELDS = [
    "has_parking", "owner_direct", "has_elevator", "can_cook", "available_now",
    "can_tax_report", "can_register_household", "parking_included_in_rent",
    "seen_in_list_page_this_run", "status_changed", "new_this_run",
    "is_duplicate", "hard_pass",
]


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


def _clean_str(value) -> Optional[str]:
    return None if _missing(value) else str(value)


def _listing_record(row: dict) -> dict[str, Any]:
    """把一筆資料整理成乾淨的 JSON record（無 NaN、型別正確）。"""
    record: dict[str, Any] = {}
    for key in _STR_FIELDS:
        record[key] = _clean_str(row.get(key))
    for key in _NUM_FIELDS:
        record[key] = to_number(row.get(key))
    for key in _BOOL_FIELDS:
        record[key] = to_bool(row.get(key))
    record["url"] = listing_url(row)
    status = (record.get("availability_status") or "active").lower()
    record["availability_status"] = status if status in AVAILABILITY_LABELS else "active"
    return record


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
    """把 DataFrame 渲染成 single-file HTML 字串（資料嵌入 JSON、前端渲染卡片）。"""
    if "score" in df.columns:
        df = df.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)

    records = [_listing_record(row) for row in df.to_dict(orient="records")]
    # </ 逸出成 <\/：避免資料裡出現 </script> 提前關閉 script tag
    data_json = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")

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
  <div class="grid" id="grid"></div>
  <footer class="page">點擊標題或「在 591 開啟」按鈕可前往 591 物件頁　·　報告產生時間：{generated}　·　由 591parser 產生，僅供個人找房參考</footer>
</main>
<script id="listings-data" type="application/json">{data_json}</script>
<script>
(function () {{
  "use strict";
  var LISTINGS = JSON.parse(document.getElementById("listings-data").textContent);

  var AVAIL_LABELS = {{ active: "有效", rented: "已出租", removed: "已下架", expired: "已過期",
                       blocked: "被阻擋", error: "錯誤", unknown: "未知" }};
  var PARKING_LABELS = {{ flat: "平面車位", mechanical: "機械車位", unknown: "車位(型式未確認)", none: "無車位" }};
  var PRIORITY_CLASSES = {{ "優先約看": "p-top", "可備選": "p-backup", "先跳過": "p-skip" }};
  var CONF_LABELS = {{ high: "費用已確認", medium: "一項費用未知", low: "費用不完整" }};
  var FEATURES = [
    ["has_elevator", "電梯"], ["can_cook", "可開伙"], ["available_now", "即可入住"],
    ["owner_direct", "屋主直租"], ["can_tax_report", "可報稅"],
    ["can_register_household", "可遷戶籍"], ["parking_included_in_rent", "租金含車位"]
  ];

  function esc(value) {{
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }}
  function money(value, fallback) {{
    if (value == null || isNaN(value)) return fallback || "—";
    return Math.round(value).toLocaleString("en-US");
  }}
  function text(value, fallback) {{ return (value == null || value === "") ? (fallback || "—") : esc(value); }}
  function dtShort(value) {{ return value ? esc(String(value).slice(0, 16)) : "—"; }}

  function costBreakdown(l) {{
    var parts = ["租金 " + money(l.price, "?")];
    if (l.management_fee == null) parts.push("管理費 ?");
    else if (l.management_fee > 0) parts.push("管理費 " + money(l.management_fee));
    if (l.has_parking) {{
      if (l.parking_fee == null) parts.push("車位費 ?");
      else if (l.parking_fee > 0) parts.push("車位費 " + money(l.parking_fee));
    }}
    return parts.join(" + ");
  }}

  function statusStrip(l, avail) {{
    var seen = l.seen_in_list_page_this_run;
    var bits = [
      '<span class="avail avail-' + avail + '">狀態：' + AVAIL_LABELS[avail] + "</span>",
      "<span>本輪看到：" + (seen == null ? "—" : (seen ? "是" : "否")) + "</span>",
      "<span>最後檢查：" + dtShort(l.last_checked_at) + "</span>",
      "<span>第一次看到：" + dtShort(l.first_seen_at) + "</span>"
    ];
    if (l.status_changed && l.status_change_note)
      bits.push('<span class="change-note">狀態變更：' + esc(l.status_change_note) + "</span>");
    if (l.is_duplicate) bits.push('<span class="dup-note">疑似重複刊登</span>');
    if (l.new_this_run) bits.push('<span class="new-note">本輪新物件</span>');
    return '<div class="status-strip">' + bits.join("") + "</div>";
  }}

  function buildCard(l) {{
    var avail = l.availability_status || "active";
    var isActive = avail === "active";
    // 非 active 一律不可顯示「優先約看」
    var priority = isActive ? (l.priority || "") : "先跳過";
    var pClass = PRIORITY_CLASSES[priority] || "p-skip";
    var badge = isActive
      ? '<span class="badge ' + pClass + '">' + text(priority, "未分級") + "</span>"
      : '<span class="badge b-unavail">' + AVAIL_LABELS[avail] + "</span>";

    var score = l.score == null ? 0 : l.score;
    var cost = l.total_monthly_cost;
    var costClass = "";
    if (isActive && cost != null) costClass = cost <= 36000 ? "cost-good" : (cost > 38000 ? "cost-bad" : "");
    var conf = l.cost_confidence || "";
    var confHtml = CONF_LABELS[conf]
      ? '<span class="conf conf-' + esc(conf) + '">' + CONF_LABELS[conf] + "</span>" : "";

    var floorText = (l.floor != null && l.total_floors != null)
      ? text(l.floor) + "F/" + money(l.total_floors) + "F" : text(l.floor);
    var meta = [
      ["格局", text(l.layout)],
      ["坪數", l.size_ping != null ? money(l.size_ping) + " 坪" : "—"],
      ["樓層", floorText],
      ["社區", text(l.community_name)],
      ["地址", text(l.address)],
      ["車位", text(PARKING_LABELS[l.parking_type] || l.parking_type)],
      ["刊登者", text(l.agent_type)],
      ["押金", text(l.deposit)]
    ].map(function (kv) {{
      return '<div class="meta-item"><span class="meta-label">' + kv[0] +
             '</span><span class="meta-value">' + kv[1] + "</span></div>";
    }}).join("");

    var tags = FEATURES.filter(function (f) {{ return l[f[0]] === true; }})
      .map(function (f) {{ return '<span class="tag">' + f[1] + "</span>"; }});
    if (l.furniture_appliances) tags.push('<span class="tag">附家具家電</span>');

    var desc = (l.description || "").trim();
    var descHtml = desc
      ? '<p class="desc">' + esc(desc.slice(0, 160)) + (desc.length > 160 ? "…" : "") + "</p>" : "";

    var footerBits = [];
    if (l.distance_to_taiyuan_note) footerBits.push(esc(l.distance_to_taiyuan_note));
    if (l.posted_time) footerBits.push("刊登：" + esc(l.posted_time));
    footerBits.push("ID " + text(l.listing_id));

    var url = l.url || "";
    var title = text(l.title, "(無標題)");
    var titleHtml = url
      ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + title + "</a>" : title;
    var openBtn = url
      ? '<a class="open-btn" href="' + esc(url) + '" target="_blank" rel="noopener">在 591 開啟 ↗</a>' : "";

    var el = document.createElement("article");
    el.className = "listing " + pClass + "-border" + (isActive ? "" : " unavail");
    var d = el.dataset;
    d.priority = priority;
    d.circle = l.life_circle_guess || "其他";
    d.search = [l.title, l.address, l.community_name, l.life_circle_guess, l.layout, l.description]
      .map(function (v) {{ return v || ""; }}).join(" ").toLowerCase();
    d.score = score;
    d.cost = cost != null ? cost : 999999999;
    d.price = l.price != null ? l.price : 999999999;
    d.size = l.size_ping != null ? l.size_ping : 0;
    d.availability = avail;
    d.seenThisRun = l.seen_in_list_page_this_run === false ? "false" : "true";
    d.statusChanged = l.status_changed === true ? "true" : "false";
    d.newThisRun = l.new_this_run === true ? "true" : "false";
    d.parking = l.parking_type || "none";
    d.rooms = l.rooms != null ? String(Math.round(l.rooms)) : "";
    d.ownerDirect = l.owner_direct === true ? "true" : "false";
    d.costConfidence = conf || "low";
    d.duplicate = l.is_duplicate === true ? "true" : "false";
    d.totalCost = cost != null ? String(Math.round(cost)) : "";
    d.hardPass = l.hard_pass === true ? "true" : "false";

    var pct = Math.max(0, Math.min(100, score));
    el.innerHTML =
      statusStrip(l, avail) +
      '<div class="listing-head">' + badge +
        '<span class="circle-chip">' + esc(d.circle) + "</span>" +
        '<div class="score-ring" style="--pct:' + pct + '"><span>' + (Math.round(score * 10) / 10) + "</span></div>" +
      "</div>" +
      '<h2 class="listing-title">' + titleHtml + "</h2>" +
      '<div class="price-row ' + costClass + '">' +
        '<span class="price-main">' + money(cost, "月付 ?") + '</span><span class="price-unit">元/月</span>' + confHtml +
        '<div class="price-breakdown">' + esc(costBreakdown(l)) + "</div>" +
      "</div>" +
      '<div class="meta-grid">' + meta + "</div>" +
      '<div class="tags">' + tags.join("") + "</div>" +
      descHtml +
      '<div class="listing-footer"><span>' + footerBits.join("　·　") + "</span>" + openBtn + "</div>";
    return el;
  }}

  var grid = document.getElementById("grid");
  var frag = document.createDocumentFragment();
  LISTINGS.forEach(function (l) {{ frag.appendChild(buildCard(l)); }});
  grid.appendChild(frag);
  var items = Array.prototype.slice.call(grid.children);

  // hideUnavailable = true：預設 availability 篩選為 "active"
  var state = {{
    priority: "", circle: "", q: "", availability: "active", costCap: null,
    newOnly: false, changedOnly: false, flatOnly: false, hideMech: false,
    rooms23: false, ownerOnly: false, confirmedOnly: false, hideDup: true
  }};

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
