"""
熔断器（三态状态机：CLOSED → OPEN → HALF_OPEN）

仅适用于单进程/单 Worker 模式（P2 #11/#21）：
    _breakers 为进程内存级注册表，多 Worker 下各进程状态不共享。
    若未来扩展多 Worker，需将熔断状态迁移到 Redis。

状态机：
    CLOSED（正常）-- 连续 fail_threshold 次失败 --> OPEN（秒拒）
    OPEN -- recover_timeout 秒后 --> HALF_OPEN（试探）
    HALF_OPEN -- 成功 --> CLOSED；-- 失败 --> OPEN
"""
import time
import asyncio

from ..core.logging import setup_logging

logger = setup_logging()


class CircuitBreakerError(Exception):
    """熔断器开启/下游超时抛出的业务异常（P2 #7：供上层精确捕获，避免误吞真实错误）"""


class SimpleBreaker:
    """协程安全熔断器（asyncio.Lock 保护状态，P0 #1）"""

    def __init__(self, fail_threshold: int = 3, recover_timeout: float = 30,
                 call_timeout: float = 30.0):
        # 校验：fail_threshold <= 0 会导致"一次失败即熔断"，直接拒绝启动（P3 #24）
        if fail_threshold < 1:
            raise ValueError(f"fail_threshold 必须 >= 1，当前为 {fail_threshold}")
        if recover_timeout <= 0:
            raise ValueError(f"recover_timeout 必须 > 0，当前为 {recover_timeout}")
        self.fail_threshold = fail_threshold
        self.recover_timeout = recover_timeout
        self.call_timeout = call_timeout               # 下游调用超时（P2 #38）
        self._lock = asyncio.Lock()                    # 保护 fail_count/state/last_fail_time
        self.fail_count = 0
        self.state = "CLOSED"                          # CLOSED / OPEN / HALF_OPEN
        self.last_fail_time = 0.0

    async def call(self, async_func, *args, **kwargs):
        """执行被保护的下游调用；熔断开启时抛 CircuitBreakerError"""
        async with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_fail_time > self.recover_timeout:
                    self._set_state("HALF_OPEN")
                else:
                    raise CircuitBreakerError("熔断器已开启，服务暂时不可用")
        try:
            # 下游慢请求加超时保护，防挂起协程占满事件循环/连接池（P2 #38）
            result = await asyncio.wait_for(
                async_func(*args, **kwargs), timeout=self.call_timeout
            )
        except asyncio.TimeoutError as e:
            await self._record_failure()
            raise CircuitBreakerError("下游调用超时") from e
        except Exception as e:
            await self._record_failure()
            raise e
        async with self._lock:
            if self.state == "HALF_OPEN":
                self._set_state("CLOSED")
                self.fail_count = 0
        return result

    async def _record_failure(self):
        """记录一次失败；达到阈值则开启熔断（锁内更新，P0 #1 防并发计数错乱）"""
        async with self._lock:
            if self.state == "HALF_OPEN":
                # HALF_OPEN 试探期一次失败即应重回 OPEN（标准熔断语义，文档已声明）；
                # 避免下游仍宕机时让 fail_threshold 个试探请求白跑。
                self._set_state("OPEN")
                self.fail_count = 0
                self.last_fail_time = time.time()
                return
            self.fail_count += 1
            self.last_fail_time = time.time()
            if self.fail_count >= self.fail_threshold:
                self._set_state("OPEN")
                self.fail_count = 0

    def _set_state(self, new_state: str):
        """状态变更：写日志，便于可观测（P3 #40）"""
        if self.state != new_state:
            logger.warning(f"熔断器状态变更: {self.state} -> {new_state}")
            self.state = new_state


# 熔断器注册表（按端点名隔离，避免单点故障影响全局）
_breakers: dict = {}


def get_breaker(name: str = "default", fail_threshold: int = 3,
                recover_timeout: float = 30, call_timeout: float = 30.0) -> SimpleBreaker:
    """获取指定名称的熔断器实例（每个端点独立；阈值/超时可按接口动态配置，P2 #8）"""
    if name not in _breakers:
        _breakers[name] = SimpleBreaker(
            fail_threshold=fail_threshold,
            recover_timeout=recover_timeout,
            call_timeout=call_timeout,
        )
    return _breakers[name]


# 兼容旧代码：默认熔断器
deepseek_breaker = get_breaker("default")
