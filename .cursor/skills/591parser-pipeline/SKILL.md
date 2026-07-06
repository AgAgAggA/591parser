---
name: 591parser-pipeline
description: How to run, extend, and debug the 591parser project - CLI commands (crawl/score/export/run), module layout, data model columns, scoring weights, config.yaml tuning, and testing conventions. Use when modifying the crawler, parser, scoring, filtering, or export logic, adding output formats, changing search conditions, or investigating parse failures.
---

# 591parser Pipeline

Playwright-based crawler for 竹北 whole-apartment rentals on 591, with scoring and export. Python 3.11+, virtualenv at `.venv/`.

## CLI (run from project root)

```bash
source .venv/bin/activate
python main.py run --max-pages 30 --headless true      # crawl -> score -> export
python main.py crawl --url "..." --max-pages 30 --save-html
python main.py score  --input output/zhubei_591_all.csv
python main.py export --input output/zhubei_591_scored.csv
pytest tests/ -v
```

Crawl is **incremental by default** (skips listing_ids already in `all.csv`); add `--full` to re-crawl everything. Prefilter in `config.yaml` (`crawl.prefilter`) skips obviously unqualified cards before opening detail pages.

## Module map

| File | Responsibility |
| --- | --- |
| `main.py` | Typer CLI: crawl / score / export / run, incremental merge |
| `src/crawler.py` | Playwright control, pagination, delays, CAPTCHA detection (`CaptchaDetectedError`) |
| `src/parser_list.py` | List-page card parsing, listing_id dedup |
| `src/parser_detail.py` | Detail-page field parsing (selectors + regex fallback) |
| `src/models.py` | Pydantic `Listing` model + `LISTING_COLUMNS` (CSV column order) |
| `src/score.py` | Scoring logic (reads weights from config) |
| `src/export.py` | CSV / Excel / HTML writers, `apply_filter`, `export_all` |
| `src/utils.py` | Pure text parsers (money, parking, layout, life circle), config/logging, `PROJECT_ROOT` |

Rules of the codebase:

- All tunable numbers (weights, brackets, filters, life-circle keywords) live in `config.yaml`, never hardcoded.
- Parse failures never crash: fields stay `None`, `parse_status` becomes `partial`/`failed`, errors go to `logs/failed_urls.log`.
- Pure text-parsing functions belong in `src/utils.py` (no Playwright/bs4 deps) so they are unit-testable; add tests to `tests/test_parsers.py`.
- Paths are resolved relative to `PROJECT_ROOT` (`src/utils.py`), so the CLI works from any cwd.
- CSVs are written `utf-8-sig` (Excel-friendly), `listing_id` always read as `str`.

## Data flow and outputs

```
crawl -> output/zhubei_591_all.csv        (all parsed listings + raw_text, parse_status)
score -> output/zhubei_591_scored.csv     (adds 7 sub-scores, score 0-100, priority)
export -> output/zhubei_591_filtered.csv  (parking + 2-3 rooms + <=38000 + not suite)
       -> output/zhubei_591_top_candidates.xlsx (formatted, sorted by score)
       -> output/zhubei_591_report.html   (interactive report, links to 591)
```

Key columns: `total_monthly_cost` = price + management_fee + parking_fee; `cost_confidence` high/medium/low (unknown fees are never guessed); `parking_type` flat/mechanical/unknown/none; `life_circle_guess` from keyword table; `priority` = 優先約看 (score>=80) / 可備選 (65-79) / 先跳過.

## Scoring (max 100)

location 25 + cost 20 + parking 15 + condition 15 + commute 10 + layout 10 + landlord 5. All brackets and per-value scores are in `config.yaml` under `scoring:`. Life-circle priority: 台元 > 縣三 > 遠百/勝利 > 高鐵/嘉豐 > 華興/市公所 > 其他.

## Debugging checklist

1. `logs/run.log` — full run log; `logs/failed_urls.log` — failed listing_id/URL/error/HTML snapshot path.
2. Re-run crawl with `--save-html` and inspect `raw_pages/list/`, `raw_pages/detail/{listing_id}.html` offline.
3. Exit code 2 = CAPTCHA detected (stop, wait, retry later with `--headless false`); exit code 1 = nothing parsed (check URL).
4. If a field stops parsing after a 591 redesign, fix the selector in `src/parser_detail.py` and/or extend the regex fallback in `src/utils.py`, then add a regression test with the real text snippet.

For 591 URL parameters and site behavior, see the `591-website-guide` skill.
