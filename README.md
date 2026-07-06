# 591 竹北租屋爬蟲

解析 591 租屋網「新竹縣竹北市」整層住家物件，建立可用來找房、比較、打分的 CSV / Excel 清單。

針對的找房條件：

- 整層住家、必須有車位（平面車位優先）
- 2 房 / 小 3 房優先，排除套房、雅房、4 房以上
- 總月付 2.6 萬–3.6 萬為主，全包超過 3.8 萬標記低優先
- 生活圈優先順序：台元 > 縣三 > 遠百/勝利 > 高鐵/嘉豐 > 華興/市公所

## 安裝

需要 Python 3.11+。

```bash
# 建立虛擬環境（建議）
python3.11 -m venv .venv
source .venv/bin/activate

# 安裝套件
pip install -r requirements.txt

# 安裝 Playwright 瀏覽器（只需一次）
playwright install chromium
```

WSL / Linux 若缺系統相依套件，可執行：

```bash
playwright install-deps chromium
```

## 執行

一條龍（列表掃描 -> detail 解析 -> stale check -> 打分 -> 匯出）：

```bash
python main.py run --max-pages 30 --headless true --refresh-stale true
```

每次執行會：

1. 載入 `data/listings_state.sqlite` 的歷史狀態（沒有就新建）。
2. 掃列表頁，標記本輪看到的物件（`seen_count`、`last_seen_at`）；沒看到的只累計 `missing_count`，**不會**直接判定下架。
3. 對本輪看到的物件進 detail page 解析，判斷存活狀態（active / rented / removed / expired / blocked / error / unknown）並更新價格、格局、車位等欄位。
4. 對「之前 active/unknown/error、這次列表沒看到」的舊物件做 stale check：進 detail page 確認，只有頁面明確顯示已出租/下架才改狀態。
5. 重新打分（硬條件 priority）、標記重複刊登、輸出全部檔案，並在 terminal 印出 delta report 與 QA summary。

其他指令：

```bash
python main.py crawl-list --max-pages 30           # 只掃列表頁更新 seen/missing
python main.py refresh-details --only-active true  # 不掃列表，重新檢查既有物件 detail
python main.py check-stale --missing-days 0        # 只做 stale check（缺席物件確認）
python main.py export-report                       # 從 state 重產全部 CSV + HTML
python main.py status-summary                      # 顯示各狀態統計與 QA summary

# 舊版 CSV 工具（不經過 state）
python main.py score --input output/zhubei_591_all.csv
python main.py report --input output/zhubei_591_scored.csv
```

常用參數：`--refresh-stale true/false`、`--active-only true/false`、`--max-stale-checks 200`、`--stop-on-blocked true`、`--save-html true/false`、`--state-path data/listings_state.sqlite`。

另有**爬前預篩**（`config.yaml` 的 `crawl.prefilter`）：列表頁卡片就明顯不符條件的物件（租金 > 32000、4 房以上、套雅房、樓中樓）不進詳細頁；同樣條件也套用在 stale check 佇列，可大幅縮短時間。

URL 參數說明：`kind=1` 整層住家、`region=5` 新竹縣、`section=54` 竹北市。換區域只要換 URL 即可。

跑測試：

```bash
pytest tests/ -v
```

## 手機瀏覽：GitHub Pages（固定網址，本機關機也能看）

報告是**單一自足的靜態 HTML**（inline CSS/JS，無 backend、無 localhost API、無本機路徑），
每次產生報告時會自動複製一份到 `docs/index.html`（`config.yaml` 的 `output.pages_dir`），
`docs/` 就是 GitHub Pages 的發布目錄。報告頁首與頁尾都有「報告產生時間」。

### 首次設定（只需一次）

```bash
# 1. 在 GitHub 建一個 repo（例如 591parser，public 或 private+Pages 皆可）
# 2. 在專案根目錄：
git remote add origin git@github.com:<你的帳號>/591parser.git
git push -u origin main

# 3. 到 GitHub repo -> Settings -> Pages：
#    Source 選 "Deploy from a branch"，Branch 選 main、資料夾選 /docs，存檔。
```

一兩分鐘後，固定網址就是：

```text
https://<你的帳號>.github.io/591parser/
```

### 之後更新報告

```bash
./scripts/deploy_pages.sh   # 手動：commit docs/ 並 push，Pages 自動更新
```

排程（`scripts/scheduled_run.sh`，每天 08:00 / 20:00）跑完爬蟲後會**自動呼叫部署腳本**，
手機用同一個網址重新整理就是最新報告。若尚未設定 git remote，部署會安靜跳過、不影響爬蟲。

目前的固定網址：<https://agagagga.github.io/591parser/>（repo：`AgAgAggA/591parser`）。

## 排程自動更新

已安裝 systemd user timer，每天 08:00 與 20:00 自動執行 `scripts/scheduled_run.sh`
（爬取 + 更新狀態 + 重產報告；log 在 `logs/scheduled.log`，flock 防重疊）：

```bash
systemctl --user list-timers 591parser.timer   # 查看下次執行時間
systemctl --user start 591parser.service       # 手動觸發一次
journalctl --user -u 591parser.service -n 50   # 查看執行紀錄
systemctl --user disable --now 591parser.timer # 停用排程
```

單元檔在 `~/.config/systemd/user/591parser.{service,timer}`；已啟用 linger，
WSL 開著就會執行（Windows 關機或 WSL shutdown 期間錯過的排程，下次啟動會自動補跑一次，`Persistent=true`）。

## 物件存活狀態（availability）

| 狀態 | 意義 |
|---|---|
| `active` | 物件頁正常存在，仍可出租 |
| `rented` | 頁面顯示已出租 / 已成交 / 已租出 |
| `removed` | 已下架 / 不存在 / 404 |
| `expired` | 刊登過期 |
| `blocked` | 被 591 擋住（CAPTCHA / 反爬），立即停止本輪檢查 |
| `error` | 連線錯誤、timeout（retry 最多 2 次、間隔 10 秒） |
| `unknown` | 頁面載入但內容不足以判斷 |

重要原則：

- 列表頁缺席**不會**直接標 removed，一定要 detail page 確認（`missing_count` 只是排入 stale check 的訊號）。
- 即使物件 removed / rented，也**不會從 state 刪除**，歷史資料保留供分析（流動速度、重複刊登、重新上架）。
- 狀態改變時記錄 `status_changed`、`previous_status`、`status_change_note`（如 `active -> rented`），並設定/清除 `unavailable_since`。

## 輸出檔案

| 檔案 | 內容 |
|---|---|
| `data/listings_state.sqlite` | 持久化 state：所有物件的歷史狀態（永不刪除） |
| `output/zhubei_591_all_current.csv` | state 內全部物件（含 removed / rented / blocked / error） |
| `output/zhubei_591_active.csv` | 只有 `availability_status == "active"` |
| `output/zhubei_591_unavailable.csv` | `rented / removed / expired` |
| `output/zhubei_591_status_changes.csv` | 本輪狀態有改變的物件 |
| `output/zhubei_591_top_candidates.csv` | active + 2/3 房 + 非套房 + 有車位 + 月付 <= 36000，依 score 排序 |
| `output/zhubei_591_report.html` | 單檔互動卡片報告：**預設只顯示 active 並隱藏重複刊登**，卡片顯示存活狀態/檢查時間/狀態變更，支援 車位/房數/屋主直租/費用確認/月付上限 等篩選 |

### 主要欄位說明

| 欄位 | 說明 |
|---|---|
| `price` | 租金（元/月） |
| `management_fee` / `parking_fee` | 管理費 / 車位費；`0` 表示已含或無此費用 |
| `total_monthly_cost` | 租金 + 管理費 + 車位費 |
| `cost_confidence` | `high`：費用都確認；`medium`：一項未知；`low`：租金未知或多項未知。未知費用不會亂猜金額 |
| `rooms` / `living_rooms` / `bathrooms` | 從「2房2廳1衛」解析 |
| `is_suite` | 套房 / 雅房（會被 filtered 排除） |
| `has_parking` / `parking_type` | `flat` 平面（含坡道平面）/ `mechanical` 機械 / `unknown` 有車位但型式未確認 / `none` |
| `life_circle_guess` | 由地址、標題、描述關鍵字推測：台元 / 縣三 / 遠百/勝利 / 高鐵/嘉豐 / 華興/市公所 / 其他 |
| `distance_to_taiyuan_note` | 到台元的相對距離註記（依生活圈粗估，非精確距離） |
| `score` | 0–100 總分 |
| `priority` | **硬條件 + score**：必須通過硬條件（2-3 房、非套房、有車位、月付已知且 <= 36000）且 score >= 80 才是「優先約看」；通過硬條件且 score >= 65 為「可備選」；費用未知者最多「可備選」；非 active 一律「先跳過」 |
| `hard_pass` | 是否通過全部硬條件 |
| `availability_status` / `availability_reason` | 存活狀態與判斷原因（見上表） |
| `first_seen_at` / `last_seen_at` / `last_checked_at` | 第一次看到 / 最後在列表看到 / 最後 detail 檢查時間 |
| `unavailable_since` | 確認為 rented/removed/expired 的時間 |
| `seen_count` / `missing_count` | 列表頁看到次數 / 連續缺席次數 |
| `is_duplicate` / `duplicate_group` | 重複刊登標記（同社區+同租金+同房數+相近坪數，保留分數最高一筆） |
| `parse_status` / `parse_error` | `ok` / `partial`（部分欄位失敗）/ `failed`（整頁失敗） |

### 打分權重

| 分項 | 滿分 |
|---|---|
| location_score（生活圈） | 25 |
| cost_score（總月付） | 20 |
| parking_score（車位型式） | 15 |
| condition_score（電梯、開伙、家具、可即入住） | 15 |
| commute_score（到台元通勤） | 10 |
| layout_score（房數） | 10 |
| landlord_contract_score（屋主直租、報稅、遷戶籍） | 5 |

## 如何調整篩選 / 打分條件

全部設定都在 [config.yaml](config.yaml)，不需要改程式碼：

- `filter`：filtered.csv 的條件（允許房數、月付上限、是否必須有車位）
- `scoring.weights`：各分項滿分
- `scoring.cost_brackets`：月付級距對應分數
- `scoring.location_scores` / `commute_scores`：各生活圈分數
- `life_circles`：生活圈關鍵字表（由上而下依優先順序比對）
- `crawl.delay_min_seconds` / `delay_max_seconds`：頁面之間的延遲

## 反爬 / 網站限制說明（請務必閱讀）

- 本工具**只讀取公開頁面**，每頁之間有 2–5 秒隨機延遲，請勿調低延遲或高頻爬取。
- 偵測到 CAPTCHA、驗證頁或阻擋頁時會**立即停止並輸出提示**，本工具**不會也不應**繞過 CAPTCHA、登入牆、付費牆或任何反爬機制。
- 不保證能抓到 591 後端隱藏、已下架、僅限 App、或被阻擋的物件；列表頁顯示的總數可能與實際抓到的筆數不同。
- 591 前端會改版，selector 失效時程式會 fallback 到文字 regex，但仍可能有欄位解析不到（填 null 並記錄在 `parse_status`）。
- 爬取結果僅供個人找房參考，請遵守 591 的服務條款與 robots 規範。

## Debug

- `logs/run.log`：完整執行 log
- `logs/failed_urls.log`：解析失敗的 listing_id、URL、錯誤訊息、HTML 快照路徑
- `--save-html`：把原始 HTML 存到 `raw_pages/list/`、`raw_pages/detail/`，方便離線重新解析

## 專案結構

```text
591parser/
  README.md
  requirements.txt
  config.yaml          # 所有可調參數
  main.py              # CLI：run / crawl-list / refresh-details / check-stale / export-report / status-summary
  src/
    crawler.py         # Playwright 瀏覽器控制、翻頁、延遲、retry、CAPTCHA 偵測
    availability.py    # detect_availability：active/rented/removed/expired/blocked/error/unknown
    state.py           # SQLite state store、狀態轉移、重複刊登偵測
    pipeline.py        # state-based 五步驟 crawl 流程 + delta report + QA summary
    parser_list.py     # 列表頁卡片解析（listing_id 去重）
    parser_detail.py   # 詳細頁欄位解析（selector + regex fallback）
    models.py          # pydantic 資料模型
    score.py           # 打分邏輯 + 硬條件 priority
    export.py          # CSV / Excel / state 輸出
    report_html.py     # 單檔 HTML 互動報告（availability 篩選）
    utils.py           # 金額 / 車位 / 格局 / 生活圈純解析函式
  tests/
    test_parsers.py       # 解析與打分 unit tests
    test_availability.py  # availability / 狀態轉移 / 硬條件 priority tests
  data/    # listings_state.sqlite（持久化 state）
  output/  logs/  raw_pages/
```
