"""可复用机制库：生产机制独立参考实现（全部可 import，见各模块头部资产标记）

中文对照：熔断器=circuit_breaker / 分布式锁=distributed_lock / 限流器=rate_limiter_lua /
singleflight+幂等=singleflight / LRU+TTL缓存=lru_ttl_cache / 原子扣减=atomic_stock /
HMAC防重放=hmac_replay_guard / Token预算=token_budget / 重试退降=retry_backoff /
雪花ID=snowflake_id / 状态机=state_machine
"""
from .circuit_breaker import CircuitBreaker  # noqa: F401
from .distributed_lock import DistributedLock  # noqa: F401
from .lru_ttl_cache import LRUTTLCache  # noqa: F401
from .singleflight import *  # noqa: F401,F403
from .snowflake_id import Snowflake  # noqa: F401
from .state_machine import StateMachine  # noqa: F401
