# 可复用代码

从社区成熟开源实现提炼的并发/稳定性原语, 与本项目代码一一对应。
每个 Python 文件内置一个可运行自检: 直接 `python 文件名` 即可。

| 文件 | 对应本项目代码 | 来源(GitHub/官方) |
|---|---|---|
| 分布式锁+lua.py | app/payment/_ledger.py(_acquire/_release_lock)、app/core/semantic_cache.py(重建锁)、app/cdc/worker.py(leader 锁) | redisson/redisson、redis.io SET NX 模式 |
| 分布式限流器+lua.py | app/middleware/rate_limit.py | cloudflare 双窗口滑动计数、brandur/redis-cell(令牌桶) |
| 熔断器.py | app/middleware/circuit_breaker.py | arlyon/aiobreaker、resilience4j/resilience4j |
| singleflight+幂等.py | app/core/task_manager.py(幂等)、app/routes/v2.py(任务创建)、app/core/semantic_cache.py(重建合并) | golang/sync(singleflight)、stripe 幂等键 |
| LRU+TTL缓存.py | app/core/semantic_cache.py::_LRUCache | tkem/cachetools |

## 对照检查发现的问题

- P0 app/core/db_maintenance.py compress_old_conversations: DELETE 原始记录与 INSERT 摘要是两条独立 autocommit 语句, 中途崩溃丢对话数据。必须包进 conn.transaction()。
- P1 app/middleware/rate_limit.py release_concurrent: GET 后再 DECR 非原子, 并发释放会把计数打成负数。改用本目录"分布式限流器+lua.py"的 CONCURRENT_RELEASE_LUA(原子递减下限归零)。
- P1 app/core/task_manager.py + app/routes/v2.py(1691-1707) 幂等: GET 检查与 SETEX 保存两步竞态, 并发同消息重复建任务、重复消耗 LLM 配额。改用 singleflight+幂等.py 的 claim_idempotency_key(SET NX 原子占位)。
- P1 app/cdc/worker.py _renew_lock: 续期返回值被忽略; 锁过期被其他实例接管后, 本实例仍以 leader 自居继续写, 违反单写者设计。需检查续期返回值, 失败即停止本轮消费。
- P2 app/middleware/circuit_breaker.py: HALF_OPEN 缺单探针门控(全部请求放行试探), 试探失败需重新累计满阈值才回 OPEN。标准行为见本目录"熔断器.py"。
- P2 app/core/cache_warmup.py: 把"正在查询..."占位文案当真实答案写入缓存, 用户可能一直拿到占位文案; 建议改用 SemanticCache.set_empty("__EMPTY__") + is_empty 机制。
- P2 semantic_cache.py 与 cdc/worker.py 各自内联了同一把"锁续期 Lua"(重复), 可收敛到本目录"分布式锁+lua.py"的 RENEW_LUA。

## 说明

- Lua 脚本为社区标准实现原文; 本环境无可用 Redis 实例, 未做真机验证, 接入前请先在测试环境跑通(连接 Redis 后可用各 wrapper 直接冒烟)。
- Python 侧逻辑已全部通过内置自检(见交付报告自测证据)。
