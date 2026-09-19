# 可复用 MCP Server 资产库

> 从 agent_gateway 生产实战沉淀的可复用 MCP Server。每个 Server 一个子目录：
> 代码自包含（只依赖 `_env.py` 读配置）+ 验证 Client + README。
> 规则与五步法见 `harness/03-参考-项目经验/` 与 agent_gateway `mcp_assets/MCP指南.md`。

## 资产清单

| # | 目录（按作用命名） | 能力 | 依赖 | 验证 |
|---|------|------|------|------|
| 001 | `分布式锁与限流` | Redis 分布式锁（Lua 原子释放）+ 固定窗口/滑动窗口/令牌桶限流 | mcp, redis | `test_client.py` |
| 002 | `知识库双通道检索` | pg_trgm 关键词 + 向量双通道召回（通用表结构配置化） | mcp, asyncpg | `test_client.py` |
| 003 | `语义缓存` | L0 LRU → L1 哈希 → L2 pg_trgm 三级语义缓存 | mcp, asyncpg | `test_client.py` |
| 004 | `JWT令牌签发与校验` | HS256 签发/校验/刷新轮换 + jti 吊销黑名单（纯标准库） | mcp, redis(可选) | `--self-check` + `test_client.py` |
| 005 | `模型归因纠错映射` | 关键词 → 权威答案/引导 映射表，LLM 前拦截（零依赖） | mcp | `--self-check` + `test_client.py` |

## 运行前提

- Python 3.11+，按 Server 需要 `pip install mcp redis asyncpg`
- 配置：进程环境变量 → 本目录 `.env` → WSL 安全目录（`_env.py` 自动读取，零配置）
- Windows 注意：MCP stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5）；
  验证 Client 需在完整权限终端运行；`--self-check` 纯逻辑自检不受此限制

## 使用方式

```bash
# 拉起任意 Server（stdio 传输），供任何 MCP Host 连接
python "MCP sever/分布式锁与限流/redis_lock_ratelimit_server.py"

# 自带验证 Client（握手 → 列工具 → 真实调用）
python "MCP sever/分布式锁与限流/test_client.py"

# 纯逻辑自检（JWT / 映射表 / 令牌桶数学，不需要管道与外部服务）
python "MCP sever/JWT令牌签发与校验/jwt_token_service_server.py" --self-check
```

## 封装纪律（为什么这样拆）

- Tool = 原子动作；领域知识（映射表/阈值）下沉 Server；编排逻辑留 Host
- 错误返回 `{"error": ...}` 而非抛异常；docstring 写清「做什么、何时用、参数含义」
- 内部策略（阈值/重试）收默认值，`top_k` 这类决策权暴露给 Host
- 密钥不出 Server 边界；远程部署必须鉴权

## 组合使用

这 5 个 Server 可与 `../skill/` 流程技能组合成验收工具：见
`../AI应用验收工具/`（五阶段验收：认证→并发限流→语义缓存→知识检索→纠错映射，
`python verify_all.py` 一键执行）。
