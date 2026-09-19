import os
import threading
import contextvars
import json
import asyncio
import time
from ..core.logging import setup_logging
from ..core.config import EMBEDDING_MODEL
from .article_normalizer import normalize_article_ref
from .retrieval_diagnostics import chunk_keys, diagnostics_enabled
from .retrieval_trace import _LAST_LEGS, record as record_retrieval
from ..services.rag_request_trace import record_span
from ..services.rag_runtime_config import get_rerank_top

logger = setup_logging()

# 天气子系统已拆至 weather.py（2026-09-10：tools.py 超 600 行硬限，按子系统边界
# 拆分）；此处 re-export 保持既有 import 路径兼容（stream_utils/dispatch_tool/测试）。
from .weather import (  # noqa: F401
    fetch_weather_async, _geocode_contains,
)

# 扩召子系统（条号直钉/邻接/章扩/保护带）已拆至 kb_expand.py（2026-09-14 防熵拆分）；旧名 re-export 兼容。
from .kb_expand import (  # noqa: F401
    NEIGHBOR_ENABLED, NEIGHBOR_GUARD, NEIGHBOR_TOP,
    CHAPTER_EXPAND_ENABLED, CHAPTER_EXPAND_K,
    _apply_neighbor_guard, _cosine, _expand_chapter, _expand_neighbors,
    _promote_pinned,
)


# ============================================================
# 口语化表述 → 专业术语映射表
# ============================================================
_COLLOQUIAL_MAP = None
_COLLOQUIAL_MAP_PATH = None
_LOAD_LOCK = threading.Lock()

def _load_colloquial_map() -> dict:
    """加载口语化表述映射表（tests/民法典映射表.txt）

    2026-09-12 深检 P2：首调并发竞态修复——构建在局部 dict 完成后一次性发布。
    原写法锁内置空 dict 后在锁外填充：并发快路径会把空/部分 dict 当作已加载
    返回（口语化映射对该请求静默失效）。双检锁与 law_mapping.py 同款。
    """
    global _COLLOQUIAL_MAP
    if _COLLOQUIAL_MAP is not None:
        return _COLLOQUIAL_MAP
    with _LOAD_LOCK:
        if _COLLOQUIAL_MAP is not None:
            return _COLLOQUIAL_MAP
        mapping = {}
        path = _COLLOQUIAL_MAP_PATH or os.path.join(
            os.path.dirname(__file__), "..", "..", "tests", "民法典映射表.txt"
        )
        if not os.path.exists(path):
            # 回退到容器内路径
            path = "/app/tests/民法典映射表.txt"
        if os.path.exists(path):
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
                            mapping[colloquial] = legal
        _COLLOQUIAL_MAP = mapping
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


def _perm_clause(permissions: list | None, param_index: int = 3) -> str:
    """知识库权限过滤 SQL 片段（P0 #29/#41）。

    None=不过滤（内部/admin）；[]=仅公开（空数组 && 会漏掉公开文档，须等值判断）；
    非空=公开+命中权限组。三处调用（trgm/ILIKE/向量）占位符布局一致：
    trgm/ILIKE 默认布局为 $1 检索参数、$2 limit、$3 权限数组；
    向量路前面多了 embedding_model，调用时传 param_index=4。
    """
    if permissions is None:
        return ""
    if permissions:
        return (
            " AND (COALESCE(permission, '{}') = '{}' "
            f"OR permission && ${param_index}) "
        )
    return " AND COALESCE(permission, '{}') = '{}' "


async def _recall_pg_trgm(conn, search_query: str, recall_limit: int,
                          permissions: list | None = None) -> list:
    """pg_trgm 词面召回（带相似度分数）。

    打分方式经实测对比（2026-09-12，55 条民法典标注用例）：
      similarity 整体重叠             → recall@5 = 0.7212
      word_similarity 片段最大匹配     → recall@5 = 0.7121
      GREATEST(两者)                  → 0.7121（**等价于只用 word_similarity**）

    最后一条值得记住：word_similarity 在所有块上恒高于 similarity（相关块 0.583 vs
    0.047、无关块 0.083 vs 0.012），尺度不同，取 max 会被它完全主导，不是真正的组合。

    word_similarity 在**短关键词查询**上确实明显更强（「第五百三十三条」时该条在
    similarity 下全库排第 22 名、召回窗 15 进不了池，word_similarity 排第 1），
    但本知识库用例以自然语言长句为主，整体上 similarity 更稳，故维持后者。
    若要两全需按 RRF 做**双路融合**（而非取 max），净收益约 ±1%，暂不引入额外查询开销。

    permissions=None = 不过滤（内部/admin）；[] = 仅公开；['vip'] = 公开+vip。
    """
    args = [search_query, recall_limit]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, source, heading, content, source_doc, "
        "similarity(content, $1) as sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' AND valid "
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
    args = [json.dumps(query_embedding), recall_limit, EMBEDDING_MODEL]
    if permissions:
        args.append(permissions)
    # 2026-09-12 去近似化（ragclosure/reports/召回率提升分析.md §九）：civil 通道仅
    # ~1300 行，ivfflat(lists=100) 默认 probes=1 只扫 ~1 个簇，纯属负收益——实测金条
    # 位次随 LIMIT 漂移（17↔22↔9），8/11 零命中用例的金条连近似 top-60 都进不了。
    # probes 拉满 = 全簇扫描（结果等价精确搜索），该量级耗时毫秒级。
    # ponytail: 行数过 5 万时需重建 lists 并重估此设置，或调回近似 + 调 probes。
    async with conn.transaction():
        await conn.execute("SET LOCAL ivfflat.probes = 1000")
        rows = await conn.fetch(
            "SELECT chunk_key, source, heading, content, source_doc, "
            "1 - (embedding <=> $1::vector) AS sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            "AND valid AND embedding IS NOT NULL "
            "AND embedding_model = $3 "
            + _perm_clause(permissions, 4) +
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


def _rrf_merge(*ranked_lists: list, k: int = 60, weights: tuple | None = None) -> list:
    """RRF 倒数排名融合：trgm 相似度 / 向量余弦 / ILIKE 命中不在同一度量空间，
    按排名位置融合回避归一化；k=60 削弱单路榜首 dominance（社区经验值）。

    weights（2026-09-12 新增，按路给权重）：trgm 路的排序**接近随机**——实测
    `similarity()` 对"短查询 vs 长法条"是相关块 0.047 vs 无关块 0.012，区分度仅
    4 倍且都落在噪声区。与向量路**等权**融合等于把噪声抬进前排。在 50 条标注 trace
    上离线模拟：trgm 权重降到 0.3~0.5 时 RRF 前 5 命中率 0.78 → 0.82；完全归零
    反而降到 0.80（仍有少量信号，只是不该等权）。默认 None = 保持等权（行为不变）。
    """
    if weights is None:
        weights = (1.0,) * len(ranked_lists)
    scores = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, item in enumerate(lst):
            e = scores.setdefault(item["chunk_key"], {"item": item, "score": 0.0})
            e["score"] += w / (k + rank + 1)
    return [e["item"] for e in sorted(scores.values(), key=lambda x: -x["score"])]


async def _keyword_fill(conn, query: str, recall_limit: int, candidates: list,
                        permissions: list | None = None):
    """关键词 ILIKE 补充（去重；补满 recall_limit 即停；支持权限过滤）

    2026-09-12 诊断结论（**该函数当前恒不产出结果，两处原因，尚未修复**）：

    ① 配额被饿死：`recall_limit - len(candidates)` 恒为 0——调用方
       `_recall_two_ways` 先用 `_recall_pg_trgm` 取满 recall_limit 条（pg_trgm 对
       任何查询都能排满，哪怕最高分只有 0.001），随后 `len(candidates) >=
       recall_limit` 立即 break。实测两个真实查询各新增 0 条。
    ② 提取的不是关键词：按标点切分后拿**整个句子**去 ILIKE 匹配，而中文没有词间
       空格，`'楼上漏水把我家泡了'` 这类整句在法条原文里并不存在。

    已尝试并**回滚**的修法：改用 `word_similarity` 做片段匹配——长查询下同样无效
    （其高分只在短查询上出现），且会移除本函数的 ILIKE 通配符转义防护
    （test_keyword_fill_escapes_like_wildcards 覆盖，2026-09-07 审查 P2）。
    正确方向是**先做中文片段切分再匹配**，属独立改造，需单独立项。
    """
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
            "FROM knowledge_chunks WHERE source = 'civil_code' AND valid "
            + _perm_clause(permissions) +
            "AND content ILIKE $1 ESCAPE '\\' LIMIT $2",
            *args,
        )
        for r in more:
            _dedup(candidates, r["chunk_key"], r["heading"], r["content"], 0.5)
        if len(candidates) >= recall_limit:
            break


# 召回窗 15 / 重排候选池（2026-09-06 用户决策：原 max(top_k*4,20) 召回 + LLM 全量精排
# 过大过贵——LLM rerank 曾 100% 超时，每查询白等 30s）
# 2026-09-12：改为环境变量可配。此前硬编码导致 tests/run_retrieval_eval.py 的
# TOP_K=20 形同虚设——最终返回被 RERANK_TOP 卡住，recall@10/@20 恒等于 recall@5。
# 2026-09-12 二次调整（重排候选池 5→15）：实测重排只看 RRF 前 5 时**集合被锁死**
# ——正确法条在融合池位次 6~26 的 10 条失败用例重排根本无权挽救（0 打坏 0 救回）；
# 扩到 15 并配合 RRF×CE 线性融合（见 _blend_rrf_ce 的实测数据），recall@5 0.7633→0.7933。
# 候选池变大增加 CPU 重排耗时（15 对约 3~5s），KB_RERANK_TOP 可无码调回。
RECALL_LIMIT = int(os.getenv("KB_RECALL_LIMIT", "15"))
# 2026-09-15 holdout 45 条复测：
#   TOP=8  recall@5 0.8259 / MRR 0.7889 / avg 1103ms
#   TOP=12 recall@5 0.8630 / MRR 0.8519 / avg 2337ms
#   TOP=15 recall@5 0.8852 / MRR 0.8624 / avg 3062ms
# 2026-09-15 最终漏斗补映射后：TOP=15 recall@10 0.9630 / MRR 0.9037。
# 法律场景优先召回，默认取 15；运行期仍可安全调整。
RERANK_TOP = int(os.getenv("KB_RERANK_TOP", "15"))

# RRF 位次分 × cross-encoder 分数的线性融合权重（1.0 = 纯 CE 序）
# 2026-09-12 首轮实测（50 条标注）：纯 RRF 0.7633 / 纯 CE 0.7300~0.7400 / 0.2~0.6 宽
# 平台 0.7933，当时默认 0.4 取平台中点。
# 2026-09-12 二次调参（train 100 条离线扫描，CE 序×RRF 序全组合复算，采集快照
# tests/rerank_sweep_train.jsonl）：blend 0.55~0.65 平台 recall@5=0.8895（0.4 时
# 0.8781，净 +4/-2 条），MRR 基本中性（0.7847→0.7827）；0.7 再多救 1 条但 MRR -0.02，
# 不取。默认升到 0.6（平台中点）。holdout 验收见 ragclosure/reports/召回率提升分析.md §八。
RERANK_BLEND = float(os.getenv("KB_RERANK_BLEND", "0.6"))

# RRF 融合时 trgm 路的权重（向量路固定 1.0）
# 2026-09-12：trgm 排序接近随机（见 _rrf_merge docstring 的实测数据），等权融合会把
# 噪声抬进前排。离线模拟 0.3~0.5 区间最优（RRF 前 5 命中率 0.78 → 0.82）。
RRF_TRGM_WEIGHT = float(os.getenv("KB_RRF_TRGM_WEIGHT", "0.4"))

# 高分 CE 保护带（2026-09-16）：实现与实测数据见 kb_ce_band.py；tools.py 只留接线。
# 默认关闭，验收通过后再改默认值（同邻接/章扩召的处置惯例）。
from .kb_ce_band import (  # noqa: E402  (常量须在下方接线前就位)
    CE_BAND_ENABLED, CE_BAND_LEG_TOP, CE_BAND_MARGIN, _promote_high_ce,
)


# 2026-09-16 重排层拆到 kb_rerank.py，此处 re-export 保持 tools.* 调用路径不变。
from .kb_rerank import (  # noqa: E402,F401
    RERANK_MODEL, RERANK_QUANT, REWRITE_TRIGGER_TRGM_SIM, REWRITE_TRIGGER_VEC_SIM,
    _blend_rrf_ce, _clean_rewrite, _get_reranker, _is_low_confidence,
    _rerank_local, _rewrite_query_for_recall, _select_rerank_candidates,
)




async def _recall_two_ways(conn, search_query: str, recall_limit: int,
                           permissions: list | None) -> tuple:
    """双路召回：trgm（+ILIKE 关键词补充）与向量，返回 (trgm, vector, max_trgm_sim, max_vec_sim)。

    max_trgm_sim 在关键词补充前取值：ILIKE 命中固定 similarity=0.5，混入会虚高
    触发判断（词面命中是弱信号，不该被当成"高置信"压制改写重试）。
    """
    # trgm 只占本机 PG，embedding 是外部 HTTP，二者互不依赖，可并行启动。
    trgm, embedding = await asyncio.gather(
        _recall_pg_trgm(conn, search_query, recall_limit, permissions),
        _generate_embedding(search_query),
    )
    max_trgm = max((c["similarity"] for c in trgm), default=0.0)
    await _keyword_fill(conn, search_query, recall_limit, trgm, permissions)
    vector = await _recall_pg_vector(conn, embedding, recall_limit, permissions)
    max_vec = max((c["similarity"] for c in vector), default=0.0)
    _LAST_EMBEDDING.set(embedding)  # 供章内扩召复用，免二次 embedding 调用
    return trgm, vector, max_trgm, max_vec


# 章内余弦扩召（2026-09-13）：top1 定章 → 章内成员按与查询向量余弦择优入池，
# 让 CE×RRF 公平裁决（实现与实测数据见 kb_expand.expand_chapter）。
_LAST_EMBEDDING: contextvars.ContextVar = contextvars.ContextVar("kb_last_embedding",
                                                                default=None)


async def _recall_and_fuse(conn, query: str, search_query: str, recall_limit: int,
                           permissions: list | None, username: str,
                           diagnostics: dict | None) -> tuple:
    """双路召回 → 低置信改写重试 → RRF 融合，返回 (recall_lists, merged, retried)。

    改写重试只在双路都低置信时触发，延迟只付给失败查询。_LAST_LEGS 与各阶段
    埋点在本函数内写，调用方不需要感知召回细节。
    """
    recall_started = time.perf_counter()
    trgm, vector, max_trgm, max_vec = await _recall_two_ways(
        conn, search_query, recall_limit, permissions)
    # 第 5 位是向量腿故障标志（2026-09-14 审计 P1）：embedding 生成失败时
    # _recall_pg_vector 静默返回空——不记标志，监测侧无法区分「没召回到」
    # 与「这一路挂了」（RC-1b 单腿故障规则依赖它）
    _vec_failed = _LAST_EMBEDDING.get() is None
    _LAST_LEGS.set((len(trgm), len(vector), max_trgm, max_vec, _vec_failed))
    if diagnostics is not None:
        diagnostics.update({
            "trgm": chunk_keys(trgm),
            "vector": chunk_keys(vector),
        })
    record_span(
        "recall",
        latency_ms=round((time.perf_counter() - recall_started) * 1000),
        result_count=len(trgm) + len(vector),
        attributes={
            "trgm_hits": len(trgm),
            "vec_hits": len(vector),
            "vec_failed": _vec_failed,
            "recall_limit": recall_limit,
        },
    )
    recall_lists = [trgm, vector]
    retried = False
    fusion_started = time.perf_counter()
    rewritten = ""
    if _is_low_confidence(max_trgm, max_vec):
        rewritten = await _rewrite_query_for_recall(query, username=username)
    if rewritten:
        logger.info(f"检索低置信（trgm={max_trgm:.2f}, vec={max_vec:.2f}），触发改写重试: "
                    f"{search_query[:40]}... → {rewritten[:40]}...")
        retry_started = time.perf_counter()
        trgm2, vec2, _, _ = await _recall_two_ways(
            conn, rewritten, recall_limit, permissions)
        record_span(
            "recall_retry",
            latency_ms=round((time.perf_counter() - retry_started) * 1000),
            result_count=len(trgm2) + len(vec2),
            attributes={"trgm_hits": len(trgm2), "vec_hits": len(vec2)},
        )
        recall_lists.extend([trgm2, vec2])
        if diagnostics is not None:
            diagnostics.update({
                "trgm_retry": chunk_keys(trgm2),
                "vector_retry": chunk_keys(vec2),
            })
        merged = _rrf_merge(trgm, vector, trgm2, vec2,
                            weights=(RRF_TRGM_WEIGHT, 1.0, RRF_TRGM_WEIGHT, 1.0))
        retried = True
    else:
        merged = _rrf_merge(trgm, vector, weights=(RRF_TRGM_WEIGHT, 1.0))
    record_span(
        "fusion",
        latency_ms=round((time.perf_counter() - fusion_started) * 1000),
        result_count=len(merged),
        attributes={"rewrite_retry": retried, "rrf_trgm_weight": RRF_TRGM_WEIGHT},
    )
    if diagnostics is not None:
        diagnostics["rrf"] = chunk_keys(merged)
    return recall_lists, merged, retried


async def _expand_and_guard(conn, merged: list, search_query: str,
                            permissions: list | None, rerank_top: int,
                            method: str) -> tuple:
    """邻接/章级扩召 + 固定条保护，返回 (merged, method)。

    扩召会往池子里插新块，guard 先记下扩展前的靠前条目，扩展后压回窗口内，
    避免扩召把原 RRF 高分条挤出候选。
    """
    guard = []
    if NEIGHBOR_ENABLED:
        guard = merged[:NEIGHBOR_GUARD]
        merged = await _expand_neighbors(conn, merged, permissions)
        method += "+neighbor"
    if CHAPTER_EXPAND_ENABLED:
        guard = guard or merged[:NEIGHBOR_GUARD]
        merged = await _expand_chapter(
            conn, merged, _LAST_EMBEDDING.get(), permissions)
        method += "+chapter"
    merged = _promote_pinned(merged, search_query)
    if guard:
        merged = _apply_neighbor_guard(merged, guard, rerank_top)
    return merged, method


async def _search_two_legs(conn, query: str, search_query: str,
                           top_k: int, permissions: list | None,
                           username: str = "") -> dict:
    """双路召回（trgm + 向量）→ RRF 融合 → 本地重排 RRF 前 5。

    触发式改写重试（2026-09-07 决策）：双路召回都低置信时，才花一次 LLM 调用把口语
    改写成条文术语风格重试一轮，四路结果重新 RRF 融合——延迟只付给失败查询，
    不默认全量开。method 标签带 rewrite_retry，供评测统计触发率与改写收益。
    """
    recall_limit = max(RECALL_LIMIT, top_k)
    rerank_top = await get_rerank_top(RERANK_TOP)
    diagnostics = {} if diagnostics_enabled() else None
    recall_lists, merged, retried = await _recall_and_fuse(
        conn, query, search_query, recall_limit, permissions, username, diagnostics)
    method = "trgm+vector" + ("+rewrite_retry" if retried else "")
    if not merged:
        return {
            "results": [],
            "method": method,
            **({"diagnostics": diagnostics} if diagnostics is not None else {}),
        }
    merged, method = await _expand_and_guard(
        conn, merged, search_query, permissions, rerank_top, method)
    top_candidates = _select_rerank_candidates(merged, recall_lists, rerank_top)
    if diagnostics is not None:
        diagnostics["rerank_candidates"] = chunk_keys(top_candidates)
    rerank_started = time.perf_counter()
    reranked = await _rerank_local(query, top_candidates)
    record_span(
        "rerank",
        latency_ms=round((time.perf_counter() - rerank_started) * 1000),
        result_count=len(reranked or []),
        attributes={"candidate_count": len(top_candidates), "rerank_top": rerank_top},
    )
    if reranked:
        if RERANK_BLEND > 0:
            reranked = _blend_rrf_ce(top_candidates, reranked, RERANK_BLEND)
        if CE_BAND_ENABLED:
            # 保护带只动 TopK 窗口内的成员，且条件是「CE 更高 + 单腿靠前」，
            # 不改变其余条目的融合序；被挤出的必然是窗口内融合分最低那条。
            # 形参必须是单腿召回列表：传融合后的 merged 会让 _best_leg_rank 对
            # dict 切片直接抛 TypeError（2026-09-19 审查 agents P2-2 实测复现）。
            reranked = _promote_high_ce(reranked, recall_lists, top_k,
                                        CE_BAND_LEG_TOP, CE_BAND_MARGIN)
            method += "+ce_band"
        final_results = reranked[:top_k]
        if diagnostics is not None:
            diagnostics["final"] = chunk_keys(final_results)
        return {
            "results": final_results,
            "method": method + "+rrf+local_rerank",
            **({"diagnostics": diagnostics} if diagnostics is not None else {}),
        }
    final_results = top_candidates[:top_k]
    if diagnostics is not None:
        diagnostics["final"] = chunk_keys(final_results)
    return {
        "results": final_results,
        "method": method + "+rrf",
        **({"diagnostics": diagnostics} if diagnostics is not None else {}),
    }


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

    # ===== 法条编号归一化（2026-09-12 修复）=====
    # normalize_article_ref 早已实现却从未接入检索链路（全仓仅单测调用），
    # 导致用户写"第533条"而库内是"第五百三十三条"时 trgm 词面路与 ILIKE
    # 关键词路双双失配（实测 similarity 0.0065 vs 0.0556，相差 8.5 倍），
    # 只剩向量一路投票，正确条文极易被挤出召回池。
    _normalized = normalize_article_ref(_search_query)
    if _normalized != _search_query:
        logger.info(f"法条编号归一化: {_search_query[:30]}... → {_normalized[:60]}...")
        _search_query = _normalized

    pool = await get_pool()
    _t0 = time.perf_counter()
    async with pool.acquire(timeout=5) as conn:
        res = await _search_two_legs(conn, query, _search_query, top_k, permissions,
                                     username=username)
    record_retrieval(query, res, _LAST_LEGS.get(), (time.perf_counter() - _t0) * 1000)
    return res


# web_search 子系统已拆至 web_search.py（2026-09-07：tools.py 超 600 行硬限，按子系统边界拆分），
# 此处 re-export 保持既有 import 路径兼容（stream_utils/dispatch_tool/历史测试）。
from .web_search import (  # noqa: F401
    _SEARCH_MAX_PER_WINDOW, _check_search_rate, _instant_answer,
    web_search,
)

# 通用查询 Embedding 客户端（求职助手下线后由 project_kb.py 迁入，2026-09-20）；
# 本模块向量腿 _recall_two_ways 直接调用，re-export 保持 patch/import 路径兼容。
from .kb_embedding import _generate_embedding  # noqa: F401
