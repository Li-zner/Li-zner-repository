---
title: "MOC-MCP生态"
created: 2026-09-06
tags:
  - MOC
  - tech/mcp
  - evergreen
aliases:
  - MCP 图谱
  - MCP Server 图谱
---

# MOC-MCP 生态

MCP 是本库「能力复用」的协议层：一次封装，任何 MCP Host（Claude Desktop / Cursor / 自研网关）生态复用。核心纪律：**MCP 是插座，skill 是操作规程**。

## 自研 Server（5 个，均出自生产实战）

- [[MCP sever/README|MCP sever（总入口）]]：资产清单 / 运行前提 / 封装纪律 / 组合使用
- [[MCP sever/分布式锁与限流/README|001 分布式锁与限流]]：Redis 锁（Lua 原子释放）+ 三种限流
- [[MCP sever/知识库双通道检索/README|002 知识库双通道检索]]：pg_trgm 关键词 + 向量双通道召回
- [[MCP sever/语义缓存/README|003 语义缓存]]：L0 LRU → L1 哈希 → L2 pg_trgm 三级
- [[MCP sever/JWT令牌签发与校验/README|004 JWT 令牌签发与校验]]：HS256 + 刷新轮换 + jti 吊销（纯标准库）
- [[MCP sever/模型归因纠错映射/README|005 模型归因纠错映射]]：触发词映射表，LLM 前确定性拦截

## 开发方法论

- [[skill/mcp-server开发五步法/SKILL|mcp-server 开发五步法]]：能力边界三问 → 原子 Tool 封装 → 配置治理 → 验证闭环 → 踩坑规避
- [[skill/避坑检查清单/SKILL|避坑检查清单·第七类 MCP 专项]]：命名管道 / URI scheme / 绝对路径 / 封装粒度

## 官方最佳实践（对照自测）

- [[AI应用资产/skills/mcp-builder/SKILL|mcp-builder 官方技能]]：Server 构建最佳实践
- [[AI应用资产/skills/mcp-builder/reference/evaluation|evaluation 评测框架]]：写完 Server 用它自测

## 组合样板（MCP + skill）

- [[AI应用验收工具/README|AI 应用验收工具]]：五阶段验收 = 验收流程 skill + 5 个 Server 能力，`verify_all.py` 一键执行
- [[AI应用验收工具/SKILL|验收流程 SKILL]]：每阶段调哪个工具、断言什么、通过标准

## 接入视角与选型纪律

- [[skill/AI Agent 开发全流程/SKILL|AI Agent 开发全流程·Agent 标准件清单]]：开发新 Agent 时按表白捡能力
- [[vbcoding全流程/03-支撑资产/ARCHITECTURE_RULES|架构规则·成熟库优先]]：新增能力先查 awesome-mcp-servers，不重复自研
- [[前端资产/mcp/前端MCP清单|前端 MCP 清单]]：浏览器实测 / 组件生成 / 设计稿转码的接入配置

## 相关图谱

- [[图谱/MOC-生产机制与稳定性|MOC-生产机制与稳定性]]：被封装的机制本体
- [[图谱/MOC-Agent与AI应用|MOC-Agent 与 AI 应用]]：Host 侧怎么用这些能力
- [[图谱/MOC-前端|MOC-前端]]：前端类 MCP
