#!/usr/bin/env bash
# 把 output/ 的 HTML 報告發布成手機可開的公開網址（Cloudflare quick tunnel）。
#
# 用法：
#   ./publish.sh          # 先重新產生報告，再開 tunnel
#   ./publish.sh --no-regen
#
# 說明：
# - 使用 cloudflared quick tunnel：免帳號、免設定，網址形如
#   https://xxxx.trycloudflare.com（每次啟動都會換，腳本結束即失效）。
# - 報告是靜態單檔，重跑爬蟲後執行 python main.py export-report
#   重新產生，手機重新整理即可看到新資料。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8591}"

if [[ "${1:-}" != "--no-regen" ]]; then
  if [[ -f .venv/bin/activate ]]; then
    source .venv/bin/activate
    python main.py export-report || echo "（export-report 失敗，改用既有報告）"
  fi
fi

if [[ ! -f output/zhubei_591_report.html ]]; then
  echo "找不到 output/zhubei_591_report.html，請先執行 python main.py run" >&2
  exit 1
fi

# 讓根路徑 / 直接開報告（symlink：之後重產報告，手機重新整理就是新資料）
ln -sf zhubei_591_report.html output/index.html

python3 -m http.server "$PORT" --bind 127.0.0.1 --directory output >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

echo "本機預覽： http://127.0.0.1:$PORT/"
echo "啟動 Cloudflare tunnel 中，看到 https://xxxx.trycloudflare.com 就可以用手機開啟…"
exec bin/cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate
