# Agent 网关 SLO 与容量规划

> 版本: v1.0 / 2026-08-02
> 目的: 用可量化的目标回答"系统健康吗"，并把告警与容量基线钉在数字上。

## 一、SLI / SLO 定义

| 维度 | SLI（怎么测） | SLO（目标） | 对应告警 |
|------|--------------|------------|---------|
| 可用性 | 5xx 请求占比（`gateway_requests_total`） | 错误率 < 1%（月度） | HighErrorRate |
| 延迟 | p95 响应耗时（`gateway_request_duration_seconds`） | p95 < 5s | P95LatencyHigh |
| 体验 | 首 token 时间（`requests.first_token_time`） | p95 < 3s（目标值，未达成，实测远高于此） | 人工观测 |
| 成本 | 每万 token 花费（`llm_tokens_total` × 10 元） | 日消耗 < 50 元（可配置） | LLMTokenSpike |
| 缓存 | 语义缓存命中率（`semantic_cache_hits_total` / 总数） | 命中率 > 30% | CacheHitRateLow |
| 容量 | 并发处理能力 | 未做负载压测，以单实例内存上限为约束 | 无（容量基线待定） |

## 二、容量基线（2026-07 压测实测）

- **并发能力**: 未做负载压测；历史 Locust 数据不可复现，已作废
- **单实例**: ~200 并发
- **限流配置**: user QPS 2000 / 并发 200 / 日 50 万请求；admin 更宽
- **连接池**: asyncpg min=20 max=50/实例；Redis 无密码内网
- **构建**: 层缓存后增量构建 ~分钟级（原全量 20+ 分钟）

### 扩容触发线（建议）
| 指标 | 阈值 | 动作 |
|------|------|------|
| CPU（网关容器） | > 70% 持续 5 分钟 | 加实例 |
| 并发计数 | 接近 user 上限 200 | 检查是否滥用 or 扩容 |
| P95 延迟 | > 5s | 查 LLM/DB，非扩容 |
| Redis 内存 | > 80% | 检查缓存清理策略 |

## 三、SLO 达成条件（必须全部满足才算健康）

1. 过去 5 分钟 5xx 错误率 < 5%（严格目标 1%）
2. p95 延迟 < 10s（严格目标 5s）
3. 所有 4 实例 `docker ps` 为 healthy
4. Prometheus 探针 `up` 全部为 1
5. 缓存命中率 > 10%（严格目标 30%）

## 四、不达标时的处置顺序

1. 查 Grafana 仪表盘（请求量/错误率/延迟三面板）
2. 查 Tempo 链路（慢在哪一跳：nginx → 网关 → LLM/DB）
3. 查 Loki 日志（错误堆栈）
4. 按告警描述执行：限流调参 / 换 Key / 扩容

## 五、已配套的告警（alert.rules.yml）

9 条告警：RateLimitTriggered、HighErrorRate、TrafficDropped、P95LatencyHigh、
LLMTokenSpike、CacheHitRateLow、PaymentFailureHigh、LLMErrorRateHigh、
GatewayInstanceDown。全部经 Alertmanager 投递。

---

*维护约定: 修改 SLO 数字必须同步更新 alert.rules.yml 与本文档，并注明日期。*
