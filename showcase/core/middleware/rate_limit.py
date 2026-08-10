import time
import os
from fastapi import HTTPException

from ..core.redis import get_redis
from ..core.config import SECOND_REQUEST_LIMIT, DAILY_REQUEST_LIMIT, DAILY_TOKEN_LIMIT

# ============================================================
# 角色分级限流配置
# ============================================================
# admin 用户获得更高额度
_ROLE_LIMITS = {
    "admin": {
        "qps": int(os.getenv("ADMIN_QPS_LIMIT", "5000")),
        "daily_req": int(os.getenv("ADMIN_DAILY_REQ", "1000000")),
        "daily_token": int(os.getenv("ADMIN_DAILY_TOKEN", "50000000")),
        "concurrent": int(os.getenv("ADMIN_CONCURRENT", "500")),
    },
    "user": {
        "qps": int(os.getenv("USER_QPS_LIMIT", "2000")),
        "daily_req": int(os.getenv("USER_DAILY_REQ", "500000")),
        "daily_token": int(os.getenv("USER_DAILY_TOKEN", "50000000")),
        "concurrent": int(os.getenv("USER_CONCURRENT", "200")),
    },
}

_DEFAULT_LIMITS = _ROLE_LIMITS["user"]


def _get_limits(role: str = "user") -> dict:
    return _ROLE_LIMITS.get(role, _DEFAULT_LIMITS)


async def check_qps(username: str, role: str = "user") -> bool:
    limits = _get_limits(role)
    r = await get_redis()
    key = f"qps:{username}:{int(time.time())}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 2)
    return count <= limits["qps"]


async def check_concurrent(username: str, role: str = "user") -> bool:
    """检查用户并发请求数，超过限制则拒绝"""
    limits = _get_limits(role)
    r = await get_redis()
    key = f"concurrent:{username}"
    current = await r.incr(key)
    if current == 1:
        await r.expire(key, 30)  # 30秒超时自动释放
    if current > limits["concurrent"]:
        await r.decr(key)
        return False
    return True


async def release_concurrent(username: str):
    """释放一个并发槽位"""
    r = await get_redis()
    key = f"concurrent:{username}"
    current = await r.get(key)
    if current and int(current) > 0:
        await r.decr(key)


async def get_daily_usage(username: str, date_str: str):
    r = await get_redis()
    req_key = f"daily_req:{username}:{date_str}"
    token_key = f"daily_token:{username}:{date_str}"
    req_count = int(await r.get(req_key) or 0)
    token_sum = int(await r.get(token_key) or 0)
    return {"request_count": req_count, "token_sum": token_sum}


async def update_daily_usage(username: str, date_str: str, inc_request=1, inc_token=0):
    r = await get_redis()
    req_key = f"daily_req:{username}:{date_str}"
    token_key = f"daily_token:{username}:{date_str}"
    new_req = await r.incr(req_key, inc_request)
    new_token = await r.incr(token_key, inc_token)
    if new_req == inc_request:
        now = time.time()
        tomorrow = int(now) - (int(now) % 86400) + 86400
        ttl = tomorrow - int(now)
        if ttl > 0:
            await r.expire(req_key, ttl)
            await r.expire(token_key, ttl)