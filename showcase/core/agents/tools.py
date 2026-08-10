import os
import json
import httpx
from ..core.logging import setup_logging
from ..core.config import HTTP_TIMEOUT_SHORT, HTTP_TIMEOUT_MEDIUM, HTTP_TIMEOUT_LONG

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


async def search_knowledge(query: str, top_k: int = 5):
    """语义搜索知识库（pg_trgm 召回 + DeepSeek Rerank）
    
    在检索前，先通过法律依据纠正映射表检查用户问题是否属于其他法律领域。
    如果命中映射表，直接返回纠正引导信息，不执行知识库搜索。
    """
    # ===== 法律依据纠正映射表检查 =====
    try:
        from .law_mapping import check_query as _check_law
        _match = _check_law(query)
        if _match:
            from ..core.logging import setup_logging as _setup_log
            _log = _setup_log()
            _log.info(f"⚖️ 法律映射表命中 #{_match['id']}: {_match['scenario']} → {_match['law']}")
            return {
                "results": [{
                    "heading": f"⚠️ 该问题不属于民法典调整范围",
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
    except Exception as _e:
        # 映射表检查失败不应影响正常检索
        pass

    import math, json, os
    from ..core.db import get_pool

    # ===== 口语→术语映射：优先使用映射后的查询进行检索 =====
    _mapped_query_first = _map_colloquial_to_legal(query)
    _search_query = _mapped_query_first  # 使用映射后的查询（如无映射则与原查询相同）
    if _search_query != query:
        logger.info(f"🔄 搜索前置口语映射: {query[:30]}... → {_search_query[:60]}...")
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # === 第一阶段：pg_trgm 宽召回（取 top_k*4 个候选）===
        recall_limit = max(top_k * 4, 20)
        rows = await conn.fetch(
            "SELECT chunk_key, source, heading, content, "
            "similarity(content, $1) as sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            "AND content % $1 "
            "ORDER BY sim DESC LIMIT $2",
            _search_query, recall_limit
        )
        candidates = [{
            "heading": r["heading"],
            "content": r["content"][:500],
            "similarity": round(r["sim"], 4) if r["sim"] else 0
        } for r in rows]

        # 补充关键词匹配（使用映射后的查询关键词）
        if len(candidates) < recall_limit:
            keywords = _search_query.replace("?", "").replace("，", " ").replace("？", " ").split()
            for kw in keywords:
                if len(kw) < 2:
                    continue
                more = await conn.fetch(
                    "SELECT chunk_key, source, heading, content, 0.5 as sim "
                    "FROM knowledge_chunks WHERE source = 'civil_code' "
                    "AND content ILIKE $1 LIMIT $2",
                    f"%{kw}%", recall_limit - len(candidates)
                )
                for r in more:
                    h = r["heading"]
                    c = r["content"][:500]
                    if not any(e["heading"] == h and e["content"][:50] == c[:50] for e in candidates):
                        candidates.append({"heading": h, "content": c, "similarity": 0.5})
                if len(candidates) >= recall_limit:
                    break

        if not candidates:
            # ===== 第一轮检索无结果 → 尝试口语化表述映射 =====
            try:
                _mapped_query = _map_colloquial_to_legal(query)
                if _mapped_query != query:
                    # 用映射后的专业术语重新检索
                    map_rows = await conn.fetch(
                        "SELECT chunk_key, source, heading, content, "
                        "similarity(content, $1) as sim "
                        "FROM knowledge_chunks WHERE source = 'civil_code' "
                        "AND content % $1 "
                        "ORDER BY sim DESC LIMIT $2",
                        _mapped_query, recall_limit
                    )
                    for r in map_rows:
                        h = r["heading"]
                        c = r["content"][:500]
                        if not any(e["heading"] == h and e["content"][:50] == c[:50] for e in candidates):
                            candidates.append({
                                "heading": h,
                                "content": c,
                                "similarity": round(r["sim"], 4) if r["sim"] else 0
                            })
                    # 补充关键词
                    if len(candidates) < recall_limit:
                        for kw in _mapped_query.replace("?", "").replace("，", " ").replace("？", " ").split():
                            if len(kw) < 2:
                                continue
                            more = await conn.fetch(
                                "SELECT chunk_key, source, heading, content, 0.5 as sim "
                                "FROM knowledge_chunks WHERE source = 'civil_code' "
                                "AND content ILIKE $1 LIMIT $2",
                                f"%{kw}%", recall_limit - len(candidates)
                            )
                            for r in more:
                                h = r["heading"]
                                c = r["content"][:500]
                                if not any(e["heading"] == h and e["content"][:50] == c[:50] for e in candidates):
                                    candidates.append({"heading": h, "content": c, "similarity": 0.5})
                            if len(candidates) >= recall_limit:
                                break
                    if candidates:
                        logger.info(f"✅ 口语映射后找到 {len(candidates)} 条结果")
            except Exception as _map_err:
                logger.warning(f"口语映射检索失败: {_map_err}")

        if not candidates:
            return {"results": [], "method": "pg_trgm"}

        # === 第二阶段：DeepSeek Rerank ===
        try:
            candidates = await _rerank_with_deepseek(query, candidates, top_k)
            method = "pg_trgm+rerank"
        except Exception as e:
            logger.warning(f"Rerank失败，使用pg_trgm原始排序: {e}")
            # 降级：直接截取 top_k
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
    if len(scores) != len(candidates):
        raise ValueError(f"评分数量({len(scores)})与候选数({len(candidates)})不匹配")

    # 合并评分并排序
    for i, c in enumerate(candidates):
        c["rerank_score"] = round(scores[i], 2) if isinstance(scores[i], (int, float)) else 0

    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    return candidates[:top_k]


async def web_search(query: str, max_results: int = 5):
    """
    联网搜索工具 — 当用户询问实时信息、营业时间、评价、排队情况等
    现有工具无法覆盖的内容时调用。
    
    使用 DuckDuckGo 搜索（免费，无需 API Key），返回结构化结果。
    """
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

        logger.info(f"🌐 联网搜索完成: query={query[:30]}, results={len(results)}")
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
    logger.info(f"📚 项目知识库检索: query={query[:30]}, method={method}, results={len(results)}")
    return {"results": results, "method": method}