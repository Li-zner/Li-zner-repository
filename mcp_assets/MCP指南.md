# MCP 使用规则与建立方法（实战总结）

> 基于本资产库实战（weather-demo + civil-code-rag 两个 Server 全流程）归纳。
> 适用：用 Python mcp SDK（FastMCP 高层 API）建立与使用 MCP Server。

---

## 一、建立方法（Server 开发者视角，五步法）

### 第 1 步：选型与准备
- SDK：`pip install mcp`（本环境 1.26.0），用 `from mcp.server.fastmcp import FastMCP` 高层 API
- 环境：Python 3.11+；只装必要的库（httpx/asyncpg 等按需）
- 结构：每个 Server 一个目录 = 主文件 + 验证 Client + 数据脚本（参考 `mcp_assets/servers/*`）

### 第 2 步：定义能力边界（最重要，动手前先想清楚）
| 原语 | 是什么 | 何时用 | 反例 |
|---|---|---|---|
| **Tool** | 模型可调用的动作 | 一个原子能力（查天气、检索法条） | ❌ 把"映射→召回→Rerank→生成回答"打成一个黑盒 |
| **Resource** | 模型可读的数据 | 领域知识、覆盖说明、映射表 | ❌ 把数据库连接暴露成 Resource |
| **Prompt** | 可复用模板 | 改写、格式化、引导 | ❌ 把业务逻辑写进 Prompt |

**边界三问**：
1. 这个能力拆开后，Host 还能理解每个动作吗？（能 → 拆；不能 → 合并）
2. 领域知识该下沉吗？（模型不懂你的领域 → 下沉 Server，如法律映射表）
3. 什么不该封装？——**编排逻辑**（多 Agent 圆桌、状态机）留在 Host，Server 只给原子能力

### 第 3 步：实现（FastMCP 模式）
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
    return {"results": ...}   # 结构化返回；错误返回 {"error": "..."} 而非抛异常

@mcp.resource("scheme://path")   # URI scheme 必须合法：字母开头，仅 [a-zA-Z0-9+-.]
async def a_resource() -> str: ...

@mcp.prompt()
def a_prompt(question: str) -> str: ...

if __name__ == "__main__":
    mcp.run()   # 默认 stdio 传输
```

**参数设计规则**：
- 参数暴露是取舍：`top_k`/`use_rerank` 这类决策权交给 Host；内部策略（阈值、重试）收在 Server 默认值
- docstring 是工具的"说明书"，写清：做什么、何时用、参数含义
- 结构化返回（dict/JSON 文本），错误用 `{"error": ...}` 而不是抛异常（Host 更容易容错）

### 第 4 步：配置与依赖治理
- **密钥**：进程环境变量 → `.env` → 安全目录（本项目复用根目录 `_env.py`，Server 零配置）
- **自包含**：不要 import 宿主应用的内部模块（会拉起整个应用初始化：连接池/日志/配置）——检索逻辑独立实现，只复用零依赖的配置读取器
- **数据文件**：相对路径要基于 `__file__` 计算（`Path(__file__).resolve().parent...`），不依赖 cwd

### 第 5 步：验证闭环（每个 Server 必带）
```python
# test_client.py 固定流程：
# 1. 握手 initialize（确认 protocol 版本）
# 2. list_tools / list_resources / list_prompts（发现能力）
# 3. call_tool / read_resource / get_prompt（真实调用，用真实数据）
# 4. 三条路径必测：正常路径 / 边界路径（守卫命中）/ 参数变体（开关）
```
- 数据脚本（导入/补缺）必须**幂等**（chunk_key/md5 去重），可重复运行
- 真实数据验证：不要只测 mock，连真实库/真实 API 跑一遍

---

## 二、使用规则（Host / Client 视角）

### 1. 传输选型
| 传输 | 场景 | 特点 |
|---|---|---|
| **stdio** | 本地开发、本机 Host | 子进程 + 标准输入输出；拉起即用 |
| **Streamable HTTP** | 远程生产、跨机 | HTTP 长连接 + SSE；需要鉴权（OAuth） |

### 2. 会话流程（Client 四步）
```
stdio_client(ServerParams) → ClientSession
  1. initialize()          握手（协议版本协商）
  2. list_tools()          发现能力（运行时发现，不预先写死）
  3. call_tool(name, args) 调用（args 必须匹配 schema）
  4. 退出上下文管理器      自动关闭
```

### 3. 调用纪律
- 参数必须匹配工具 schema（模型用错参数 → 工具返回校验错误）
- 工具返回结构化数据，错误在返回体里（`{"error": ...}`），Client 按返回体容错
- 长任务/流式：按工具约定处理（如 SSE 或轮询），不要无限等待

### 4. 安全规则
- **密钥不出 Server 边界**：Host 永远不持有你的 API Key；Server 是唯一可信边界
- 远程部署必须鉴权（OAuth 2.1 for MCP），否则任何能连到你端口的人都能调用你的工具（= 烧你的钱）
- 工具描述/名称不要泄露内部实现细节

### 5. 生态规则
- 写一次，Claude Desktop / Cursor / 自研网关任何 MCP Host 通用（这就是协议的价值）
- 用 `mcp dev` 拉起带 Inspector 调试；`mcp install` 直接装进 Claude Desktop

---

## 三、踩坑清单（本项目实测，详见 docs/踩坑与陷阱总汇.md 第七类）

1. **URI scheme 合法性**：`civil_code://` 含下划线非法（RFC 3986，scheme 仅 `[a-zA-Z0-9+-.]`），Server 启动即崩
2. **Windows 命名管道**：stdio 依赖父子进程管道，受限环境（沙箱）被拒（WinError 5）
3. **子进程 args 绝对路径**：相对路径按父进程 cwd 解析，找不到文件
4. **控制台编码**：GBK 控制台设 `PYTHONIOENCODING=utf-8`；程序内避免打印 emoji
5. **逐条慢操作超时**：批量导入用 executemany + 一次探测，不逐条探测
6. **封装粒度**：黑盒化（一个 tool 吞掉流程）比拆分更危险——Host 失去干预/成本/可观测

---

## 四、面试话术（30 秒版）

> "MCP 是把 AI 能力协议化的标准：Host（应用）通过 Client 连接 Server，用 Tool 执行动作、Resource 读数据、Prompt 复用模板。我把网关的 RAG 检索封装成了 MCP Server——封装时做的三个决策是：检索拆成原子 Tool 而不是黑盒流程、法律映射等领域知识下沉 Server、Rerank 阈值等策略收在 Server 内部只暴露 top_k 参数。过程中还发现并修复了知识库构建脚本的正则缺陷。"
