# 个人 MCP 资产库（mcp_assets）

> 个人 AI 能力资产库：把"能力"协议化为 MCP Server，一次封装，生态复用。
> 每个 Server 一个子目录：代码自包含（不依赖项目内部模块，只复用根目录 `_env.py` 读配置）+ 验证 Client。

## 资产清单

| # | Server | 能力 | 状态 | 位置 |
|---|--------|------|------|------|
| 001 | `civil-code-rag` | 民法典知识检索（法律误区守卫 → 口语映射 → 双通道召回 → Rerank）；三原语齐全：Tool `search_civil_code` + Resource `civil://coverage`/`civil://law_mapping` + Prompt `legal_query_rewriter` | ✅ 可用 | `servers/civil-code-rag/` |
| 002 | `weather-demo` | 高德实时天气（真实数据已验证）+ 服务器时间 | ✅ 可用 | `servers/weather-demo/` |

## 使用方式

```bash
# 拉起任意 Server（stdio 传输），供任何 MCP Host（Claude Desktop / Cursor / 自研网关）连接
python mcp_assets/servers/civil-code-rag/civil_code_server.py
python mcp_assets/servers/weather-demo/weather_server.py

# 自带验证 Client（握手 → 列工具 → 调用）
python mcp_assets/servers/civil-code-rag/test_client.py
python mcp_assets/servers/weather-demo/quick_client.py
```

## 运行前提

- Python 3.11+，依赖：`mcp`、`httpx`、`asyncpg`（仅检索类 Server）
- 配置：根目录 `_env.py` 自动读取（进程环境变量 → 项目 .env → WSL 安全目录），无需手动配 key
- Windows 注意：MCP stdio 依赖命名管道，受限环境（沙箱）会被拒；子进程 args 用绝对路径

## 知识库数据修复记录（2026-08）

**发现**：`knowledge_chunks` 表 source='civil_code' 原 861 条只覆盖第一~四编，
第五编（婚姻家庭）/ 第六编（继承）/ 第七编（侵权责任）整编缺失。

**根因**：原构建脚本条号正则 `第[一二三四五六七八九十百]+条` 缺"千""零"字符，
`第一千零四十条` 及之后全部无法匹配 → 千位以上条号法条漏导。

**修复**：`servers/civil-code-rag/import_civil_books.py`（正则修复 + 全量幂等补缺），
docx 1260 条法条全量入库，重复 chunk 已清理（1263 → 1260，与 docx 精确一致）。

**待办**：402 条新数据 embedding 为 NULL（Ollama:11434 当前未运行，不影响检索——
pg_trgm + ILIKE + Rerank 均不用向量）。Ollama 启动后运行
`servers/civil-code-rag/import_civil_books.py` 的 embedding 补全即可（脚本幂等）。
