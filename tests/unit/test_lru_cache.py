"""语义缓存 L0 热缓存（LRU）单元测试（离线）"""
from app.core.semantic_cache import _LRUCache


def test_lru_basic_get_set():
    c = _LRUCache(capacity=3)
    c.set("a", "1")
    c.set("b", "2")
    assert c.get("a") == "1"
    assert c.get("b") == "2"


def test_lru_eviction_oldest_first():
    c = _LRUCache(capacity=2)
    c.set("a", "1")
    c.set("b", "2")
    c.set("c", "3")           # 容量 2，淘汰最久未用的 a
    assert c.get("a") is None
    assert c.get("b") == "2"
    assert c.get("c") == "3"


def test_lru_access_refreshes_order():
    c = _LRUCache(capacity=2)
    c.set("a", "1")
    c.set("b", "2")
    c.get("a")                # 访问 a，a 变最新
    c.set("c", "3")           # 淘汰 b（最久未用）
    assert c.get("a") == "1"
    assert c.get("b") is None
    assert c.get("c") == "3"


def test_lru_overwrite_value():
    c = _LRUCache(capacity=2)
    c.set("a", "1")
    c.set("a", "2")
    assert c.get("a") == "2"
    assert len(c) == 1


def test_lru_ttl_expiry():
    """防雪崩：条目过期后不再命中（ttl=-1 保证立即过期，避免 ttl=0 的同微秒时序竞态）"""
    c = _LRUCache(capacity=2, default_ttl=600)
    c.set("expired", "1", ttl=-1)
    assert c.get("expired") is None


def test_lru_default_ttl_hit():
    """默认 TTL 内正常命中"""
    c = _LRUCache(capacity=2, default_ttl=600)
    c.set("alive", "1")
    assert c.get("alive") == "1"
