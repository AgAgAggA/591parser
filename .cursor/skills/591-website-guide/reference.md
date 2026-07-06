# 591 Enum Reference

## `region` (city)

| ID | Region | ID | Region | ID | Region |
| --- | --- | --- | --- | --- | --- |
| 1 | 台北市 | 2 | 基隆市 | 3 | 新北市 |
| 4 | 新竹市 | 5 | 新竹縣 | 6 | 桃園市 |
| 7 | 苗栗縣 | 8 | 台中市 | 10 | 彰化縣 |
| 11 | 南投縣 | 12 | 嘉義市 | 13 | 嘉義縣 |
| 14 | 雲林縣 | 15 | 台南市 | 17 | 高雄市 |
| 19 | 屏東縣 | 21 | 宜蘭縣 | 22 | 台東縣 |
| 23 | 花蓮縣 | 24 | 澎湖縣 | 25 | 金門縣 |

IDs 9, 16, 18, 20 are reserved/unused — treat as invalid.

## `kind` (rental type)

| ID | Type |
| --- | --- |
| 0 | 不限 (all) |
| 1 | 整層住家 (whole apartment) |
| 2 | 獨立套房 (independent studio) |
| 3 | 分租套房 (sublet studio) |
| 4 | 雅房 (room, shared bath) |
| 8 | 車位 (parking space) |
| 9 | 住宅 (residential aggregate) |
| 10 | 套房 (studio aggregate = 2+3) |

Kinds 5/6/7/11 redirect to sibling 591 sites (store/office/factory/land) and lose the rent filter UI.

## `other` (amenity flags, comma-joined)

| Slug | Label |
| --- | --- |
| `newPost` | 新上架 |
| `near_subway` | 近捷運 |
| `pet` | 可養寵物 |
| `cook` | 可開伙 |
| `cartplace` | 有車位 |
| `lift` | 有電梯 |
| `balcony_1` | 有陽台 |
| `lease` | 可短期租賃 |
| `social-housing` | 社會住宅 |
| `rental-subsidy` | 租金補貼 |
| `elderly-friendly` | 高齡友善 |
| `tax-deductible` | 可報稅 |
| `naturalization` | 可入籍 |

## `shape` (building type, partial)

| ID | Type |
| --- | --- |
| 1 | 公寓 |
| 3 | 透天厝 |
| 4 | 別墅 |

## `section` for 台北市 (region=1) — example of region-scoped IDs

| ID | District | ID | District | ID | District |
| --- | --- | --- | --- | --- | --- |
| 1 | 中正區 | 2 | 大同區 | 3 | 中山區 |
| 4 | 松山區 | 5 | 大安區 | 6 | 萬華區 |
| 7 | 信義區 | 8 | 士林區 | 9 | 北投區 |
| 10 | 內湖區 | 11 | 南港區 | 12 | 文山區 |

For 新竹縣 (region=5): 竹北市 = `54` (verified, used by this project). Derive other districts by clicking the checkbox on `https://rent.591.com.tw/list?region=5` and reading the URL.
