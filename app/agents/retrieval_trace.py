"""知识库检索追踪：把每次检索的客观事实落库 + 打指标。

单独成模块的两个原因：tools.py 已接近 600 行硬限；「记录」与「检索」职责不同。

设计约束（与 core/error_aggregator.py 同一哲学）：
- 只记录、不阻断：任何异常都吞掉，绝不影响检索主流程
- 写库走 spawn() 后台任务，不占请求路径；Prometheus 计数同步打，微秒级
- **只存客观事实，不存判断结论**：诊断规则属于消费方（ragclosure 独立演进），
  两侧职责分离——规则改了不需要动线上代码，线上也不需要依赖诊断工具
- query 会入库（诊断必需，要能回答"哪些问题召不回"）。保留期见
  RETRIEVAL_TRACE_RETENTION_DAYS，清理见本模块 purge_expired()

为什么分路召回的命中数必须在这里抓：它只存在于 _search_two_legs 的局部变量里，
用 contextvar 传出即可，不必改函数签名与返回值（也就不会污染给 LLM 的 tool 消息）。
"""
from __future__ import annotations

import contextvars
import json
import os

from ..core.logging import setup_logging
from ..services.rag_request_trace import (
    current_persona,
    get_request_uid,
    record_span,
)

logger = setup_logging()

_LAST_LEGS: contextvars.ContextVar = contextvars.ContextVar("kb_last_legs", default=None)

_ENABLED = os.getenv("RETRIEVAL_TRACE_ENABLED", "1") == "1"
RETENTION_DAYS = int(os.getenv("RETRIEVAL_TRACE_RETENTION_DAYS", "7"))
# 与 tools.py:225 同源（同一环境变量）—— 此处独立读取是为了避免与 tools 循环 import
_REWRITE_TRGM = float(os.getenv("KB_REWRITE_TRGM_SIM", "0.45"))
_REWRITE_VEC = float(os.getenv("KB_REWRITE_VEC_SIM", "0.50"))


def _unpack_legs(legs) -> tuple:
    """legs 元组解包（兼容旧 4 元与含故障标志的 5 元形态，2026-09-14 审计 P1）。

    返回 (trgm_hits, vec_hits, max_trgm, max_vec, vec_failed)。
    """
    if not legs:
        return (None, None, None, None, False)
    trgm = legs[0] if len(legs) > 0 else None
    vec = legs[1] if len(legs) > 1 else None
    max_t = legs[2] if len(legs) > 2 else None
    max_v = legs[3] if len(legs) > 3 else None
    vec_failed = bool(legs[4]) if len(legs) > 4 else False
    return (trgm, vec, max_t, max_v, vec_failed)


def record(query: str, res: dict, legs, latency_ms: float,
           persona: str = "") -> None:
    """在检索出口调用：打指标（同步） + 落库（后台）。失败一律静默。

    persona 缺省时从请求上下文透传（rag_request_trace.current_persona）——
    评测流量以 persona='civil_code_eval' 走 chat 链路，写死缺省会丢来源标记；
    无请求上下文（批跑脚本直调 search_knowledge）时回落 civil_code，行为不变。
    """
    if not persona:
        persona = current_persona() or "civil_code"
    if not _ENABLED:
        return
    import uuid
    trace_uid = uuid.uuid4()  # 独立于后续 try：任何一段失败都不能丢整条 trace
    request_uid = get_request_uid()
    if not request_uid:
        try:
            from ..core.metrics import rag_request_trace_missing_total
            rag_request_trace_missing_total.labels(kind="retrieval").inc()
        except Exception:  # noqa: silent-except — 指标失败不影响检索
            pass
    results = res.get("results") or []
    trgm_hits, vec_hits, _max_t, _max_v, vec_failed = _unpack_legs(legs)
    record_span(
        "retrieval",
        latency_ms=int(latency_ms),
        result_count=len(results),
        attributes={
            "method": res.get("method") or "",
            "trgm_hits": trgm_hits,
            "vec_hits": vec_hits,
            "vec_failed": vec_failed,
        },
    )
    try:
        _emit_metrics(res, legs, latency_ms, persona)
    except Exception as e:  # 指标失败绝不能影响检索
        logger.warning(f"检索指标上报失败（不影响主流程）: {type(e).__name__}: {e}")
    try:
        # 请求级关联 id：答案侧失效（RC-4b 等）经此精确 join 回该次检索的分路事实。
        # 多轮 tool call 时 answer_trace 保留**首轮** uid（其 query 即用户原话）。
        from ..services.answer_trace import set_last_retrieval
        set_last_retrieval({
            "query": query, "persona": persona, "retrieval_uid": trace_uid,
            "returned_count": len(res.get("results") or []),
            "keys": [c.get("chunk_key") or c.get("id") or "" for c in (res.get("results") or [])
                     if isinstance(c, dict)],
            "method": res.get("method") or "", "latency_ms": int(latency_ms),
        })
    except Exception as e:
        logger.warning(f"检索事实快照失败（不影响主流程）: {type(e).__name__}: {e}")
    try:
        from ..core.concurrency import spawn
        spawn(_persist(query, res, legs, latency_ms, persona, trace_uid,
                       request_uid),
              name="retrieval-trace")
    except Exception as e:
        logger.warning(f"检索 trace 落库任务创建失败: {type(e).__name__}: {e}")


def _emit_metrics(res: dict, legs, latency_ms: float, persona: str) -> None:
    from ..core.metrics import (
        kb_leg_empty_total, kb_low_confidence_total, kb_recall_empty_total,
        kb_rerank_dropped_total, kb_retrieval_latency_seconds, kb_search_total,
    )

    results = res.get("results") or []
    method = str(res.get("method") or "")
    kb_search_total.labels(source=persona, result="hit" if results else "miss").inc()
    kb_retrieval_latency_seconds.observe(max(latency_ms, 0.0) / 1000.0)
    if not results:
        kb_recall_empty_total.labels(persona=persona).inc()
    if results and "local_rerank" not in method:
        kb_rerank_dropped_total.inc()

    if legs:
        trgm_hits, vec_hits, max_trgm, max_vec, _vec_failed = _unpack_legs(legs)
        if trgm_hits == 0:
            kb_leg_empty_total.labels(leg="trgm").inc()
        if vec_hits == 0:
            kb_leg_empty_total.labels(leg="vector").inc()
        if max_trgm < _REWRITE_TRGM and max_vec < _REWRITE_VEC:
            kb_low_confidence_total.inc()


async def _persist(query: str, res: dict, legs, latency_ms: float,
                   persona: str, trace_uid=None, request_uid: str = "") -> None:
    """后台落库；任何失败只记日志（表缺失、库不可用都不该影响检索）。"""
    try:
        from ..core.db import get_pool

        results = res.get("results") or []
        method = str(res.get("method") or "")
        trgm_hits, vec_hits, max_trgm, max_vec, vec_failed = _unpack_legs(legs)
        keys = [c.get("chunk_key") or c.get("id") or "" for c in results
                if isinstance(c, dict)]
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "INSERT INTO retrieval_traces (persona, query, method, trgm_hits, "
                "vec_hits, max_trgm_sim, max_vec_sim, returned_count, rerank_used, "
                "latency_ms, returned_chunk_keys, trace_uid, request_uid, vec_failed) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14)",
                persona, query[:1000], method[:128], trgm_hits, vec_hits,
                max_trgm, max_vec, len(results), "local_rerank" in method,
                int(latency_ms), json.dumps(keys), trace_uid,
                request_uid or None, vec_failed,
            )
    except Exception as e:
        logger.warning(f"检索 trace 落库失败（不影响主流程）: {type(e).__name__}: {e}")


async def purge_expired(days: int | None = None) -> int:
    """删除过期 trace，返回删除行数。供运维/定时清理调用。"""
    days = RETENTION_DAYS if days is None else days
    try:
        from ..core.db import get_pool
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchval(
                "WITH del AS (DELETE FROM retrieval_traces "
                "WHERE created_at < now() - ($1 || ' days')::interval RETURNING 1) "
                "SELECT count(*) FROM del",
                str(int(days)),
            )
        return int(row or 0)
    except Exception as e:
        logger.warning(f"检索 trace 清理失败: {type(e).__name__}: {e}")
        return 0
