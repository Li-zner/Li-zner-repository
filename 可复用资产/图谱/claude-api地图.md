---
title: "claude-api 文档地图"
created: 2026-09-06
tags:
  - MOC
  - tech/ai-agent
  - evergreen
aliases:
  - claude-api 地图
---

# claude-api 文档地图

官方技能[[AI应用资产/skills/claude-api/SKILL|SKILL.md]]（入口）的库内导航页：多语言示例与共享参考共 50 余篇，本页把它们挂成一棵完整的星，避免散落在图谱外。上游原件升级后按[[AI应用资产/skills/README|skills 留痕说明]]重新处理。

## 核心概念（shared/）

- 模型与能力：[[AI应用资产/skills/claude-api/shared/models|models]]、[[AI应用资产/skills/claude-api/shared/platform-availability|platform-availability]]、[[AI应用资产/skills/claude-api/shared/model-migration|model-migration]]、[[AI应用资产/skills/claude-api/shared/token-counting|token-counting]]、[[AI应用资产/skills/claude-api/shared/error-codes|error-codes]]
- 提示与成本：[[AI应用资产/skills/claude-api/shared/tool-use-concepts|tool-use-concepts]]、[[AI应用资产/skills/claude-api/shared/prompt-caching|prompt-caching]]、[[AI应用资产/skills/claude-api/shared/cost-optimization|cost-optimization]]、[[AI应用资产/skills/claude-api/shared/prompt-audit|prompt-audit]]
- agent 设计：[[AI应用资产/skills/claude-api/shared/agent-design|agent-design]]
- 运行环境：[[AI应用资产/skills/claude-api/shared/anthropic-cli|anthropic-cli]]、[[AI应用资产/skills/claude-api/shared/admin-api|admin-api]]、[[AI应用资产/skills/claude-api/shared/claude-platform-on-aws|claude-platform-on-aws]]、[[AI应用资产/skills/claude-api/shared/live-sources|live-sources]]

## Managed Agents 系列（shared/）

- [[AI应用资产/skills/claude-api/shared/managed-agents-overview|overview]]（先读）、[[AI应用资产/skills/claude-api/shared/managed-agents-core|core]]、[[AI应用资产/skills/claude-api/shared/managed-agents-tools|tools]]、[[AI应用资产/skills/claude-api/shared/managed-agents-environments|environments]]、[[AI应用资产/skills/claude-api/shared/managed-agents-events|events]]
- [[AI应用资产/skills/claude-api/shared/managed-agents-memory|memory]]、[[AI应用资产/skills/claude-api/shared/managed-agents-multiagent|multiagent]]、[[AI应用资产/skills/claude-api/shared/managed-agents-outcomes|outcomes]]、[[AI应用资产/skills/claude-api/shared/managed-agents-webhooks|webhooks]]、[[AI应用资产/skills/claude-api/shared/managed-agents-scheduled-deployments|scheduled-deployments]]
- [[AI应用资产/skills/claude-api/shared/managed-agents-client-patterns|client-patterns]]、[[AI应用资产/skills/claude-api/shared/managed-agents-onboarding|onboarding]]、[[AI应用资产/skills/claude-api/shared/managed-agents-api-reference|api-reference]]、[[AI应用资产/skills/claude-api/shared/managed-agents-self-hosted-sandboxes|self-hosted-sandboxes]]

## 多语言入口与命令行

- README：[[AI应用资产/skills/claude-api/python/claude-api/README|Python]]、[[AI应用资产/skills/claude-api/typescript/claude-api/README|TypeScript]]、[[AI应用资产/skills/claude-api/java/claude-api/README|Java]]、[[AI应用资产/skills/claude-api/go/claude-api/README|Go]]、[[AI应用资产/skills/claude-api/ruby/claude-api/README|Ruby]]、[[AI应用资产/skills/claude-api/csharp/claude-api/README|C#]]、[[AI应用资产/skills/claude-api/php/claude-api/README|PHP]]
- 专项与命令行：[[AI应用资产/skills/claude-api/python/claude-api/sdk-upgrade|sdk-upgrade（Python 1.x 迁移）]]、[[AI应用资产/skills/claude-api/curl/examples|curl/examples]]、[[AI应用资产/skills/claude-api/curl/managed-agents|curl/managed-agents]]
- Managed Agents 语言分册：[[AI应用资产/skills/claude-api/typescript/managed-agents/README|TS]]、[[AI应用资产/skills/claude-api/java/managed-agents/README|Java]]、[[AI应用资产/skills/claude-api/go/managed-agents/README|Go]]、[[AI应用资产/skills/claude-api/ruby/managed-agents/README|Ruby]]、[[AI应用资产/skills/claude-api/php/managed-agents/README|PHP]]

## 各语言功能分册（streaming / tool-use / batches / files-api）

- Python：[[AI应用资产/skills/claude-api/python/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/python/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/python/claude-api/batches|batches]]、[[AI应用资产/skills/claude-api/python/claude-api/files-api|files-api]]
- TypeScript：[[AI应用资产/skills/claude-api/typescript/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/typescript/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/typescript/claude-api/batches|batches]]、[[AI应用资产/skills/claude-api/typescript/claude-api/files-api|files-api]]
- Java：[[AI应用资产/skills/claude-api/java/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/java/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/java/claude-api/files-api|files-api]]
- Go：[[AI应用资产/skills/claude-api/go/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/go/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/go/claude-api/files-api|files-api]]
- Ruby：[[AI应用资产/skills/claude-api/ruby/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/ruby/claude-api/tool-use|tool-use]]
- C#：[[AI应用资产/skills/claude-api/csharp/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/csharp/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/csharp/claude-api/batches|batches]]、[[AI应用资产/skills/claude-api/csharp/claude-api/files-api|files-api]]
- PHP：[[AI应用资产/skills/claude-api/php/claude-api/streaming|streaming]]、[[AI应用资产/skills/claude-api/php/claude-api/tool-use|tool-use]]、[[AI应用资产/skills/claude-api/php/claude-api/batches|batches]]、[[AI应用资产/skills/claude-api/php/claude-api/files-api|files-api]]

## 相关图谱

- [[图谱/MOC-Agent与AI应用|MOC-Agent 与 AI 应用]]（本页在图谱中的挂靠点）
