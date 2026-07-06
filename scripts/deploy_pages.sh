#!/usr/bin/env bash
# 把 docs/（GitHub Pages 發布目錄）commit 並 push 到 GitHub。
# parser 跑完後呼叫這個腳本，Pages 網址上的報告就會自動更新。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .git ]]; then
  echo "還不是 git repo，跳過部署（請先依 README 完成 GitHub Pages 設定）" >&2
  exit 0
fi
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "尚未設定 origin remote，跳過部署（git remote add origin <你的 repo URL>）" >&2
  exit 0
fi
if [[ ! -f docs/index.html ]]; then
  echo "docs/index.html 不存在，請先執行 python main.py export-report" >&2
  exit 1
fi

git add docs
if git diff --cached --quiet -- docs; then
  echo "docs/ 沒有變更，不需部署"
  exit 0
fi

git commit -m "chore: update rent report $(date '+%Y-%m-%d %H:%M')"
git push origin HEAD
echo "已推送，GitHub Pages 會在 1-2 分鐘內更新"
