"""retention_cleanup.py — 数据保留策略清理脚本

解决"数据无限增长，数据库迟早爆"的问题。按保留期清理历史数据。

保留期默认值（可通过参数覆盖）：
  - conversation_memories : 90 天   对话记忆（隐私 + 体积大头）
  - requests              : 30 天   请求跟踪（仅运维价值）
  - transaction_logs      : 365 天  交易流水（WAL 审计，保留最长）
  - semantic_cache        : 30 天   仅清理 hit_count=0 的未命中缓存
  - user_usage            : 365 天   每日用量统计

用法（在项目根目录执行）：
    python scripts/retention_cleanup.py                 # 实际清理（默认保留期）
    python scripts/retention_cleanup.py --dry-run       # 只统计不删除
    python scripts/retention_cleanup.py --table requests --days 7
    python scripts/retention_cleanup.py --all-days 90   # 全部表统一 90 天

注意：清理不可逆，生产环境首次执行务必先 --dry-run 确认。
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get, db_url  # noqa: E402

# 各表默认保留期（天）
DEFAULT_RETENTION = {
    "conversation_memories": 90,
    "requests": 30,
    "transaction_logs": 365,
    "semantic_cache": 30,
    "user_usage": 365,
}

# 时间列名
TABLE_TIME_COL = {
    "conversation_memories": "created_at",
    "requests": "start_time",
    "transaction_logs": "created_at",
    "semantic_cache": "created_at",
    "user_usage": "date",  # text 类型日期 YYYY-MM-DD
}


async def _count(conn, table: str, cutoff: str) -> int:
    col = TABLE_TIME_COL[table]
    if table == "user_usage":
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE {col} < $1", cutoff[:10]
        )
    return await conn.fetchval(
        f"SELECT COUNT(*) FROM {table} WHERE {col} < $1", cutoff
    )


async def _delete(conn, table: str, cutoff: str) -> int:
    col = TABLE_TIME_COL[table]
    if table == "semantic_cache":
        # 语义缓存只清"从未命中"的，避免误删热数据
        return await conn.fetchval(
            f"DELETE FROM {table} WHERE {col} < $1 AND hit_count = 0", cutoff
        )
    if table == "user_usage":
        return await conn.fetchval(
            f"DELETE FROM {table} WHERE {col} < $1", cutoff[:10]
        )
    return await conn.fetchval(f"DELETE FROM {table} WHERE {col} < $1", cutoff)


async def main():
    parser = argparse.ArgumentParser(description="数据保留策略清理")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    parser.add_argument("--table", choices=list(DEFAULT_RETENTION), help="只清理指定表")
    parser.add_argument("--days", type=int, help="覆盖保留期（天）")
    parser.add_argument("--all-days", type=int, help="所有表统一保留期（天）")
    parser.add_argument("--host", default=None, help="数据库主机（默认自动探测）")
    args = parser.parse_args()

    # 解析要处理的表
    tables = [args.table] if args.table else list(DEFAULT_RETENTION)

    # 解析保留期
    retention = dict(DEFAULT_RETENTION)
    if args.all_days:
        retention = {t: args.all_days for t in DEFAULT_RETENTION}
    if args.days and args.table:
        retention[args.table] = args.days

    # 连接数据库
    import asyncpg
    host = args.host or None
    # 复用 migrate.py 的主机探测逻辑（scripts/ 已在 sys.path）
    if host is None:
        from migrate import resolve_host
        host = resolve_host()
    url = db_url(host)

    print(f"[retention] 目标库: {url.split('@')[-1]}")
    print(f"[retention] 模式: {'DRY-RUN（只统计）' if args.dry_run else '实际清理'}")
    print("-" * 60)

    conn = await asyncpg.connect(url)
    try:
        total_deleted = 0
        for table in tables:
            days = retention[table]
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cnt = await _count(conn, table, cutoff)
            print(f"  {table:<24} 保留{days:>3}天 截止{cutoff}  待清理: {cnt}")
            if not args.dry_run and cnt:
                deleted = await _delete(conn, table, cutoff)
                total_deleted += deleted
                print(f"    -> 已删除 {deleted}")
        if not args.dry_run:
            print("-" * 60)
            print(f"[retention] 共删除 {total_deleted} 行")
            # 触发 VACUUM 回收空间（生产大表建议）
            if total_deleted > 0:
                print("[retention] 提示: 大量删除后可手动执行 VACUUM ANALYZE 回收磁盘")
        else:
            print("-" * 60)
            print("[retention] DRY-RUN 完成，未删除任何数据")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
