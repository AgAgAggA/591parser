---
name: 591-website-guide
description: Domain knowledge for the 591 rental website (rent.591.com.tw) - URL query parameters, region/section/kind enums, listing URL format, unofficial API endpoints, and anti-bot behavior. Use when building search URLs, adding crawl targets, changing regions/districts, constructing hyperlinks to 591 listings, or debugging why the crawler gets blocked or parses nothing.
---

# 591 Rental Website Guide (rent.591.com.tw)

## Canonical URLs

- Search list page: `https://rent.591.com.tw/list?{params}`
- Listing detail page: `https://rent.591.com.tw/{listing_id}` — the id is a 7-8 digit integer, no slug.
  Use this format when generating hyperlinks from a `listing_id`.
- Query params like `?is_ai_video=1` can be stripped from stored URLs; the bare id URL always works.

## List page query parameters

| Param | Meaning | Syntax / Example |
| --- | --- | --- |
| `region` | City (required, single value) | `region=5` = 新竹縣 |
| `section` | District(s) within region | `section=54` = 竹北市 (region-scoped, comma-list) |
| `kind` | Rental type | `kind=1` = 整層住家 |
| `price` | Rent range NT$/month | `price=20000_38000` (`MIN_MAX`) |
| `layout` | Room count 1-4 | `layout=2,3` |
| `acreage` | Size in 坪 | `acreage=15_40` |
| `other` | Amenity flags | `other=cartplace,lift,cook` |
| `sort` | Ordering | `sort=posttime_desc` = newest first |
| `firstRow` | Pagination offset | multiples of 30: `firstRow=30`, `60`, ... |

Key enums (full tables in [reference.md](reference.md)):

- `region`: 1 台北市, 3 新北市, 4 新竹市, **5 新竹縣**, 6 桃園市, 8 台中市, 15 台南市, 17 高雄市
- `kind`: 0 不限, **1 整層住家**, 2 獨立套房, 3 分租套房, 4 雅房, 8 車位
- `other` flags: `cartplace` 有車位, `lift` 有電梯, `cook` 可開伙, `pet` 可養寵物, `near_subway` 近捷運, `newPost` 新上架
- `section` IDs are **region-scoped and not portable** — 竹北市 is `54` only under `region=5`. Re-derive per region by clicking the district checkbox in the UI and reading the URL.

This project's default target: `https://rent.591.com.tw/list?kind=1&region=5&section=54` (整層住家, 新竹縣, 竹北市).

## Page structure notes

- List page is server-rendered (Nuxt SSR); listing cards are in `.item-info`, each with an `<a>` whose href is the detail URL.
- Total match count only appears as localized text: regex `已為你找到\s*([0-9,]+)\s*間` against page text.
- Card text is multi-line and order-sensitive: title is first line, size/floor matches `/\d+坪/`, address matches `/區-/`. Match by content shape, not line index.
- Legacy params (`multiPrice`, `multiRoom`) are auto-rewritten to `price`/`layout` — always send canonical names.

## Unofficial JSON API (informational; this project uses Playwright instead)

- `GET https://rent.591.com.tw/home/search/rsList?is_format_data=1&is_new_list=1&type=1&region=5&kind=1&firstRow=0`
  requires: session cookie from first visiting `https://rent.591.com.tw/`, `X-CSRF-TOKEN` header from `<meta name="csrf-token">`, and cookie `urlJumpIp=<region>` (domain `.591.com.tw`) or district searches return empty. Missing token yields HTTP 419.
- Detail: `GET https://bff.591.com.tw/v1/house/rent/detail?id={listing_id}` with `device=pc` and `deviceid` headers from session cookies.
- These endpoints are undocumented and change without notice; the SSR HTML is the stable data surface.

## Anti-bot / etiquette rules (already enforced by this project)

- Keep 2-5 s random delay between pages (`config.yaml` `crawl.delay_min_seconds` / `delay_max_seconds`).
- On CAPTCHA / block page detection (`src/utils.py` `looks_blocked`), stop immediately — never bypass CAPTCHA, login walls, or rate limits.
- Bare `https://rent.591.com.tw/` 301-redirects based on request IP; always pass `region=` explicitly.
- Front-end markup changes over time; selectors in `src/parser_detail.py` have regex fallbacks in `src/utils.py`, and unparseable fields become null with `parse_status=partial`.
