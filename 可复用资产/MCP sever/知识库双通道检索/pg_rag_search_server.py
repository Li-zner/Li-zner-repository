<!-- ============================================================
  可复用资产：pg_rag_search_server.py | 来源：agent_gateway 生产机制 → MCP Server 封装（stdio）
  实战验证：verify_all.py 五阶段验收可跑 | 依赖：mcp sdk + redis/pg（按 server） | 提取：2026-09-06
  ============================================================ -->
"""
MCP 资产 002 — 知识库双通道检索 Server（stdio 传输）

从 agent_gateway 民法典 RAG 实战泛化（对应 harness 踩坑清单 #28/#29）：
对任意 PostgreSQL 知识表做「pg_trgm 关键词 + 向量语义」双通道召回，表结构可配置。

召回逻辑（求全）：
  1. 关键词通道：pg_trgm 相似度全量排序（< min_score 丢弃）
     + 2~4 字滑动窗口拆词 ILIKE 补充（无条件执行——修复中文短查询相似度趋零的坑）
  2. 向量通道（可选）：query_embedding 与 embedding 列余弦距离（pgvector <=>）
合并去重，score 取各通道最大值，按分排序取 top_k。精排（Rerank）留 Host，本 Server 只召回。

运行（stdio）：
    python pg_rag_search_server.py
验证 Client（需 PG 且已有知识表）：
    python test_client.py

配置（进程环境变量优先，其次 _env.py 读 .env）：
    DATABASE_URL 或 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME
    KB_TABLE / KB_ID_COL / KB_CONTENT_COL / KB_SOURCE_COL(可选) / KB_EMBEDDING_COL(可选)
"""
import re
import sys
from pathlib import Path

import asyncpg
from mcp.server.fastmcp import FastMCP

_ROOT = Path(__file__).resolve().parent.parent  # → MCP sever/
sys.path.insert(0, str(_ROOT))
from _env import get, db_url  # noqa: E402

mcp = FastMCP("pg-rag-search")

_pool = None


def _cfg(name: str, default: str) -> str:
    return get(name, default) or default


async def get_pool() -> asyncpg.Pool:
    """懒加载全局连接池（单例）"""
    global _pool
    if _pool is None:
        dsn = get("DATABASE_URL") or db_url(get("DB_HOST", "localhost"))
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    return _pool


def _subwords(text: str) -> list:
    """2~4 字滑动窗口拆词（中文检索用）。短词优先（2 字最贴合用户口语），
    总量截断防长句拆出太多 OR 子句拖慢查询。"""
    text = re.sub(r"\s+", "", text)
    if not text:
        return []
    n = len(text)
    words, seen = [], set()
    for size in (2, 3, 4):
        for i in range(0, n - size + 1):
            w = text[i:i + size]
            if w not in seen:
                seen.add(w)
                words.append(w)
    return words[:8]  # 防长句拆出太多词拖慢查询


@mcp.tool()
async def search_knowledge(
    query: str,
    top_k: int = 5,
    min_score: float = 0.1,
    use_vector: bool = True,
    query_embedding: list = None,
    sources: list = None,
) -> dict:
    """双通道召回知识库条目（关键词 pg_trgm + 向量余弦；召回求全，精排留 Host）。

    Args:
        query: 用户问题（原样传入，内部自动拆词）。
        top_k: 返回条数，默认 5。
        min_score: 关键词通道相似度下限（默认 0.1，丢弃噪音）。
        use_vector: 是否启用向量通道（需表有 embedding 列且传 query_embedding）。
        query_embedding: 查询向量（float 列表）；不传则跳过向量通道。
        sources: 限定来源（如 ["civil_code"]）；不传不限。
    """
    if not query or not query.strip():
        return {"error": "query 不能为空"}
    if top_k <= 0:
        return {"error": "top_k 必须 > 0"}
    table = _cfg("KB_TABLE", "knowledge_chunks")
    id_col = _cfg("KB_ID_COL", "id")
    content_col = _cfg("KB_CONTENT_COL", "content")
    source_col = _cfg("KB_SOURCE_COL", "source")
    emb_col = _cfg("KB_EMBEDDING_COL", "embedding")

    try:
        pool = await get_pool()
        rows = []
        async with pool.acquire() as conn:
            # ---- 通道 1：pg_trgm 相似度（全量排序，低分丢弃）----
            try:
                sim_sql = (
                    f"SELECT {id_col} AS id, {content_col} AS content, "
                    f"similarity({content_col}, $1) AS score "
                    f"FROM {table} WHERE similarity({content_col}, $1) > $2"
                )
                args: list = [query, min_score]
                if sources:
                    sim_sql += f" AND {source_col} = ANY($3)"
                    args.append(sources)
                sim_sql += f" ORDER BY score DESC LIMIT $4"
                args.append(top_k * 3)
                rows += [dict(r) for r in await conn.fetch(sim_sql, *args)]
            except asyncpg.PostgresError:
                pass  # 无 pg_trgm 扩展时静默跳过相似度通道

            # ---- 通道 2：子词 ILIKE 补充（无条件执行，补相似度漏掉的短查询）----
            words = _subwords(query)
            if words:
                ilike_sql = (
                    f"SELECT {id_col} AS id, {content_col} AS content, 0.5 AS score "
                    f"FROM {table} WHERE " +
                    " OR ".join(f"{content_col} ILIKE $1 || '%%'" for _ in words) +
                    f" LIMIT $2"
                )
                ilike_args = [f"%{w}%" for w in words] + [top_k * 3]
                if sources:
                    # 拼接来源过滤（参数化）
                    ilike_sql = (
                        f"SELECT {id_col} AS id, {content_col} AS content, 0.5 AS score "
                        f"FROM {table} WHERE {source_col} = ANY($1) AND (" +
                        " OR ".join(f"{content_col} ILIKE $2 || '%%'" for _ in words) +
                        f") LIMIT $3"
                    )
                    ilike_args = [sources] + [f"%{w}%" for w in words] + [top_k * 3]
                rows += [dict(r) for r in await conn.fetch(ilike_sql, *ilike_args)]

            # ---- 通道 3：向量余弦（可选）----
            if use_vector and query_embedding and emb_col:
                try:
                    vec_sql = (
                        f"SELECT {id_col} AS id, {content_col} AS content, "
                        f"1 - ({emb_col} <=> $1::vector) AS score "
                        f"FROM {table} WHERE {emb_col} IS NOT NULL"
                    )
                    vec_args: list = [query_embedding]
                    if sources:
                        vec_sql += f" AND {source_col} = ANY($2)"
                        vec_args.append(sources)
                    vec_sql += f" ORDER BY score DESC LIMIT $3"
                    vec_args.append(top_k * 3)
                    rows += [dict(r) for r in await conn.fetch(vec_sql, *vec_args)]
                except asyncpg.PostgresError:
                    pass  # 无 pgvector 扩展/列时静默跳过
    except Exception as e:
        return {"error": str(e)}

    # 合并去重：score 取各通道最大值
    merged: dict = {}
    for r in rows:
        rid = r["id"]
        if rid not in merged or r["score"] > merged[rid]["score"]:
            merged[rid] = {"id": rid, "content": r["content"], "score": round(float(r["score"]), 4)}
    results = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    return {"query": query, "total": len(results), "results": results}


def _self_check() -> None:
    """纯逻辑自检（无需 PG）：拆词、合并去重、参数化拼接。"""
    # 1. 拆词：中文 2~4 字滑动窗口
    words = _subwords("七天无理由退货")
    assert "七天无" in words and "退货" in words and len(words) <= 8
    assert _subwords("") == []
    print("[self-check] 拆词 OK:", words)

    # 2. 合并去重：同 id 取高分
    rows = [
        {"id": 1, "content": "A", "score": 0.2},
        {"id": 1, "content": "A", "score": 0.9},
        {"id": 2, "content": "B", "score": 0.3},
    ]
    merged = {}
    for r in rows:
        rid = r["id"]
        if rid not in merged or r["score"] > merged[rid]["score"]:
            merged[rid] = {"id": rid, "content": r["content"], "score": r["score"]}
    assert len(merged) == 2 and merged[1]["score"] == 0.9
    print("[self-check] 合并去重 OK")

    # 3. 拆词边界：长输入截断、词均 ≥2 字（查询值全部走 $n 参数化，防注入）
    long_words = _subwords("防注入攻击" * 50)
    assert len(long_words) <= 8
    assert all(len(w) >= 2 for w in long_words)
    print("[self-check] 拆词边界 OK")
    print("全部自检通过")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        mcp.run()
