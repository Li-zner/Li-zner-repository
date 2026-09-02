#!/bin/bash
# ============================================================
# deploy_github.sh — 一键提交并推送 GitHub
#
# 用法（Git Bash）:
#   bash scripts/deploy_github.sh                        # 自动提交并推送
#   bash scripts/deploy_github.sh "修复XX bug"            # 带提交说明
#
# 行为: git add -A(遵循 .gitignore) -> commit -> push origin main
# 注意: 必须用 LF 换行
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

MSG="${1:-自动更新 $(date '+%Y-%m-%d %H:%M:%S')}"

# 检查是否有变更（含未跟踪文件）
if [ -z "$(git status --porcelain)" ]; then
    echo "ℹ️  没有变更，无需提交"
    exit 0
fi

# 展示将要提交的文件
echo "==> 待提交变更:"
git status --short

echo ""
echo "==> 提交: $MSG"
git add -A
git commit -m "$MSG"

echo ""
echo "==> 推送 origin/main ..."
git push origin main

echo ""
echo "✅ 完成: $(git log -1 --oneline)"
