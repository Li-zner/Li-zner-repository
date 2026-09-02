"""
角色分级限流（QPS / 并发 / 日请求 / 日 Token）

设计要点（2026-08-18 加固）：
- QPS 滑动窗口：Redis 服务端 TIME 决定窗口（P1 #14 摆脱客户端时钟漂移），Lua 原子计数。
- 并发控制：Lua 原子"检查+增量"，超限不占位（P0 #27 杜绝双重释放）；无条件 EXPIRE 30s 自愈（P1 #4）。
- 日用量：Lua 原子 INCRBY 双 Key + 无条件 TTL（P1 #3/#15 防计数丢失 / Key 永不过期）。
- Redis 异常：Fail-open（放行并记 Error 日志），保障业务可用性优先（P1 #5）。
"""
import time

from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..core.config import (
    ADMIN_QPS_LIMIT, ADMIN_DAILY_REQ, ADMIN_DAILY_TOKEN, ADMIN_CONCURRENT,
    USER_QPS_LIMIT, USER_DAILY_REQ, USER_DAILY_TOKEN, USER_CONCURRENT,
)

logger = setup_logging()

# ============================================================
# 角色分级限流配置（统一从 core.config 读取，P2 #18 消除 os.getenv 散落）
# ============================================================
_ROLE_LIMITS = {
    "admin": {
        "qps": ADMIN_QPS_LIMIT,
        "daily_req": ADMIN_DAILY_REQ,
        "daily_token": ADMIN_DAILY_TOKEN,
        "concurrent": ADMIN_CONCURRENT,
    },
    "user": {
        "qps": USER_QPS_LIMIT,
        "daily_req": USER_DAILY_REQ,
        "daily_token": USER_DAILY_TOKEN,
        "concurrent": USER_CONCURRENT,
    },
}

_DEFAULT_LIMITS = _ROLE_LIMITS["user"]


def _get_limits(role: str = "user") -> dict:
    """直接返回常量引用，避免每次调用重建字典（P2 #6）"""
    return _ROLE_LIMITS.get(role, _DEFAULT_LIMITS)


# ============================================================
# QPS 滑动窗口 Lua
# ============================================================
# 原理：维护"当前秒"与"前一秒"两个计数器，估算 = 前窗口×(1-已过比例) + 当前窗口。
# 时间：用 Redis 服务端 TIME（高精度），彻底抛弃客户端时间戳（P1 #14/#32）。
# 优化：首次创建 key 才 EXPIRE，后续 INCR 不续期（窗口仅 1-2 秒，P2 #23）。
_QPS_COUNTER_LUA = """
local limit = tonumber(ARGV[1])
local prefix = ARGV[2]
-- 可选第 4 参：离线单测注入确定性时钟；生产传空串 → 改用 Redis 服务端 TIME（P1 #14 去客户端时钟漂移）
local now_arg = ARGV[3]
local now_sec
if now_arg ~= nil and now_arg ~= '' then
    now_sec = tonumber(now_arg)
else
    local now = redis.call('TIME')
    now_sec = tonumber(now[1]) + tonumber(now[2])/1000000
end
local curr_sec = math.floor(now_sec)
local fraction = now_sec - curr_sec
local curr_key = prefix .. curr_sec
local prev_key = prefix .. (curr_sec - 1)
local curr = tonumber(redis.call('GET', curr_key) or 0)
local prev = tonumber(redis.call('GET', prev_key) or 0)
local estimate = prev * (1 - fraction) + curr
if estimate < limit then
    redis.call('INCR', curr_key)
    if curr == 0 then
        redis.call('EXPIRE', curr_key, 2)
    end
    return 1
else
    return 0
end
"""


async def check_qps(username: str, role: str = "user", _now: float | None = None) -> bool:
    """QPS 限流（滑动窗口，服务端时间）；Redis 异常 Fail-open 放行

    _now: 仅供离线单测注入确定性时钟（详见 tests/unit/test_sliding_counter.py）；
          生产为 None → 走 Redis 服务端 TIME，摆脱客户端时钟漂移（P1 #14）。
    """
    try:
        limits = _get_limits(role)
        r = await get_redis()
        ok = await r.eval(
            _QPS_COUNTER_LUA, 0,
            limits["qps"], f"qps:counter:{username}:",
            "" if _now is None else str(_now),
        )
        return ok == 1
    except Exception as e:
        logger.error(f"QPS 限流 Redis 异常，放行请求（Fail-open）: {e}")
        return True


# ============================================================
# 并发控制 Lua
# ============================================================
# 原子"检查+增量"：超限时不修改计数器（P0 #27 拒绝不占位，杜绝调用方二次释放）；
# 通过时无条件 EXPIRE 30s 自愈（P1 #4，杜绝槽位泄漏）。
_CONCURRENT_LUA = """
local limit = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or 0)
if current >= limit then
    return 0
end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], 30)
return 1
"""


async def check_concurrent(username: str, role: str = "user") -> bool:
    """检查并占用一个并发槽位（原子）；超限返回 False 且不占位"""
    try:
        limits = _get_limits(role)
        r = await get_redis()
        ok = await r.eval(_CONCURRENT_LUA, 1, f"concurrent:{username}", limits["concurrent"])
        return ok == 1
    except Exception as e:
        logger.error(f"并发限流 Redis 异常，放行请求（Fail-open）: {e}")
        return True


# 释放：原子递减并下限归零。原 GET→DECR 两步在并发释放时会把计数打成负数（P0 #41）；
# 脚本复用 可复用代码/分布式限流器+lua.py 的社区标准实现（v<=1 直接 DEL，杜绝零值残留）
_CONCURRENT_RELEASE_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v <= 1 then
    redis.call('DEL', KEYS[1])
    return 0
end
redis.call('DECR', KEYS[1])
return 1
"""


async def release_concurrent(username: str):
    """释放一个并发槽位（Lua 原子递减下限归零，杜绝 GET→DECR 并发竞态，P0 #41；尽力而为不抛异常 P2 #35）"""
    try:
        r = await get_redis()
        await r.eval(_CONCURRENT_RELEASE_LUA, 1, f"concurrent:{username}")
    except Exception:
        pass  # 释放失败仅影响并发统计，不覆盖业务异常


# ============================================================
# 日用量（请求数 + Token 数）
# ============================================================
# 原子累加 Lua：同时 INCRBY 两个 Key 并统一设置 TTL（P1 #3/#15 防账目不一致 / Key 永不过期）
_DAILY_UPDATE_LUA = """
local inc_req = tonumber(ARGV[1])
local inc_token = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
redis.call('INCRBY', KEYS[1], inc_req)
redis.call('INCRBY', KEYS[2], inc_token)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""


async def get_daily_usage(username: str, date_str: str):
    """读取日用量（date_str 统一用 UTC 日期，见 P3 #39 时区约定）"""
    try:
        r = await get_redis()
        req_key = f"daily_req:{username}:{date_str}"
        token_key = f"daily_token:{username}:{date_str}"
        req_raw = await r.get(req_key) or "0"
        token_raw = await r.get(token_key) or "0"
        # 防御：Redis 半开状态返回空/非数字时按 0 处理（P3 #26）
        req_count = int(req_raw) if req_raw.isdigit() else 0
        token_sum = int(token_raw) if token_raw.isdigit() else 0
        return {"request_count": req_count, "token_sum": token_sum}
    except Exception:
        return {"request_count": 0, "token_sum": 0}


async def update_daily_usage(username: str, date_str: str, inc_request=1, inc_token=0):
    """日用量原子累加（Lua 同时 INCRBY 双 Key + 无条件 TTL，P1 #3/#15）"""
    try:
        r = await get_redis()
        req_key = f"daily_req:{username}:{date_str}"
        token_key = f"daily_token:{username}:{date_str}"
        # TTL 对齐到次日 UTC 零点
        now = time.time()
        tomorrow = int(now) - (int(now) % 86400) + 86400
        ttl = max(1, tomorrow - int(now))
        await r.eval(
            _DAILY_UPDATE_LUA, 2, req_key, token_key,
            inc_request, inc_token, ttl,
        )
    except Exception as e:
        logger.warning(f"日用量累加失败（不影响主流程）: {e}")
