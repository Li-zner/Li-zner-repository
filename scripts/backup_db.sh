#!/bin/bash
# ============================================================
# PostgreSQL 自动备份脚本
# 保存到项目 backups/ 目录（与 restore_drill.sh 约定一致），保留最近 7 天
# 配合 crontab 每周日凌晨 3:00 执行
# 注意: 必须 LF 换行
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/backups"
DB_USER="agent_user"
DB_NAME="agent_gateway"
CONTAINER="postgres"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="${BACKUP_DIR}/gateway_db_${TIMESTAMP}.sql"

# 创建备份目录
mkdir -p "$BACKUP_DIR"

echo "🔄 开始备份 ${DB_NAME}..."

# pg_dump 导出（通过 docker exec，走容器本地 socket）
docker exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" > "$FILENAME"

echo "✅ 备份完成: ${FILENAME} ($(du -h "$FILENAME" | cut -f1))"

# 删除 7 天前的旧备份
find "$BACKUP_DIR" -name "gateway_db_*.sql" -mtime +7 -delete
echo "🧹 已清理 7 天前的旧备份"

echo "================================"
echo "  当前备份列表:"
ls -lh "$BACKUP_DIR"/gateway_db_*.sql 2>/dev/null || echo "  （无备份）"
echo "================================"
