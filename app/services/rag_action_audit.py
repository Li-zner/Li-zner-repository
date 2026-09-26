"""RAG 修复动作的验证记录与结果落库。"""
from __future__ import annotations

import json


async def verification_table_exists(conn) -> bool:
    """兼容滚动发布窗口：新表迁移完成前，动作详情仍可读取。"""
    return bool(await conn.fetchval(
        "SELECT to_regclass('public.rag_action_verifications') IS NOT NULL"))


def verification_records(result: dict, verification: dict,
                         rollback_result: dict | None) -> list[dict]:
    """把执行结果拆成可查询的基线、验证和回滚记录。"""
    records: list[dict] = []
    baseline = result.get("baseline")
    if isinstance(baseline, dict) and baseline:
        records.append({
            "phase": "baseline",
            "passed": True,
            "metrics": baseline,
            "evidence": {
                "baseline_value": result.get("baseline_value"),
            },
            "reason": "执行前基线",
        })
    after = result.get("after")
    if isinstance(after, dict) and after:
        records.append({
            "phase": "postcheck",
            "passed": bool(verification.get("passed")),
            "metrics": after,
            "evidence": verification,
            "reason": verification.get("reason") or "执行后验证",
        })
    elif verification:
        records.append({
            "phase": "postcheck",
            "passed": bool(verification.get("passed")),
            "metrics": {},
            "evidence": {
                "result": result,
                "verification": verification,
            },
            "reason": verification.get("reason") or "执行后验证",
        })
    if rollback_result:
        records.append({
            "phase": "rollback",
            "passed": bool(rollback_result.get("passed")),
            "metrics": rollback_result,
            "evidence": result,
            "reason": "验证失败后的自动回滚",
        })
    return records


async def finish_action(conn, action_id: int, *, status: str,
                        result: dict, verification: dict,
                        rollback_result: dict | None = None,
                        attempt_no: int = 1,
                        lease_token: str | None = None) -> bool:
    """原子写回执行结果、验证时间线并释放租约。"""
    async with conn.transaction():
        row = await conn.fetchval(
            "UPDATE rag_remediation_actions SET status=$1, result=$2::jsonb, "
            "verification=$3::jsonb, rollback_result=$4::jsonb, lease_until=NULL, "
            "lease_token=NULL, ended_at=now(), updated_at=now() "
            "WHERE action_id=$5 AND status='executing' "
            "AND lease_token=$6::uuid RETURNING 1",
            status, json.dumps(result, ensure_ascii=False),
            json.dumps(verification, ensure_ascii=False),
            json.dumps(rollback_result or {}, ensure_ascii=False), action_id,
            lease_token)
        if row is None:
            return False
        if await verification_table_exists(conn):
            for record in verification_records(
                    result, verification, rollback_result):
                await conn.execute(
                    "INSERT INTO rag_action_verifications "
                    "(action_id, attempt_no, phase, passed, metrics, evidence, "
                    "reason) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7) "
                    "ON CONFLICT DO NOTHING",
                    action_id, max(1, int(attempt_no)), record["phase"],
                    record["passed"],
                    json.dumps(record["metrics"], ensure_ascii=False),
                    json.dumps(record["evidence"], ensure_ascii=False),
                    str(record["reason"])[:1000],
                )
    return True
