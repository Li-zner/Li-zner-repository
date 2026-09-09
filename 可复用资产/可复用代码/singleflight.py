# ============================================================
# 可复用资产：singleflight+幂等.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""请求合并(singleflight) + 幂等键原子占位 —— 社区标准实现可复用版

对应本项目:
- app/core/task_manager.py try_idempotent/save_idempotent + app/routes/v2.py 任务创建
  (现状为 GET 检查后再 SETEX 保存, 两步之间存在竞态, 并发同消息会重复建任务)。
- app/core/semantic_cache.py 重建锁等待(同 key 并发只放一个去打 LLM)。

来源(GitHub):
- singleflight 原版: https://github.com/golang/sync (golang.org/x/sync/singleflight)
- 幂等键语义: https://github.com/stripe/stripe-python (Idempotency-Key / SET NX 原子占位)
"""
import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


class SingleFlight:
    """并发去重: 同 key 的并发调用共享第一次执行的结果或异常

    与原版 singleflight 一致: 先到者执行 fn, 后来者挂起在同一个 Future 上;
    结果/异常广播给全部等待者。仅适用于单进程(多实例需配合 Redis 锁)。
    """

    def __init__(self) -> None:
        self._inflight: dict = {}

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        """同一 key 并发调用只执行一次 fn

        Args:
            key: 去重键(如 f"{username}:{session}:{hash(message)}")。
            fn: 零参异步工厂, 只在第一个到达者上执行。
        """
        existing = self._inflight.get(key)
        if existing is not None:
            # shield: 后来者取消不应连坐先到者的执行
            return await asyncio.shield(existing)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut  # 无 await 的 get/set 间隙, 事件循环内天然原子
        try:
            result = await fn()
        except BaseException as e:
            # 异常同样广播: 等待者收到同一错误而不是空等
            if not fut.done():
                fut.set_exception(e)
        else:
            if not fut.done():
                fut.set_result(result)
        finally:
            # 清理后同 key 的新请求开启新一轮执行(与原版语义一致)
            self._inflight.pop(key, None)
        # 首个调用者也统一从 Future 取结果/异常: 保证异常必然被检索
        # (否则无人等待的 Future 会触发 asyncio "never retrieved" 告警)
        return await fut


async def claim_idempotency_key(redis: Any, key: str, value: str,
                                ttl: int = 10) -> Optional[str]:
    """原子占位幂等键: 占位成功返回 None; 已被占用返回已存 value

    修复 check-then-act 竞态: "先 GET 检查, 再 SETEX 保存"两步之间, 并发请求
    都能通过检查而各自创建任务。SET NX 一条命令原子完成"检查+占位":
    占位失败者立即拿到已有 task_id 直接复用, 不重复执行。
    幂等键建议包含用户标识, 防跨用户串号。

    Args:
        redis: redis.asyncio 客户端。
        key: 幂等键。
        value: 要记录的值(如 task_id)。
        ttl: 占位有效期秒数; 过期后允许再次提交。
    """
    ok = await redis.set(key, value, nx=True, ex=ttl)
    if ok is True:
        return None
    return await redis.get(key)


class _FakeRedis:
    """内存版 Redis: 仅实现 set(nx,ex)/get, 供离线自检"""

    def __init__(self) -> None:
        self.store: dict = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int = None) -> Any:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)


def _self_check() -> None:
    """最小自检: 同 key 只执行一次、异常广播、幂等键原子占位"""
    import asyncio

    async def run() -> None:
        sf = SingleFlight()
        calls = []

        async def work() -> str:
            calls.append(1)
            await asyncio.sleep(0.01)
            return "result"

        rs = await asyncio.gather(*[sf.do("k", work) for _ in range(10)])
        assert len(calls) == 1, f"同 key 并发应只执行一次, 实际 {len(calls)}"
        assert rs == ["result"] * 10, "等待者应共享结果"

        async def boom() -> None:
            raise RuntimeError("x")

        rs = await asyncio.gather(
            *[sf.do("e", boom) for _ in range(3)], return_exceptions=True)
        assert all(isinstance(r, RuntimeError) for r in rs), "异常应广播给等待者"

        r = _FakeRedis()
        first = await claim_idempotency_key(r, "idem:u1:s1:abc", "task_1", ttl=10)
        second = await claim_idempotency_key(r, "idem:u1:s1:abc", "task_2", ttl=10)
        assert first is None, "首次占位应成功"
        assert second == "task_1", "并发第二个必须复用已占位 task_id, 不得重复建任务"

    asyncio.run(run())
    print("singleflight+幂等 自检通过")


if __name__ == "__main__":
    _self_check()
