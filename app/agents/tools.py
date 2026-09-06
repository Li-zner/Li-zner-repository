import os
import json
import time
import asyncio
import httpx
from ..core.logging import setup_logging
from ..core.config import (
    HTTP_TIMEOUT_SHORT, HTTP_TIMEOUT_MEDIUM,
)

logger = setup_logging()

async def fetch_weather_async(city: str):
    """异步调用高德天气 API"""
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        return {"error": "缺少高德 API Key"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SHORT) as client:
            # 1. 查城市编码
            geo_resp = await client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": amap_key, "address": city}
            )
            geo_data = geo_resp.json()
            if geo_data["status"] != "1" or not geo_data["geocodes"]:
                return {"error": f"未找到城市: {city}"}
            adcode = geo_data["geocodes"][0]["adcode"]
            
            # 2. 查天气
            weather_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": amap_key, "city": adcode, "extensions": "base"}
            )
            weather_data = weather_resp.json()
            if weather_data["status"] != "1":
                return {"error": "天气查询失败"}
            live = weather_data["lives"][0]
            return {
                "city": city,
                "temperature": live["temperature"],
                "weather": live["weather"],
                "wind": live["winddirection"]
            }
    except Exception as e:
        # 异常 str 可能带含 key 的完整 URL，用户侧只给通用文案（细节进日志）
        logger.warning(f"天气查询异常: {type(e).__name__}: {e}")
        return {"error": "天气查询失败"}


# ============================================================
# 口语化表述 → 专业术语映射表
# ============================================================
_COLLOQUIAL_MAP = None
_COLLOQUIAL_MAP_PATH = None

def _load_colloquial_map() -> dict:
    """加载口语化表述映射表（tests/民法典映射表.txt）"""
    global _COLLOQUIAL_MAP, _COLLOQUIAL_MAP_PATH
    if _COLLOQUIAL_MAP is not None:
        return _COLLOQUIAL_MAP

    _COLLOQUIAL_MAP = {}
    path = _COLLOQUIAL_MAP_PATH or os.path.join(
        os.path.dirname(__file__), "..", "..", "tests", "民法典映射表.txt"
    )
    if not os.path.exists(path):
        # 回退到容器内路径
        path = "/app/tests/民法典映射表.txt"
    if not os.path.exists(path):
        return _COLLOQUIAL_MAP
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("口语化") or line.startswith("==="):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                colloquial = parts[0].strip()
                legal = parts[1].strip()
                if colloquial and legal:
                    _COLLOQUIAL_MAP[colloquial] = legal
    return _COLLOQUIAL_MAP


def _map_colloquial_to_legal(query: str) -> str:
    """将口语化表述映射为专业术语，没找到映射则返回原查询"""
    mapping = _load_colloquial_map()
    if not mapping:
        return query
    
    # 按长度降序排序（长匹配优先）
    items = sorted(mapping.items(), key=lambda x: -len(x[0]))
    for colloquial, legal in items:
        if colloquial in query:
            # 替换为专业术语
            query = query.replace(colloquial, legal)
            break  # 只替换第一个匹配
    
    return query


def _law_mapping_check(query):
    """法律依据纠正映射表检查：命中返回引导信息（不执行检索）"""
    try:
        from .law_mapping import check_query as _check_law
        _match = _check_law(query)
        if _match:
            logger.info(f"法律映射表命中 #{_match['id']}: {_match['scenario']} → {_match['law']}")
            return {
                "results": [{
                    "heading": "该问题不属于民法典调整范围",
                    "content": _match["message"],
                    "similarity": 1.0,
                    "is_law_mapping": True,
                    "law": _match["law"],
                    "citation": _match["citation"],
                }],
                "method": "law_mapping",
                "mapping_hit": True,
                "message": _match["message"],
            }
    except Exception as e:
        # 映射表检查失败只降级跳过，不影响正常检索（P2 修复：不再裸 pass，留排查日志）
        logger.warning(f"法律映射表检查失败，跳过: {e}")
    return None


def _dedup(candidates: list, chunk_key: str, heading: str, content: str, sim: float) -> bool:
    """候选去重：heading 与内容前 50 字相同则跳过（保留 chunk_key 供引用溯源）"""
    if any(e["heading"] == heading and e["content"][:50] == content[:50] for e in candidates):
        return False
    candidates.append({"chunk_key": chunk_key, "heading": heading,
                       "content": content[:500], "similarity": sim})
    return True


def _perm_clause(permissions: list | None) -> str:
    """知识库权限过滤 SQL 片段（P0 #29/#41）。

    None=不过滤（内部/admin）；[]=仅公开（空数组 && 会漏掉公开文档，须等值判断）；
    非空=公开+命中权限组。三处调用（trgm/ILIKE/向量）占位符布局一致：
    $1 检索参数、$2 limit、$3 权限数组，故固定用 $3。
    """
    if permissions is None:
        return ""
    if permissions:
        return " AND (COALESCE(permission, '{}') = '{}' OR permission && $3) "
    return " AND COALESCE(permission, '{}') = '{}' "


async def _recall_pg_trgm(conn, search_query: str, recall_limit: int,
                          permissions: list | None = None) -> list:
    """pg_trgm 相似度召回（带相似度分数）。

    permissions=None = 不过滤（内部/admin）；[] = 仅公开；['vip'] = 公开+vip。
    """
    args = [search_query, recall_limit]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, source, heading, content, source_doc, "
        "similarity(content, $1) as sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' "
        + _perm_clause(permissions) +
        "ORDER BY sim DESC LIMIT $2",
        *args,
    )
    return [{
        "chunk_key": r["chunk_key"],
        "heading": r["heading"],
        "content": r["content"][:500],
        "similarity": round(r["sim"], 4) if r["sim"] else 0,
        "source_doc": r["source_doc"] or r["source"],
    } for r in rows]


async def _recall_pg_vector(conn, query_embedding: list, recall_limit: int,
                            permissions: list | None = None) -> list:
    """向量召回（pgvector 余弦；civil 通道 2026-09 启用——索引早已建好但一直无人查询）。

    query_embedding 为空（Ollama 不可用等）返回 []，调用方自然退化为 trgm 单路。
    """
    if not query_embedding:
        return []
    args = [json.dumps(query_embedding), recall_limit]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, source, heading, content, source_doc, "
        "1 - (embedding <=> $1::vector) AS sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' AND embedding IS NOT NULL "
        + _perm_clause(permissions) +
        "ORDER BY embedding <=> $1::vector LIMIT $2",
        *args,
    )
    return [{
        "chunk_key": r["chunk_key"],
        "heading": r["heading"],
        "content": r["content"][:500],
        "similarity": round(r["sim"], 4) if r["sim"] else 0,
        "source_doc": r["source_doc"] or r["source"],
    } for r in rows]


def _rrf_merge(*ranked_lists: list, k: int = 60) -> list:
    """RRF 倒数排名融合：trgm 相似度 / 向量余弦 / ILIKE 命中不在同一度量空间，
    按排名位置融合回避归一化；k=60 削弱单路榜首 dominance（社区经验值）。"""
    scores = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            e = scores.setdefault(item["chunk_key"], {"item": item, "score": 0.0})
            e["score"] += 1.0 / (k + rank + 1)
    return [e["item"] for e in sorted(scores.values(), key=lambda x: -x["score"])]


async def _keyword_fill(conn, query: str, recall_limit: int, candidates: list,
                        permissions: list | None = None):
    """关键词 ILIKE 补充（去重；补满 recall_limit 即停；支持权限过滤）"""
    for kw in query.replace("?", "").replace("，", " ").replace("？", " ").split():
        if len(kw) < 2:
            continue
        args = [f"%{kw}%", recall_limit - len(candidates)]
        if permissions:
            args.append(permissions)
        more = await conn.fetch(
            "SELECT chunk_key, source, heading, content, 0.5 as sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            + _perm_clause(permissions) +
            "AND content ILIKE $1 LIMIT $2",
            *args,
        )
        for r in more:
            _dedup(candidates, r["chunk_key"], r["heading"], r["content"], 0.5)
        if len(candidates) >= recall_limit:
            break


# 召回窗 15 / 重排 5（2026-09-06 用户决策：原 max(top_k*4,20) 召回 + LLM 全量精排
# 过大过贵——LLM rerank 曾 100% 超时，每查询白等 30s）
RECALL_LIMIT = 15
RERANK_TOP = 5

# 本地重排器单例（sentence-transformers CrossEncoder；镜像内 torch 已预装）
_RERANKER = None
_RERANKER_INIT_FAILED = False


def _get_reranker():
    """懒加载本地重排模型（进程内单例）；不可用返回 None，调用方回退 RRF 排序。

    模型经 HF_ENDPOINT 镜像站预置进镜像（见 Dockerfile 构建期下载）。加载前强制
    HF_HUB_OFFLINE=1：否则 huggingface_hub 每次加载都对 huggingface.co 做 HEAD
    校验，离线机器上重试 5 轮、首查实测卡死 4 分钟后仍失败（2026-09-06 冒烟实测）。
    构建期下载由 Dockerfile RUN 里显式 HF_HUB_OFFLINE=0 覆盖。
    """
    global _RERANKER, _RERANKER_INIT_FAILED
    if _RERANKER is not None:
        return _RERANKER
    if _RERANKER_INIT_FAILED:
        return None
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        from sentence_transformers import CrossEncoder
        _RERANKER = CrossEncoder(
            os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base"), max_length=256)
        return _RERANKER
    except Exception as e:
        _RERANKER_INIT_FAILED = True
        logger.warning(f"本地重排模型不可用（回退 RRF 排序）: {type(e).__name__}: {str(e)[:120]}")
        return None


async def _rerank_local(query: str, candidates: list) -> list:
    """本地 cross-encoder（bge-reranker）精排 RRF 前 5 候选。

    ponytail: CPU 软推理 5 对约 1.5-3s。模型刻意选 bge-reranker-base（278M/1.1GB）——
    主人决策：后续要把 rerank 迁到云端 Ollama，不得换成更大的模型（如 v2-m3 568M）；
    可用 RERANK_MODEL 环境变量无码切换。未启用/模型不可用返回 []，调用方保持 RRF 排序。
    """
    if os.getenv("LOCAL_RERANK_ENABLED", "1") != "1":
        return []
    ranker = _get_reranker()
    if ranker is None or not candidates:
        return []

    def _predict() -> dict:
        scores = ranker.predict([(query[:256], c["content"][:300]) for c in candidates])
        return {c["chunk_key"]: float(s) for c, s in zip(candidates, scores)}

    score_by_key = await asyncio.to_thread(_predict)
    for c in candidates:
        c["rerank_score"] = round(score_by_key.get(c["chunk_key"], 0.0), 4)
    return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)


async def _search_two_legs(conn, query: str, search_query: str,
                           top_k: int, permissions: list | None) -> dict:
    """双路召回（trgm + 向量）→ RRF 融合 → 本地重排 RRF 前 5。

    原实现的"口语映射后重试"是逻辑死代码（重试查询与首次完全相同，结果恒等），
    随本次重写移除；口语映射已在进入本函数前完成。
    """
    recall_limit = max(RECALL_LIMIT, top_k)
    trgm = await _recall_pg_trgm(conn, search_query, recall_limit, permissions)
    await _keyword_fill(conn, search_query, recall_limit, trgm, permissions)
    embedding = await _generate_embedding(search_query)
    vector = await _recall_pg_vector(conn, embedding, recall_limit, permissions)
    merged = _rrf_merge(trgm, vector)
    if not merged:
        return {"results": [], "method": "trgm+vector"}

    top_candidates = merged[:RERANK_TOP]
    reranked = await _rerank_local(query, top_candidates)
    if reranked:
        return {"results": reranked[:top_k], "method": "trgm+vector+rrf+local_rerank"}
    return {"results": top_candidates[:top_k], "method": "trgm+vector+rrf"}


async def search_knowledge(query: str, top_k: int = 5, permissions: list | None = None):
    """语义搜索知识库：trgm + 向量双路召回 → RRF 融合 → 本地 bge-reranker 重排前 5。

    在检索前，先通过法律依据纠正映射表检查用户问题是否属于其他法律领域。
    如果命中映射表，直接返回纠正引导信息，不执行知识库搜索。

    permissions: None=不过滤（内部/admin）；[]=仅公开；['vip']=公开+vip 可检索。
    """
    # ===== 法律依据纠正映射表检查 =====
    mapping_result = _law_mapping_check(query)
    if mapping_result:
        return mapping_result

    from ..core.db import get_pool

    # ===== 口语→术语映射：优先使用映射后的查询进行检索 =====
    _search_query = _map_colloquial_to_legal(query)
    if _search_query != query:
        logger.info(f"搜索前置口语映射: {query[:30]}... → {_search_query[:60]}...")

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _search_two_legs(conn, query, _search_query, top_k, permissions)


# web_search 本地限流（P1 #13/#40：防高频调用导致外部搜索 API 封 IP）
# 按 user_key 独立计数（P1：改全局限流为用户级，避免多用户并发互相误伤）。
_search_rate_lock = asyncio.Lock()
_search_rate_state: dict = {}   # user_key -> (window_start, count)
_SEARCH_WINDOW_SECONDS = 10.0
_SEARCH_MAX_PER_WINDOW = 10
# 计数字典键数上限：超过即触发惰性清扫（防每用户一个键无界增长，P2 修复）
_SEARCH_STATE_MAX_KEYS = 512


async def _check_search_rate(user_key: str = ""):
    """web_search 本地限流：每 user_key 每 10 秒最多 10 次。

    user_key 缺省为 ""（未透传用户时降到全局兜底），透传 username 后按用户隔离。
    键数超阈值时惰性清扫已过窗口的旧计数（活跃用户的窗口未过期不受影响）。
    """
    key = user_key or "_global"
    async with _search_rate_lock:
        now = time.time()
        if len(_search_rate_state) > _SEARCH_STATE_MAX_KEYS:
            expired = [k for k, (ws, _c) in _search_rate_state.items()
                       if now - ws > _SEARCH_WINDOW_SECONDS]
            for k in expired:
                del _search_rate_state[k]
        window_start, count = _search_rate_state.get(key, (0.0, 0))
        if now - window_start > _SEARCH_WINDOW_SECONDS:
            window_start, count = now, 0
        if count >= _SEARCH_MAX_PER_WINDOW:
            raise RuntimeError("搜索过于频繁，请稍后再试")
        _search_rate_state[key] = (window_start, count + 1)


async def web_search(query: str, max_results: int = 5, user_key: str = ""):
    """
    联网搜索工具 — 当用户询问实时信息、营业时间、评价、排队情况等
    现有工具无法覆盖的内容时调用。

    主路径：duckduckgo_search 的 DDGS 是纯同步客户端（8.x 只导出 DDGS，
    没有 AsyncDDGS/atext），阻塞调用放 asyncio.to_thread 执行；
    未安装/无结果/异常时降级 httpx 直连 Instant Answer API。
    user_key 为调用方用户名，用于按用户限流（缺省走全局兜底）。
    """
    await _check_search_rate(user_key)  # 本地限流（P1 #13/#40）
    try:
        from duckduckgo_search import DDGS

        def _ddg_text() -> list:
            # 同步阻塞搜索放线程池，避免卡住事件循环
            with DDGS() as ddgs:
                return ddgs.text(query, region='cn-zh', max_results=max_results) or []

        raw = await asyncio.to_thread(_ddg_text)
        results = [{
            "title": r.get("title", ""),
            "body": r.get("body", "")[:500],
            "href": r.get("href", ""),
        } for r in raw]
        if results:
            logger.info(f"联网搜索完成: query={query[:30]}, results={len(results)}")
            return {"results": results, "total": len(results)}
        logger.info("DDGS 无结果，降级 Instant Answer")
    except ImportError:
        logger.warning("duckduckgo_search 未安装，降级 Instant Answer")
    except Exception as e:
        logger.warning(f"DDGS 搜索失败，降级 Instant Answer: {e}")

    return await _instant_answer(query)


async def _instant_answer(query: str) -> dict:
    """降级：DuckDuckGo Instant Answer API（通常只返回一条摘要，搜索能力弱于主路径）"""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1"},
            )
            data = resp.json()
        results = []
        abstract = data.get("AbstractText", "")
        if abstract:
            results.append({
                "title": data.get("Heading", "摘要"),
                "body": abstract[:500],
                "href": data.get("AbstractURL", ""),
            })
        for topic in data.get("RelatedTopics", [])[:3]:
            if "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:100],
                    "body": topic.get("Text", "")[:500],
                    "href": topic.get("FirstURL", ""),
                })
        return {"results": results, "total": len(results)}
    except Exception as e:
        # httpx 部分异常 str 为空，补类型名保证日志可排查
        logger.error(f"联网搜索失败: {type(e).__name__}: {e}")
        # 异常 str 可能带含 key 的完整 URL，前端只给类型名（细节已进日志）
        return {"error": type(e).__name__, "results": [], "total": 0}


# ============================================================
# 项目知识库搜索（求职场景用，Embedding 向量召回 + pg_trgm 兜底）
# ============================================================
async def _generate_embedding(text: str):
    """调用 Ollama Embedding 生成向量。

    localhost 优先：容器内 localhost 连接拒绝是即时的（随后试 host.docker.internal），
    而反序在主机上会对 host.docker.internal 空等到连接超时（实测每查询白等 15s）。
    超时收窄到 5s：正常嵌入 <1s，给慢机留裕量即可，不该拖住整条检索。
    """
    import httpx
    urls = [
        "http://localhost:11434/api/embeddings",
        "http://host.docker.internal:11434/api/embeddings",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.post(
                    url,
                    json={"model": "shaw/dmeta-embedding-zh", "prompt": text[:512]}
                )
                resp.raise_for_status()
                return resp.json()["embedding"]
        except Exception:
            continue
    return None


async def _project_embedding_search(pool, query: str, top_k: int) -> list:
    """项目知识库 Embedding 向量召回（Ollama）；失败或无结果返回 []，由调用方降级"""
    try:
        emb = await _generate_embedding(query)
        if not emb:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_key, source, heading, content, "
                "1 - (embedding <=> $1::vector) as sim "
                "FROM knowledge_chunks WHERE source = 'project' "
                "AND embedding IS NOT NULL "
                "ORDER BY embedding <=> $1::vector "
                "LIMIT $2",
                json.dumps(emb), max(top_k * 2, 10)
            )
        return [{
            "heading": r["heading"],
            "content": r["content"][:800],
            "similarity": round(r["sim"], 4) if r["sim"] else 0
        } for r in rows if r["sim"] and r["sim"] > 0.3]
    except Exception as e:
        logger.warning(f"Embedding 检索失败，降级: {e}")
        return []


async def _project_keyword_fill(pool, query: str, results: list, top_k: int) -> None:
    """项目知识库关键词 ILIKE 兜底：提取 2-4 字中文片段（长词优先），补满 top_k 即停"""
    keywords = set()
    raw = query.replace("?", "").replace("？", "").replace("的", "").replace("怎么", "")
    # 按空格拆分
    for part in raw.split():
        if len(part) >= 2:
            keywords.add(part)
    # 滑动窗口提取 2-4 字片段
    for i in range(len(raw)):
        for j in range(2, 5):
            if i + j <= len(raw):
                kw = raw[i:i+j]
                if len(kw) >= 2:
                    keywords.add(kw)
    # 优先用长关键词
    keywords = sorted(keywords, key=len, reverse=True)[:8]

    if not keywords:
        return
    async with pool.acquire() as conn:
        for kw in keywords:
            more = await conn.fetch(
                "SELECT chunk_key, source, heading, content, 0.5 as sim "
                "FROM knowledge_chunks WHERE source = 'project' "
                "AND content ILIKE $1 LIMIT $2",
                f"%{kw}%", top_k - len(results)
            )
            for r in more:
                heading, content = r["heading"], r["content"][:800]
                if not any(e["heading"] == heading and e["content"][:50] == content[:50] for e in results):
                    results.append({"heading": heading, "content": content, "similarity": 0.5})
            if len(results) >= top_k:
                break


async def search_project_knowledge(query: str, top_k: int = 5):
    """
    搜索项目知识库 — 当访客询问项目技术细节时调用。
    知识库涵盖：支付系统架构、并发安全、异常处理、技术栈等。
    优先使用 Embedding 向量检索，降级到 pg_trgm + ILIKE。
    """
    from ..core.db import get_pool
    pool = await get_pool()
    results = await _project_embedding_search(pool, query, top_k)
    method = "embedding" if results else "pg_trgm"

    # 第二优先/降级：pg_trgm 相似度检索（embedding 部分命中时为 hybrid 补充）
    if len(results) < top_k:
        if results:
            method = "hybrid"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_key, source, heading, content, "
                "similarity(content, $1) as sim "
                "FROM knowledge_chunks WHERE source = 'project' "
                "AND content % $1 "
                "ORDER BY sim DESC LIMIT $2",
                query, max(top_k * 3, 15)
            )
            for r in rows:
                heading, content = r["heading"], r["content"][:800]
                sim = round(r["sim"], 4) if r["sim"] else 0
                if not any(e["heading"] == heading and e["content"][:50] == content[:50] for e in results):
                    results.append({"heading": heading, "content": content, "similarity": sim})

    # 关键词补充兜底（拆分为 2-4 字片段，提高中文匹配率）
    if len(results) < top_k:
        await _project_keyword_fill(pool, query, results, top_k)

    results = sorted(results, key=lambda x: x["similarity"], reverse=True)[:top_k]
    logger.info(f"项目知识库检索: query={query[:30]}, method={method}, results={len(results)}")
    return {"results": results, "method": method}