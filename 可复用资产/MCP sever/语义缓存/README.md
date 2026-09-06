# 语义缓存（MCP Server 003）

## 作用

把 agent_gateway 三级语义缓存封装为通用 MCP 工具。LLM 应用重复问题直接命中缓存 =
**省钱（token）+ 省延迟**。命中逐级回填，越用越快。

## 能力

| 工具 | 作用 |
|------|------|
| `cache_lookup` | 三级查找：L0 进程 LRU（微秒）→ L1 精确哈希（毫秒）→ L2 pg_trgm 相似度 |
| `cache_store` | 三级写入，L2 落 PG（upsert 幂等，表自动创建） |
| `cache_stats` | 各级容量与命中率统计（命中率是缓存价值的证据） |

## 设计要点

- **阈值默认 0.85，宁可不命中不要命中错的**——法律/资金场景答错比 miss 贵。
- **L0 是进程内存**：清 PG 表清不掉 L0（踩坑清单 #7/#31）——调 `cache_lookup` 走
  L0 时先过期淘汰，进程重启即清空；多实例共享只靠 L2（PG）。
- **L1/L0 是刻意简化**（`ponytail:` 注释）：原项目 L1 用 Redis 多实例共享，本 Server
  用进程内 dict，多实例部署时命中率依赖 L2。升级路径：把 L1 换成 Redis（key=query_hash）。
- **L2 故障降级 miss**：缓存挂了不阻断主流程，这是缓存的原罪设计。
- **防击穿/雪崩留给 Host**：互斥重建 + 随机过期模板见 harness 参考（生产机制 22）。

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DATABASE_URL` | 由 DB_* 拼接 | PostgreSQL 连接串 |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | localhost / app_user / 空 / app_db | 拼接用 |
| `CACHE_SIM_THRESHOLD` | `0.85` | L2 相似度阈值 |
| `CACHE_L0_CAPACITY` | `256` | L0 LRU 容量 |
| `CACHE_L2_TABLE` | `semantic_cache` | L2 表名（自动建表） |

## 运行与验证

```bash
pip install mcp asyncpg

python semantic_cache_server.py            # 拉起 Server（stdio）
python semantic_cache_server.py --self-check   # 纯逻辑自检（无需 PG）
python test_client.py                      # 完整验证（需 PG）
```

## 接入 LLM 应用的推荐姿势

```
请求 → cache_lookup(query)
  ├─ 命中 → 直接返回缓存答案（记录 level，不调 LLM）
  └─ miss  → 调 LLM 生成 → cache_store(query, answer) → 返回
```

## 库内关联

- 机制参考实现：[[可复用代码/README|可复用代码]]（LRU+TTL 缓存 = L0 原型；singleflight+幂等 = 重建合并）
- 链路位置：[[AI应用资产/RAG构建|RAG 构建]]第 8 环「语义缓存」
- 安全案例：[[AI应用资产/安全审计|AI 应用安全审计]]（缓存上下文维度隔离 = LLM02/08 实战案例）
- 验收：[[AI应用验收工具/README|AI 应用验收工具·阶段 3]]（L0 命中 / 语义命中 / 无关 miss）
- 图谱：[[图谱/MOC-生产机制与稳定性|MOC-生产机制与稳定性]]、[[图谱/MOC-RAG与知识工程|MOC-RAG 与知识工程]]
