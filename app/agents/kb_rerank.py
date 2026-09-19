"""民法典知识库重排层：CrossEncoder 重排、RRF 与 CE 融合、低置信改写。

2026-09-16 从 tools.py 拆出，保持单文件 600 行上限。本模块只做排序与改写，
不触碰数据库；召回函数仍由 tools.py 持有并调用。
"""
from __future__ import annotations

import asyncio
import os
import threading

from ..core.logging import setup_logging

logger = setup_logging()

# CrossEncoder 惰性加载锁；与召回侧 _LOAD_LOCK 相互独立。
_RERANKER_LOCK = threading.Lock()

# 推理锁（2026-09-19）：同一模型被多工作线程并发 predict 会抛
# RuntimeError: Already borrowed（int8 实测触发，A/B B 档 19:58），
# 进程内串行化即可——重排本就是 CPU 密集段，并发无收益。
_RERANK_INFER_LOCK = threading.Lock()


# 本地重排模型：默认保留轻量 bge-reranker-base；v2-m3 已实测 MRR +0.0555，
# 但 45 条 holdout 上 recall@5 不变、平均延迟 +7.8s，因此仅保留环境变量切换能力。
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")

# CE 动态 int8 量化开关（2026-09-12：面向 2G/1G 档云端——权重 ~4x 缩小 + CPU 提速，
# 同模型不新增下载）。质量代价须过 train 评测（recall@5 跌幅 >0.005 即不启用）。
RERANK_QUANT = os.getenv("KB_RERANK_QUANT", "0") == "1"

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
    # 2026-09-12 修复（外部复核 P1）：并发首调会各自加载一份模型（内存翻倍），
    # 双检锁保证单次加载
    with _RERANKER_LOCK:
        if _RERANKER is not None:
            return _RERANKER
        if _RERANKER_INIT_FAILED:
            return None
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            import torch
            from sentence_transformers import CrossEncoder
            _RERANKER = CrossEncoder(
                RERANK_MODEL, max_length=256)
            if RERANK_QUANT:
                # 动态 int8 量化（同模型压缩，非换模型）：权重 ~4x 缩小（1.1GB→~300MB），
                # CPU 推理加速，面向 2G/1G 档云端主机。质量代价由 train 评测把关。
                _RERANKER.model = torch.ao.quantization.quantize_dynamic(
                    _RERANKER.model, {torch.nn.Linear}, dtype=torch.qint8)
            return _RERANKER
        except Exception as e:
            _RERANKER_INIT_FAILED = True
            logger.warning(f"本地重排模型不可用（回退 RRF 排序）: {type(e).__name__}: {str(e)[:120]}")
            return None


async def _rerank_local(query: str, candidates: list) -> list:
    """本地 cross-encoder 精排 RRF 候选。

    默认 bge-reranker-base（278M/1.1GB）兼顾当前 4 核 CPU；可用 RERANK_MODEL
    切换 v2-m3 等 CrossEncoder 模型。模型不可用时返回 []，调用方保持 RRF 排序，
    因此升级失败不会阻断检索。

    2026-09-17：开关缺省值由 "1" 改为 "0"（缺省必须安全）。模型常驻约 1.1GB，
    2 核 1GB 实例上"忘了关"就会触发内存压力、表现为隧道 502
    （见 rag/生产部署记录-2026-09-14.md）。"没写这个键"应当等于"关闭"，
    而不是"开启"—— 指望每个部署模板都记得写上这一行是靠不住的。
    """
    if os.getenv("LOCAL_RERANK_ENABLED", "0") != "1":
        return []
    ranker = _get_reranker()
    if ranker is None or not candidates:
        return []

    def _predict() -> dict:
        with _RERANK_INFER_LOCK:
            scores = ranker.predict([(query[:256], c["content"][:300]) for c in candidates])
        return {c["chunk_key"]: float(s) for c, s in zip(candidates, scores)}

    try:
        score_by_key = await asyncio.to_thread(_predict)
    except Exception as e:
        # 推理运行时异常（2026-09-14 审计 P1）：原实现只保护模型加载，_predict 抛错会
        # 打断整条检索链。降级返回 [] 让调用方保持 RRF 排序，与"模型不可用"同语义
        logger.warning(f"本地重排推理失败（回退 RRF 排序）: {type(e).__name__}: {str(e)[:120]}")
        return []
    for c in candidates:
        c["rerank_score"] = round(score_by_key.get(c["chunk_key"], 0.0), 4)
    return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)


def _blend_rrf_ce(rrf_pool: list, ce_sorted: list, blend: float) -> list:
    """RRF 位次分 × cross-encoder 分数的线性融合，返回按融合分排序的列表。

    2026-09-12 新增。动因（50 条民法典标注、官方口径 recall@5 的决定性实验）：
      纯 RRF 序   = 0.7633
      纯 CE 序    = 0.7300~0.7400 —— CE 在难例上极准（CC010 的正确法条 CE 排第 1、
      RRF 排第 6），却在简单例上捣乱，单独使用净收益为负
      融合 0.2~0.6 宽平台 = 0.7933，两档候选池（15 / 全池）结论一致

    rrf_pool 是 RRF 序的完整候选池（位次即融合排名），ce_sorted 是 CE 打分排序后的
    同一批候选，两者是同一组 dict 对象。final = (1-blend)·rrf_norm + blend·ce，
    rrf_norm=(n-pos)/n；blend_score 落在候选对象上供保护带复用。
    """
    n = len(rrf_pool)
    pos = {c["chunk_key"]: i for i, c in enumerate(rrf_pool)}
    # blend_score 写在候选对象本身（而非只写 ce_sorted）：保护带需要按融合分定位插入点，
    # 而候选池里可能含 RRF 序有、CE 未打分（重排窗被截断）的条目，缺分会让排序 KeyError。
    for c in rrf_pool:
        rrf_n = (n - pos.get(c["chunk_key"], n)) / n if n else 0.0
        c["blend_score"] = round((1 - blend) * rrf_n
                                 + blend * c.get("rerank_score", 0.0), 4)
    return sorted(rrf_pool, key=lambda c: -c["blend_score"])


def _select_rerank_candidates(merged: list, recall_lists: list[list],
                              limit: int) -> list:
    """按来源挑选 CE 候选：RRF 头部 + 每条召回腿头部，控制数量而不丢关键候选。

    固定取 RRF 前 N 个会漏掉“单腿排名高、融合排名低”的法条；直接重排整个大池
    又会把 CPU 时间浪费在低价值候选上。候选池 8 时按 RRF 2 + trgm 3 + vector 3
    配比；12~19 恢复 RRF top-N，避免把 Batch 3 已证明有效的第 9~15 位切掉；
    20 条窗口保留 RRF 前 14，再给两路各 3 个席位，回收“单腿高位、RRF 被稀释”的金条。
    """
    if limit <= 0:
        return []
    if 12 <= limit < 20:
        return merged[:limit]
    selected, seen = [], set()

    def _add(items: list, cap: int) -> None:
        added = 0
        for item in (items or []):
            key = item.get("chunk_key") or item.get("id")
            if key and key not in seen:
                seen.add(key)
                selected.append(item)
                added += 1
                if added >= cap:
                    break

    if limit >= 20:
        rrf_cap, leg_cap = 14, 3
    elif limit >= 8:
        rrf_cap, leg_cap = 2, 3
    else:
        rrf_cap, leg_cap = 1, 2
    _add(merged, rrf_cap)
    for leg in recall_lists:
        _add(leg, leg_cap)
    return selected[:limit]


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
        from ..core.config import HTTP_TIMEOUT_LONG
        from ..core.concurrency import llm_semaphore
        async with llm_semaphore:
            ok, data = await post_chat_completion(
                {"model": None,
                 "messages": [{"role": "user", "content":
                               "把用户的口语问题改写成《民法典》条文术语风格的检索查询，"
                               "只输出改写后的查询本身，不要解释、不要引号。\n"
                               f"用户问题：{query}"}],
                 "temperature": 0, "max_tokens": 256},
                timeout=HTTP_TIMEOUT_LONG)
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
