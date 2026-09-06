---
title: "MOC-Agent与AI应用"
created: 2026-09-06
tags:
  - MOC
  - tech/ai-agent
  - evergreen
aliases:
  - Agent 图谱
  - AI 应用图谱
---

# MOC-Agent 与 AI 应用

做 agent / AI 应用项目的主题图谱：链路方法论 → 官方参考原文 → 选型 → 可复用能力。安全视角单独见 [[图谱/MOC-安全与密钥|MOC-安全与密钥]]（首要考虑项）。

## 链路方法论

- [[AI应用资产/agent全链路|agent 全链路]]：设计原则十二条（安全置顶）→ 架构 → 工具 → 记忆 → 护栏 → 评测 → 部署 → 可观测
- [[AI应用资产/安全审计|AI 应用安全审计]]：威胁模型 + 分层检查单（先于功能）
- [[skill/AI Agent 开发全流程/SKILL|AI Agent 开发全流程（skill）]]：七阶段工程纪律（Grill / ADR / TDD / 验收 / 收敛）

## 设计原则原文

- [[AI应用资产/skills/12-factor-agents/README|12-factor-agents（HumanLayer 原文整包）]]：生产 agent 十二条军规
  - 背景：[[AI应用资产/skills/12-factor-agents/content/brief-history-of-software|brief-history-of-software]]、附录：[[AI应用资产/skills/12-factor-agents/content/appendix-13-pre-fetch|Factor 13 Pre-fetch]]
  - 军规原文：[[AI应用资产/skills/12-factor-agents/content/factor-01-natural-language-to-tool-calls|01 自然语言转工具调用]]、[[AI应用资产/skills/12-factor-agents/content/factor-02-own-your-prompts|02 Own your prompts]]、[[AI应用资产/skills/12-factor-agents/content/factor-03-own-your-context-window|03 Own your context window]]、[[AI应用资产/skills/12-factor-agents/content/factor-04-tools-are-structured-outputs|04 工具即结构化输出]]、[[AI应用资产/skills/12-factor-agents/content/factor-05-unify-execution-state|05 统一执行状态]]、[[AI应用资产/skills/12-factor-agents/content/factor-06-launch-pause-resume|06 启动暂停恢复]]
  - [[AI应用资产/skills/12-factor-agents/content/factor-07-contact-humans-with-tools|07 用工具触达人类]]、[[AI应用资产/skills/12-factor-agents/content/factor-08-own-your-control-flow|08 Own your control flow]]、[[AI应用资产/skills/12-factor-agents/content/factor-09-compact-errors|09 错误折叠进上下文]]、[[AI应用资产/skills/12-factor-agents/content/factor-10-small-focused-agents|10 小而专]]、[[AI应用资产/skills/12-factor-agents/content/factor-11-trigger-from-anywhere|11 随处触发]]、[[AI应用资产/skills/12-factor-agents/content/factor-12-stateless-reducer|12 无状态 reducer]]
- [[AI应用资产/skills/claude-api/SKILL|claude-api 官方技能（总入口）]]：LLM 应用开发全参考
- [[图谱/claude-api地图|claude-api 文档地图]]：50 余篇多语言示例与共享参考的星型导航
- [[AI应用资产/skills/README|skills 原件留痕说明]]：上游拷贝的升级处理制度

## 官方参考精选

- [[AI应用资产/skills/claude-api/shared/agent-design|agent-design]]：agent 设计模式
- [[AI应用资产/skills/claude-api/shared/cost-optimization|cost-optimization]]：成本优化
- [[AI应用资产/skills/claude-api/shared/prompt-caching|prompt-caching]]：提示缓存（可省 50%+）
- [[AI应用资产/skills/claude-api/shared/tool-use-concepts|tool-use-concepts]]：工具调用概念

## 选型

- [[AI应用资产/选型清单|选型清单]]：八类开源项目实测星数 + 取用建议 + 30 秒架构串讲

## 可复用能力（本库已封装）

- [[MCP sever/README|MCP sever]]：认证 / 锁与限流 / 语义缓存 / 检索 / 纠错五件套
- [[MCP sever/模型归因纠错映射/README|模型归因纠错映射]]：模型顽固错、规则可判定时的架构绕过
- [[可复用代码/README|可复用代码·Token 预算守卫]]：注入放大消耗的经济止血阀

## 相关图谱

- [[图谱/MOC-RAG与知识工程|MOC-RAG 与知识工程]]：私有知识链路
- [[图谱/MOC-MCP生态|MOC-MCP 生态]]：工具层协议
- [[图谱/MOC-开发流程与质量|MOC-开发流程与质量]]：AI 应用一样走 vbcoding 流程，验收标准换成评测分
