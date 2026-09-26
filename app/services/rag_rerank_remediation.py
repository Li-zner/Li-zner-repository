"""RERANK_TOP 自动修复动作与固定门禁验证。

验证口径统一走 rag_quality_gate 的 20 条固定门禁用例，与完整评测同一套打分逻辑，
不再在本模块内维护私有抽样用例（原 5 条 smoke 取自标注集，属重复）。
"""
from __future__ import annotations

from .rag_quality_gate import compare_gate, gate_summary, run_gate


async def adjust_rerank_top(params: dict) -> dict:
    """调整 RERANK_TOP，执行前后各跑一次门禁；退化时由统一回滚逻辑恢复。"""
    from ..agents.tools import RERANK_TOP
    from .rag_runtime_config import get_rerank_top, set_rerank_top

    target = int(params["value"])
    try:
        baseline_value = await get_rerank_top(RERANK_TOP)
        baseline = gate_summary(await run_gate())
    except Exception as exc:
        return {
            "result": {
                "changed": False,
                "error": type(exc).__name__,
            },
            "verification": {
                "passed": False,
                "reason": "执行前门禁基线采集失败",
                "error": type(exc).__name__,
            },
        }
    if target == baseline_value:
        return {
            "result": {"changed": False, "value": target, "baseline": baseline},
            "verification": {"passed": True, "reason": "参数未变化"},
        }
    try:
        await set_rerank_top(target)
        after = gate_summary(await run_gate())
    except Exception as exc:
        return {
            "result": {
                "changed": True,
                "baseline_value": baseline_value,
                "value": target,
                "baseline": baseline,
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
            "changed": True,
            "baseline_value": baseline_value,
            "value": target,
            "baseline": baseline,
            "after": after,
        },
        "verification": {
            "passed": verdict["passed"],
            "reasons": verdict["reasons"],
            "insufficient_sample": verdict["insufficient_sample"],
        },
    }


async def rollback_rerank_top(_params: dict, result: dict) -> dict:
    """恢复动作执行前的 RERANK_TOP，并验证恢复值。"""
    from ..agents.tools import RERANK_TOP
    from .rag_runtime_config import get_rerank_top, set_rerank_top

    previous = result.get("baseline_value")
    if previous is None:
        return {
            "passed": True,
            "skipped": True,
            "reason": "未捕获执行前基线，配置未发生可回滚变更",
        }
    await set_rerank_top(int(previous))
    restored = await get_rerank_top(RERANK_TOP)
    return {
        "passed": restored == int(previous),
        "restored_value": restored,
    }
