# ============================================================
# 可复用资产：重试+退避+降级.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""重试 + 指数退避 + 完全抖动 + 降级链(asyncio)—— 社区标准实现可复用版

适用场景: 电商支付/短信回调、APP 推送、网关转发、LLM API 调用——一切「外部依赖会临时失败」的调用。
设计要点(为什么这样做):
- 指数退避 + 完全抖动(full jitter): 每次重试前随机等待 [0, min(max_delay, base*2^n)],
  把失败方的重试在时间轴上摊开, 防止下游恢复瞬间被重试风暴再次打垮(AWS 官方建议)。
- 默认只重试网络/超时类异常: 业务性失败(参数错误、余额不足)重试多少次结果都一样,
  除非调用方显式把它们列入 retryable。
- 降级链(fallbacks): 重试耗尽后按序尝试备选异步函数(读缓存值/返回默认值/走简化逻辑),
  全部失败才抛最后一次的真实异常——调用方拿到的要么是可用结果, 要么是真错误。
来源(GitHub/官方):
- tenacity(Python 重试库事实标准): https://github.com/jd/tenacity
- AWS 退避与抖动: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- Retry + Fallback 组合: https://github.com/resilience4j/resilience4j
"""
import asyncio
import random
from typing import Any, Awaitable, Callable

# 默认可重试异常: 连接失败与超时; 兼容 3.10(asyncio.TimeoutError)与 3.11+(并入 TimeoutError)
DEFAULT_RETRYABLE: tuple = (ConnectionError, TimeoutError, asyncio.TimeoutError)


async def retry_call(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    retryable: tuple = DEFAULT_RETRYABLE,
    fallbacks: list | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
    **kwargs: Any,
) -> Any:
    """带重试/退避/降级地执行 fn(*args, **kwargs)

    Args:
        fn: 异步被调函数; 本函数的选项全部为具名参数, 不会误传给 fn。
        max_attempts: 总尝试次数(含首次), >= 1。
        base_delay / max_delay: 退避基数与上限(秒)。
        retryable: 触发重试的异常元组; 不在其中的异常立即上抛(业务失败重试无意义)。
        fallbacks: 降级链, 每项是无参异步函数; 重试耗尽后按序尝试。
        on_retry: 每次重试前回调(第几次尝试, 异常), 用于打点告警。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts 必须 >= 1")
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except retryable as exc:  # 只有可重试异常走退避, 其余立即上抛
            last_exc = exc
            if attempt == max_attempts:
                break
            if on_retry is not None:
                on_retry(attempt, exc)
            # 完全抖动: 等待上限随次数指数增长, 实际等待在 [0, 上限] 均匀随机
            ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await asyncio.sleep(random.uniform(0, ceiling))
    # 重试耗尽 → 走降级链; 降级全部失败 → 抛最后一次真实异常(不吞错误)
    for fallback in fallbacks or []:
        try:
            return await fallback()
        except Exception:
            continue  # 降级失败不掩盖原异常, 继续尝试下一个降级
    assert last_exc is not None, "重试耗尽必然携带最后一次异常"
    raise last_exc


def _self_check() -> None:
    """最小自检: 重试后成功 / 不可重试不重试 / 降级链兜底 / 无降级抛真异常"""
    import asyncio

    async def run() -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("临时故障")
            return "ok"

        got = await retry_call(flaky, max_attempts=5, base_delay=0.001)
        assert got == "ok" and calls["n"] == 3, "应恰好第 3 次成功"

        calls2 = {"n": 0}

        async def biz_fail() -> None:
            calls2["n"] += 1
            raise ValueError("参数错误, 重试无意义")

        try:
            await retry_call(biz_fail, retryable=(ConnectionError,))
        except ValueError:
            pass
        else:
            raise AssertionError("不可重试异常应立即上抛")
        assert calls2["n"] == 1, "不可重试异常不应重试"

        fallback_used: list = []

        async def fb_cache() -> str:
            fallback_used.append("cache")
            return "cached"

        async def always_fail() -> str:
            raise ConnectionError("still down")

        got = await retry_call(always_fail, max_attempts=2, base_delay=0.001,
                               fallbacks=[always_fail, fb_cache])
        assert got == "cached" and fallback_used == ["cache"], "降级链应跳过失败的降级取到缓存值"

        try:
            await retry_call(always_fail, max_attempts=2, base_delay=0.001)
        except ConnectionError:
            pass
        else:
            raise AssertionError("无降级时应抛最后一次真实异常")

    asyncio.run(run())
    print("重试+退避+降级 自检通过")


if __name__ == "__main__":
    _self_check()
