"""純解析函式與共用工具。

這個模組不依賴 Playwright / BeautifulSoup，全部是純文字處理，
方便 unit test，也作為 selector 失敗時的 regex fallback。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

DEFAULT_LIFE_CIRCLES: list[dict[str, Any]] = [
    {"name": "台元", "keywords": ["台元", "台元科技園區", "福興一路", "台科", "博愛街"]},
    {"name": "縣三", "keywords": ["縣三", "十興", "國賓大悅"]},
    {"name": "遠百/勝利", "keywords": ["遠百", "勝利", "莊敬", "自強北路", "成功"]},
    {"name": "高鐵/嘉豐", "keywords": ["高鐵", "六家", "嘉豐", "東興", "文興", "隘口", "喜來登"]},
    {"name": "華興/市公所", "keywords": ["華興", "中正西路", "市公所", "新國", "文化中心"]},
]


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """讀取 config.yaml，找不到時回傳空 dict（各處都有預設值）。"""
    config_path = path or PROJECT_ROOT / "config.yaml"
    try:
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logging.getLogger(__name__).warning("找不到設定檔 %s，使用內建預設值", config_path)
        return {}


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Optional[Path] = None) -> logging.Logger:
    """設定 root logger：console + logs/run.log。回傳 failed_urls 專用 logger。"""
    log_dir = log_dir or PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        file_handler = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    failed = logging.getLogger("failed_urls")
    failed.setLevel(logging.INFO)
    failed.propagate = False
    if not failed.handlers:
        fh = logging.FileHandler(log_dir / "failed_urls.log", encoding="utf-8")
        fh.setFormatter(fmt)
        failed.addHandler(fh)
    return failed


def log_failed_url(listing_id: str, url: str, error: str, html_path: Optional[str] = None) -> None:
    logging.getLogger("failed_urls").info(
        "listing_id=%s url=%s error=%s html=%s", listing_id, url, error, html_path or "-"
    )


# ---------------------------------------------------------------------------
# 金額解析
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)")


def parse_money(text: Optional[str]) -> Optional[int]:
    """從文字中抓出第一個金額數字，例如 '30,000 元/月' -> 30000。"""
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_rent(text: Optional[str]) -> Optional[int]:
    """解析租金字串，優先找 'X元/月' 格式。"""
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*元\s*/\s*月", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return parse_money(text)


# 費用金額必須帶「元」或「/月」後綴，避免把附近無關數字（如 591）誤當金額
_FEE_AMOUNT_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{2,6})\s*(?:元|/\s*月)")


def parse_fee(text: Optional[str]) -> tuple[Optional[int], str]:
    """解析管理費 / 車位費描述。

    回傳 (金額, 狀態)，狀態為：
      included : 已含在租金內（金額 0）
      extra    : 另計且金額已知
      none     : 無此費用（金額 0）
      unknown  : 有此費用但金額不明，或完全無法判斷
    """
    if not text:
        return None, "unknown"
    text = text.strip()

    # 各訊號取「最靠近開頭」者：例如 '7,300元/月 租金含車位' 應判金額而非「含」
    candidates: list[tuple[int, tuple[Optional[int], str]]] = []
    m = re.search(r"無|不需|免", text)
    if m and not re.search(r"無法", text):
        candidates.append((m.start(), (0, "none")))
    m = re.search(r"含|內含|已含|包含", text)
    if m:
        candidates.append((m.start(), (0, "included")))
    m = _FEE_AMOUNT_RE.search(text)
    if m:
        candidates.append((m.start(), (int(m.group(1).replace(",", "")), "extra")))
    if candidates:
        return min(candidates, key=lambda c: c[0])[1]
    return None, "unknown"


def compute_total_cost(
    rent: Optional[int],
    management_fee: Optional[int],
    management_status: str,
    parking_fee: Optional[int],
    parking_status: str,
    has_parking: bool,
) -> tuple[Optional[int], str]:
    """計算 total_monthly_cost 與 cost_confidence。

    不亂猜：未知費用當 0 加總，但 confidence 降級。
    """
    if rent is None:
        return None, "low"

    unknown_count = 0
    total = rent
    if management_status == "unknown":
        unknown_count += 1
    else:
        total += management_fee or 0

    # 沒有車位就不存在車位費的問題
    if has_parking:
        if parking_status == "unknown":
            unknown_count += 1
        else:
            total += parking_fee or 0

    if unknown_count == 0:
        confidence = "high"
    elif unknown_count == 1:
        confidence = "medium"
    else:
        confidence = "low"
    return total, confidence


# ---------------------------------------------------------------------------
# 車位解析
# ---------------------------------------------------------------------------

FLAT_PARKING_KEYWORDS = ["平面車位", "坡道平面", "B1平車", "B2平車", "平車", "汽車位", "平面式", "平面汽車位"]
MECHANICAL_PARKING_KEYWORDS = ["機械車位", "機械式", "機上", "機下", "升降"]
UNKNOWN_PARKING_KEYWORDS = ["有車位", "車位另計", "可租車位", "含車位", "附車位", "配車位", "車位"]
NO_PARKING_PATTERNS = [r"無車位", r"沒有車位", r"不含車位", r"無提供車位", r"車位\s*[:：]?\s*無"]


def parse_parking(text: Optional[str]) -> tuple[bool, str]:
    """回傳 (has_parking, parking_type)。

    parking_type: flat / mechanical / unknown / none

    機械車位判斷「優先」於平面：同時出現機械與平面關鍵字時
    （例如描述寫「另有機械車位」），保守地判成 mechanical，
    避免把機械車位標成平面而誤導。
    """
    if not text:
        return False, "none"
    for pattern in NO_PARKING_PATTERNS:
        if re.search(pattern, text):
            return False, "none"
    if any(kw in text for kw in MECHANICAL_PARKING_KEYWORDS):
        return True, "mechanical"
    if any(kw in text for kw in FLAT_PARKING_KEYWORDS):
        return True, "flat"
    if any(kw in text for kw in UNKNOWN_PARKING_KEYWORDS):
        return True, "unknown"
    return False, "none"


def parking_included_in_rent(text: Optional[str]) -> Optional[bool]:
    """判斷車位費是否含在租金內；判斷不了回傳 None。"""
    if not text:
        return None
    if re.search(r"含車位|車位含|租金含車位|含平面車位|含機械車位", text):
        return True
    if re.search(r"車位另計|車位另收|車位費另", text):
        return False
    return None


# ---------------------------------------------------------------------------
# 格局解析
# ---------------------------------------------------------------------------

# 房數限 1-9，(?<!\d) 避免把「591房屋交易」誤判成 591 房
_LAYOUT_RE = re.compile(r"(?<!\d)([1-9])\s*房(?:\s*([0-9])\s*廳)?(?:\s*([0-9])\s*衛)?")


def parse_layout(text: Optional[str]) -> dict[str, Any]:
    """解析 '2房2廳1衛' 等格局字串。

    回傳 dict: layout, rooms, living_rooms, bathrooms, is_suite
    """
    result: dict[str, Any] = {
        "layout": None,
        "rooms": None,
        "living_rooms": None,
        "bathrooms": None,
        "is_suite": False,
    }
    if not text:
        return result
    if re.search(r"套房|雅房", text):
        result["is_suite"] = True
        m_suite = re.search(r"(獨立套房|分租套房|套房|雅房)", text)
        result["layout"] = m_suite.group(1) if m_suite else "套房"

    # 取「資訊最完整」的一組（例如同時有 '4房' 與 '4房2廳2衛' 時取後者）
    best = None
    best_groups = -1
    for m in _LAYOUT_RE.finditer(text):
        groups = sum(1 for g in m.groups() if g is not None)
        if groups > best_groups:
            best, best_groups = m, groups
    if best:
        result["rooms"] = int(best.group(1))
        if best.group(2):
            result["living_rooms"] = int(best.group(2))
        if best.group(3):
            result["bathrooms"] = int(best.group(3))
        parts = f"{best.group(1)}房"
        if best.group(2):
            parts += f"{best.group(2)}廳"
        if best.group(3):
            parts += f"{best.group(3)}衛"
        # 描述常出現「全套房設計」等字樣；2 房以上的整層住家不視為套房
        if result["rooms"] >= 2:
            result["is_suite"] = False
        if not result["is_suite"]:
            result["layout"] = parts
    return result


# ---------------------------------------------------------------------------
# 坪數 / 樓層
# ---------------------------------------------------------------------------

def parse_size_ping(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*坪", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def parse_floor(text: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """解析樓層，回傳 (floor 字串, total_floors)。

    支援 '5F/12F'、'樓層：5/12'、'5樓/12樓'。
    """
    if not text:
        return None, None
    m = re.search(r"(\d+|B\d+)\s*F?\s*/\s*(\d+)\s*F?", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"(\d+)\s*樓\s*/\s*(\d+)\s*樓", text)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# 生活圈推測
# ---------------------------------------------------------------------------

def _match_life_circle(text: str, circles: list[dict[str, Any]]) -> Optional[str]:
    for circle in circles:
        if any(kw in text for kw in circle.get("keywords", [])):
            return str(circle.get("name", "其他"))
    return None


def guess_life_circle(
    text: Optional[str],
    life_circles: Optional[list[dict[str, Any]]] = None,
) -> str:
    """依關鍵字表（有優先順序）推測生活圈，未命中回傳 '其他'。"""
    if not text:
        return "其他"
    circles = life_circles or DEFAULT_LIFE_CIRCLES
    return _match_life_circle(text, circles) or "其他"


def guess_life_circle_layered(
    address: Optional[str],
    title: Optional[str],
    description: Optional[str],
    life_circles: Optional[list[dict[str, Any]]] = None,
) -> str:
    """分層推測生活圈：地址 > 標題 > 描述。

    地址是最可靠的訊號；描述常出現行銷用語（例如市公所物件寫
    「10分鐘到台元」），若混在一起比對會把華興/市公所誤判成台元。
    只有前一層完全沒有命中任何生活圈時，才往下一層找。
    """
    circles = life_circles or DEFAULT_LIFE_CIRCLES
    for source in (address, title, description):
        if not source:
            continue
        hit = _match_life_circle(str(source), circles)
        if hit:
            return hit
    return "其他"


def distance_to_taiyuan_note(life_circle: str) -> str:
    """依生活圈給一個到台元的相對距離註記（非精確距離）。"""
    notes = {
        "台元": "台元生活圈內，步行/短程可達",
        "縣三": "縣三生活圈，開車約 5-10 分鐘到台元",
        "遠百/勝利": "遠百/勝利生活圈，開車約 5-10 分鐘到台元",
        "高鐵/嘉豐": "高鐵/嘉豐生活圈，開車約 10-15 分鐘到台元",
        "華興/市公所": "華興/市公所生活圈，開車約 10 分鐘到台元",
    }
    return notes.get(life_circle, "未分類區域，距離台元不確定")


# ---------------------------------------------------------------------------
# 阻擋 / CAPTCHA 偵測
# ---------------------------------------------------------------------------

BLOCK_KEYWORDS = [
    "captcha", "geetest", "滑動驗證", "安全驗證", "驗證碼",
    "access denied", "存取被拒", "請完成驗證", "cloudflare",
]


def looks_blocked(html: Optional[str]) -> bool:
    """粗略判斷是否遇到 CAPTCHA / 反爬阻擋頁。"""
    if not html:
        return True
    if len(html) < 1500:
        return True
    lowered = html.lower()
    return any(kw in lowered for kw in BLOCK_KEYWORDS)


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------

def parse_bool_yes_no(text: Optional[str], positive: str, negative: str) -> Optional[bool]:
    """在文字中找 '可開伙/不可開伙' 這類正反關鍵字，找不到回傳 None。"""
    if not text:
        return None
    # 先比對否定詞，避免 '不可開伙' 被 '可開伙' 誤判
    if negative in text:
        return False
    if positive in text:
        return True
    return None


def truncate(text: Optional[str], limit: int = 8000) -> Optional[str]:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
