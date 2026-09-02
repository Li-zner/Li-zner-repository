"""分布式限流器(Redis + Lua)—— 社区标准实现可复用版

对应本项目: app/middleware/rate_limit.py(QPS 滑动窗口/并发槽位/日配额)。
来源(GitHub/官方):
- 双计数器滑动窗口: Cloudflare "How we built rate limiting..."(blog.cloudflare.com),
  开源等价实现见 https://github.com/cloudflare/cloudflare-blog
- 令牌桶: https://github.com/brandur/redis-cell (GCRA 令牌桶) 与 Stripe 限流实践
- 注: 本项目版已用 Redis 服务端 TIME 替代客户端时钟(防漂移), 此处保留该改进。

包含 4 个脚本:
- SLIDING_WINDOW_LUA: QPS 双窗口滑动计数(当前秒+上一秒加权), 服务端时间。
- CONCURRENT_ACQUIRE_LUA / CONCURRENT_RELEASE_LUA: 并发槽位原子占位/释放。
  (释放脚本修复了 GET 后再 DECR 的竞态: 并发释放会把计数打成负数)
- DAILY_QUOTA_LUA: 日请求数+日 Token 双 Key 原子累加, TTL 对齐次日 UTC 零点。
- TOKEN_BUCKET_LUA: 平滑突发令牌桶(服务端时间), 适合放行脉冲流量。

说明: Lua 脚本为社区标准原文, 需真实 Redis 验证(本环境无实例);
Python 侧纯逻辑(ttl_until_utc_midnight)有自检。
"""
import time
from typing import Any, Optional

# QPS 双窗口滑动计数: estimate = prev*(1-已过比例) + curr, 超限拒绝且不计数
SLIDING_WINDOW_LUA = """
local limit = tonumber(ARGV[1])
local prefix = ARGV[2]
local now_arg = ARGV[3]
local now_sec
if now_arg ~= nil and now_arg ~= '' then
    now_sec = tonumber(now_arg)
else
    local now = redis.call('TIME')
    now_sec = tonumber(now[1]) + tonumber(now[2]) / 1000000
end
local curr_sec = math.floor(now_sec)
local fraction = now_sec - curr_sec
local curr_key = prefix .. curr_sec
local prev_key = prefix .. (curr_sec - 1)
local curr = tonumber(redis.call('GET', curr_key) or 0)
local prev = tonumber(redis.call('GET', prev_key) or 0)
if prev * (1 - fraction) + curr >= limit then
    return 0
end
redis.call('INCR', curr_key)
if curr == 0 then
    redis.call('EXPIRE', curr_key, 2)
end
return 1
"""

# 并发槽位: 原子"检查+占位", 超限不占位; 无条件 EXPIRE 兜底自愈防槽位泄漏
CONCURRENT_ACQUIRE_LUA = """
local limit = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or 0)
if current >= limit then
    return 0
end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

# 并发槽位释放: 原子递减并下限归零(先 GET 再 DECR 两步并发时会打成负数)
CONCURRENT_RELEASE_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v <= 1 then
    redis.call('DEL', KEYS[1])
    return 0
end
redis.call('DECR', KEYS[1])
return 1
"""

# 日配额: 同一脚本原子累加请求数+Token 数, 并统一设置 TTL(防两 Key 账目不一致)
DAILY_QUOTA_LUA = """
redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('INCRBY', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""

# 令牌桶: 按服务端时钟按速率补充令牌, 桶满截断; 每次消费 requested 个
TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local requested = tonumber(ARGV[3]) or 1
local now_arg = ARGV[4]
local now_ms
if now_arg ~= nil and now_arg ~= '' then
    now_ms = tonumber(now_arg)
else
    local t = redis.call('TIME')
    now_ms = t[1] * 1000 + math.floor(t[2] / 1000)
end
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    ts = now_ms
end
tokens = math.min(capacity, tokens + (now_ms - ts) / 1000 * rate)
local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now_ms)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 1)
return allowed
"""


def ttl_until_utc_midnight(now: Optional[float] = None) -> int:
    """距次日 UTC 零点的秒数(日配额 Key 的 TTL; 日期切分统一 UTC 避免时区歧义)

    Args:
        now: Unix 秒; None 用系统当前时间。
    Returns:
        >= 1 的 TTL 秒数。
    """
    now = now if now is not None else time.time()
    return int(now) - int(now) % 86400 + 86400 - int(now)


async def check_qps(redis: Any, key_prefix: str, limit: int,
                    now: Optional[float] = None) -> bool:
    """滑动窗口 QPS 限流; 放行返回 True

    Args:
        redis: redis.asyncio 客户端。
        key_prefix: 计数 Key 前缀(如 f"qps:{username}:"), 秒级后缀由脚本拼接。
        limit: 每秒允许请求数。
        now: 仅测试注入时钟; 生产传 None 走 Redis 服务端 TIME。
    """
    ok = await redis.eval(
        SLIDING_WINDOW_LUA, 0, limit, key_prefix, "" if now is None else str(now)
    )
    return ok == 1


async def acquire_slot(redis: Any, key: str, limit: int, slot_ttl: int = 30) -> bool:
    """占一个并发槽位; 超限返回 False 且不占位(调用方失败也无需补偿释放)"""
    ok = await redis.eval(CONCURRENT_ACQUIRE_LUA, 1, key, limit, slot_ttl)
    return ok == 1


async def release_slot(redis: Any, key: str) -> int:
    """原子释放并发槽位(下限归零, 绝不会打成负数); 返回剩余槽位数"""
    return int(await redis.eval(CONCURRENT_RELEASE_LUA, 1, key))


async def update_daily_quota(redis: Any, req_key: str, token_key: str,
                             inc_req: int, inc_token: int,
                             ttl: Optional[int] = None) -> None:
    """日请求数+日 Token 原子累加; ttl 缺省对齐次日 UTC 零点"""
    if ttl is None:
        ttl = ttl_until_utc_midnight()
    await redis.eval(DAILY_QUOTA_LUA, 2, req_key, token_key, inc_req, inc_token, ttl)


async def check_token_bucket(redis: Any, key: str, rate: float, capacity: int,
                             requested: int = 1, now_ms: Optional[int] = None) -> bool:
    """令牌桶限流(平滑突发); 放行返回 True

    Args:
        rate: 每秒补充令牌数; capacity: 桶容量(允许的最大突发)。
    """
    ok = await redis.eval(
        TOKEN_BUCKET_LUA, 1, key, rate, capacity, requested,
        "" if now_ms is None else str(now_ms),
    )
    return ok == 1


def _self_check() -> None:
    """自检纯逻辑: TTL 对齐 UTC 零点的三个边界"""
    assert ttl_until_utc_midnight(0) == 86400, "零点整应为全天"
    assert ttl_until_utc_midnight(86399) == 1, "当日最后一秒应只剩 1 秒"
    assert ttl_until_utc_midnight(86400) == 86400, "次日零点重新起算"
    assert ttl_until_utc_midnight() >= 1, "任何时刻 TTL 至少 1 秒"
    print("分布式限流器+lua 自检通过(Lua 需真实 Redis 验证)")


if __name__ == "__main__":
    _self_check()
