---
name: 591-html-report
description: Generate and style the 591parser HTML report - converting scored CSV output into the single-file interactive report (output/zhubei_591_report.html) with clickable 591 listing links, priority colors, filtering and sorting. Use when regenerating the report, changing report columns, styling, filters, or hyperlink behavior.
---

# 591 HTML Report

Single-file static HTML report generated from the scored CSV. No server or build step; open it directly in a browser.

## Generate

```bash
source .venv/bin/activate
python main.py export-report                       # from state DB -> output/zhubei_591_report.html (preferred)
python main.py report --input output/zhubei_591_scored.csv   # legacy CSV path still works
```

`run`, `crawl-list`, `refresh-details`, `check-stale` also regenerate the default report automatically (via `src/export.py` `export_state_outputs`, which merges each `ListingState` with its stored `payload` through `states_to_report_dataframe`). Output path is `config.yaml` `output.report_html`.

## Implementation map (`src/report_html.py`)

Single-file architecture: listings are embedded as JSON in `<script id="listings-data" type="application/json">` (`_listing_record()` builds clean records; `</` is escaped to `<\/`). Cards are rendered client-side by inline JS (`buildCard()`), so the file works from `file://`, GitHub Pages, or any static host — no fetch, no localhost, no backend. Python side only renders summary cards + filter chips (needs pandas); everything else happens in the browser.

Card/gallery layout: one `<article class="listing">` per listing in a responsive CSS grid.

- JS `buildCard(l)`: renders one card — status strip (availability label, 本輪看到, 最後檢查/第一次看到 timestamps, status-change note, duplicate/new badges), priority badge, life-circle chip, conic-gradient score ring, price block with cost breakdown, 2-column meta grid, feature tag badges, clamped description, footer with "在 591 開啟" button. All text goes through the JS `esc()` helper.
- Embedded fields are listed in `_STR_FIELDS` / `_NUM_FIELDS` / `_BOOL_FIELDS` — add a field there AND use it in `buildCard()`.
- Mobile (<=640px): single-column grid, filters collapse behind a "☰ 篩選" toggle, summary cards 3-per-row.
- `publish_to_pages()` in `src/export.py` copies the report to `docs/index.html` (+ `.nojekyll`) for GitHub Pages; `scripts/deploy_pages.sh` commits & pushes `docs/`.
- Availability: default view shows **only `availability_status == "active"`** and hides duplicates (`hideDup: true` in the JS state). Non-active cards get class `unavail` (grayscale) and their badge shows the status label (`AVAILABILITY_LABELS`) instead of priority — never 優先約看.
- Data attributes per card: `data-availability`, `data-seen-this-run`, `data-status-changed`, `data-new-this-run`, `data-parking`, `data-rooms`, `data-owner-direct`, `data-cost-confidence`, `data-duplicate`, `data-total-cost`, `data-hard-pass`, plus legacy `data-priority/-circle/-search/-score/-cost/-price/-size`.
- Toolbar filters: availability chips (只看有效物件 default / 顯示已下架已出租), boolean toggle chips (`data-toggle`: newOnly, changedOnly, flatOnly, hideMech, rooms23, ownerOnly, confirmedOnly, hideDup), cost-cap select (`#costcap`: 28000/30000/33000/36000/全部), priority + life-circle chips, sort dropdown, text search.
- `_summary_html(df)`: 11 summary cards — 物件總數/有效物件/本輪新物件/消失待確認/已出租下架/狀態變更/硬條件通過/優先約看/平面車位/機械車位/月付中位數. Median cost uses **active listings only**. `_col()` guards against missing columns so legacy CSVs (no availability fields) still render — missing availability defaults to "active".
- `listing_url(row)`: `https://rent.591.com.tw/{listing_id}` preferred over stored url.

## Conventions when editing

- Keep everything inline (CSS + JS + JSON data in the template); the report must stay a single portable file. Never add fetch()/XHR or external assets.
- Escape scraped text: `html.escape()` on the Python side (chips/summary), the `esc()` helper in JS (cards) — titles and descriptions come from the web.
- Missing values render as `—`; missing numerics get sentinel sort values (cost/price `999999999`, size `0`); missing `data-total-cost` is excluded when a cost cap is chosen.
- Default order is score descending (NaN scores sink via `na_position="last"`).
- After changes, validate with a headless render (cards are JS-built, so grep on the raw file won't count them):
  load `file://.../docs/index.html` in Playwright, assert `article.listing` count equals the state row count and there are no `pageerror` events, at desktop and 390px-wide mobile viewports.
