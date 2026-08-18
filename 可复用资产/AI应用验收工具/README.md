# AI 应用验收工具（MCP + skill 组合）

> 本工具是《Vibe-Coding Practical Handbook》5.3「MCP + skills 怎么组合」的落地实例：
> **MCP 是插座，skills 是操作规程**。MCP 提供能力，skill 约束步骤、风格和安全边界。
>
> - MCP 解决「Agent 能调用什么工具」→ 见 `../MCP sever/`（5 个能力 Server）
> - skill 解决「Agent 应该按什么流程调用工具」→ 本目录 `SKILL.md`（五阶段验收流程）
> - 一键执行 → 本目录 `verify_all.py`（把流程固化成可运行检查）

## 组合设计三问（来自 5.3 原文）

| 问题 | 本工具的回答 |
|------|-------------|
| 1. Agent 需要什么外部能力？ | 认证（JWT）、并发与限流（锁）、语义缓存、知识检索、纠错映射——5 个 MCP Server，见 `../MCP sever/` |
| 2. 这些能力的权限边界是什么？ | 验收数据一律用 `__accept__` 前缀的测试 key；不写生产数据；密钥不出 Server 边界；无 Redis/PG 的阶段自动 SKIP 并说明 |
| 3. 调用这些能力的流程是否已稳定可固化？ | 五阶段验收流程在 agent_gateway 测试清单中已稳定（认证→并发→缓存→检索→纠错），固化为 `SKILL.md`，可重复执行 |

## 为什么这样组合（对照 5.3 例子）

只用 MCP：Agent 有 5 个能力，但不知道按什么顺序验收、通过标准是什么。
只用 skill：Agent 知道验收流程，但没有工具只能靠已有上下文，验不了真实系统。
**MCP + skill**：skill 规定每阶段调哪个工具、断言什么、通过标准；工具补能力，skill 补顺序——降低工具误用概率。

## 结构

```
AI应用验收工具/
├── README.md          # 本文件：组合设计与使用说明
├── SKILL.md           # 验收流程 skill（复制到任意 skills 目录即用）
└── verify_all.py      # 一键验收执行器（流程的可运行检查，含 --phase/--skip-pg）
```

## 使用方式

```bash
# 一键跑全部五阶段
python verify_all.py

# 只跑某阶段（如认证）
python verify_all.py --phase 1

# 无 PostgreSQL 环境时跳过阶段 3/4
python verify_all.py --skip-pg
```

退出码：0 = 全部通过；1 = 存在失败；2 = 全部跳过（无基础设施）。

## 前置条件

- Python 3.11+，`pip install mcp`（阶段 3/4 另需 `asyncpg`，阶段 2 另需 `redis`）
- 阶段 1 需配置 `JWT_SECRET`；阶段 3/4 需 PostgreSQL；阶段 2 需 Redis
- 依赖的 5 个 Server 位于同级 `../MCP sever/`（本工具自动定位；整体搬移时保持两个文件夹的相对位置）

## 阶段与通过标准速览（详见 SKILL.md）

| 阶段 | 能力来源 | 验证点 |
|------|---------|--------|
| 1 认证 | `JWT令牌签发与校验` | 签发/验证往返、篡改拒绝、刷新轮换、吊销生效 |
| 2 并发限流 | `分布式锁与限流` | 锁互斥、他人 token 释放被拒、限流超限拒绝 |
| 3 语义缓存 | `语义缓存` | 写入、L0 命中、近似问法语义命中、无关问法 miss |
| 4 知识检索 | `知识库双通道检索` | 真实查询返回结果、非法入参报错 |
| 5 纠错映射 | `模型归因纠错映射` | 添加、contains 命中、清理（不残留测试数据） |
