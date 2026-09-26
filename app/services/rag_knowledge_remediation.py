"""知识库 L2 变更集动作：应用已审批的变更集并按固定门禁验证。

与 rag_rerank_remediation 同一套结构：执行前采门禁基线，应用变更集后复测，
compare_gate 判退化；verification 不通过时由 rag_actions.verify_or_rollback
统一调用本模块的回滚函数按快照恢复。
"""
from __future__ import annotations


async def apply_knowledge_change(params: dict) -> dict:
    """应用已审批的 L2 变更集；基线采集失败则不执行。"""
    from ..core.db import get_pool
    from .rag_change_sets import apply_change_set
    from .rag_quality_gate import compare_gate, gate_summary, run_gate

    change_set_id = int(params["change_set_id"])
    try:
        baseline = gate_summary(await run_gate())
    except Exception as exc:
        return {
            "result": {"applied": False, "error": type(exc).__name__},
            "verification": {
                "passed": False,
                "reason": "执行前门禁基线采集失败",
                "error": type(exc).__name__,
            },
        }
    try:
        pool = await get_pool()
        async with pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                summary = await apply_change_set(conn, change_set_id)
    except Exception as exc:
        return {
            "result": {
                "applied": False,
                "change_set_id": change_set_id,
                "baseline": baseline,
                "error": type(exc).__name__,
            },
            "verification": {
                "passed": False,
                "reason": "变更集应用失败",
                "error": type(exc).__name__,
            },
        }
    summary.pop("snapshot", None)
    try:
        after = gate_summary(await run_gate())
    except Exception as exc:
        # 应用已发生但复测失败：交给统一回滚路径按快照恢复。
        return {
            "result": {
                "applied": True,
                "change_set_id": change_set_id,
                "baseline": baseline,
                **summary,
                "error": type(exc).__name__,
            },
            "verification": {
                "passed": False,
                "reason": "执行后门禁复测异常",
                "error": type(exc).__name__,
            },
        }
    verdict = compare_gate(baseline, after)
    return {
        "result": {
            "applied": True,
            "change_set_id": change_set_id,
            "baseline": baseline,
            "after": after,
            **summary,
        },
        "verification": {
            "passed": verdict["passed"],
            "reasons": verdict["reasons"],
            "insufficient_sample": verdict["insufficient_sample"],
        },
    }


async def rollback_knowledge_change(params: dict, result: dict) -> dict:
    """按变更集快照回滚；变更集未被应用过则无事可做。"""
    from ..core.db import get_pool
    from .rag_change_sets import rollback_change_set

    if not result.get("applied"):
        return {
            "passed": True,
            "skipped": True,
            "reason": "变更集未被应用，无需回滚",
        }
    change_set_id = int(params["change_set_id"])
    pool = await get_pool()
    async with pool.acquire(timeout=10) as conn:
        async with conn.transaction():
            summary = await rollback_change_set(conn, change_set_id)
    return {"passed": summary.get("status") == "rolled_back", **summary}
