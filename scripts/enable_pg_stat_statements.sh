#!/bin/bash
# ============================================================
# 启用 pg_stat_statements（慢查询监控前置）
# 步骤:
#   1) CREATE EXTENSION（运行时即可，幂等）
#   2) 检查 shared_preload_libraries 是否已加载
#   3) 若未加载: 修改 docker-compose.yml 的 postgres 增加 command，
#      然后重建容器（数据在 volume 不丢）
# ============================================================
set -e

echo "==> 1. CREATE EXTENSION（幂等）"
docker exec postgres psql -U agent_user -d agent_gateway \
    -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"

echo "==> 2. 检查 shared_preload_libraries"
PRELOAD=$(docker exec postgres psql -U agent_user -d agent_gateway -t -A \
    -c "SHOW shared_preload_libraries;")

if echo "$PRELOAD" | grep -q "pg_stat_statements"; then
    echo "✅ 已加载: shared_preload_libraries = ${PRELOAD}"
else
    echo "❌ 未加载（当前: '${PRELOAD}'）——统计为空，需重建容器。"
    echo "   在 docker-compose.yml 的 postgres 服务加:"
    echo "     command: postgres -c shared_preload_libraries=pg_stat_statements"
    echo "   然后: docker compose up -d postgres   # 数据在 volume 不丢"
    exit 1
fi

echo "==> 3. 验证可查"
docker exec postgres psql -U agent_user -d agent_gateway -c \
    "SELECT count(*) AS 已采集语句 FROM pg_stat_statements;"
echo "✅ 完成。可运行: python scripts/slow_query_monitor.py --check"
