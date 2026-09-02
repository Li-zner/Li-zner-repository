# agent_gateway

企业级 Multi-Agent 网关：一套引擎承载旅游规划、民法典咨询、求职助手三个业务场景，已上线公网，日常 800 并发稳定运行，极限压测 1600 并发可用性 99.28%。

## 亮点

- **一核双用、可插拔架构**：Agent 引擎自研，业务属性外置为配置包，新增场景零核心改动
- **RAG 知识问答**：双通道检索 + 法条引用，支撑民法典咨询
- **完整工程化**：CI/CD 滚动发布、数据库版本化迁移、SLO 告警、WAF 加固、可观测、备份演练

## 目录导览

- [showcase/](showcase/README.md) —— 项目展示（给 HR / 招聘方看的完整细则）
- [docs/](docs/) —— 架构文档与学习笔记
- [app/](app/) —— 核心代码

## 技术栈

Python · FastAPI · PostgreSQL · Redis · Docker Compose · Nginx · DeepSeek API

## 演示

- 公网：https://the-world-agent.cloud
- 百度网盘：https://pan.baidu.com/s/1ProMzaj1ANz_NdQloEKGTw?pwd=sj4b

