"""RAG 运行期参数覆盖：Redis 持久化，供策略动作热更新使用。"""
from __future__ import annotations

import asyncio
import time

from ..core.logging import setup_logging
from ..core.redis import get_redis

logger = setup_logging()

RERANK_TOP_KEY = "rag:runtime:rerank_top"
RERANK_TOP_MIN = 5
RERANK_TOP_MAX = 20
_CACHE_TTL_SECONDS = 5.0

_rerank_cache: tuple[int, float] | None = None
_runtime_lock = asyncio.Lock()


def _validate_rerank_top(value: int) -> int:
    """校验重排候选池边界，越界直接拒绝，避免运行期误配置。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("RERANK_TOP 必须是整数")
    if not RERANK_TOP_MIN <= value <= RERANK_TOP_MAX:
        raise ValueError(
            f"RERANK_TOP 必须在 {RERANK_TOP_MIN}~{RERANK_TOP_MAX} 之间")
    return value


async def get_rerank_top(default: int) -> int:
    """读取运行期 RERANK_TOP；Redis 不可用时回退静态默认值。"""
    global _rerank_cache
    now = time.monotonic()
    if _rerank_cache and _rerank_cache[1] > now:
        return _rerank_cache[0]
    async with _runtime_lock:
        now = time.monotonic()
        if _rerank_cache and _rerank_cache[1] > now:
            return _rerank_cache[0]
        try:
            raw = await (await get_redis()).get(RERANK_TOP_KEY)
            value = _validate_rerank_top(int(raw)) if raw is not None else default
        except Exception as exc:
            logger.debug(f"读取运行期 RERANK_TOP 失败，使用默认值: {exc}")
            value = default
        _rerank_cache = (value, now + _CACHE_TTL_SECONDS)
        return value


async def set_rerank_top(value: int) -> int:
    """写入运行期 RERANK_TOP；写入失败必须抛出，禁止动作假成功。"""
    normalized = _validate_rerank_top(value)
    global _rerank_cache
    async with _runtime_lock:
        await (await get_redis()).set(RERANK_TOP_KEY, str(normalized))
        _rerank_cache = (
            normalized, time.monotonic() + _CACHE_TTL_SECONDS)
    return normalized


async def clear_rerank_top() -> None:
    """清除运行期覆盖，恢复静态默认值。"""
    global _rerank_cache
    async with _runtime_lock:
        await (await get_redis()).delete(RERANK_TOP_KEY)
        _rerank_cache = None
