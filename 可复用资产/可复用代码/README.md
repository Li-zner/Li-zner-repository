# 可复用代码

从社区成熟开源实现提炼的后端通用机制库(并发 / 稳定性 / 电商 / 网关 / AI 预算), 分两部分:
一为原库机制(对应旧项目代码沉淀), 二为通用机制补充(无旧项目对应, 按场景直接取用)。
每个 Python 文件内置一个可运行自检: 直接 `python 文件名` 即可。

## 一、原库机制(对应旧项目沉淀)

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

## 二、通用机制补充(2026-09, 按场景直用)

| 文件 | 适用场景 | 来源(GitHub/官方) |
|---|---|---|
| 重试+退避+降级.py | 电商支付/短信回调、APP 推送、网关转发、LLM API 调用的临时失败恢复 | jd/tenacity、AWS 退避与抖动、resilience4j(Fallback) |
| 雪花ID.py | 电商订单号、网关 trace_id、消息去重键、分库分表主键 | twitter-archive/snowflake、bwmarrin/snowflake |
| HMAC签名+防重放.py | 网关/开放 API 请求验签、支付与 Webhook 回调验签 | stripe/stripe-python(Webhook 签名)、AWS SigV4 |
| 原子扣减-防超卖.py | 电商秒杀库存、APP 余额配额、AI token 配额的原子扣减 | redis.io 原子 DECR 模式、MySQL 条件更新乐观锁 |
| Token预算守卫.py | AI 后端按用户/租户的 token 日预算与单次限额 | BerriAI/litellm(budget)、openai/tiktoken |
| 状态机.py | 电商订单流转、任务审批流、AI 任务生命周期的合法迁移控制 | fgmacedo/python-statemachine、statelyai/xstate |

### 补充部分约定

- 全部仅依赖标准库; 每个文件内置可运行自检, `python 文件名` 即验证(2026-09-05 全部通过)。
- 涉及 Redis/DB 的文件同时给出「生产 Lua/SQL」与「进程内模拟实现」: 自检跑模拟,
  接入生产前先真机冒烟(与原库说明一致)。
- 场景速查: 秒杀超卖 → 原子扣减; 回调总失败 → 重试退避; 订单乱流 → 状态机;
  外部刷接口 → HMAC 验签; LLM 花费失控 → Token 预算守卫; 需要全局唯一号 → 雪花 ID。

## 说明

- Lua 脚本为社区标准实现原文; 本环境无可用 Redis 实例, 未做真机验证, 接入前请先在测试环境跑通(连接 Redis 后可用各 wrapper 直接冒烟)。
- Python 侧逻辑已全部通过内置自检(见交付报告自测证据)。

## 图谱入口

- 主题图谱：[[图谱/MOC-生产机制与稳定性|MOC-生产机制与稳定性]]（机制四层落点总表）
- 本目录机制的生产封装：[[MCP sever/README|MCP sever]]（分布式锁与限流 / 语义缓存）
- 上线前对照：[[skill/生产机制自查/SKILL|生产机制自查 24 项]]、[[skill/面试手写八段训练/SKILL|面试手写八段训练]]
- 相邻主题：[[图谱/MOC-MCP生态|MOC-MCP 生态]]、[[图谱/MOC-安全与密钥|MOC-安全与密钥]]（HMAC 验签）、[[图谱/MOC-总览|MOC-总览]]


## Import 用法（2026-09-06 import 化完成）

已重命名为英文 snake_case 模块并新增 `__init__.py`，本目录现在是一个**可直接 import 的包**：

```python
import sys; sys.path.insert(0, "可复用资产/可复用代码")
from circuit_breaker import CircuitBreaker   # 熔断器
from distributed_lock import DistributedLock # 分布式锁
from snowflake_id import Snowflake           # 雪花ID
from lru_ttl_cache import LRUTTLCache        # LRU+TTL 缓存
```

### 中文对照表（旧名 → 现模块名）

| 旧中文名 | 现模块 | 主类 |
|---|---|---|
| 熔断器 | `circuit_breaker.py` | CircuitBreaker |
| 分布式锁+lua | `distributed_lock.py` | DistributedLock |
| 分布式限流器+lua | `rate_limiter_lua.py` | LUA 脚本集 |
| singleflight+幂等 | `singleflight.py` | singleflight 原语 |
| LRU+TTL缓存 | `lru_ttl_cache.py` | LRUTTLCache |
| 原子扣减-防超卖 | `atomic_stock.py` | StockHolder / LUA |
| HMAC签名+防重放 | `hmac_replay_guard.py` | SignatureVerifier |
| Token预算守卫 | `token_budget.py` | TokenBudget |
| 重试+退避+降级 | `retry_backoff.py` | 重试原语 |
| 雪花ID | `snowflake_id.py` | Snowflake |
| 状态机 | `state_machine.py` | StateMachine |

依赖：标准库为主；`distributed_lock` / `rate_limiter_lua` 需 redis-py。
冒烟：`python -c "import sys; sys.path.insert(0,'.'); from circuit_breaker import CircuitBreaker"`。

## 接线范本（examples/）

- `examples/demo_gateway.py`——把本库模块接成完整后端的最小网关：门禁序（QPS/槽位→语义缓存→防击穿锁）→ 业务（熔断器包裹）→ 计量 → finally 清理。运行自检：`python examples/demo_gateway.py selfcheck`。**新项目先读它再动手**，结构照抄、业务替换。
