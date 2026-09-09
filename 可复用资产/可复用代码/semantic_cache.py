""" ============================================================
 *  可复用资产：semantic_cache.py
 *  来源：agent_gateway/app/core/semantic_cache.py（生产语义缓存，跨用户污染/穿透/击穿三事故修复后的语义）
 *  实战验证：生产运行中（公网网关主流量）；本件为独立可 import 参考（Redis 必需）
 *  依赖：redis-py（异步客户端）
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================

三级语义缓存（参考实现）语义要点——三条历史事故的修复结晶，改造时勿回退：

1. 防跨用户污染：缓存键必须包含 cache_ctx（人格/权限/画像维度指纹）。
   事故：同 query 不同权限用户共享缓存 → 越权读到他人上下文的回答。
2. 防穿透：未命中写短 TTL "__EMPTY__" 占位（L0 热缓存，默认 30s），
   同 query 短时间重复出现时直接回繁忙提示，不打穿底层 LLM。
   占位只写 L0 不写长存：无过期机制的存储会把占位钉成"永久繁忙"。
3. 防击穿：未命中时先抢互斥重建锁（SET NX + 随机 token），抢到的唯一请求去
   调 LLM 重建，其余等待后重读缓存；锁带 TTL 自愈 + 后台续期（业务耗时可能
   超 TTL）；释放必须校验 token（Lua compare-and-del），防止误删他人的锁。
"""
import asyncio
import uuid
from typing import Any, Optional

EMPTY = "__EMPTY__"
DEFAULT_PLACEHOLDER_TTL = 30
DEFAULT_LOCK_TTL = 45
DEFAULT_LOCK_RETRY_DELAY = 0.3

_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class SemanticCache:
    """语义缓存：get/set + 穿透占位 + 互斥重建锁。

    cache_ctx 是防跨用户污染的关键维度（人格|权限|画像指纹），
    由调用方按业务拼装；不同 ctx 之间永不共享缓存。
    """

    def __init__(self, redis_client: Any, placeholder_ttl: int = DEFAULT_PLACEHOLDER_TTL,
                 lock_ttl: int = DEFAULT_LOCK_TTL):
        self.r = redis_client
        self.placeholder_ttl = placeholder_ttl
        self.lock_ttl = lock_ttl

    def _cache_key(self, query: str, cache_ctx: str = "") -> str:
        return f"semcache:{cache_ctx}:{query}"

    def _lock_key(self, query: str, cache_ctx: str = "") -> str:
        return f"semcache_lock:{cache_ctx}:{query}"

    async def get(self, query: str, cache_ctx: str = "") -> Optional[str]:
        """命中返回缓存文本；EMPTY 为穿透占位命中（调用方回繁忙提示）；None 为未命中"""
        return await self.r.get(self._cache_key(query, cache_ctx))

    async def set(self, query: str, answer: str, cache_ctx: str = "") -> None:
        await self.r.set(self._cache_key(query, cache_ctx), answer)

    async def is_empty(self, response: Optional[str]) -> bool:
        """判断是否穿透占位"""
        return response is not None and str(response) == EMPTY

    async def set_empty(self, query: str, cache_ctx: str = "", ttl: Optional[int] = None) -> None:
        """穿透占位：只写 L0/短 TTL 存储，不写长存——占位钉死成永久繁忙的教训"""
        ttl = ttl or self.placeholder_ttl
        await self.r.set(self._cache_key(query, cache_ctx), EMPTY, ex=ttl)

    async def acquire_rebuild_lock(self, query: str, cache_ctx: str = "") -> Optional[str]:
        """抢互斥重建锁：返回 token；None = 已有人持有（调用方等待后重读缓存）"""
        token = uuid.uuid4().hex
        ok = await self.r.set(self._lock_key(query, cache_ctx), token,
                              nx=True, ex=self.lock_ttl)
        return token if ok else None

    async def renew_rebuild_lock(self, query: str, token: str,
                                 cache_ctx: str = "", ttl: Optional[int] = None) -> bool:
        """后台续期：校验 token 防续了别人的锁；锁已被抢/失效返回 False（调用方停止续期）"""
        ttl = ttl or self.lock_ttl
        key = self._lock_key(query, cache_ctx)
        cur = await self.r.get(key)
        if cur == token:
            await self.r.set(key, token, xx=True, ex=ttl)
            return True
        return False

    async def release_rebuild_lock(self, query: str, token: str, cache_ctx: str = "") -> bool:
        key = self._lock_key(query, cache_ctx)
        return bool(await self.r.eval(_RELEASE_LOCK_LUA, 1, key, token))


async def rebuild_with_lock(cache: SemanticCache, query: str, cache_ctx: str,
                            rebuild_coro_factory, is_valid) -> tuple:
    """标准重建编排（完整用法示例；亦可自行组合上面的原语）：

    抢锁 → 抢到：调 rebuild_coro_factory() 产出答案并写缓存；
          未抢到：等 0.3s 重读缓存，命中（且通过 is_valid 校验）即返回。

    返回 (answer, from_cache: bool)
    """
    token = await cache.acquire_rebuild_lock(query, cache_ctx)
    if token is None:
        await asyncio.sleep(DEFAULT_LOCK_RETRY_DELAY)
        cached = await cache.get(query, cache_ctx)
        if cached and not await cache.is_empty(cached) and is_valid(cached):
            return cached, True
    answer = await rebuild_coro_factory()
    if answer:
        await cache.set(query, answer, cache_ctx)
    return answer, False


def self_check() -> None:
    """模块自检：FakeRedis 上跑一遍 占位/命中/锁互斥/续期/释放/跨用户隔离"""
    class _FakeRedis:
        def __init__(self):
            self.d = {}

        async def get(self, k):
            return self.d.get(k)

        async def set(self, k, v, nx=False, ex=None, xx=False):
            if xx and k not in self.d:
                return None
            if nx and k in self.d:
                return None
            self.d[k] = v
            return "OK"

        async def eval(self, script, numkeys, key, *args):
            if self.d.get(key) == args[0]:
                del self.d[key]
                return 1
            return 0

    async def run():
        c = SemanticCache(_FakeRedis())
        assert await c.get("q", "ctx") is None
        await c.set_empty("q", "ctx")
        assert await c.is_empty(await c.get("q", "ctx"))
        tok1 = await c.acquire_rebuild_lock("q2", "ctx")
        tok2 = await c.acquire_rebuild_lock("q2", "ctx")
        assert tok1 and tok2 is None, "互斥：第二个必须抢不到"
        assert await c.renew_rebuild_lock("q2", tok1, cache_ctx="ctx")
        assert not await c.renew_rebuild_lock("q2", "wrong-token", cache_ctx="ctx")
        await c.release_rebuild_lock("q2", tok1, cache_ctx="ctx")
        assert not await c.release_rebuild_lock("q2", tok1, cache_ctx="ctx"), "重复释放必须失败"
        # 跨用户隔离
        await c.set("q3", "回答A", cache_ctx="user1")
        assert await c.get("q3", cache_ctx="user2") is None

    asyncio.run(run())
    print("semantic_cache self_check OK")


if __name__ == "__main__":
    self_check()
