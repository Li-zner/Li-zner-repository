"""缓存防击穿：互斥重建锁单元测试（离线）

验证 SemanticCache.acquire_rebuild_lock / release_rebuild_lock：
- 空闲时 acquire 成功返回 token
- 已占用时 acquire 返回 None（同一 query 只允许一个重建）
- 仅持有者能 release（防误删）
"""
import pytest

from app.core.semantic_cache import SemanticCache
from fake_redis import FakeRedis

_QUERY = "北京三日游攻略"


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()

    async def _get_redis():
        return r

    monkeypatch.setattr(SemanticCache, "_lock_key", lambda q, cache_ctx="": f"cache:rebuild:test-{q}")
    monkeypatch.setattr("app.core.semantic_cache.get_redis", _get_redis)
    return r


@pytest.mark.asyncio
async def test_acquire_when_free(fake_redis):
    token = await SemanticCache.acquire_rebuild_lock(_QUERY)
    assert token


@pytest.mark.asyncio
async def test_second_acquire_blocked(fake_redis):
    t1 = await SemanticCache.acquire_rebuild_lock(_QUERY)
    assert t1
    t2 = await SemanticCache.acquire_rebuild_lock(_QUERY)
    assert t2 is None  # 同一 query 已有人在重建


@pytest.mark.asyncio
async def test_release_owner_only(fake_redis):
    t1 = await SemanticCache.acquire_rebuild_lock(_QUERY)
    await fake_redis.set("cache:rebuild:test-" + _QUERY, "other-token")
    await SemanticCache.release_rebuild_lock(_QUERY, t1)
    # 原持有者 token 不能删掉新持有者的锁
    assert await fake_redis.get("cache:rebuild:test-" + _QUERY) == "other-token"


@pytest.mark.asyncio
async def test_release_owner_deletes(fake_redis):
    t1 = await SemanticCache.acquire_rebuild_lock(_QUERY)
    await SemanticCache.release_rebuild_lock(_QUERY, t1)
    assert await fake_redis.get("cache:rebuild:test-" + _QUERY) is None
