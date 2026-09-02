"""离线测试用的极简异步 Redis 模拟（单一来源）

实现锁/计数相关方法，语义与真实 Redis 一致：
- set(nx=True, ex=...)  对应 SET NX PX
- eval                   支持两类 Lua：条件删除锁、滑动窗口计数器限流
"""
import time


class FakeRedis:
    def __init__(self):
        self._data = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._data:
            return None
        self._data[key] = value
        return True

    async def get(self, key):
        return self._data.get(key)

    async def setex(self, key, ttl, value):
        # 离线模拟不实现 TTL 过期（单测不跨 TTL 断言），仅落地值
        self._data[key] = value
        return True

    async def delete(self, key):
        self._data.pop(key, None)

    async def incr(self, key):
        self._data[key] = int(self._data.get(key, 0)) + 1
        return self._data[key]

    async def eval(self, script, numkeys, *args):
        # ---- 滑动窗口计数器 Lua（双窗口加权估算）----
        # 当前 Lua 参数形状（2026-08-18 改造）：[limit, prefix, now_arg]，
        # key 由 prefix + 秒数构造；now_arg 为空时用真实时钟（单测总会注入，保持确定性）。
        if "estimate" in script:
            limit = int(args[0])
            prefix = args[1]
            now = float(args[2]) if args[2] else time.time()
            frac = now - int(now)              # 秒内已过比例（与 Lua math.floor 语义一致）
            curr_sec = int(now)
            curr_key = f"{prefix}{curr_sec}"
            prev_key = f"{prefix}{curr_sec - 1}"
            curr = int(self._data.get(curr_key, 0))
            prev = int(self._data.get(prev_key, 0))
            estimate = prev * (1 - frac) + curr
            if estimate < limit:
                self._data[curr_key] = curr + 1
                return 1
            return 0
        # ---- 并发槽位释放 Lua（原子递减下限归零，P0 #41）----
        if "DECR" in script:
            key = args[0]
            v = int(self._data.get(key, 0))
            if v <= 1:
                self._data.pop(key, None)
                return 0
            self._data[key] = v - 1
            return 1
        # ---- CDC leader 锁续期 Lua（校验 token 后 expire）----
        if "expire" in script:
            key, token = args[0], args[1]
            return 1 if self._data.get(key) == token else 0
        # ---- 分布式锁 Lua（条件删除）----
        key, token = args[0], args[1]
        if self._data.get(key) == token:
            self._data.pop(key, None)
            return 1
        return 0
