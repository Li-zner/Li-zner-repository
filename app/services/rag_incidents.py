"""RAG incident 生命周期：verdict 归并、认领、解决和查询。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

_ACTIVE_STATUSES = ("open", "acknowledged", "mitigating", "observing")
_SAMPLE_LIMIT = 10


def _json_value(value, fallback):
    """JSONB 读取兼容：asyncpg 可能返回字符串。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return fallback


def _severity_rank_sql(column: str) -> str:
    """把 severity 转成可比较排名，供 SQL 升级判断使用。"""
    return (
        "CASE " + column + " "
        "WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
        "WHEN 'medium' THEN 2 ELSE 1 END"
    )


def compute_dedup_key(verdict: dict) -> str:
    """同一人格、同一规则的活跃事件共用一个归并键。"""
    raw = "|".join((
        str(verdict.get("code") or ""),
        str(verdict.get("persona") or "civil_code"),
        "agent_gateway",
    ))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{verdict.get('code') or 'unknown'}:{digest}"


async def upsert_incident(conn, verdict_id: int, verdict: dict) -> int:
    """把新 verdict 归并到活跃 incident；升级严重度并去重累积请求。"""
    dedup_key = compute_dedup_key(verdict)
    severity = str(verdict.get("severity") or "low")
    sample = json.dumps([verdict_id])
    row = await conn.fetchrow(
        "INSERT INTO rag_incidents (dedup_key, active_key, code, name, severity, "
        "status, title, summary, suspected_cause, recommended_action, persona, "
        "target_id, first_seen_at, last_seen_at, occurrence_count, "
        "affected_requests, sample_verdict_ids) "
        "VALUES ($1,$1,$2,$3,$4,'open',$5,$6,$7,$8,$9,'agent_gateway',now(),"
        "now(),1,0,$10::jsonb) "
        "ON CONFLICT (active_key) DO UPDATE SET "
        "last_seen_at=now(), "
        "occurrence_count=rag_incidents.occurrence_count+1, "
        "severity=CASE WHEN "
        f"{_severity_rank_sql('EXCLUDED.severity')} > "
        f"{_severity_rank_sql('rag_incidents.severity')} "
        "THEN EXCLUDED.severity ELSE rag_incidents.severity END, "
        "title=EXCLUDED.title, summary=EXCLUDED.summary, "
        "suspected_cause=EXCLUDED.suspected_cause, "
        "recommended_action=EXCLUDED.recommended_action, "
        "sample_verdict_ids=CASE WHEN "
        "jsonb_array_length(rag_incidents.sample_verdict_ids) < $11 "
        "THEN rag_incidents.sample_verdict_ids || EXCLUDED.sample_verdict_ids "
        "ELSE rag_incidents.sample_verdict_ids END, "
        "updated_at=now() "
        "RETURNING incident_id",
        dedup_key, verdict.get("code"), verdict.get("name"),
        severity, verdict.get("title"), verdict.get("title"),
        verdict.get("title"), verdict.get("action"),
        verdict.get("persona") or "civil_code", sample, _SAMPLE_LIMIT,
    )
    incident_id = int(row["incident_id"])
    request_uid = verdict.get("request_uid")
    if request_uid:
        added = await conn.fetchval(
            "INSERT INTO rag_incident_requests (incident_id, request_uid) "
            "VALUES ($1,$2::uuid) "
            "ON CONFLICT (incident_id, request_uid) DO NOTHING RETURNING 1",
            incident_id, str(request_uid))
        if added:
            await conn.execute(
                "UPDATE rag_incidents SET affected_requests=affected_requests+1, "
                "updated_at=now() WHERE incident_id=$1",
                incident_id)
    return incident_id


async def refresh_incident(conn, incident_id: int, verdict: dict) -> None:
    """规则重放更新已有 verdict 时，同步刷新 incident 的可展示决策字段。"""
    await conn.execute(
        "UPDATE rag_incidents SET severity=$1, title=$2, summary=$3, "
        "suspected_cause=$4, recommended_action=$5, updated_at=now() "
        "WHERE incident_id=$6",
        str(verdict.get("severity") or "low"),
        verdict.get("title"),
        verdict.get("title"),
        verdict.get("title"),
        verdict.get("action"),
        incident_id,
    )


def _optional_value(row, key: str):
    """读取可选列，兼容 asyncpg Record 与测试用的 dict。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _incident_row(row) -> dict[str, Any]:
    """统一 API 输出结构。"""
    resolved_at = _optional_value(row, "resolved_at")
    closed_at = _optional_value(row, "closed_at")
    return {
        "incident_id": int(row["incident_id"]),
        "code": row["code"],
        "name": row["name"],
        "severity": row["severity"],
        "status": row["status"],
        "title": row["title"],
        "summary": row["summary"],
        "suspected_cause": row["suspected_cause"],
        "recommended_action": row["recommended_action"],
        "persona": row["persona"],
        "target_id": row["target_id"],
        "occurrence_count": row["occurrence_count"],
        "affected_requests": row["affected_requests"],
        "first_seen_at": str(row["first_seen_at"]),
        "last_seen_at": str(row["last_seen_at"]),
        "owner": row["owner"],
        "acknowledged_by": row["acknowledged_by"],
        "resolved_at": str(resolved_at) if resolved_at else None,
        "closed_at": str(closed_at) if closed_at else None,
    }


async def list_incidents(conn, *, limit: int = 50, status: str | None = None,
                         severity: str | None = None) -> list[dict]:
    """查询 incident 列表。"""
    rows = await conn.fetch(
        "SELECT * FROM rag_incidents "
        "WHERE ($1::text IS NULL OR status = $1) "
        "AND ($2::text IS NULL OR severity = $2) "
        "ORDER BY "
        "CASE WHEN status IN ('resolved','closed') THEN 1 ELSE 0 END, "
        "CASE WHEN status NOT IN ('resolved','closed') THEN "
        + _severity_rank_sql("severity") + " ELSE 0 END DESC, "
        "CASE WHEN status NOT IN ('resolved','closed') "
        "THEN last_seen_at ELSE NULL END DESC, "
        "COALESCE(resolved_at, closed_at, last_seen_at) DESC "
        "LIMIT $3",
        status, severity, max(1, min(int(limit), 200)))
    return [_incident_row(row) for row in rows]


async def get_incident(conn, incident_id: int) -> dict | None:
    """读取 incident 详情及其关联 verdict。"""
    row = await conn.fetchrow(
        "SELECT * FROM rag_incidents WHERE incident_id = $1",
        incident_id)
    if row is None:
        return None
    verdicts = await conn.fetch(
        "SELECT id, created_at, source, source_id, request_uid, query_snippet, "
        "code, severity, title, evidence, action, patch "
        "FROM rag_verdicts WHERE incident_id = $1 ORDER BY id DESC LIMIT 50",
        incident_id)
    result = _incident_row(row)
    result["verdicts"] = [
        {
            "id": int(v["id"]),
            "created_at": str(v["created_at"]),
            "source": v["source"],
            "source_id": int(v["source_id"]),
            "request_uid": str(v["request_uid"]) if v["request_uid"] else None,
            "query_snippet": v["query_snippet"],
            "code": v["code"],
            "severity": v["severity"],
            "title": v["title"],
            "evidence": _json_value(v["evidence"], []),
            "action": v["action"],
            "patch": _json_value(v["patch"], {}),
        }
        for v in verdicts
    ]
    return result


async def transition_incident(conn, incident_id: int, *, status: str,
                              owner: str = "") -> dict | None:
    """执行受限状态迁移，返回更新后的 incident。"""
    allowed = {
        "acknowledged": ("open",),
        "resolved": ("open", "acknowledged", "mitigating", "observing"),
        "closed": ("resolved",),
    }
    sources = allowed.get(status)
    if not sources:
        raise ValueError(f"不支持的 incident 状态: {status}")
    async with conn.transaction():
        row = await conn.fetchrow(
            "UPDATE rag_incidents SET status=$1, "
            "owner=COALESCE(NULLIF($2,''),owner), "
            "acknowledged_by=CASE WHEN $3 THEN COALESCE(NULLIF($2,''),"
            "acknowledged_by) ELSE acknowledged_by END, "
            "acknowledged_at=CASE WHEN $3 THEN COALESCE("
            "acknowledged_at,now()) ELSE acknowledged_at END, "
            "resolved_at=CASE WHEN $4 THEN now() ELSE resolved_at END, "
            "closed_at=CASE WHEN $5 THEN now() ELSE closed_at END, "
            "active_key=CASE WHEN $4 OR $5 THEN NULL ELSE active_key END, "
            "updated_at=now() "
            "WHERE incident_id=$6 AND status = ANY($7::text[]) "
            "RETURNING *",
            status, owner, status == "acknowledged", status == "resolved",
            status == "closed", incident_id, list(sources))
        if row is None:
            return None
        if status in ("resolved", "closed"):
            await conn.execute(
                "UPDATE rag_remediation_actions SET status='cancelled', "
                "ended_at=now(), updated_at=now() "
                "WHERE incident_id=$1 "
                "AND status IN ('proposed','waiting_approval','queued')",
                incident_id,
            )
        return _incident_row(row)


async def clear_stale_incidents(conn, *, owner: str,
                                stale_hours: int = 24) -> list[dict]:
    """超管批量清理：把静默超过 stale_hours 的活跃事件标记为已解决。

    刻意不 DELETE 行——rag_incidents 本身就是问题日志，清理只改状态；
    active_key 置空后同一规则再触发会由 upsert_incident 开新事件，
    正在爆发的事件（last_seen_at 很近）不会被误清。
    """
    rows = await conn.fetch(
        "SELECT incident_id FROM rag_incidents "
        "WHERE status = ANY($1::text[]) "
        "AND last_seen_at < now() - make_interval(hours => $2::int) "
        "ORDER BY incident_id",
        list(_ACTIVE_STATUSES), max(1, int(stale_hours)))
    cleared: list[dict] = []
    for row in rows:
        # 逐条走 transition_incident：状态守卫 + 取消未完成修复动作，
        # 两条清理之间独立提交，单条失败不影响已清理部分
        item = await transition_incident(
            conn, int(row["incident_id"]), status="resolved", owner=owner)
        if item:
            cleared.append(item)
    return cleared


def utc_now() -> datetime:
    """返回当前 UTC 时间，便于测试注入和日志记录。"""
    return datetime.now(timezone.utc)
