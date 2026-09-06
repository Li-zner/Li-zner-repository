# skills——AI 应用工程相关技能（本地拷贝）

> **用途**：从 Anthropic 官方技能库下载的 AI 应用开发技能原件，供本库 agent 直接加载。
> **来源**：https://github.com/anthropics/skills （2026-09-05 克隆 main 分支拷贝）。
> **何时用**：构建/调试 LLM 应用、开发 MCP Server 时按下表取用。

## 技能索引

| 目录 | 干什么 | 什么时候用 |
|---|---|---|
| `claude-api/` | LLM 应用开发全参考：tool-use、流式、批处理、prompt 缓存、成本优化、**agent 设计**、多语言 SDK 示例（Python/TS/Go/Java/C# 等 30+ 篇文档） | 写任何调 LLM 的代码前查阅；agent 架构设计读 `shared/agent-design.md`，省钱读 `shared/cost-optimization.md` |
| `mcp-builder/` | MCP Server 开发全套：最佳实践（`reference/mcp_best_practices.md`）、Python/Node 双栈参考实现、**评测脚本与评测集样例** | 开发新 MCP Server 时——与本库 `skill/mcp-server开发五步法` 互补：那边是流程方法论，这边是官方最佳实践细则与评测工具 |
| `12-factor-agents/` | 生产 agent 十二条军规原文（HumanLayer）：确定性代码为主、Own your prompts/context、人确认工具化、agent=无状态 reducer 等 | 设计 agent 架构时对照 `../agent全链路.md` 第 0.5 节读原文（`content/factor-01` 至 `factor-12`，零填充编号为完整版） |

## 与本库既有资产的关系（不重复，互相引用）

- `skill/AI Agent 开发全流程/SKILL.md`：agent 开发的工程纪律与阶段流程（本库原创）；
- `skill/mcp-server开发五步法/SKILL.md`：MCP 开发的五步流程（本库原创）；
- `MCP sever/`：已沉淀的可复用 MCP Server 实现代码；
- 本目录：官方的 API 细节参考与评测工具——写代码时查这里，定流程时看上面三个。

## 使用方式

1. claude-api：按语言进对应子目录，`claude-api/README.md` 是入口；跨语言通用的主题在 `shared/`。
2. mcp-builder：先读 SKILL.md，开发时对照 `reference/mcp_best_practices.md`，
   完成后用 `scripts/evaluation.py` + `example_evaluation.xml` 的格式写评测集自测。
3. mcp-builder 的 `scripts/requirements.txt` 声明了脚本依赖，使用前先安装。

## 本地修改留痕（与上游的差异）

按本库规范（全库禁 emoji、LF 换行）做了两处机械处理：

1. 换行符 CRLF 统一转为 LF；
2. 剥离 emoji 共 22 处（散布于 SKILL.md 与各参考文档，全部为装饰性字符）。

12-factor-agents 附加说明：

- 上游为文档型仓库（正文 content/ 共 30 篇 + 图片 41MB），**img/ 图片未随库**（体量不成比例），
  正文内相对图片链接会失效，看图请到上游 https://github.com/humanlayer/12-factor-agents ；
- 仅拷贝 README、LICENSE、content/ 三部分；上游的 packages/workshops/drafts/hack 未随库；
- 许可为双许可：正文内容 CC BY-SA 4.0、示例代码 Apache-2.0（见目录内 LICENSE），再分发时一并携带。

除上述机械清理外未改动任何语义内容；升级上游版本后需重做同样处理。

## 许可

各目录内 LICENSE.txt 为上游附带许可（mcp-builder 为 MIT），随目录原样保留；再分发时一并携带。
