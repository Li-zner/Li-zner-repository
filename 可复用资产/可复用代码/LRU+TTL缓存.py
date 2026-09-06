"""进程内 LRU + TTL 缓存(O(1) 读写)—— 社区标准实现可复用版

对应本项目: app/core/semantic_cache.py::_LRUCache(L0 热缓存)。
来源(GitHub): https://github.com/tkem/cachetools (TTLCache/LRUCache 的 OrderedDict 实现)

要点:
- OrderedDict move_to_end/popitem 实现 O(1) 的 LRU 提升与淘汰。
- 过期惰性删除(读时检查), 省一个后台线程; 容量满时淘汰最久未使用。
- time_fn 可注入时钟, 测试零等待。
"""
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class LRUTTLCache:
    """固定容量 + 条目过期的进程内缓存(单事件循环内使用, 无需加锁)

    Args:
        capacity: 最大条目数, 满后淘汰最久未使用。
        default_ttl: 默认过期秒数; set() 可按条目覆盖。
        time_fn: 时钟函数(默认 time.monotonic; 测试可注入假时钟)。
    """

    def __init__(self, capacity: int = 256, default_ttl: float = 600.0,
                 time_fn: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须 >= 1, 当前为 {capacity}")
        self._cache: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()
        self._capacity = capacity
        self._default_ttl = default_ttl
        self._time = time_fn

    def get(self, key: str) -> Optional[Any]:
        """命中返回值并提升为最近使用; 过期/未命中返回 None(过期即淘汰)"""
        item = self._cache.get(key)
        if item is None:
            return None
        value, expire_at = item
        if self._time() >= expire_at:
            self._cache.pop(key, None)  # 惰性过期: 读时才删
            return None
        self._cache.move_to_end(key)  # O(1) 提升为最近使用
        return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """写入(存在则覆盖并视为最近使用); ttl 缺省用 default_ttl"""
        if ttl is None:
            ttl = self._default_ttl
        self._cache[key] = (value, self._time() + ttl)
        self._cache.move_to_end(key)
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)  # 淘汰最久未使用

    def __len__(self) -> int:
        return len(self._cache)


def _self_check() -> None:
    """最小自检: LRU 淘汰顺序、TTL 过期(注入时钟, 零等待)"""
    t = {"now": 100.0}

    def clock() -> float:
        return t["now"]

    c = LRUTTLCache(capacity=2, default_ttl=10, time_fn=clock)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1 and len(c) == 2, "命中应返回值"
    c.set("c", 3)  # 满员: b 最久未使用, 被淘汰
    assert c.get("b") is None, "LRU 应淘汰最久未使用的 b"
    assert c.get("a") == 1 and c.get("c") == 3
    t["now"] += 11  # 全部过期
    assert c.get("a") is None, "过期项应惰性清除"
    assert c.get("c") is None
    print("LRU+TTL缓存 自检通过")


if __name__ == "__main__":
    _self_check()
