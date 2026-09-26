"""中控台「问数」只读连接池（P1 只读观测）。

与业务主池完全隔离：本池连的是 `rag_readonly` 角色（授权脚本
`scripts/rag_readonly_role.sql`），只有白名单表的 SELECT 权、只读事务、
5 秒语句超时。这是"AI 不能改数据"的唯一硬保障——SQL 文本守卫（data_ask_sql）
只负责快速失败和给出人话报错，越权即使绕过解析也打不进写路径。

fail-closed：RAG_READONLY_DSN 未配置时本模块一律抛错，绝不回退 DATABASE_URL。
"""
from __future__ import annotations

import asyncio

import asyncpg

from .logging import setup_logging

logger = setup_logging()

# 只读观测是单人低频操作，池子给到 2 条足够；与角色的 CONNECTION LIMIT 2 对齐，
# 超出的请求在池内排队而不是把主库连接预算吃掉。
_POOL_MIN_SIZE = 0
_POOL_MAX_SIZE = 2
# 获取连接等待上限：拿不到连接说明有人连着不放，直接失败比挂着更可控。
_ACQUIRE_TIMEOUT = 5.0
# 服务端语句超时（毫秒）。角色默认值已设 5s，这里再显式 SET LOCAL 一次，
# 防止换库/换角色时漏配默认值。
_STATEMENT_TIMEOUT_MS = 5000
# 客户端兜底超时：比服务端宽 1 秒，正常情况下先由 PG 报错，这里只防网络半死。
_COMMAND_TIMEOUT = 6.0


class ReadonlyPoolNotConfigured(RuntimeError):
    """RAG_READONLY_DSN 未配置：只读观测不可用，不回退主账号。"""


_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _create_pool(dsn: str) -> asyncpg.Pool:
    """按只读 DSN 建池。口令不写进日志（沿用 db.py 的口径）。"""
    try:
        return await asyncpg.create_pool(
            dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            timeout=_ACQUIRE_TIMEOUT,
            command_timeout=_COMMAND_TIMEOUT,
            max_inactive_connection_lifetime=180,
        )
    except Exception as exc:
        logger.error("只读连接池初始化失败: %s", type(exc).__name__)
        raise RuntimeError("只读数据库连接失败，请检查 RAG_READONLY_DSN 配置") from exc


async def get_readonly_pool() -> asyncpg.Pool:
    """懒加载取池（中控台按需创建，避免未启用问数时白占连接）。"""
    global _pool
    if _pool is not None:
        return _pool
    # 双重检查在锁内完成：并发首次请求只会建一个池。
    async with _pool_lock:
        if _pool is None:
            from ..core.config import RAG_READONLY_DSN
            if not RAG_READONLY_DSN:
                raise ReadonlyPoolNotConfigured(
                    "RAG_READONLY_DSN 未配置，问数功能不可用")
            _pool = await _create_pool(RAG_READONLY_DSN)
            logger.info("只读连接池已初始化 (max=%d)", _POOL_MAX_SIZE)
    return _pool


async def close_readonly_pool() -> None:
    """关闭只读池（控制台进程退出时调用）。"""
    global _pool
    if _pool is not None:
        pool, _pool = _pool, None
        await pool.close()


async def run_readonly_query(sql: str, args: tuple = ()) -> list[asyncpg.Record]:
    """在只读事务里执行已通过守卫的 SQL。

    入参 sql 必须来自 data_ask_sql.guard_sql()（或本模块调用方的常量语句）；
    args 走 asyncpg 位置参数（$1/$2），不把值拼进 SQL 文本。本函数不再做语义
    校验，安全由「只读角色 + 下面的 SET LOCAL + 客户端超时」三层兜底。
    """
    pool = await get_readonly_pool()
    async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            # 角色已默认 default_transaction_read_only=on，这里再显式声明一次：
            # 换角色或换库时漏配默认值不至于退化成可写事务。
            await conn.execute("SET LOCAL TRANSACTION READ ONLY")
            # 同样是对角色 statement_timeout 的兜底；值是模块常量字面量，非外部输入。
            await conn.execute(
                f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")
            return await conn.fetch(sql, *args, timeout=_COMMAND_TIMEOUT)
