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
import uuid
from contextlib import asynccontextmanager

from ..core.logging import setup_logging

logger = setup_logging()


class CircuitBreakerError(Exception):
    """熔断器基类异常（P2 #7：供上层精确捕获，避免误吞真实错误）"""


class CircuitOpenError(CircuitBreakerError):
    """熔断器处于 OPEN/HALF_OPEN 试探期，下游调用被秒拒（服务不可用）。"""


class CircuitTimeoutError(CircuitBreakerError):
    """下游调用超时（call_timeout 到期），区别于熔断拒绝对外暴露。"""


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
        self._probing = False                          # HALF_OPEN 试探进行中（单探针语义）
        self._probe_token: str | None = None           # 探针身份（2026-09-12 清欠：防旁观取消误翻状态）

    async def call(self, async_func, *args, **kwargs):
        """执行被保护的下游调用；熔断开启时抛 CircuitBreakerError"""
        async with self.guard():
            return await asyncio.wait_for(
                async_func(*args, **kwargs), timeout=self.call_timeout
            )

    @asynccontextmanager
    async def guard(self):
        """供异步生成器/流式请求复用的熔断上下文。"""
        probe_token = await self._before()
        try:
            yield
        except asyncio.CancelledError:
            await self._cancel_probe(probe_token)
            raise
        except GeneratorExit:
            # 流式生成器被消费方提前丢弃（stream_utils.py:81 的 LLM 流：客户端
            # 断连或命中拦截后停止迭代，事件循环收尾关闭）：GeneratorExit 属
            # BaseException，下面三个分支都接不住，探针会永挂 → HALF_OPEN 卡死
            # 到进程重启。此路径不得 await（关闭中挂起会打断 athrow 流程），
            # 故走无 await 的同步释放。
            self._release_probe(probe_token)
            raise
        except asyncio.TimeoutError as e:
            await self._record_failure(probe_token)
            raise CircuitTimeoutError("下游调用超时") from e
        except Exception:
            await self._record_failure(probe_token)
            raise
        else:
            await self._record_success(probe_token)

    async def _before(self) -> "str | None":
        """调用前检查状态；按需进入 HALF_OPEN 并抢占唯一探针。

        返回探针 token（本次调用成为探针时非 None）。2026-09-12 清欠 P2：
        探针带唯一身份——此前任何在途调用被取消都会触发 _cancel_probe 把
        HALF_OPEN 翻回 OPEN，原探针仍在飞而下个请求又成新探针（双探针触达下游）。
        """
        async with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_fail_time > self.recover_timeout:
                    self._set_state("HALF_OPEN")
                    self._probing = True
                    self._probe_token = uuid.uuid4().hex
                    return self._probe_token
                raise CircuitOpenError("熔断器已开启，服务暂时不可用")
            if self.state == "HALF_OPEN" and self._probing:
                raise CircuitOpenError("熔断器试探进行中，服务暂时不可用")
            return None

    async def _cancel_probe(self, token: "str | None"):
        """探针取消不等于下游恢复，回到 OPEN 等待下个恢复窗口；仅探针持有者可触发。"""
        async with self._lock:
            self._release_probe(token)

    def _release_probe(self, token: "str | None"):
        """_cancel_probe 的状态迁移本体（无 await，供 GeneratorExit 路径直接调用）。"""
        if token and token == self._probe_token and self.state == "HALF_OPEN":
            self._set_state("OPEN")
            self.last_fail_time = time.time()
            self._probing = False

    async def _record_success(self, token: "str | None"):
        """成功请求重置连续失败计数；HALF_OPEN→CLOSED 仅探针本人成功时发生。"""
        async with self._lock:
            if self.state == "HALF_OPEN" and token and token == self._probe_token:
                self._set_state("CLOSED")
                self._probing = False
            if self.state != "HALF_OPEN":
                self.fail_count = 0
                self._probing = False

    async def _record_failure(self, token: "str | None" = None):
        """记录一次失败；达到阈值则开启熔断（锁内更新，P0 #1 防并发计数错乱）"""
        async with self._lock:
            if self.state == "HALF_OPEN":
                # HALF_OPEN 试探期一次失败即应重回 OPEN（标准熔断语义，文档已声明）。
                # 2026-09-12 清欠：仅探针本人的失败才翻转状态，旁观者的失败不抢探针。
                if token and token == self._probe_token:
                    self._set_state("OPEN")
                    self.fail_count = 0
                    self.last_fail_time = time.time()
                    self._probing = False
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
