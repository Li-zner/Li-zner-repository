"""RAG incident 到修复动作的确定性推荐。"""
from __future__ import annotations


_L0_MAP = {
    "RC-1": ("probe_health", {"target": "gateway"}, "确认网关检索依赖可用"),
    "RC-1b": ("probe_health", {"target": "worker"}, "确认检索 worker 与依赖可用"),
    "RC-6": ("probe_health", {"target": "worker"}, "确认重排执行器可用"),
    "RC-8": ("rerun_diagnosis", {}, "重跑诊断确认延迟是否持续"),
}

_L1_MAP = {
    "RC-8": (
        "adjust_rerank_top",
        {"value": 12},
        "降低候选池以缓解延迟，执行前会做小样本质量校验",
    ),
}


async def recommend_incident_actions(conn, incident_id: int) -> list[dict]:
    """按 incident 规则码返回可执行动作，不写入动作表。"""
    row = await conn.fetchrow(
        "SELECT incident_id, code, severity, recommended_action "
        "FROM rag_incidents WHERE incident_id=$1",
        incident_id,
    )
    if row is None:
        return []
    code = str(row["code"] or "")
    items: list[dict] = []
    l0 = _L0_MAP.get(code)
    if l0:
        action_key, params, reason = l0
        items.append({
            "action_key": action_key,
            "params": params,
            "reason": reason,
            "risk_level": "L0",
            "mode": "auto",
        })
    l1 = _L1_MAP.get(code)
    if l1:
        action_key, params, reason = l1
        items.append({
            "action_key": action_key,
            "params": params,
            "reason": reason,
            "risk_level": "L1",
            "mode": "approval",
        })
    if not items and row["recommended_action"]:
        items.append({
            "action_key": "",
            "params": {},
            "reason": row["recommended_action"],
            "risk_level": "",
            "mode": "manual",
        })
    return items


async def auto_plan_l0_actions(conn, incident_id: int) -> list[int]:
    """自动创建安全的 L0 动作；L1/L2/L3 只返回建议，不自动落库。"""
    from .rag_actions import create_action

    created: list[int] = []
    for item in await recommend_incident_actions(conn, incident_id):
        if item["mode"] != "auto" or not item["action_key"]:
            continue
        action = await create_action(
            conn,
            incident_id=incident_id,
            action_key=item["action_key"],
            params=item["params"],
            reason=item["reason"],
            requested_by="auto-remediation",
        )
        created.append(action["action_id"])
    return created
