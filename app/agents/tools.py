import os
import json
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
            lives = weather_data.get("lives") or []
            if not lives:
                # lives 为空数组（2026-09-07 审查 P2）：原先 [0] IndexError 落
                # "天气查询失败"，实际语义是"该地区无天气数据"
                return {"error": f"{city} 暂无天气数据"}
            live = lives[0]
            # 港澳台：高德无天气数据（lives 缺字段）；geo 解析会落邻城（港→深/澳→珠），
            # 此时如实标注数据来源城市，由访客自行参考
            if not live.get("weather") or not live.get("temperature"):
                return {"error": f"{city} 暂无天气数据"}
            return {
                "city": live.get("city", city),
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
        # ILIKE 通配符转义（2026-09-07 审查 P2）：用户关键词含 %/_ 时原样拼进
        # 模式可构造成全表模糊匹配（性能面）；ESCAPE 声明转义字符
        kw_esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args = [f"%{kw_esc}%", recall_limit - len(candidates)]
        if permissions:
            args.append(permissions)
        more = await conn.fetch(
            "SELECT chunk_key, source, heading, content, 0.5 as sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            + _perm_clause(permissions) +
            "AND content ILIKE $1 ESCAPE '\\' LIMIT $2",
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

# 触发式改写重试阈值（2026-09-07 决策：不默认全量开 LLM 查询改写，仅双路召回都低置信时
# 触发一次改写重试，延迟只付给失败的查询）。
# ponytail: 阈值未经标注集标定（经验初值），上线后用 tests/run_retrieval_eval.py 在标注集上
# 校准触发率与改写收益；KB_REWRITE_* 环境变量可无码调整。
REWRITE_TRIGGER_TRGM_SIM = float(os.getenv("KB_REWRITE_TRGM_SIM", "0.45"))
REWRITE_TRIGGER_VEC_SIM = float(os.getenv("KB_REWRITE_VEC_SIM", "0.50"))

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


def _is_low_confidence(max_trgm_sim: float, max_vec_sim: float) -> bool:
    """双路召回最高分均低于阈值 → 低置信（触发式改写重试的门禁，2026-09-07 决策）。

    语义：任一路"强命中"即不打扰——trgm 词面强命中或向量语义强命中任一存在，
    就没必要花一次 LLM 调用改写；严格小于阈值（等于阈值视为有信号）。
    """
    return (max_trgm_sim < REWRITE_TRIGGER_TRGM_SIM
            and max_vec_sim < REWRITE_TRIGGER_VEC_SIM)


def _clean_rewrite(text: str, original: str) -> str:
    """清洗 LLM 改写输出：取首行、剥引号与"改写："类前缀、限长 64 字。

    空串/与原查询等价/过短 → 返回空串（调用方不重试）。
    """
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip()
    line = line.strip('"“”\'「」《》')
    for prefix in ("改写：", "改写:", "查询：", "检索："):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    line = line.rstrip("。").strip('"“”')
    if not line or line == original or len(line) < 2:
        return ""
    return line[:64]


async def _rewrite_query_for_recall(query: str, username: str = "") -> str:
    """低置信时的一次 LLM 查询改写（口语 → 条文术语风格检索表述），失败一律返回空串。

    改写只用于召回重试；重排仍用用户原话（cross-encoder 需要真实问题语义）。
    post_chat_completion 自带主/备模型降级；任何失败 fail-open，检索主流程不受影响。
    username 供扣费（2026-09-09 主人拍板：内部 LLM 调用计入计费）。
    """
    try:
        from ..services.chat_support import post_chat_completion
        from ..core.config import HTTP_TIMEOUT_SHORT
        ok, data = await post_chat_completion(
            {"model": None,
             "messages": [{"role": "user", "content":
                           "把用户的口语问题改写成《民法典》条文术语风格的检索查询，"
                           "只输出改写后的查询本身，不要解释、不要引号。\n"
                           f"用户问题：{query}"}],
             "temperature": 0, "max_tokens": 80},
            timeout=HTTP_TIMEOUT_SHORT)
        if not ok:
            logger.debug(f"检索改写上游失败（跳过重试）: {str(data)[:120]}")
            return ""
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # 计入计费（2026-09-09 主人拍板）：检索改写属用户请求触发，本路径无既有指标走全量入口
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        return _clean_rewrite(content, query)
    except Exception as e:
        logger.debug(f"检索改写异常（跳过重试）: {type(e).__name__}")
        return ""


async def _recall_two_ways(conn, search_query: str, recall_limit: int,
                           permissions: list | None) -> tuple:
    """双路召回：trgm（+ILIKE 关键词补充）与向量，返回 (trgm, vector, max_trgm_sim, max_vec_sim)。

    max_trgm_sim 在关键词补充前取值：ILIKE 命中固定 similarity=0.5，混入会虚高
    触发判断（词面命中是弱信号，不该被当成"高置信"压制改写重试）。
    """
    trgm = await _recall_pg_trgm(conn, search_query, recall_limit, permissions)
    max_trgm = max((c["similarity"] for c in trgm), default=0.0)
    await _keyword_fill(conn, search_query, recall_limit, trgm, permissions)
    embedding = await _generate_embedding(search_query)
    vector = await _recall_pg_vector(conn, embedding, recall_limit, permissions)
    max_vec = max((c["similarity"] for c in vector), default=0.0)
    return trgm, vector, max_trgm, max_vec


async def _search_two_legs(conn, query: str, search_query: str,
                           top_k: int, permissions: list | None,
                           username: str = "") -> dict:
    """双路召回（trgm + 向量）→ RRF 融合 → 本地重排 RRF 前 5。

    触发式改写重试（2026-09-07 决策）：双路召回都低置信时，才花一次 LLM 调用把口语
    改写成条文术语风格重试一轮，四路结果重新 RRF 融合——延迟只付给失败查询，
    不默认全量开。method 标签带 rewrite_retry，供评测统计触发率与改写收益。
    """
    recall_limit = max(RECALL_LIMIT, top_k)
    trgm, vector, max_trgm, max_vec = await _recall_two_ways(
        conn, search_query, recall_limit, permissions)
    retried = False
    if _is_low_confidence(max_trgm, max_vec):
        rewritten = await _rewrite_query_for_recall(query, username=username)
        if rewritten:
            logger.info(f"检索低置信（trgm={max_trgm:.2f}, vec={max_vec:.2f}），触发改写重试: "
                        f"{search_query[:40]}... → {rewritten[:40]}...")
            trgm2, vec2, _, _ = await _recall_two_ways(
                conn, rewritten, recall_limit, permissions)
            merged = _rrf_merge(trgm, vector, trgm2, vec2)
            retried = True
        else:
            merged = _rrf_merge(trgm, vector)
    else:
        merged = _rrf_merge(trgm, vector)
    method = "trgm+vector" + ("+rewrite_retry" if retried else "")
    if not merged:
        return {"results": [], "method": method}
    top_candidates = merged[:RERANK_TOP]
    reranked = await _rerank_local(query, top_candidates)
    if reranked:
        return {"results": reranked[:top_k], "method": method + "+rrf+local_rerank"}
    return {"results": top_candidates[:top_k], "method": method + "+rrf"}


async def search_knowledge(query: str, top_k: int = 5, permissions: list | None = None,
                           username: str = ""):
    """语义搜索知识库：trgm + 向量双路召回 → RRF 融合 → 本地 bge-reranker 重排前 5。

    在检索前，先通过法律依据纠正映射表检查用户问题是否属于其他法律领域。
    如果命中映射表，直接返回纠正引导信息，不执行知识库搜索。

    permissions: None=不过滤（内部/admin）；[]=仅公开；['vip']=公开+vip 可检索。
    username 透传供改写重试扣费（2026-09-09 主人拍板）。
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
        return await _search_two_legs(conn, query, _search_query, top_k, permissions,
                                      username=username)


# web_search 子系统已拆至 web_search.py（2026-09-07：tools.py 超 600 行硬限，按子系统边界拆分），
# 此处 re-export 保持既有 import 路径兼容（stream_utils/dispatch_tool/历史测试）。
from .web_search import (  # noqa: F401
    _SEARCH_MAX_PER_WINDOW, _check_search_rate, _instant_answer,
    _search_rate_state, web_search,
)

# ============================================================
# 项目知识库搜索（求职场景用，Embedding 向量召回 + pg_trgm 兜底）
# ============================================================
async def _generate_embedding(text: str):
    """调用 Ollama Embedding 生成向量。

    URL 读配置 EMBEDDING_API_URL（2026-09-07 审查 P2：配置项原先存在但此路径
    硬编码两个 URL 不用）；自动派生另一台主机名做兜底——容器内 localhost 连接
    拒绝是即时的，主机上 host.docker.internal 会黑洞等超时，故配置值优先。
    超时收窄到 5s：正常嵌入 <1s，给慢机留裕量即可，不该拖住整条检索。
    """
    import httpx
    from ..core.config import EMBEDDING_API_URL, EMBEDDING_MODEL
    urls = [EMBEDDING_API_URL]
    alt_url = EMBEDDING_API_URL.replace("localhost", "host.docker.internal")
    if alt_url != EMBEDDING_API_URL:
        urls.append(alt_url)
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.post(
                    url,
                    json={"model": EMBEDDING_MODEL, "prompt": text[:512]}
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