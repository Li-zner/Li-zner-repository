---
name: AI应用验收
description: "按五阶段流程验收 AI 应用（认证→并发限流→语义缓存→知识检索→纠错映射），每阶段调用配套 MCP 工具并断言通过标准。当任务涉及'验收/联调/上线前检查/AI 应用测试'时使用。"
---

# AI 应用验收（MCP + skill 组合流程）

组合原理：**MCP 提供能力，skill 约束步骤、风格和安全边界**。本流程调用
`../MCP sever/` 下的 5 个 Server，按固定顺序验收，每阶段有明确通过标准。

## 何时使用

- 新 AI 应用上线前验收（认证/并发/缓存/检索/纠错五件套）
- 变更后回归（改了一个 Server 或配置，重跑对应阶段）
- 面试/演示前确认能力可用（`python verify_all.py` 一跑见真章）

## 五阶段流程

### 阶段 1：认证验收（JWT令牌签发与校验）

调用工具：`create_access_token` → `verify_token` → `create_refresh_token` → `refresh_access_token` → `revoke_token`

步骤与通过标准：
1. 签发 access（subject 用 `__accept__` 前缀）→ 返回 token
2. 验证 → `"valid": true`
3. 篡改 token（改末尾字符）→ 返回 `"error"`（验签必须拒绝）
4. 签发 refresh → 刷新轮换 → 返回新 access_token（轮换防重放）
5. 吊销 token → 再验证 → `"error"`（吊销生效）
6. 边界：空 subject / 空 token → `"error"`

### 阶段 2：并发与限流验收（分布式锁与限流）

调用工具：`acquire_lock` → `release_lock` → `fixed_window_limit`

步骤与通过标准：
1. 加锁（key 用 `__accept__` 前缀，ttl=10）→ `"acquired": true` 且返回 token
2. 用**错误 token** 释放 → `"released": false`（Lua 持有者校验必须生效，防误删他人锁）
3. 用**正确 token** 释放 → `"released": true`
4. 固定窗口 limit=2：前 2 次放行，第 3 次 `"allowed": false`（限流生效）
5. 边界：空 key / ttl<=0 → `"error"`

### 阶段 3：语义缓存验收（语义缓存，需 PostgreSQL）

调用工具：`cache_store` → `cache_lookup`

步骤与通过标准：
1. 写入（query 用 `__accept__` 前缀）→ `"stored": true`
2. 同一问法查 → `"hit": true` 且 level 为 L0（进程内命中）
3. 近似问法 + 阈值 0.1 查 → `"hit": true`（语义命中，验证 pg_trgm）
4. 完全无关问法 → `"hit": false`（不误命中）
5. 边界：空 query → `"error"`

### 阶段 4：知识检索验收（知识库双通道检索，需 PostgreSQL）

调用工具：`search_knowledge`

步骤与通过标准：
1. 真实查询（top_k=3）→ 返回 `"total"` 字段且无 `"error"`（召回通道可用）
2. 参数变体：限定 `sources` → 正常返回（过滤生效）
3. 边界：空 query / top_k=0 → `"error"`（守卫生效）

### 阶段 5：纠错映射验收（模型归因纠错映射）

调用工具：`add_mapping` → `lookup_redirect` → `remove_mapping`

步骤与通过标准：
1. 添加测试映射（trigger 用 `__accept__` 前缀）→ `"added": true`
2. 包含触发词的文本 → `"hit": true`（contains 生效）
3. 幂等更新（同 trigger+mode 再添加）→ `"updated": true` 且不新增条目
4. 完全无关文本 → `"hit": false`
5. 边界：空 text / 空 answer → `"error"`
6. **清理**：删除测试映射 → `"removed": true`（验收不留残留）

## 一键执行

```bash
python verify_all.py             # 全部阶段
python verify_all.py --phase N   # 单阶段
python verify_all.py --skip-pg   # 无 PostgreSQL 时跳过阶段 3/4
```

退出码：0 全通过 / 1 有失败 / 2 全部跳过。

## 安全边界（必须遵守）

- 验收数据一律用 `__accept__` 前缀的测试 key，阶段 5 验收后必须清理
- 不写任何生产业务数据；密钥不出 Server 边界（Host 不持有 API Key）
- 无基础设施（Redis/PG/未配 JWT_SECRET）的阶段：SKIP 并说明原因，不假装通过
- 依赖 Server 位于同级 `../MCP sever/`，搬移时保持相对位置

## 失败处置

1. 某阶段 FAIL → 按 `避坑检查清单` 技能逐条对照（Redis/PG 连接、编码、配置漂移等）
2. 修完重跑对应阶段（`--phase N`），不要重跑全部浪费 token
3. 连续 FAIL 且非环境问题 → 检查该 Server 的 test_client.py 单独复现
4. 流程本身有问题（顺序/标准不合理）→ 先手动执行几次，稳定后再改本文件（5.3 原文：
   「如果流程未稳定，不要立即写 skill，手动执行几次再固化」）
