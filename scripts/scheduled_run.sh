#!/usr/bin/env bash
# 排程執行：爬取 + 更新狀態 + 重產報告（給 systemd timer / cron 呼叫）。
# log 寫到 logs/scheduled.log，用 flock 防止重疊執行。
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs
LOG="logs/scheduled.log"
LOCK="logs/scheduled.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 上一輪還在執行，跳過本次" >>"$LOG"
  exit 0
fi

echo "[$(date '+%F %T')] ===== 排程開始 =====" >>"$LOG"
source .venv/bin/activate
python main.py run --max-pages 30 --headless true --refresh-stale true \
  --max-stale-checks 200 >>"$LOG" 2>&1
STATUS=$?

# run 成功時已更新 output/ 與 docs/index.html；接著推上 GitHub Pages
if [[ $STATUS -eq 0 ]]; then
  ./scripts/deploy_pages.sh >>"$LOG" 2>&1 || echo "[$(date '+%F %T')] Pages 部署失敗（見上方訊息）" >>"$LOG"
fi

echo "[$(date '+%F %T')] ===== 排程結束（exit=$STATUS）=====" >>"$LOG"
exit $STATUS
