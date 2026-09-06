#!/bin/bash
# ============================================================
# restore_drill.sh — 备份恢复演练（验证备份真的能恢复）
#
# 生产铁律: "备份不能恢复 = 没有备份"。
# 本脚本把最新备份恢复到临时库，校验表与行数后删除，验证备份可用性。
#
# 用法:
#   bash restore_drill.sh              # 恢复最新备份并校验
#   bash restore_drill.sh <备份文件>   # 恢复指定备份
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_DIR="backups"
TMP_DB="gateway_restore_test"
PG_CONTAINER="postgres"
PG_USER="agent_user"

# 1. 确定备份文件
BACKUP_FILE="${1:-}"
if [ -z "$BACKUP_FILE" ]; then
    BACKUP_FILE="$(ls -1t "$BACKUP_DIR"/gateway_db_*.sql 2>/dev/null | head -1)"
fi
if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    echo "错误: 未找到备份文件，请检查 $BACKUP_DIR 目录" >&2
    exit 1
fi
echo "==> 备份文件: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# 2. 清理旧的临时库
echo "==> 清理旧临时库 $TMP_DB ..."
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c \
    "DROP DATABASE IF EXISTS $TMP_DB" >/dev/null 2>&1 || true

# 3. 创建临时库
echo "==> 创建临时库 $TMP_DB ..."
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c \
    "CREATE DATABASE $TMP_DB" >/dev/null

# 4. 恢复备份（把备份文件拷进容器后 psql 恢复）
echo "==> 恢复数据到临时库 ..."
docker cp "$BACKUP_FILE" "$PG_CONTAINER:/tmp/restore_test.sql"
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMP_DB" -q -f /tmp/restore_test.sql
docker exec "$PG_CONTAINER" rm -f /tmp/restore_test.sql

# 5. 校验：列出各表行数
echo ""
echo "==> 恢复校验（表行数）:"
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMP_DB" -c "
    SELECT 'users' AS tbl, COUNT(*) FROM users
    UNION ALL SELECT 'requests', COUNT(*) FROM requests
    UNION ALL SELECT 'conversation_memories', COUNT(*) FROM conversation_memories
    UNION ALL SELECT 'knowledge_chunks', COUNT(*) FROM knowledge_chunks
    UNION ALL SELECT 'payment_orders', COUNT(*) FROM payment_orders
    UNION ALL SELECT 'semantic_cache', COUNT(*) FROM semantic_cache
    UNION ALL SELECT 'user_wallets', COUNT(*) FROM user_wallets;
"

# 6. 清理临时库
echo ""
echo "==> 校验完成，删除临时库 $TMP_DB ..."
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -c \
    "DROP DATABASE IF EXISTS $TMP_DB" >/dev/null 2>&1 || true

echo "==> 恢复演练通过 ✅（备份可用）"
echo "==> 建议: 每季度执行一次本脚本，并把结果记录到 docs/disaster-recovery.md"
