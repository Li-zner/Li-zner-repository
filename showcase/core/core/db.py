import os
import asyncpg
from ..core.config import DATABASE_URL
from ..core.logging import setup_logging

logger = setup_logging()

_pool = None


_POOL_CONFIG = {
    "min_size": int(os.getenv("DB_POOL_MIN_SIZE", "20")),
    "max_size": int(os.getenv("DB_POOL_MAX_SIZE", "50")),
}


async def init_pool():
    """初始化连接池（在 lifespan 中调用，全局唯一创建点）"""
    global _pool
    if _pool is not None:
        return _pool
    # 安全：连接错误时不暴露完整 DATABASE_URL（可能含密码）
    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=_POOL_CONFIG["min_size"],
            max_size=_POOL_CONFIG["max_size"],
        )
    except Exception as e:
        logger.error(f"数据库连接池初始化失败: {e}")
        raise RuntimeError("数据库连接失败，请检查数据库配置") from e
    logger.info(f"数据库连接池已初始化 (min={_POOL_CONFIG['min_size']}, max={_POOL_CONFIG['max_size']})")
    return _pool


async def get_pool():
    """获取已初始化的连接池（必须先在 lifespan 中调用 init_pool）"""
    global _pool
    if _pool is None:
        raise RuntimeError("数据库连接池未初始化，请在 lifespan 中调用 init_pool()")
    return _pool


async def close_pool():
    """关闭连接池（在 lifespan 中调用）"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_db_conn():
    """FastAPI 依赖项：从连接池获取一个连接"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
