"""控制面就绪检查：验证数据库和 Redis 依赖是否可用。"""

from __future__ import annotations

import asyncio


async def _check_database() -> str:
    """只做轻量查询，不暴露连接串或内部错误。"""
    try:
        from .db import get_pool

        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            value = await conn.fetchval("SELECT 1")
        return "ok" if value == 1 else "error"
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(f"readiness database failed: {type(exc).__name__}")
        return "error"


async def _check_redis() -> str:
    """通过 PING 验证 Redis 可用。"""
    try:
        from .redis import get_redis

        redis = await get_redis()
        return "ok" if await redis.ping() else "error"
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(f"readiness redis failed: {type(exc).__name__}")
        return "error"


async def check_dependencies() -> dict:
    """返回依赖就绪快照，true 表示可以接收流量。"""
    database, redis = await asyncio.gather(_check_database(), _check_redis())
    return {
        "ok": database == "ok" and redis == "ok",
        "database": database,
        "redis": redis,
    }
