# agent_gateway

企业级 Multi-Agent 网关：一套引擎承载旅游规划、民法典咨询、求职助手三个业务场景，已上线公网（Vue3 PWA 前端 + FastAPI 后端），日常 800 并发稳定运行，极限压测 1600 并发可用性 99.28%。

## 亮点

- **一核双用、可插拔架构**：Agent 引擎自研（多 Agent 圆桌协议：并行分析→交叉审阅→仲裁合成），业务属性外置为配置包，新增场景零核心改动
- **前端 v2（Vue3 + TS + Vite）**：SSE 流式分帧器、Pinia 状态管理、DOMPurify 消毒链、PWA 离线壳、移动端适配，vitest + playwright 双测试
- **RAG 知识问答**：pg_trgm + pgvector 双路召回 → RRF 融合 → bge-reranker 本地精排（替代高延迟 LLM rerank），法条引用可溯源
- **支付体系**：幂等订单 / 分布式锁钱包 / 待结算补偿（锁竞争落占位单，维护循环原子结算防双扣）/ 渠道抽象可插拔
- **完整工程化**：AST 编码规范门禁 + GitHub Actions、数据库版本化迁移（Alembic）、SLO 告警、Docker 三实例滚动发布、备份恢复演练

## 目录导览

- [showcase/](showcase/README.md) —— 项目展示（给 HR / 招聘方看的完整细则）
- [docs/](docs/) —— 架构文档与学习笔记
- [app/](app/) —— 后端核心代码（FastAPI 分层：routes / services / agents / payment / core）
- [frontend/](frontend/) —— 前端 v2 源码（Vue3 + TypeScript + Vite）
- [tests/](tests/) —— 单测与检索评测（pytest + vitest + 自建召回评测集）
- [scripts/](scripts/) —— 工程化脚本（编码规范门禁 / 迁移 / 种子数据）
- [alembic/](alembic/) —— 数据库版本化迁移
- [deploy/](deploy/) —— 精简版一键部署（2C1G 实机验证）
- [可复用资产/](可复用资产/) —— 跨项目可复用资产（AI 应用资产 / 前端资产 / harness 模板 / MCP / skill）
- [可复用代码/](可复用代码/) —— 独立机制实现（分布式锁 / 熔断器 / 单飞幂等 / LRU+TTL / 限流器）

## 技术栈

Python · FastAPI · PostgreSQL(pgvector) · Redis · Vue3 · TypeScript · Vite · Docker Compose · Nginx · DeepSeek/Qwen API · 阿里云短信/OSS

## 演示

- 公网：https://the-world-agent.cloud
- 百度网盘：https://pan.baidu.com/s/1ProMzaj1ANz_NdQloEKGTw?pwd=sj4b
