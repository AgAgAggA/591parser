"""591 竹北租屋爬蟲 CLI（state-based，含物件存活狀態追蹤）。

用法：
  python main.py run --max-pages 30 --headless false --refresh-stale true
  python main.py crawl-list --max-pages 30
  python main.py refresh-details --only-active true
  python main.py check-stale --missing-days 0
  python main.py export-report
  python main.py status-summary

  # 舊版 CSV 工具（不經過 state）
  python main.py score --input output/zhubei_591_all.csv
  python main.py report --input output/zhubei_591_scored.csv
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from src.export import export_all, export_state_outputs, write_csv
from src.pipeline import RunReport, qa_summary, run_pipeline
from src.report_html import write_html_report
from src.score import score_dataframe
from src.state import StateStore
from src.utils import PROJECT_ROOT, load_config, setup_logging

app = typer.Typer(help="591 租屋網竹北整層住家爬蟲 / 存活追蹤 / 打分 / 匯出工具", no_args_is_help=True)
console = Console()
logger = logging.getLogger("main")

DEFAULT_URL = "https://rent.591.com.tw/list?kind=1&region=5&section=54"


def _parse_bool(value: str) -> bool:
    """讓 CLI 可以寫 --headless false / --refresh-stale true 這種形式。"""
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _read_csv(path: str) -> pd.DataFrame:
    csv_path = Path(path)
    if not csv_path.is_absolute():
        csv_path = PROJECT_ROOT / csv_path
    if not csv_path.exists():
        console.print(f"[red]找不到輸入檔：{csv_path}[/red]")
        raise typer.Exit(code=1)
    return pd.read_csv(csv_path, dtype={"listing_id": str})


# ---------------------------------------------------------------------------
# 報表輸出（terminal）
# ---------------------------------------------------------------------------

def _print_delta_report(report: RunReport) -> None:
    counts = report.status_counts
    lines = [
        ("Run timestamp", report.run_timestamp),
        ("List-page seen listings", report.list_seen),
        ("Previously active but missing from list", report.previously_active_missing),
        ("Detail checked", report.detail_checked + report.stale_checked),
        ("Active", counts.get("active", 0)),
        ("New listings", report.new_listings),
        ("Recovered listings", report.recovered),
        ("Rented", counts.get("rented", 0)),
        ("Removed", counts.get("removed", 0)),
        ("Expired", counts.get("expired", 0)),
        ("Blocked", counts.get("blocked", 0)),
        ("Error", counts.get("error", 0)),
        ("Unknown", counts.get("unknown", 0)),
        ("Status changed", report.status_changed),
        ("Top candidates", report.top_candidates),
    ]
    table = Table(title="Delta report")
    table.add_column("項目")
    table.add_column("數值", justify="right")
    for label, value in lines:
        table.add_row(label, str(value))
    console.print(table)

    if report.changes:
        console.print(f"[bold]Status changes（前 20 / 共 {len(report.changes)}）[/bold]")
        for change in report.changes[:20]:
            console.print(
                f"  {change['listing_id']} | {change['old_status']} -> {change['new_status']} | "
                f"{(change['title'] or '')[:30]} | {change['url']} | {change['reason']}"
            )
    if report.blocked_hit:
        console.print(
            "[red bold]警告：本輪偵測到 blocked / CAPTCHA，detail 檢查已提前停止並儲存 state。\n"
            "請稍後（建議數小時後）再執行，不要提高頻率，也不要嘗試繞過驗證。[/red bold]"
        )


def _print_qa_summary(states: dict, config: dict) -> None:
    qa = qa_summary(states, config)
    console.print("[bold]QA summary[/bold]")
    for key, value in qa.items():
        if key.startswith("_debug"):
            continue
        console.print(f"  {key}: {value}")
    for counter, debug_key in (
        ("huaxing_mislabeled_as_taiyuan_count", "_debug_huaxing_ids"),
        ("mechanical_displayed_as_flat_count", "_debug_mechanical_ids"),
        ("rent_includes_management_but_unknown_fee_count", "_debug_mgmt_ids"),
    ):
        if qa[counter]:
            console.print(f"  [yellow]{counter} 前 20 個 listing_id：{qa[debug_key]}[/yellow]")


def _finish_run(states: dict, report: Optional[RunReport], config: dict) -> None:
    paths = export_state_outputs(states, config)
    if report is not None:
        _print_delta_report(report)
    _print_qa_summary(states, config)
    console.print("[green bold]輸出完成：[/green bold]")
    for name, path in paths.items():
        console.print(f"  {name}: {path}")


# ---------------------------------------------------------------------------
# State-based 指令
# ---------------------------------------------------------------------------

@app.command("run")
def run_command(
    url: str = typer.Option(DEFAULT_URL, help="591 搜尋列表頁 URL"),
    max_pages: int = typer.Option(30, help="最多掃描幾頁列表頁"),
    headless: str = typer.Option("true", help="是否使用 headless 瀏覽器 (true/false)"),
    save_html: str = typer.Option("false", "--save-html", help="儲存原始 HTML 到 raw_pages/ (true/false)"),
    refresh_stale: str = typer.Option("true", "--refresh-stale", help="對列表缺席的舊 active 物件做 detail 確認 (true/false)"),
    active_only: str = typer.Option("false", "--active-only", help="detail 只重新檢查 active/unknown 物件 (true/false)"),
    max_stale_checks: int = typer.Option(200, "--max-stale-checks", help="stale check 單輪上限"),
    stop_on_blocked: str = typer.Option("true", "--stop-on-blocked", help="遇到 blocked 立即停止 (true/false)"),
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑（預設 config state.path）"),
) -> None:
    """完整流程：列表掃描 -> detail 解析 -> stale check -> 打分 -> 匯出全部檔案。"""
    setup_logging()
    config = load_config()
    console.print(f"[bold]開始執行 pipeline[/bold] {url}（最多 {max_pages} 頁）")
    states, report = run_pipeline(
        url=url, max_pages=max_pages,
        headless=_parse_bool(headless), save_html=_parse_bool(save_html),
        refresh_stale=_parse_bool(refresh_stale), only_active=_parse_bool(active_only),
        max_stale_checks=max_stale_checks, stop_on_blocked=_parse_bool(stop_on_blocked),
        state_path=state_path, config=config,
    )
    _finish_run(states, report, config)


@app.command("crawl-list")
def crawl_list_command(
    url: str = typer.Option(DEFAULT_URL, help="591 搜尋列表頁 URL"),
    max_pages: int = typer.Option(30, help="最多掃描幾頁列表頁"),
    headless: str = typer.Option("true", help="是否使用 headless 瀏覽器 (true/false)"),
    save_html: str = typer.Option("false", "--save-html", help="儲存原始 HTML (true/false)"),
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑"),
) -> None:
    """只掃列表頁並更新 seen/missing 旗標，不進 detail、不做 stale check。"""
    setup_logging()
    config = load_config()
    states, report = run_pipeline(
        url=url, max_pages=max_pages,
        headless=_parse_bool(headless), save_html=_parse_bool(save_html),
        refresh_details=False, refresh_stale=False,
        state_path=state_path, config=config,
    )
    _finish_run(states, report, config)


@app.command("refresh-details")
def refresh_details_command(
    only_active: str = typer.Option("true", "--only-active", help="只重新檢查 active 物件 (true/false)"),
    headless: str = typer.Option("true", help="是否使用 headless 瀏覽器 (true/false)"),
    save_html: str = typer.Option("false", "--save-html", help="儲存原始 HTML (true/false)"),
    stop_on_blocked: str = typer.Option("true", "--stop-on-blocked", help="遇到 blocked 立即停止 (true/false)"),
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑"),
) -> None:
    """不掃列表頁，直接重新檢查既有物件的 detail page（更新價格與存活狀態）。"""
    setup_logging()
    config = load_config()
    states, report = run_pipeline(
        url=DEFAULT_URL, headless=_parse_bool(headless), save_html=_parse_bool(save_html),
        refresh_details=True, refresh_stale=False, only_active=_parse_bool(only_active),
        stop_on_blocked=_parse_bool(stop_on_blocked),
        state_path=state_path, config=config, skip_list_crawl=True,
    )
    _finish_run(states, report, config)


@app.command("check-stale")
def check_stale_command(
    missing_days: int = typer.Option(0, "--missing-days", help="至少幾天沒在列表頁看到才檢查（0 = 全部缺席者）"),
    max_stale_checks: int = typer.Option(200, "--max-stale-checks", help="stale check 單輪上限"),
    headless: str = typer.Option("true", help="是否使用 headless 瀏覽器 (true/false)"),
    save_html: str = typer.Option("false", "--save-html", help="儲存原始 HTML (true/false)"),
    stop_on_blocked: str = typer.Option("true", "--stop-on-blocked", help="遇到 blocked 立即停止 (true/false)"),
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑"),
) -> None:
    """只對「上輪列表缺席的舊 active/unknown/error 物件」做 detail 存活確認。"""
    setup_logging()
    config = load_config()
    states, report = run_pipeline(
        url=DEFAULT_URL, headless=_parse_bool(headless), save_html=_parse_bool(save_html),
        refresh_details=False, refresh_stale=True,
        max_stale_checks=max_stale_checks, stop_on_blocked=_parse_bool(stop_on_blocked),
        state_path=state_path, config=config, skip_list_crawl=True,
        stale_missing_days=missing_days,
    )
    _finish_run(states, report, config)


@app.command("export-report")
def export_report_command(
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑"),
) -> None:
    """不爬取，直接從既有 state 重新產生全部 CSV 與 HTML report。"""
    setup_logging()
    config = load_config()
    store = StateStore(state_path or config.get("state", {}).get("path", "data/listings_state.sqlite"))
    states = store.load()
    if not states:
        console.print("[yellow]state 是空的，請先執行 python main.py run[/yellow]")
        raise typer.Exit(code=1)
    _finish_run(states, None, config)


@app.command("status-summary")
def status_summary_command(
    state_path: Optional[str] = typer.Option(None, "--state-path", help="state 檔路徑"),
) -> None:
    """顯示 state 內各 availability 狀態的統計與 QA summary（不爬取）。"""
    setup_logging()
    config = load_config()
    store = StateStore(state_path or config.get("state", {}).get("path", "data/listings_state.sqlite"))
    states = store.load()
    if not states:
        console.print("[yellow]state 是空的，請先執行 python main.py run[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title="Availability 統計")
    table.add_column("狀態")
    table.add_column("數量", justify="right")
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state.availability_status] = counts.get(state.availability_status, 0) + 1
    for status in ("active", "rented", "removed", "expired", "blocked", "error", "unknown"):
        if counts.get(status):
            table.add_row(status, str(counts[status]))
    console.print(table)
    _print_qa_summary(states, config)


# ---------------------------------------------------------------------------
# 舊版 CSV 工具（不經過 state，保留向下相容）
# ---------------------------------------------------------------------------

@app.command("score")
def score_command(
    input: str = typer.Option("output/zhubei_591_all.csv", "--input", help="輸入 CSV"),
    output: Optional[str] = typer.Option(None, "--output", help="輸出 CSV（預設 config output.scored_csv）"),
) -> None:
    """對 CSV 打分（舊版工具）。"""
    setup_logging()
    config = load_config()
    df = _read_csv(input)
    scored = score_dataframe(df, config)
    out_path = output or config.get("output", {}).get("scored_csv", "output/zhubei_591_scored.csv")
    write_csv(scored, out_path)
    console.print(f"[green]打分完成 -> {out_path}[/green]")


@app.command("export")
def export_command(
    input: str = typer.Option("output/zhubei_591_scored.csv", "--input", help="輸入 scored CSV"),
) -> None:
    """從 scored.csv 產出 filtered.csv 與 top_candidates.xlsx（舊版工具）。"""
    setup_logging()
    config = load_config()
    df = _read_csv(input)
    if "score" not in df.columns:
        df = score_dataframe(df, config)
    export_all(df, config)
    console.print("[green]匯出完成[/green]")


@app.command("report")
def report_command(
    input: str = typer.Option("output/zhubei_591_scored.csv", "--input", help="輸入 CSV（scored 或 all）"),
    output: Optional[str] = typer.Option(None, "--output", help="輸出 HTML（預設 config output.report_html）"),
    title: str = typer.Option("591 竹北租屋報告", "--title", help="報告標題"),
) -> None:
    """把 CSV 轉成 HTML 報告（舊版工具；建議改用 export-report）。"""
    setup_logging()
    config = load_config()
    df = _read_csv(input)
    if "score" not in df.columns:
        df = score_dataframe(df, config)
    out_path = output or config.get("output", {}).get("report_html", "output/zhubei_591_report.html")
    out = write_html_report(df, out_path, title=title)
    console.print(f"[green]HTML 報告完成 -> {out}[/green]")


if __name__ == "__main__":
    app()
