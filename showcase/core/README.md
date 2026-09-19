# 核心代码导读

> 这里是网关**最核心的引擎与基础设施代码**（脱敏展示版），来自生产环境的 `app/` 目录精选。所有密钥均通过环境变量注入，仓库内无任何硬编码凭据。

## 目录结构

```
core/
├── agents/        # 自研 Agent 引擎（本项目最大亮点）
│   ├── router.py           # 语义意图路由：用户问题 → 该找谁回答
│   ├── runner.py           # Agent 执行器：工具调用循环 / 状态机推进
│   ├── orchestrator.py     # 多 Agent 并行编排与结果聚合
│   ├── routing_table.py    # 意图-场景路由表（业务属性外置）
│   ├── tools.py            # 工具集：高德天气/路线、RAG 检索、重排、DuckDuckGo 搜索
│   ├── sub_agents.py       # 子 Agent 定义
│   ├── memory.py           # Agent 短期记忆
│   ├── document_parser.py  # 知识文档解析（分块）
│   ├── law_mapping.py      # 法条映射（民法典领域）
│   ├── article_normalizer.py # 法条文本规范化
│   └── tool_definitions.py # 工具 JSON Schema 定义
├── core/          # 基础设施与业务核心
│   ├── semantic_cache.py   # 三级语义缓存（L0/L1/L2 + 抗雪崩）
│   ├── memory_manager.py   # 冷热分层记忆（Redis + PostgreSQL）
│   ├── persona_manager.py  # 多场景人格管理（配置包外置）
│   ├── safety_filter.py    # 安全过滤（输入/输出）
│   ├── plugin_loader.py    # 插件式加载
│   ├── error_aggregator.py # 错误聚合与告警指标
│   ├── task_manager.py     # 后台任务管理
│   ├── cache_warmup.py     # 缓存预热
│   ├── metrics.py          # Prometheus 指标
│   ├── config.py           # 配置读取（全部走环境变量）
│   ├── redis.py / db.py    # Redis / PostgreSQL 连接池
│   ├── password.py         # 密码哈希（SHA-256 预处理 + bcrypt，超长密码全链路）
│   ├── quota.py            # 用量配额
│   ├── stream_utils.py     # LLM 流式输出与工具分发
│   ├── concurrency.py      # 并发控制（信号量）
│   ├── logging.py          # 结构化日志（trace_id 关联）
│   ├── jfast.py            # 高性能 JSON 序列化
│   └── constants.py        # 常量
└── middleware/    # 网关中间件
    ├── auth.py             # JWT 认证（access 2h + refresh 30d 轮换 + 黑名单）+ 密码校验
    ├── rate_limit.py       # Redis 分布式限流
    └── circuit_breaker.py  # 熔断降级
```

## 推荐阅读顺序

1. **`agents/router.py`** → 理解「意图路由」如何把用户问题分发给正确的 AI 助手
2. **`agents/runner.py`** → 理解 Agent 如何调用工具、迭代求解
3. **`agents/orchestrator.py`** → 理解多 Agent 如何并行协作
4. **`core/semantic_cache.py`** → 理解多级缓存结构与隔离键如何防止跨用户、跨会话串答
5. **`middleware/auth.py`** → 理解认证与刷新令牌轮换

## 说明

- 为保护生产安全，本目录为 `app/{core,agents,middleware}` 的**精选子集**：含核心引擎与基础设施
- 支付、数据库迁移、运维脚本、路由层等内部工程内容**不在此公开**
- 完整架构与工程能力见 [项目展示](../README.md)
