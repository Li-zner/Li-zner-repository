<!-- ============================================================
  可复用资产：semantic_cache_server.py | 来源：agent_gateway 生产机制 → MCP Server 封装（stdio）
  实战验证：verify_all.py 五阶段验收可跑 | 依赖：mcp sdk + redis/pg（按 server） | 提取：2026-09-06
  ============================================================ -->
"""
MCP 资产 003 — 语义缓存 Server（stdio 传输）

从 agent_gateway 三级语义缓存（core/semantic_cache.py）泛化：L0 进程 LRU → L1 哈希
→ L2 pg_trgm 相似度。命中逐级回填，越用越快；阈值「宁可不命中，不要命中错的」。

能力：
  1. cache_lookup —— 三级查找，返回命中层级与缓存答案
  2. cache_store  —— 三级写入（L2 落 PG，upsert 幂等）
  3. cache_stats  —— 各级容量/命中统计

运行（stdio）：
    python semantic_cache_server.py
验证 Client（需 PG）：
    python test_client.py

配置（进程环境变量优先，其次 _env.py 读 .env）：
    DATABASE_URL 或 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME
    CACHE_SIM_THRESHOLD（默认 0.85）  CACHE_L0_CAPACITY（默认 256）  CACHE_L2_TABLE（默认 semantic_cache）
"""
import hashlib
import sys
import time
from collections import OrderedDict
from pathlib import Path

import asyncpg
from mcp.server.fastmcp import FastMCP

_ROOT = Path(__file__).resolve().parent.parent  # → MCP sever/
sys.path.insert(0, str(_ROOT))
from _env import get, db_url  # noqa: E402

mcp = FastMCP("semantic-cache")

_pool = None
THRESHOLD = float(get("CACHE_SIM_THRESHOLD", "0.85"))
TABLE = get("CACHE_L2_TABLE", "semantic_cache")

# L0：进程内 LRU（OrderedDict，TTL 过期即淘汰）
L0 = OrderedDict()
L0_CAPACITY = int(get("CACHE_L0_CAPACITY", "256"))

# L1：进程内哈希表（精确匹配）；多实例间共享只靠 L2（PG）
L1: dict = {}

# 统计
STATS = {"l0_hit": 0, "l1_hit": 0, "l2_hit": 0, "miss": 0}


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = get("DATABASE_URL") or db_url(get("DB_HOST", "localhost"))
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    return _pool


def _norm(query: str) -> str:
    """归一化：去空白/标点/全半角统一——提高精确命中率"""
    import unicodedata
    s = unicodedata.normalize("NFKC", query).lower()
    return "".join(ch for ch in s if ch.isalnum())


def _hash(query: str) -> str:
    return hashlib.sha256(_norm(query).encode("utf-8")).hexdigest()


async def _ensure_table(pool) -> None:
    await pool.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            query_hash TEXT PRIMARY KEY,
            query_text TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )


@mcp.tool()
async def cache_lookup(query: str, threshold: float = None) -> dict:
    """三级语义缓存查找：L0 内存 LRU → L1 精确哈希 → L2 pg_trgm 相似度。

    Args:
        query: 用户问题原文。
        threshold: 相似度阈值，默认 0.85（宁可不命中，不要命中错的）。
    """
    if not query or not query.strip():
        return {"error": "query 不能为空"}
    t = threshold if threshold is not None else THRESHOLD
    now = time.time()

    # L0：LRU（命中即移到尾部）
    value = _l0_get(query, now)
    if value is not None:
        STATS["l0_hit"] += 1
        return {"hit": True, "level": "L0", "response": value}

    # L1：精确哈希
    h = _hash(query)
    l1 = L1.get(h)
    if l1 is not None:
        value, expire = l1
        if now > expire:
            L1.pop(h, None)
        else:
            STATS["l1_hit"] += 1
            _l0_set(query, value)
            return {"hit": True, "level": "L1", "response": value}

    # L2：pg_trgm 相似度（语义兜底）
    try:
        pool = await get_pool()
        await _ensure_table(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT response, similarity(query_text, $1) AS sim "
                f"FROM {TABLE} WHERE similarity(query_text, $1) > $2 "
                f"ORDER BY sim DESC LIMIT 1",
                query, t,
            )
        if row:
            STATS["l2_hit"] += 1
            _l0_set(query, row["response"])
            L1[h] = (row["response"], now + 3600)
            return {"hit": True, "level": "L2", "similarity": round(float(row["sim"]), 4),
                    "response": row["response"]}
    except Exception as e:
        return {"error": str(e)}  # L2 故障时降级为 miss，不阻断主流程

    STATS["miss"] += 1
    return {"hit": False, "level": None, "response": None}


@mcp.tool()
async def cache_store(query: str, response: str, ttl_seconds: int = 86400) -> dict:
    """三级写入缓存（命中逐级回填的前提）。L0/L1 立即生效，L2 落 PG 持久化。

    Args:
        query: 用户问题原文。
        response: 要缓存的答案。
        ttl_seconds: 缓存有效期，默认 1 天（L2 不自动过期，由清理任务负责）。
    """
    if not query or not response:
        return {"error": "query 与 response 不能为空"}
    now = time.time()
    _l0_set(query, response)
    L1[_hash(query)] = (response, now + ttl_seconds)
    try:
        pool = await get_pool()
        await _ensure_table(pool)
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {TABLE} (query_hash, query_text, response) VALUES ($1, $2, $3) "
                f"ON CONFLICT (query_hash) DO UPDATE SET response = EXCLUDED.response",
                _hash(query), query, response,
            )
    except Exception as e:
        return {"error": str(e), "stored": "L0/L1"}
    return {"stored": True, "levels": ["L0", "L1", "L2"]}


@mcp.tool()
async def cache_stats() -> dict:
    """各级缓存容量与命中统计（命中率 = 命中总数 / 查询总数）"""
    total = sum(STATS.values())
    return {
        "l0_size": len(L0), "l0_capacity": L0_CAPACITY, "l1_size": len(L1),
        "hits": dict(STATS),
        "hit_rate": round(STATS["l0_hit"] + STATS["l1_hit"] + STATS["l2_hit"]) / max(total, 1),
    }


def _l0_get(query: str, now: float):
    """L0 读取：过期即淘汰并返回 None；命中移到尾部（标记最近使用）"""
    hit = L0.get(query)
    if hit is None:
        return None
    value, expire = hit
    if now > expire:
        L0.pop(query, None)
        return None
    L0.move_to_end(query)
    return value


def _l0_set(query: str, value: str) -> None:
    L0[query] = (value, time.time() + 600)  # L0 TTL 10 分钟
    L0.move_to_end(query)
    while len(L0) > L0_CAPACITY:
        L0.popitem(last=False)  # 淘汰最久未用


def _self_check() -> None:
    """纯逻辑自检（无需 PG）：归一化、LRU 淘汰、TTL 过期。"""
    assert _norm("离婚 财产 怎么分！") == _norm("离婚财产怎么分")
    print("[self-check] 归一化 OK")

    # LRU：容量淘汰最久未用；命中后移到尾部
    for i in range(L0_CAPACITY + 5):
        _l0_set(f"q{i}", f"a{i}")
    assert len(L0) == L0_CAPACITY
    assert "q0" not in L0 and "q5" in L0  # 最早写入的被淘汰
    L0.move_to_end("q5")                   # 访问 q5
    _l0_set("new", "x")
    assert "q5" in L0 and "q6" not in L0   # 淘汰的是未再访问的
    print("[self-check] LRU 淘汰 OK")

    # TTL：过期即失效（_l0_get 内部判定）
    assert _l0_get("new", time.time()) == "x"
    L0["new"] = ("x", time.time() - 1)
    assert _l0_get("new", time.time()) is None and "new" not in L0
    print("[self-check] TTL 过期 OK")
    print("全部自检通过")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        mcp.run()
