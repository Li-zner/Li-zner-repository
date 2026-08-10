import os
import redis.asyncio as aioredis
from .logging import setup_logging

logger = setup_logging()
redis_client = None


def _build_redis_url():
    """构建并清理 Redis URL"""
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    if "@" in url:
        parts = url.split("@")
        auth_part = parts[0].replace("redis://", "")
        if auth_part in ("", ":"):
            url = "redis://" + parts[1]
    return url


async def init_redis():
    """初始化 Redis 连接池（在 lifespan 中调用）"""
    global redis_client
    if redis_client is not None:
        return redis_client
    redis_url = _build_redis_url()
    redis_client = aioredis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_connect_timeout=3,
        socket_timeout=3,
        retry_on_timeout=True,
        health_check_interval=30
    )
    try:
        await redis_client.ping()
        logger.info(f"Redis 连接池已初始化: {redis_url.split('@')[-1] if '@' in redis_url else redis_url}")
    except Exception as e:
        logger.warning(f"Redis 连接失败: {e}")
    return redis_client


async def get_redis():
    """获取 Redis 客户端（首次调用时自动初始化）"""
    global redis_client
    if redis_client is None:
        await init_redis()
    return redis_client


async def close_redis():
    """关闭 Redis 连接池（在 lifespan 中调用）"""
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
        logger.info("Redis 连接池已关闭")