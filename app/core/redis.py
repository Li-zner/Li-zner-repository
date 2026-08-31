import os
import redis.asyncio as aioredis
from urllib.parse import urlparse
from .logging import setup_logging

logger = setup_logging()
redis_client = None

# Redis 连接池上限（默认 20，可从环境变量覆盖；P0 去掉硬编码）
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "20"))


def _build_redis_url():
    """构建并清理 Redis URL（用 urlparse 标准化，避免空认证段误判，P1）"""
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    parsed = urlparse(url)
    # 空认证段（redis://@host 或 redis://:@host）→ 按无认证重建，避免误判
    if parsed.username == "" and parsed.password in ("", None):
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"{parsed.scheme}://{netloc}{parsed.path or ''}"
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
        max_connections=REDIS_MAX_CONNECTIONS,
        socket_connect_timeout=3,
        socket_timeout=3,
        retry_on_timeout=True,
        retry_on_error=[ConnectionError, TimeoutError],   # 网络抖动自动重试（P2）
        health_check_interval=30,
        # 注：pool_timeout（池获取连接超时，P0 #19）需 redis-py>=7，requirements 锁定 5.2，
        # 故移除；高并发下获取连接阻塞由 socket_timeout=3 兜底（部分缓解）。
    )
    try:
        await redis_client.ping()
        logger.info(f"Redis 连接池已初始化: {redis_url.split('@')[-1] if '@' in redis_url else redis_url}")
    except Exception as e:
        # 启动探活失败 → 拒绝启动（fail loudly），避免 get_redis() 返回未初始化对象（P0）
        logger.error(f"Redis 连接失败: {e}")
        await redis_client.close()
        redis_client = None
        raise RuntimeError("Redis 连接失败，请检查 Redis 配置") from e
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