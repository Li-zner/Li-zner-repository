---
name: mcp-server开发五步法
description: "按五步法新建一个可复用 MCP Server（FastMCP）：能力边界三问 → 原子 Tool 封装 → 配置与依赖治理 → 验证闭环 → 踩坑规避。当任务要求'把 X 能力封装成 MCP / 新建 MCP Server'时使用。"
---

# MCP Server 开发五步法

把「能力」协议化为 MCP Server：一次封装，任何 MCP Host（Claude Desktop / Cursor / 自研网关）生态复用。

## 第 1 步：选型与准备

- SDK：`pip install mcp`，用 `from mcp.server.fastmcp import FastMCP` 高层 API
- Python 3.11+；只装必要的库（redis/asyncpg/httpx 按需）
- 结构：每个 Server 一个目录 = 主文件 + 验证 Client + README + 数据脚本（如有）

## 第 2 步：定义能力边界（最重要，动手前先想清楚）

| 原语 | 是什么 | 何时用 | 反例 |
|---|---|---|---|
| **Tool** | 模型可调用的动作 | 一个原子能力（查天气、检索法条、取锁） | 把「映射→召回→Rerank→生成回答」打成一个黑盒 |
| **Resource** | 模型可读的数据 | 领域知识、覆盖说明、映射表 | 把数据库连接暴露成 Resource |
| **Prompt** | 可复用模板 | 改写、格式化、引导 | 把业务逻辑写进 Prompt |

**边界三问**：
1. 拆开后 Host 还能理解每个动作吗？（能 → 拆；不能 → 合并）
2. 领域知识该下沉吗？（模型不懂你的领域 → 下沉 Server，如法律映射表）
3. 什么不该封装？——**编排逻辑**（多 Agent 圆桌、状态机）留 Host，Server 只给原子能力

## 第 3 步：实现（FastMCP 模式）

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("server-name")

@mcp.tool()
async def my_tool(param: str, top_k: int = 5) -> dict:
    """一句话说清工具做什么 + 何时用。

    Args:
        param: 参数含义（模型靠 docstring 决定怎么用，必须写清楚）。
        top_k: 返回条数，默认 5。
    """
    ...
    return {"results": ...}   # 结构化返回；错误返回 {"error": ...} 而非抛异常

@mcp.resource("scheme://path")   # URI scheme 必须合法：字母开头，仅 [a-zA-Z0-9+-.]
async def a_resource() -> str: ...

@mcp.prompt()
def a_prompt(question: str) -> str: ...

if __name__ == "__main__":
    mcp.run()   # 默认 stdio 传输
```

**参数设计规则**：
- 参数暴露是取舍：`top_k`/`use_rerank` 这类决策权交给 Host；内部策略（阈值、重试）收在 Server 默认值
- docstring 是工具的「说明书」：写清做什么、何时用、参数含义
- 结构化返回（dict/JSON 文本），错误用 `{"error": ...}` 而不是抛异常（Host 更容易容错）

## 第 4 步：配置与依赖治理

- **密钥**：进程环境变量 → `.env` → 安全目录（复用 `_env.py` 零依赖读取器，Server 零配置）
- **自包含**：不要 import 宿主应用的内部模块（会拉起整个应用初始化）——逻辑独立实现
- **数据文件**：相对路径基于 `__file__` 计算（`Path(__file__).resolve().parent...`），不依赖 cwd
- **注入防护**：查询值全部参数化（`$1/$2`）；表名列名来自环境变量（运维可控），不来自用户输入

## 第 5 步：验证闭环（每个 Server 必带）

```python
# test_client.py 固定流程：
# 1. 握手 initialize（确认 protocol 版本）
# 2. list_tools / list_resources / list_prompts（发现能力）
# 3. call_tool / read_resource / get_prompt（真实调用，用真实数据）
# 4. 三条路径必测：正常路径 / 边界路径（守卫命中）/ 参数变体（开关）
```
- 数据脚本（导入/补缺）必须**幂等**（chunk_key/md5 去重），可重复运行
- 真实数据验证：不要只测 mock，连真实库/真实 API 跑一遍
- 纯逻辑（JWT/映射表/令牌桶数学）加 `--self-check` 自检，无外部服务也能验证

## 踩坑清单（MCP 专项，每一条都真实踩过）

1. **URI scheme 合法性**：`civil_code://` 含下划线非法（RFC 3986，scheme 仅 `[a-zA-Z0-9+-.]`），Server 启动即崩
2. **Windows 命名管道**：stdio 依赖父子进程管道，受限环境（沙箱）被拒（WinError 5）——验证 Client 需完整权限终端
3. **子进程 args 绝对路径**：相对路径按父进程 cwd 解析，找不到文件
4. **控制台编码**：GBK 控制台设 `PYTHONIOENCODING=utf-8`；程序内避免打印 emoji
5. **逐条慢操作超时**：批量导入用 executemany + 一次探测，不逐条探测
6. **封装粒度**：黑盒化（一个 tool 吞掉流程）比拆分更危险——Host 失去干预/成本/可观测

## 交付检查

- [ ] 三原语边界三问回答过（Tool 原子、知识下沉、编排留 Host）
- [ ] docstring 写清「做什么/何时用/参数含义」
- [ ] 错误返回体化，不抛异常
- [ ] 自包含 + `_env.py` 读配置 + 数据路径基于 `__file__`
- [ ] test_client.py 四步流程 + 三条路径齐全
- [ ] `--self-check`（纯逻辑部分）可无外部服务运行
- [ ] README：作用 / 能力表 / 为什么这样设计 / 配置 / 运行验证 / 踩坑
