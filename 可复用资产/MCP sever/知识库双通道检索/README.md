# 知识库双通道检索（MCP Server 002）

## 作用

把 agent_gateway 民法典 RAG 验证过的「召回」阶段封装为通用 MCP 工具：对任意 PostgreSQL
知识表做 pg_trgm 关键词 + 向量语义双通道召回。**召回求全，精排（Rerank）留 Host**——
这是 MCP 封装纪律：Tool 只给原子能力，编排与生成回答是 Host 的职责。

## 能力

| 工具 | 作用 | 设计要点 |
|------|------|---------|
| `search_knowledge` | 双通道召回 | 见下方检索逻辑 |

检索逻辑（对应踩坑清单 #28/#29 修复版）：
1. **关键词通道**：pg_trgm `similarity` 全量排序，`< min_score` 丢弃——不设硬阈值截断
   候选配额，避免噪音条款挤掉子词补充（原 bug）。
2. **子词补充**：2~4 字滑动窗口拆词 + `ILIKE` **无条件执行**——中文短查询（6 字内）
   trigram 相似度趋零（Jaccard 分母问题），拆词补充是召回率兜底。
3. **向量通道**（可选）：`embedding <=> $1::vector` 余弦距离，需表有 embedding 列且
   Host 传入 `query_embedding`；缺任一条件自动跳过，不报错。
4. **合并去重**：同 id 取各通道最高分，按分排序取 top_k。

## 为什么这样设计

- **两段式控质量与成本**：召回多取候选（求全），Rerank 只对 top_k 精排（求准）——全量
  精排太贵。本 Server 把 Rerank 决策权留给 Host（可用 LLM Rerank 或规则排序）。
- **表结构配置化**：`KB_TABLE`/`KB_*_COL` 环境变量切换任意知识表，零代码改动换库。
- **注入防护**：查询值全部走 `$1/$2` 参数化；表名列名来自环境变量（运维可控），不来自用户输入。

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DATABASE_URL` | 由 DB_* 拼接 | 连接串（密码自动 URL 编码） |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | localhost / app_user / 空 / app_db | 拼接用 |
| `KB_TABLE` | `knowledge_chunks` | 知识表名 |
| `KB_ID_COL` | `id` | 主键列 |
| `KB_CONTENT_COL` | `content` | 正文列 |
| `KB_SOURCE_COL` | `source` | 来源列（`sources` 过滤用） |
| `KB_EMBEDDING_COL` | `embedding` | 向量列（无则跳过向量通道） |

## 运行与验证

```bash
pip install mcp asyncpg

python pg_rag_search_server.py            # 拉起 Server（stdio）
python pg_rag_search_server.py --self-check   # 纯逻辑自检（无需 PG）
python test_client.py                     # 完整验证（需 PG + 知识表）
```

## 前置要求（PostgreSQL 侧）

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;              -- 关键词相似度
CREATE EXTENSION IF NOT EXISTS vector;               -- 向量通道（可选）
CREATE INDEX idx_kb_trgm ON knowledge_chunks USING gin (content public.gin_trgm_ops);
CREATE INDEX idx_kb_emb  ON knowledge_chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');
```

## 踩坑提示

- 知识库构建脚本的条号正则曾漏「千」「零」导致整编缺失——数据脚本必须全量幂等校验
  （见 `mcp_assets/servers/civil-code-rag/import_civil_books.py` 修复版）。
- 导入去重：按 chunk_key/md5 幂等，清理时「保留带 embedding 的，其次内容长的」。
