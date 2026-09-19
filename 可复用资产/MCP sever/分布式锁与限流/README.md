# 分布式锁与限流（MCP Server 001）

## 作用

把 agent_gateway 生产验证过的「多实例并发安全」原语封装为 MCP 原子工具，任何需要
互斥、防抖、成本控制的 Host（Agent 应用/脚本/其他 MCP Host）可直接调用。

## 能力

| 工具 | 作用 | 对应项目出处 |
|------|------|------|
| `acquire_lock` | 获取分布式锁（SET NX EX + 唯一 token） | `app/payment/service.py` `_acquire_lock` |
| `release_lock` | 释放锁（Lua 值匹配才删，防误删他人锁） | 同上 `_release_lock`（真 bug 修复版） |
| `fixed_window_limit` | 固定窗口限流（INCR + 首次设过期） | `app/middleware/rate_limit.py` |
| `sliding_window_limit` | 滑动窗口限流（ZSET，无边界双倍流量） | 生产级模板 1 |
| `token_bucket_limit` | 令牌桶限流（Lua 原子，允许突发） | 生产级模板 2 |

## 为什么这样设计

- **锁必须带 token + Lua 释放**：分布式锁三个坑（死锁/误删/过期）里「误删他人锁」是
  踩过的真 bug——先 get 再 delete 有窗口，Lua 让检查+删除原子化。
- **TTL 是上限不是下限**：锁过期时间必须大于最长临界区操作；操作可能超时需看门狗续期
  （模板见 harness 参考）。
- **三种限流按场景选**：固定窗口最省（1 个 key），但窗口边界可能双倍流量；滑动窗口精确；
  令牌桶适合聊天这种「允许突发、平均受限」的场景。
- **锁粒度 = 并发度**：锁 key 越细（订单级而非用户级）并发越高，按业务取舍。

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `REDIS_URL` | `redis://localhost:6379` | Redis 连接串 |

## 运行与验证

```bash
pip install mcp redis

python redis_lock_ratelimit_server.py            # 拉起 Server（stdio）
python redis_lock_ratelimit_server.py --self-check   # 纯逻辑自检（无需 Redis）
python test_client.py                            # 完整验证（需 Redis）
```

## 踩坑提示

- Windows 沙箱/受限环境无法创建命名管道（WinError 5）——这是协议特性，验证 Client
  需在完整权限终端运行；`--self-check` 不受影响。
- 多实例部署时所有实例必须连同一个 Redis，计数/锁才全局一致。
