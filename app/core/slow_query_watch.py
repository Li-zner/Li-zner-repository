"""慢查询后台观测（Prometheus 指标源）

- 周期查询 pg_stat_statements（mean_exec_time > 阈值），更新 Gauge 指标
- Redis 锁保证多实例只有一个在查（锁时长 = 查询间隔）
- pg_stat_statements 不可用时静默跳过（指标保持旧值，不阻断）
"""
import asyncio

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.metrics import app_slow_queries_total, app_slow_query_max_ms
from ..core.logging import setup_logging

logger = setup_logging()

_WATCH_LOCK = "slowq:watch:lock"
_DEFAULT_THRESHOLD_MS = 1000
_DEFAULT_INTERVAL = 300


async def check_once(threshold_ms: int = _DEFAULT_THRESHOLD_MS):
    """单次刷新：查询 pg_stat_statements 并更新 Gauge"""
    r = await get_redis()
    # 多实例只允许一个执行查询（锁时长 = 查询间隔，天然续期）
    if not await r.set(_WATCH_LOCK, "1", nx=True, ex=_DEFAULT_INTERVAL):
        return
    pool = await get_pool()
    try:
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) FILTER (WHERE mean_exec_time > $1) AS cnt,
                       COALESCE(max(mean_exec_time) FILTER (WHERE mean_exec_time > $1), 0) AS mx
                FROM pg_stat_statements
                """,
                threshold_ms,
            )
    except Exception as e:
        logger.warning(f"慢查询观测跳过（pg_stat_statements 不可用?）: {e}")
        return
    if row:
        app_slow_queries_total.labels(threshold_ms=str(threshold_ms)).set(row["cnt"])
        app_slow_query_max_ms.set(row["mx"] or 0)


async def slow_query_watch_loop(
    interval: int = _DEFAULT_INTERVAL,
    threshold_ms: int = _DEFAULT_THRESHOLD_MS,
):
    """常驻循环（由 lifespan 启动）"""
    while True:
        try:
            await check_once(threshold_ms)
        except Exception as e:  # noqa: BLE001 - 观测任务不因单次异常退出
            logger.warning(f"慢查询观测异常: {e}")
        await asyncio.sleep(interval)
