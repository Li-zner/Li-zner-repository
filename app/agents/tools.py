import os
import json
import time
import asyncio
import httpx
from ..core.logging import setup_logging
from ..core.config import (
    HTTP_TIMEOUT_SHORT, HTTP_TIMEOUT_MEDIUM, HTTP_TIMEOUT_LONG,
    DEEPSEEK_API_BASE, DEEPSEEK_MODEL, RERANK_SIM_THRESHOLD,
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
        return {"error": str(e)}


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
    except Exception:
        pass  # 映射表检查失败不应影响正常检索
    return None


def _dedup(candidates: list, heading: str, content: str, sim: float) -> bool:
    """候选去重：heading 与内容前 50 字相同则跳过"""
    if any(e["heading"] == heading and e["content"][:50] == content[:50] for e in candidates):
        return False
    candidates.append({"heading": heading, "content": content[:500], "similarity": sim})
    return True


async def _recall_pg_trgm(conn, search_query: str, recall_limit: int,
                          permissions: list | None = None) -> list:
    """pg_trgm 相似度召回（带相似度分数）。

    permissions=None = 不过滤（内部/admin）；[] = 仅公开；['vip'] = 公开+vip。
    """
    perm_clause = ""
    args = [search_query, recall_limit]
    if permissions:
        # 有权限组：公开 + 命中权限组（P0 #29/#41）
        perm_clause = " AND (COALESCE(permission, '{}') = '{}' OR permission && $3) "
        args.append(permissions)
    elif permissions is not None:
        # permissions=[]：仅公开，避免空数组 && 导致公开文档被排除（P0 #29）
        perm_clause = " AND COALESCE(permission, '{}') = '{}' "
    rows = await conn.fetch(
        "SELECT chunk_key, source, heading, content, source_doc, "
        "similarity(content, $1) as sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' "
        + perm_clause +
        "ORDER BY sim DESC LIMIT $2",
        *args,
    )
    return [{
        "heading": r["heading"],
        "content": r["content"][:500],
        "similarity": round(r["sim"], 4) if r["sim"] else 0,
        "source_doc": r["source_doc"] or r["source"],
    } for r in rows]


async def _keyword_fill(conn, query: str, recall_limit: int, candidates: list,
                        permissions: list | None = None):
    """关键词 ILIKE 补充（去重；补满 recall_limit 即停；支持权限过滤）"""
    perm_clause = ""
    for kw in query.replace("?", "").replace("，", " ").replace("？", " ").split():
        if len(kw) < 2:
            continue
        args = [f"%{kw}%", recall_limit - len(candidates)]
        if permissions:
            perm_clause = " AND (COALESCE(permission, '{}') = '{}' OR permission && $3) "
            args.append(permissions)
        elif permissions is not None:
            # permissions=[]：仅公开（P0 #29）
            perm_clause = " AND COALESCE(permission, '{}') = '{}' "
        more = await conn.fetch(
            "SELECT chunk_key, source, heading, content, 0.5 as sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            + perm_clause +
            "AND content ILIKE $1 LIMIT $2",
            *args,
        )
        for r in more:
            _dedup(candidates, r["heading"], r["content"], 0.5)
        if len(candidates) >= recall_limit:
            break


async def search_knowledge(query: str, top_k: int = 5, permissions: list | None = None):
    """语义搜索知识库（pg_trgm 召回 + 关键词补充 + DeepSeek Rerank）

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

    recall_limit = max(top_k * 4, 20)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # === 第一阶段：pg_trgm 宽召回 + 关键词补充 ===
        candidates = await _recall_pg_trgm(conn, _search_query, recall_limit, permissions)
        await _keyword_fill(conn, _search_query, recall_limit, candidates, permissions)

        # === 无结果 → 用口语映射后的专业术语重试 ===
        if not candidates:
            try:
                _mapped_query = _map_colloquial_to_legal(query)
                if _mapped_query != query:
                    candidates = await _recall_pg_trgm(conn, _mapped_query, recall_limit, permissions)
                    await _keyword_fill(conn, _mapped_query, recall_limit, candidates, permissions)
                    if candidates:
                        logger.info(f"口语映射后找到 {len(candidates)} 条结果")
            except Exception as _map_err:
                logger.warning(f"口语映射检索失败: {_map_err}")

        if not candidates:
            return {"results": [], "method": "pg_trgm"}

        # === 第二阶段：阈值粗筛 → 只在模糊时调用昂贵的 LLM Rerank ===
        # 粗筛：候选集中存在相似度 >= RERANK_SIM_THRESHOLD 的高置信结果时，
        # 说明 pg_trgm 已给出明确答案，直接按相似度排序截取，跳过 LLM Rerank（省 token）
        _top_sim = max(c["similarity"] for c in candidates)
        if _top_sim >= RERANK_SIM_THRESHOLD:
            candidates = sorted(candidates, key=lambda x: x["similarity"], reverse=True)[:top_k]
            method = "pg_trgm"
        else:
            # 检索结果模糊（低相似度）→ 升级 LLM Rerank 精排
            try:
                candidates = await _rerank_with_deepseek(query, candidates, top_k)
                method = "pg_trgm+rerank"
            except Exception as e:
                logger.warning(f"Rerank失败，使用pg_trgm原始排序: {e}")
                candidates = sorted(candidates, key=lambda x: x["similarity"], reverse=True)[:top_k]
                method = "pg_trgm"

        return {"results": candidates, "method": method}


async def _rerank_with_deepseek(query: str, candidates: list, top_k: int) -> list:
    """使用 DeepSeek 对候选结果进行重排序"""
    import os, json, httpx

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set")

    # 构建评分 prompt
    items_text = "\n\n".join([
        f"[{i+1}] {c['heading']}\n{c['content'][:300]}"
        for i, c in enumerate(candidates)
    ])
    prompt = (
        "你是一个法律知识检索重排序专家。请判断以下候选段落与用户查询的相关性。\n\n"
        f"用户查询：{query}\n\n"
        f"候选段落：\n{items_text}\n\n"
        "请对每个候选段落给出相关性评分（0-10分，10分最相关），只返回JSON格式的评分数组，不要其他文字。\n"
        "格式：{\"scores\": [分数1, 分数2, ...]}（分数顺序与候选段落一一对应）"
    )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_LONG) as client:
        resp = await client.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1024
            }
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)

    scores = result.get("scores", [])
    if not scores or len(scores) != len(candidates):
        # 空/不匹配评分直接失败，由调用方回退 pg_trgm 排序（P1 #42）
        raise ValueError(f"评分数量({len(scores)})与候选数({len(candidates)})不匹配")

    # 合并评分并排序
    for i, c in enumerate(candidates):
        c["rerank_score"] = round(scores[i], 2) if isinstance(scores[i], (int, float)) else 0

    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    return candidates[:top_k]


# web_search 本地限流（P1 #13/#40：防高频调用导致外部搜索 API 封 IP）
_search_rate_lock = asyncio.Lock()
_search_rate_last = 0.0
_search_rate_count = 0
_SEARCH_WINDOW_SECONDS = 10.0
_SEARCH_MAX_PER_WINDOW = 10


async def _check_search_rate():
    """web_search 本地限流：每 10 秒最多 10 次（全局兜底；每用户限流需调用方传标识）"""
    global _search_rate_last, _search_rate_count
    async with _search_rate_lock:
        now = time.time()
        if now - _search_rate_last > _SEARCH_WINDOW_SECONDS:
            _search_rate_count = 0
            _search_rate_last = now
        if _search_rate_count >= _SEARCH_MAX_PER_WINDOW:
            raise RuntimeError("搜索过于频繁，请稍后再试")
        _search_rate_count += 1


async def web_search(query: str, max_results: int = 5):
    """
    联网搜索工具 — 当用户询问实时信息、营业时间、评价、排队情况等
    现有工具无法覆盖的内容时调用。

    使用 DuckDuckGo 搜索（免费，无需 API Key），返回结构化结果。
    """
    await _check_search_rate()  # 本地限流（P1 #13/#40）
    try:
        from duckduckgo_search import DDGS
        results = []
        async with DDGS() as ddgs:
            async for r in ddgs.atext(
                query,
                region='cn-zh',
                max_results=max_results,
            ):
                results.append({
                    "title": r.get("title", ""),
                    "body": r.get("body", "")[:500],
                    "href": r.get("href", ""),
                })
        if not results:
            # 降级：用 httpx 直接请求 DuckDuckGo API
            logger.info("DDGS 无结果，降级使用 httpx 直连")
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1"},
                )
                data = resp.json()
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

        logger.info(f"联网搜索完成: query={query[:30]}, results={len(results)}")
        return {"results": results, "total": len(results)}

    except ImportError:
        logger.warning("duckduckgo_search 未安装，降级使用 httpx 直连")
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
        logger.error(f"联网搜索失败: {e}")
        return {"error": str(e), "results": [], "total": 0}


# ============================================================
# 项目知识库搜索（求职场景用，Embedding 向量召回 + pg_trgm 兜底）
# ============================================================
async def _generate_embedding(text: str):
    """调用 Ollama Embedding 生成向量"""
    import httpx
    urls = [
        "http://host.docker.internal:11434/api/embeddings",
        "http://localhost:11434/api/embeddings",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                resp = await client.post(
                    url,
                    json={"model": "shaw/dmeta-embedding-zh", "prompt": text[:512]}
                )
                resp.raise_for_status()
                return resp.json()["embedding"]
        except Exception:
            continue
    return None


async def search_project_knowledge(query: str, top_k: int = 5):
    """
    搜索项目知识库 — 当访客询问项目技术细节时调用。
    知识库涵盖：支付系统架构、并发安全、异常处理、技术栈等。
    优先使用 Embedding 向量检索，降级到 pg_trgm + ILIKE。
    """
    from ..core.db import get_pool
    pool = await get_pool()
    results = []
    method = "embedding"

    # 第一优先：Embedding 向量检索
    try:
        emb = await _generate_embedding(query)
        if emb:
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
                results = [{
                    "heading": r["heading"],
                    "content": r["content"][:800],
                    "similarity": round(r["sim"], 4) if r["sim"] else 0
                } for r in rows if r["sim"] and r["sim"] > 0.3]
    except Exception as e:
        logger.warning(f"Embedding 检索失败，降级: {e}")
        method = "fallback"

    # 第二优先/降级：pg_trgm 相似度检索
    if len(results) < top_k:
        method = "pg_trgm" if not results else "hybrid"
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
        # 从查询中提取有意义的 2-4 字中文词组
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
        
        if keywords:
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

    results = sorted(results, key=lambda x: x["similarity"], reverse=True)[:top_k]
    logger.info(f"项目知识库检索: query={query[:30]}, method={method}, results={len(results)}")
    return {"results": results, "method": method}