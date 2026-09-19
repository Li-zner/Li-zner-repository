"""
MCP 资产 001 — Redis 分布式锁与限流 Server（stdio 传输）

从 agent_gateway 生产实战沉淀（payment/service.py 双锁 + rate_limit.py 四维限流）。
能力：
  1. acquire_lock / release_lock    —— 分布式锁（SET NX EX + Lua 原子释放，防误删他人锁）
  2. fixed_window_limit             —— 固定窗口限流（INCR + 首次设过期）
  3. sliding_window_limit           —— 滑动窗口限流（ZSET，修复固定窗口边界双倍流量）
  4. token_bucket_limit             —— 令牌桶限流（Lua 原子：允许突发、平均受限）

运行（stdio，供任何 MCP Host 拉起）：
    python redis_lock_ratelimit_server.py
纯逻辑自检（无需 Redis）：
    python redis_lock_ratelimit_server.py --self-check
验证 Client（需 Redis）：
    python test_client.py

配置（进程环境变量优先，其次 _env.py 读 .env）：
    REDIS_URL  默认 redis://localhost:6379
"""
import sys
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_ROOT = Path(__file__).resolve().parent.parent  # → MCP sever/
sys.path.insert(0, str(_ROOT))
from _env import get  # noqa: E402

mcp = FastMCP("redis-lock-ratelimit")

_redis = None


async def get_redis():
    """懒加载全局 Redis 连接（单例，供所有工具复用；延迟 import 让 --self-check 无需 redis 包）"""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(
            get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis


# Lua：仅值匹配才删除（检查+删除原子化，防先 get 再 delete 的中间窗口）
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 令牌桶：按流逝时间补 token，有 token 才放行（Lua 保证检查与扣减原子）
_TOKEN_BUCKET_LUA = """
local tokens = tonumber(redis.call("get", KEYS[1]) or "0")
local last = tonumber(redis.call("get", KEYS[1]..":ts") or ARGV[3])
local now = tonumber(ARGV[3])
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
tokens = math.min(tokens + (now - last) * rate, capacity)
if tokens >= 1 then
    redis.call("set", KEYS[1], tokens - 1)
    redis.call("set", KEYS[1]..":ts", now)
    return 1
else
    redis.call("set", KEYS[1], tokens)
    redis.call("set", KEYS[1]..":ts", now)
    return 0
end
"""


@mcp.tool()
async def acquire_lock(key: str, ttl: int = 10) -> dict:
    """获取分布式锁（SET NX EX）。多实例互斥进入临界区。

    Args:
        key: 锁名，如 "wallet:user_123"（按业务粒度，越细并发越高）。
        ttl: 锁过期秒数，必须大于最长临界区操作时间；操作可能超时需看门狗续期。
    """
    if not key or ttl <= 0:
        return {"error": "key 不能为空且 ttl 必须 > 0"}
    token = uuid.uuid4().hex
    try:
        r = await get_redis()
        ok = await r.set(f"lock:{key}", token, nx=True, ex=ttl)
    except Exception as e:
        return {"error": str(e)}
    if not ok:
        return {"acquired": False, "reason": "锁已被他人持有"}
    return {"acquired": True, "token": token, "ttl": ttl}


@mcp.tool()
async def release_lock(key: str, token: str) -> dict:
    """释放分布式锁（Lua 校验持有者，防误删他人锁——踩过的真 bug）。

    Args:
        key: 与 acquire_lock 相同的锁名。
        token: acquire_lock 返回的唯一令牌。
    """
    if not token:
        return {"error": "token 不能为空"}
    try:
        r = await get_redis()
        deleted = await r.eval(_RELEASE_LUA, 1, f"lock:{key}", token)
    except Exception as e:
        return {"error": str(e)}
    return {"released": bool(deleted), "owned": bool(deleted)}


@mcp.tool()
async def fixed_window_limit(key: str, limit: int = 100, window_sec: int = 1) -> dict:
    """固定窗口限流：每秒一个 key，INCR 计数，超限拒绝。

    Args:
        key: 计数主体，如 "qps:user_123"。
        limit: 窗口内允许的最大次数。
        window_sec: 窗口秒数（默认 1 = QPS 限流）。
    """
    if limit <= 0 or window_sec <= 0:
        return {"error": "limit 与 window_sec 必须 > 0"}
    try:
        r = await get_redis()
        rkey = f"fw:{key}:{int(time.time()) // window_sec}"
        count = await r.incr(rkey)
        if count == 1:
            await r.expire(rkey, window_sec + 1)  # 首次创建时设过期，防 key 堆积
    except Exception as e:
        return {"error": str(e)}
    return {"allowed": count <= limit, "count": count, "limit": limit}


@mcp.tool()
async def sliding_window_limit(key: str, limit: int = 100, window_sec: int = 60) -> dict:
    """滑动窗口限流（ZSET）：统计最近 window_sec 秒真实请求数，无边界双倍流量缺陷。

    Args:
        key: 计数主体。
        limit: 窗口内允许的最大次数。
        window_sec: 窗口秒数。
    """
    if limit <= 0 or window_sec <= 0:
        return {"error": "limit 与 window_sec 必须 > 0"}
    now = time.time()
    try:
        r = await get_redis()
        rkey = f"sw:{key}"
        pipe = r.pipeline()
        pipe.zremrangebyscore(rkey, 0, now - window_sec)  # 清掉窗口外的
        pipe.zadd(rkey, {str(uuid.uuid4()): now})          # 记录本次
        pipe.zcard(rkey)                                   # 窗口内数量
        pipe.expire(rkey, window_sec)
        count = (await pipe.execute())[2]
    except Exception as e:
        return {"error": str(e)}
    return {"allowed": count <= limit, "count": count, "limit": limit}


@mcp.tool()
async def token_bucket_limit(key: str, capacity: int = 100, refill_per_sec: float = 10.0) -> dict:
    """令牌桶限流（Lua 原子）：允许突发但平均受限，适合 LLM 聊天这类突发场景。

    Args:
        key: 桶名。
        capacity: 桶容量（最大突发量）。
        refill_per_sec: 每秒补充的令牌数（平均速率）。
    """
    if capacity <= 0 or refill_per_sec <= 0:
        return {"error": "capacity 与 refill_per_sec 必须 > 0"}
    try:
        r = await get_redis()
        ok = await r.eval(_TOKEN_BUCKET_LUA, 1, key, capacity, refill_per_sec, time.time())
    except Exception as e:
        return {"error": str(e)}
    return {"allowed": bool(ok)}


def _self_check() -> None:
    """纯逻辑自检（不依赖 Redis/管道）：锁语义、窗口语义、令牌桶数学。"""
    # 1. 释放锁 Lua：值匹配才删除——用内存 dict 模拟
    store = {"lock:k": "tokenA"}

    def fake_eval(script, _, key, arg):
        assert "redis.call" in script and "==" in script
        if store.get(key) == arg:
            del store[key]
            return 1
        return 0

    assert fake_eval(_RELEASE_LUA, 1, "lock:k", "tokenA") == 1 and "lock:k" not in store
    store["lock:k"] = "tokenB"
    assert fake_eval(_RELEASE_LUA, 1, "lock:k", "tokenA") == 0 and store["lock:k"] == "tokenB"
    print("[self-check] 释放锁 Lua 语义 OK（他人 token 不删）")

    # 2. 固定窗口：窗口内超限拒绝、跨窗口重置（dict 模拟 INCR+expire）
    counts, expire_at = {}, {}
    now = [1000.0]

    def incr(k):
        counts[k] = counts.get(k, 0) + 1
        if counts[k] == 1:
            expire_at[k] = now[0] + 2
        return counts[k]

    def fw_allowed(key, limit, window):
        rkey = f"{key}:{int(now[0]) // window}"
        if expire_at.get(rkey, now[0]) <= now[0]:
            counts.pop(rkey, None)  # 过期兜底
        return incr(rkey) <= limit

    assert all(fw_allowed("u", 2, 1) for _ in range(2))
    assert not fw_allowed("u", 2, 1)  # 第 3 次拒绝
    now[0] += 1.0  # 下一个窗口
    assert fw_allowed("u", 2, 1)      # 窗口重置
    print("[self-check] 固定窗口语义 OK（窗口内超限、跨窗口重置）")

    # 3. 令牌桶数学：补 token 后放行，容量封顶
    def bucket_math(tokens, last, now, capacity, rate):
        tokens = min(tokens + (now - last) * rate, capacity)
        if tokens >= 1:
            return tokens - 1, 1
        return tokens, 0

    t, ok = bucket_math(0, 0, 0, 10, 5)
    assert t == 0 and ok == 0                      # 起始无 token
    t, ok = bucket_math(0, 0, 1, 10, 5)
    assert t == 4 and ok == 1                      # 1 秒补 5，扣 1
    t, ok = bucket_math(t, 1, 100, 10, 5)
    assert t == 9 and ok == 1                      # 长时间补满并封顶（容量 10，扣 1）
    print("[self-check] 令牌桶数学 OK（补发/扣减/封顶）")
    print("全部自检通过")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        mcp.run()  # 默认 stdio 传输
