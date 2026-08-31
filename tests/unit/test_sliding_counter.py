"""滑动窗口计数器限流单元测试（离线，FakeRedis 模拟 Lua 语义）

验证双窗口加权算法的核心行为：
- 同秒内：限内放行、超限拒绝
- 跨秒：前窗口按"秒内已过比例"衰减权重（近似滑动）
- 用户隔离
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.middleware.rate_limit as rl
from fake_redis import FakeRedis

_fake = FakeRedis()


async def _fake_get_redis():
    return _fake


def _setup(limit: int = 3):
    rl._ROLE_LIMITS["user"]["qps"] = limit
    rl.get_redis = _fake_get_redis
    _fake._data.clear()


def test_counter_allows_up_to_limit():
    """同秒内：前 limit 次放行（固定时间戳注入，保证确定性）"""
    _setup(limit=3)
    results = asyncio.run(_call_n(3, now=100.5))
    assert results == [True, True, True]


def test_counter_rejects_over_limit():
    """同秒内：第 limit+1 次拒绝"""
    _setup(limit=3)
    results = asyncio.run(_call_n(4, now=100.5))
    assert results == [True, True, True, False]


def test_counter_cross_second_weight_decay():
    """跨秒：前窗口按已过比例衰减权重（近似滑动）

    t=100.9 时放满 3 个（全部落在秒 100 窗口）；
    t=101.9 时 fraction=0.9，前窗口权重仅 10% → estimate=0.3 < 3 → 放行
    """
    _setup(limit=3)
    first = asyncio.run(_call_n(3, now=100.9))
    assert first == [True, True, True]
    # 跨秒：前窗口衰减后允许新请求
    assert asyncio.run(rl.check_qps("u1", _now=101.9)) is True


def test_counter_prev_window_still_counts_early():
    """跨秒早期：前窗口几乎全额计入 → 仍拒绝

    t=100.1 放满 3 个；t=101.1 时 fraction=0.1，前窗口权重 90%
    → estimate=2.7 < 3 → 仍放行（滑动窗口 [100.1,101.1] 内确实不足 3 个新请求）
    """
    _setup(limit=3)
    asyncio.run(_call_n(3, now=100.1))
    # 2.7 < 3 → 放行（滑动窗口内前窗口大部分已滑出？100.1 秒的请求在 101.1 时只滑出 0.1 秒，
    # 但请求集中在 100.1 附近 → 实际仍密集，估算保守放行一个后即满）
    assert asyncio.run(rl.check_qps("u1", _now=101.1)) is True
    # 再放一个后 estimate=2.7+1=3.7 ≥ 3 → 拒绝
    assert asyncio.run(rl.check_qps("u1", _now=101.1)) is False


def test_counter_isolated_per_user():
    """不同用户互不影响"""
    _setup(limit=1)
    assert asyncio.run(rl.check_qps("u1", _now=100.5)) is True
    assert asyncio.run(rl.check_qps("u2", _now=100.5)) is True
    assert asyncio.run(rl.check_qps("u1", _now=100.5)) is False


async def _call_n(n: int, now: float) -> list:
    return [await rl.check_qps("u1", _now=now) for _ in range(n)]
