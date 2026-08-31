"""分布式锁语义单元测试（离线）

验证 payment/service 的锁契约：
- acquire 成功返回令牌、失败返回 None
- release 只有持有者能删（FakeRedis.eval 模拟 Lua 条件删除语义）
- 锁被占时并发 acquire 失败
真实 Lua 的原子性由集成测试（真实 Redis）验证。
"""
import pytest

from app.payment import service as payment_service
from app.payment import _ledger as ledger
from fake_redis import FakeRedis


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()

    async def _get_redis():
        return r

    # 锁原语已移到 _ledger（2026-08-31），get_redis 在其命名空间解析，
    # patch 需落 _ledger 而非 service，否则 FakeRedis 注入不到锁函数内部。
    monkeypatch.setattr(ledger, "get_redis", _get_redis)
    return r


@pytest.mark.asyncio
async def test_acquire_returns_token(fake_redis):
    token = await payment_service._acquire_lock("wallet:u1", ttl=10)
    assert token  # 非空 = 持有锁


@pytest.mark.asyncio
async def test_second_acquire_fails_when_held(fake_redis):
    t1 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    assert t1
    t2 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    assert t2 is None  # 锁被持有


@pytest.mark.asyncio
async def test_release_with_wrong_token_keeps_lock(fake_redis):
    t1 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    # 模拟锁已过期被他人持有
    await fake_redis.set(payment_service._LOCK_PREFIX + "wallet:u1", "other-token")
    await payment_service._release_lock("wallet:u1", t1)
    # 原持有者 token 不能删掉新持有者的锁
    assert await fake_redis.get(payment_service._LOCK_PREFIX + "wallet:u1") == "other-token"


@pytest.mark.asyncio
async def test_release_with_correct_token_deletes(fake_redis):
    t1 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    await payment_service._release_lock("wallet:u1", t1)
    assert await fake_redis.get(payment_service._LOCK_PREFIX + "wallet:u1") is None


@pytest.mark.asyncio
async def test_lock_reacquirable_after_release(fake_redis):
    t1 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    await payment_service._release_lock("wallet:u1", t1)
    t2 = await payment_service._acquire_lock("wallet:u1", ttl=10)
    assert t2
