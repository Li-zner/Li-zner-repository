"""熔断器(CLOSED/OPEN/HALF_OPEN 三态, asyncio 协程安全)—— 社区标准实现可复用版

对应本项目: app/middleware/circuit_breaker.py。
来源(GitHub):
- Python 异步熔断器: https://github.com/arlyon/aiobreaker
- HALF_OPEN 单探针门控状态机: https://github.com/resilience4j/resilience4j

与本项目版本的差异(即社区标准行为):
- HALF_OPEN 只放一个试探请求, 其余直接拒绝(本项目版本全部放行, 下游仍挂时试探风暴)。
- 试探失败立即回 OPEN, 不再重计阈值(本项目版本需再累计满 fail_threshold 次)。
- 下游超时同样计失败; 外部取消(CancelledError)不计失败(客户端断连不代表下游故障)。
"""
import asyncio
import time
from typing import Any, Awaitable, Callable

# 状态常量(用字符串而非枚举: 日志直读, 与本项目现状一致)
CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreakerError(Exception):
    """熔断开启/试探名额被占/下游超时时抛出; 上层据此与真实下游错误区分"""


class CircuitBreaker:
    """协程安全熔断器: 保护异步下游调用, 防故障扩散与重试风暴

    Args:
        name: 熔断器名(注册表键, 每个下游端点独立)。
        fail_threshold: 连续失败多少次后熔断(>= 1)。
        recover_timeout: OPEN 状态多少秒后进入 HALF_OPEN 试探。
        call_timeout: 单次下游调用超时秒数(超时按失败计)。
    """

    def __init__(self, name: str, fail_threshold: int = 3,
                 recover_timeout: float = 30.0, call_timeout: float = 30.0) -> None:
        if fail_threshold < 1:
            raise ValueError(f"fail_threshold 必须 >= 1, 当前为 {fail_threshold}")
        if recover_timeout <= 0 or call_timeout <= 0:
            raise ValueError("recover_timeout / call_timeout 必须 > 0")
        self.name = name
        self.fail_threshold = fail_threshold
        self.recover_timeout = recover_timeout
        self.call_timeout = call_timeout
        self._lock = asyncio.Lock()  # 保护状态机; call 的下游等待不持锁
        self._state = CLOSED
        self._fail_count = 0
        self._opened_at = 0.0
        self._probe_inflight = False  # HALF_OPEN 单探针门控

    @property
    def state(self) -> str:
        return self._state

    async def call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """在熔断保护下执行下游调用; 熔断开启时抛 CircuitBreakerError(不触达下游)"""
        async with self._lock:
            now = time.monotonic()
            if self._state == OPEN:
                if now - self._opened_at >= self.recover_timeout:
                    self._state = HALF_OPEN
                    self._probe_inflight = False
                else:
                    remain = self.recover_timeout - (now - self._opened_at)
                    raise CircuitBreakerError(
                        f"熔断器[{self.name}]开启中, {remain:.1f}s 后重试")
            if self._state == HALF_OPEN and self._probe_inflight:
                # 单探针门控: 已有试探在进行, 其余请求快速失败(防试探风暴)
                raise CircuitBreakerError(f"熔断器[{self.name}]试探进行中, 请稍后")
            self._probe_inflight = self._state == HALF_OPEN
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=self.call_timeout)
        except asyncio.CancelledError:
            # 外部取消不代表下游故障: 只释放试探名额, 不计失败
            await self._end_probe()
            raise
        except (asyncio.TimeoutError, TimeoutError):
            # 兼容 3.10(两者不同)与 3.11+(TimeoutError 合并); 超时按失败计
            await self._on_failure()
            raise CircuitBreakerError(
                f"熔断器[{self.name}]下游调用超时({self.call_timeout}s)") from None
        except Exception:
            await self._on_failure()
            raise  # 原样上抛真实错误, 交由调用方处理
        await self._on_success()
        return result

    async def _on_success(self) -> None:
        """成功: HALF_OPEN 试探成功则闭合; 计数清零"""
        async with self._lock:
            if self._state == HALF_OPEN:
                self._state = CLOSED
                self._probe_inflight = False
            self._fail_count = 0

    async def _on_failure(self) -> None:
        """失败: HALF_OPEN 试探失败立即回 OPEN; CLOSED 连续失败达阈值熔断"""
        async with self._lock:
            if self._state == HALF_OPEN:
                self._trip_locked()
                return
            self._fail_count += 1
            if self._fail_count >= self.fail_threshold:
                self._trip_locked()

    async def _end_probe(self) -> None:
        """取消场景: 释放试探名额但不改状态, 允许下一个试探"""
        async with self._lock:
            if self._state == HALF_OPEN:
                self._probe_inflight = False

    def _trip_locked(self) -> None:
        """进入 OPEN(调用方需已持锁)"""
        self._state = OPEN
        self._opened_at = time.monotonic()
        self._fail_count = 0
        self._probe_inflight = False


# 按下游端点隔离的注册表(单进程内全局; 多进程各实例独立, 与本项目一致)
_breakers: dict = {}


def get_breaker(name: str, fail_threshold: int = 3,
                recover_timeout: float = 30.0, call_timeout: float = 30.0) -> CircuitBreaker:
    """取(或创建)指定名称的熔断器; 同名复用同一实例"""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name, fail_threshold=fail_threshold,
            recover_timeout=recover_timeout, call_timeout=call_timeout)
    return _breakers[name]


def _self_check() -> None:
    """最小自检: 熔断触发、OPEN 快速失败、单探针门控、试探成功闭合"""
    import asyncio

    async def run() -> None:
        calls = []

        async def fail() -> None:
            calls.append(1)
            raise ValueError("boom")

        br = CircuitBreaker("t", fail_threshold=2, recover_timeout=0.05, call_timeout=1)
        for _ in range(2):
            try:
                await br.call(fail)
            except ValueError:
                pass
        assert br.state == OPEN, "两次失败后应熔断"
        try:
            await br.call(fail)
        except CircuitBreakerError:
            pass
        else:
            raise AssertionError("熔断期应快速失败")
        assert len(calls) == 2, "熔断期不应触达下游"
        await asyncio.sleep(0.06)  # 越过恢复窗口
        # HALF_OPEN: 并发两请求只放一个试探, 另一个被门控拒绝
        results = await asyncio.gather(
            br.call(fail), br.call(fail), return_exceptions=True)
        kinds = {type(r).__name__ for r in results}
        assert len(calls) == 3, f"试探只应放行一个, 实际触达 {len(calls)} 次"
        assert kinds == {"ValueError", "CircuitBreakerError"}, kinds
        assert br.state == OPEN, "试探失败应立即回 OPEN"
        await asyncio.sleep(0.06)

        async def ok() -> int:
            return 42

        assert await br.call(ok) == 42
        assert br.state == CLOSED, "试探成功应闭合"

    asyncio.run(run())
    print("熔断器 自检通过")


if __name__ == "__main__":
    _self_check()
