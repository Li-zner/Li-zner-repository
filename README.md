# agent_gateway — 企业级 Multi-Agent 网关

一套引擎承载多个业务场景（旅游规划 / 民法典咨询 / 求职助手），**一核双用、可插拔**：业务属性外置为配置包，新增场景零核心改动。已上线公网，日常数百并发稳定运行。

> 本仓库为**公开教学/展示版**：不含密钥、凭据与用户数据，用于展示核心架构与工程实践。

## 架构亮点

### 1. 分层模型（严格单向依赖）
```
接入层 → 网关层 → 业务层 → 数据层 → 可观测
```
接入层只做 TLS/静态资源/WAF；网关层做认证/限流/熔断/幂等；业务层做事务/锁/状态机；数据层做 SQL/迁移/索引。**业务逻辑不写进路由层**，路由只做校验 + 调 service + 组装响应。

### 2. 模块化重构（本仓库核心展示）
对三个超长文件做了「纯移动、行为等价」的模块化重构，消除上帝函数与超长文件：

| 文件 | 重构前 | 重构后 | 手法 |
|---|---|---|---|
| `app/main.py` | 1076 行 | **354 行** | 拆出 6 路由模块（auth/phone/oauth/users/conversation/observability）+ `core/otel.py`（OTEL 可用性单一权威源） |
| `app/payment/service.py` | 1193 行 | **581 行** | 删死 DDL（迁移接管）+ 资金函数拆纯缝 + 分层出 `_ledger.py`（账本原语）/ `query.py`（只读），对外公开面 re-export 保留 |
| `app/routes/v2.py` | 1848 行 | 抽 `_finalize_answer`(6 处 finalize 尾统一) / `_route_intent` / `_record_token_usage`，`generate` 951→890 | 撤销重复逻辑、封装意图路由与 Token 计量 |

**约束纪律**：函数 ≤80 行、文件 ≤600 行（防熵检查）；资金/流式事务块**不擅自拆分**（保原子性）；每步用单测 + AST 门禁 + 全链路回归验证。

### 3. 资金链路（一致性优先）
- 充值/扣费/退款：**分布式锁 + 乐观锁双保险**，订单/余额/流水**同事务**原子提交，账实一致。
- 乐观锁版本冲突自动重试（带退避）；模拟支付渠道调用**移出事务**防慢渠道占用连接池；流水表只追加（WAL 可对账）。

### 4. 工程化与可观测
- **CI 门禁**：`check_code_rules.py`（纯 AST 零依赖）接入 CI，函数/文件超限违规发布失败。
- **数据库**：Alembic 版本化迁移（DDL 唯一入口，启动只探活 + fail loudly）；表带 `created_at/updated_at`、资金表加 `version` 乐观锁。
- **可观测**：Prometheus 指标 + OpenTelemetry 追踪 + 结构化日志（trace_id）+ 慢查询观测。
- **安全**：密钥 fail-loudly（无默认值兜底）、上传白名单 + 内容防注入、SQL 参数化、CORS 白名单、登录限流、敏感字段脱敏。

## 技术栈
Python · FastAPI · PostgreSQL(pgvector) · Redis · Docker Compose · Nginx · Alembic · Prometheus · OpenTelemetry · DeepSeek API

## 测试与验证
- 离线单测：锁/滑动窗口限流/乐观锁/语义缓存（FakeRedis 模拟 Lua）。
- 全链路回归：登录 → 上传 → 聊天(流式) → 充值 → 支付 → 退款 → 评分防重。
- 定位体验：浏览器地理授权 → 真实城市；IP 定位兜底对私有 IP 返回空（不编造"局域网"）。

## 演示
- 公网：https://the-world-agent.cloud

> 本公开版不包含：`.env` 类密钥、`deploy/.env.lite`、用户数据、评估报表等非展示内容。
