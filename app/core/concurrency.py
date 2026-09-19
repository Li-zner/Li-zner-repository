"""全局并发控制

LLM 调用限流：每个实例最多同时进行 LLM_MAX_CONCURRENCY 个流式请求。
防止高并发下所有请求同时解析 LLM 返回的大 JSON，导致 CPU 飙高、P99 延迟失控。

用法（在 LLM 调用处）：
    from ..core.concurrency import llm_semaphore
    async with llm_semaphore:
        async with httpx.AsyncClient(...) as client:
            ...
"""
import asyncio
import os

from .logging import setup_logging

logger = setup_logging()

# 每实例最大并发 LLM 调用数（默认 200）
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "200"))

# 校验：负数会导致 Semaphore 创建失败且报错信息不明确（P1）
if LLM_MAX_CONCURRENCY < 1:
    raise ValueError(f"LLM_MAX_CONCURRENCY 必须 >= 1，当前为 {LLM_MAX_CONCURRENCY}")

# Python 3.10+ 允许在无运行循环时创建 Semaphore（惰性绑定）
llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)


# ---- 后台任务持引用（2026-09-07 审查 P2）----
# asyncio.create_task 只持弱引用，事件循环只存弱引用，CPython GC 可把还在跑的
# 后台任务中途回收。模块级 set 持强引用 + done callback 自移除，异常统一记日志
# （不再出现"协程异常无人认领"的 RuntimeWarning）。全仓后台任务统一走 spawn()。
_background_tasks: set = set()


def spawn(coro, name: str = "") -> asyncio.Task:
    """创建后台任务并持强引用（防 GC 中途回收）；完成后自移除，异常记日志不外抛"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 同步失败时协程已创建未消费，显式关闭防 "never awaited" 告警
        coro.close()
        raise
    task = loop.create_task(coro, name=name or None)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(f"后台任务异常（{t.get_name()}）: {t.exception()}")

    task.add_done_callback(_done)
    _background_tasks.add(task)
    return task


async def drain_background_tasks(timeout: float = 10.0) -> int:
    """等待全部 spawn 后台任务结束（进程关闭排空，2026-09-14 审计 P1）。

    原先关闭流程只取消手工句柄——审计/缓存/计费等 spawn 任务可能未完成就被
    Redis/PG 连接池关闭打断（丢审计、漏计费）。先给宽限期优雅收尾，超时再取消。
    返回残留（被取消）任务数。
    """
    pending = {t for t in _background_tasks if not t.done()}
    if not pending:
        return 0
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for t in still_pending:
        t.cancel()
    if still_pending:
        # 取消后等一拍让清理路径跑完；仍不退出的任务随循环关闭终结
        await asyncio.wait(still_pending, timeout=2.0)
        logger.warning(f"关闭排空超时，强制取消 {len(still_pending)} 个后台任务")
    logger.info(f"后台任务排空完成：{len(done)} 正常结束，{len(still_pending)} 取消")
    return len(still_pending)
