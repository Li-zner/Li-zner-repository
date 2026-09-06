---
title: "MOC-RAG与知识工程"
created: 2026-09-06
tags:
  - MOC
  - tech/rag
  - evergreen
aliases:
  - RAG 图谱
  - 知识工程图谱
---

# MOC-RAG 与知识工程

「让 AI 回答私有知识」的主题图谱：九环链路方法论 + 已验证检索实现 + 配套缓存与纠错组件。

## 链路方法论

- [[AI应用资产/RAG构建|RAG 构建（九环链路）]]：解析 → 切分 → 向量化 → 混合检索 → 重排 → 引用 → 评测；铁律：上限由解析与切分决定

## 已验证实现（本库组件）

- [[MCP sever/知识库双通道检索/README|知识库双通道检索]]：RAG 链路第 4 环「检索」的封装（pg_trgm + 向量，召回求全、精排留 Host）
- [[MCP sever/语义缓存/README|语义缓存]]：第 8 环「语义缓存」——重复问法直接命中，省钱省延迟
- [[MCP sever/模型归因纠错映射/README|模型归因纠错映射]]：生成前的确定性拦截（模型顽固错的架构绕过）

## 踩坑与评测

- [[skill/避坑检查清单/SKILL|避坑检查清单·第六类 AI 检索]]：正则漏条号 / pg_trgm 中文短查询 / 缓存 L0 清不掉 / 非法 JSON（#27-#32）
- [[AI应用资产/选型清单|选型清单·RAG 框架与基建]]：MinerU / pgvector / ragas / RAGFlow 等实测选型

## 知识库方法论（另一侧：人用的知识库）

- [[skill/知识库笔记写作/SKILL|知识库笔记写作]]：Obsidian vault 规范（PARA / frontmatter / 双链 / MOC）——本图谱即按它构建

## 相关图谱

- [[图谱/MOC-Agent与AI应用|MOC-Agent 与 AI 应用]]：Agentic RAG 与评测集建设
- [[图谱/MOC-MCP生态|MOC-MCP 生态]]：检索 / 缓存组件的封装方法
- [[图谱/MOC-生产机制与稳定性|MOC-生产机制与稳定性]]：缓存三防与幂等导入
