# ============================================================
# 可复用资产：分布式锁+lua.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""分布式锁（Redis SET NX + Lua 释放/续期）—— 社区标准实现可复用版

对应本项目: app/payment/_ledger.py(_acquire_lock/_release_lock)、
            app/core/semantic_cache.py(缓存重建锁)、app/cdc/worker.py(leader 锁)。
来源(GitHub/官方):
- 加锁/释放标准模式: https://redis.io/docs/latest/commands/set/ (NX PX + Lua compare-and-delete)
- Redisson RLock(watchdog 续期): https://github.com/redisson/redisson

为什么这么写:
- 加锁必须 SET NX PX 一条命令原子完成; 先 SETNX 再 EXPIRE 两步在中间崩溃会留下死锁。
- 释放/续期必须 Lua 校验 token: 锁过期被他人持有时, 原持有者晚归会误删/误续他人锁。
- token 用 uuid4 唯一标识持有者, 等价于 Redisson 的 client-id。
"""
import asyncio
import uuid
from typing import Any, Optional

# 仅当值等于本持有者 token 时才删除(Redis 官方 compare-and-delete 惯用法)
RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 仅当值等于本持有者 token 时才续期(Redisson watchdog 续期的最小等价实现)
RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class DistributedLock:
    """Redis 分布式锁: acquire/release/renew 全部原子, 支持 async with

    Args:
        redis: redis.asyncio.Redis 客户端。
        name: 锁名(业务语义, 如 "wallet:{user_id}"), 内部自动加前缀。
        ttl: 自动过期秒数(持有者崩溃后的自愈时间, 防死锁)。
    """

    def __init__(self, redis: Any, name: str, ttl: float = 10.0,
                 prefix: str = "lock:") -> None:
        self.redis = redis
        self.key = f"{prefix}{name}"
        self.ttl_ms = int(ttl * 1000)
        self._token: Optional[str] = None

    async def acquire(self) -> Optional[str]:
        """尝试加锁(不阻塞); 成功返回 token, 已被他人持有返回 None

        等价于 Redisson tryLock(0, ttl); token 在释放/续期时校验持有者身份。
        """
        token = uuid.uuid4().hex
        ok = await self.redis.set(self.key, token, nx=True, px=self.ttl_ms)
        if ok is True:
            self._token = token
            return token
        return None

    async def release(self) -> bool:
        """释放锁(仅持有者能释放, Lua 原子校验防误删他人锁)"""
        if not self._token:
            return False
        token, self._token = self._token, None
        return await self.redis.eval(RELEASE_LUA, 1, self.key, token) == 1

    async def renew(self) -> bool:
        """续期(仅持有者能续, Lua 原子校验); 长任务周期调用防锁提前过期被抢

        等价于 Redisson watchdog: 把 TTL 重置回初始值; 返回 False 表示已失去锁,
        调用方必须立即停止被锁保护的工作(否则会双写者)。
        """
        if not self._token:
            return False
        ok = await self.redis.eval(RENEW_LUA, 1, self.key, self._token, self.ttl_ms)
        if ok != 1:
            self._token = None  # 已失去锁: 清掉 token 防止后续误续/误删
        return ok == 1

    async def __aenter__(self) -> "DistributedLock":
        """阻塞式获取(每 0.05s 重试, 总等待 5s); 不想等待可直接用 acquire()"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while await self.acquire() is None:
            if loop.time() > deadline:
                raise TimeoutError(f"获取锁超时: {self.key}")
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.release()


class _FakeRedis:
    """内存版 Redis: 仅实现本文件用到的 set(nx,px)/eval, 供离线自检"""

    def __init__(self) -> None:
        self.store: dict = {}
        self.now = [0.0]  # 模拟时钟(毫秒), 测试过期自愈用

    def _get(self, key: str) -> Any:
        item = self.store.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] < self.now[0]:
            return None  # 已过期视同不存在
        return item[0]

    async def set(self, key: str, value: Any, nx: bool = False, px: int = None) -> Any:
        if nx and self._get(key) is not None:
            return None
        self.store[key] = (value, (self.now[0] + int(px)) if px else None)
        return True

    async def eval(self, script: str, numkeys: int, key: str, *args: Any) -> int:
        if "pexpire" in script:  # RENEW_LUA
            if self._get(key) == args[0]:
                self.store[key] = (self.store[key][0], self.now[0] + int(args[1]))
                return 1
            return 0
        if "del" in script:  # RELEASE_LUA
            if self._get(key) == args[0]:
                del self.store[key]
                return 1
            return 0
        raise AssertionError("未知脚本")


def _self_check() -> None:
    """最小自检: 互斥、错 token 不可释放/续期、过期自愈、async with 自动释放"""

    async def run() -> None:
        r = _FakeRedis()
        lock1 = DistributedLock(r, "wallet:u1", ttl=0.05)
        assert await lock1.acquire(), "首次加锁应成功"
        lock2 = DistributedLock(r, "wallet:u1", ttl=0.05)
        assert await lock2.acquire() is None, "互斥: 第二次加锁必须失败"
        assert await r.eval(RELEASE_LUA, 1, lock1.key, "wrong") == 0, "错 token 不可释放"
        assert await lock1.release() is True, "持有者应能释放"
        assert await lock2.acquire(), "释放后可重新加锁"
        lock3 = DistributedLock(r, "k", ttl=10)
        await lock3.acquire()
        assert await lock3.renew() is True, "持有者可续期"
        lock3._token = "bad"
        assert await lock3.renew() is False, "非持有者不可续期"
        r.now[0] += 60_000  # 时间前进, 锁已过期
        assert await DistributedLock(r, "wallet:u1", ttl=0.05).acquire(), "过期锁可被重新获取"
        async with DistributedLock(r, "ctx", ttl=10) as lk:
            assert lk._token
        assert "lock:ctx" not in r.store, "async with 退出应自动释放"

    asyncio.run(run())
    print("分布式锁+lua 自检通过")


if __name__ == "__main__":
    _self_check()
