"""RAG 修复动作策略引擎：参数、环境、风险和冷却期的统一裁决。"""
from __future__ import annotations

import json
import os
from typing import Any

POLICY_VERSION = "auto-remediation-2026.09.15.2"
DEFAULT_ENVIRONMENT = "local"
# 目标标识与提案金额的硬上限：动作参数可能由 AI 生成，边界只在策略层收一次，
# 校验器与执行器共用同一口径，避免两处数字漂移
MAX_TARGET_CHARS = 64
WALLET_PROPOSAL_MAX_AMOUNT = 5000.0


class PolicyRejectedError(ValueError):
    """动作被策略拒绝，调用方不得落库执行。"""


def _environment() -> str:
    """解析当前动作环境；未配置时按 local 处理。"""
    return (
        os.getenv("RAG_ACTION_ENVIRONMENT")
        or os.getenv("APP_ENV")
        or DEFAULT_ENVIRONMENT
    ).strip().lower()


def _auto_l1_enabled() -> bool:
    """L1 自动执行总开关，默认关闭。"""
    return os.getenv("RAG_AUTO_REMEDIATION_ENABLED", "0") == "1"


def _validate_adjust_rerank_top(params: dict[str, Any]) -> dict[str, Any]:
    """调整 RERANK_TOP 只接受 5~20 的整数。"""
    unknown = set(params) - {"value"}
    if unknown:
        raise PolicyRejectedError(
            f"adjust_rerank_top 含未知参数: {sorted(unknown)}")
    value = params.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyRejectedError("adjust_rerank_top.value 必须是整数")
    if not 5 <= value <= 20:
        raise PolicyRejectedError("adjust_rerank_top.value 必须在 5~20 之间")
    return {"value": value}


def _validate_clear_exact_cache(params: dict[str, Any]) -> dict[str, Any]:
    """精确清缓存必须给 query，且限制长度防异常入参。"""
    unknown = set(params) - {"query", "cache_ctx"}
    if unknown:
        raise PolicyRejectedError(
            f"clear_exact_cache 含未知参数: {sorted(unknown)}")
    query = str(params.get("query") or "").strip()
    if not query:
        raise PolicyRejectedError("clear_exact_cache 缺少 query")
    if len(query) > 500:
        raise PolicyRejectedError("clear_exact_cache.query 过长")
    return {
        "query": query,
        "cache_ctx": str(params.get("cache_ctx") or "")[:256],
    }


def _validate_empty(params: dict[str, Any]) -> dict[str, Any]:
    """无参数动作只允许空对象。"""
    if params:
        raise PolicyRejectedError("该动作不接受参数")
    return {}


def _target_id(params: dict[str, Any], key: str,
               unknown: set[str]) -> str:
    """取业务动作的目标标识：只接受非空短字符串，禁未知键。"""
    if unknown:
        raise PolicyRejectedError(f"{key} 之外的未知参数: {sorted(unknown)}")
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PolicyRejectedError(f"{key} 必须是非空字符串")
    if len(value) > MAX_TARGET_CHARS:
        raise PolicyRejectedError(f"{key} 超过 {MAX_TARGET_CHARS} 字符")
    return value.strip()


def _validate_boolean(params: dict[str, Any], key: str) -> bool:
    """开关位只接受真正的布尔值。"""
    value = params.get(key)
    if not isinstance(value, bool):
        raise PolicyRejectedError(f"{key} 必须是布尔值")
    return value


def _validate_set_user_active(params: dict[str, Any]) -> dict[str, Any]:
    """账号启停：单目标 + 明确的布尔目标态。"""
    username = _target_id(params, "username", set(params) - {"username", "active"})
    return {"username": username,
            "active": _validate_boolean(params, "active")}


def _validate_set_channel_active(params: dict[str, Any]) -> dict[str, Any]:
    """渠道开关：单目标 + 只碰 is_active，费率不在动作面内。"""
    code = _target_id(params, "channel_code",
                      set(params) - {"channel_code", "active"})
    return {"channel_code": code,
            "active": _validate_boolean(params, "active")}


def _validate_propose_wallet_adjustment(params: dict[str, Any]) -> dict[str, Any]:
    """钱包调整提案：金额非零且受绝对上限约束（元）。"""
    username = _target_id(params, "username", set(params) - {"username", "amount"})
    amount = params.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise PolicyRejectedError("amount 必须是数字")
    if amount == 0:
        raise PolicyRejectedError("amount 不能为 0")
    if abs(amount) > WALLET_PROPOSAL_MAX_AMOUNT:
        raise PolicyRejectedError(
            f"单次提案金额上限 {WALLET_PROPOSAL_MAX_AMOUNT} 元")
    return {"username": username, "amount": round(float(amount), 2)}


def _validate_probe_health(params: dict[str, Any]) -> dict[str, Any]:
    """健康探测只接受白名单目标名和 URL 字段。"""
    unknown = set(params or {}) - {"target", "url"}
    if unknown:
        raise PolicyRejectedError(f"probe_health 含未知参数: {sorted(unknown)}")
    return dict(params or {})


def _validate_apply_knowledge_change(params: dict[str, Any]) -> dict[str, Any]:
    """L2 知识库变更只携带目标变更集 id；变更集本体在创建时已校验。"""
    unknown = set(params) - {"change_set_id"}
    if unknown:
        raise PolicyRejectedError(
            f"apply_knowledge_change 含未知参数: {sorted(unknown)}")
    cs_id = params.get("change_set_id")
    if isinstance(cs_id, bool) or not isinstance(cs_id, int) or cs_id < 1:
        raise PolicyRejectedError(
            "apply_knowledge_change.change_set_id 必须是正整数")
    return {"change_set_id": cs_id}


_VALIDATORS = {
    "adjust_rerank_top": _validate_adjust_rerank_top,
    "apply_knowledge_change": _validate_apply_knowledge_change,
    "clear_exact_cache": _validate_clear_exact_cache,
    "probe_health": _validate_probe_health,
    "propose_wallet_adjustment": _validate_propose_wallet_adjustment,
    "rerun_diagnosis": _validate_empty,
    "set_channel_active": _validate_set_channel_active,
    "set_user_active": _validate_set_user_active,
}


def validate_action_params(action_key: str, params: dict | None) -> dict:
    """按动作白名单验证并规范化参数。"""
    validator = _VALIDATORS.get(action_key)
    if validator is None:
        raise PolicyRejectedError(f"动作没有参数校验器: {action_key}")
    if not isinstance(params, dict):
        raise PolicyRejectedError("动作参数必须是对象")
    return validator(params)


async def _cooldown_until(conn, action_key: str, environment: str,
                          seconds: int, target_ref: str | None = None):
    """返回当前环境同动作仍有效的冷却截止时间；已过期返回 None。

    target_ref 非空时按目标各算一份冷却：业务动作（禁用某账号、关某渠道）若
    沿用全局冷却，禁完 A 就会把 B 一起挡在门外，那是功能错误而不是安全收益。
    传 None 保持原全局语义，RAG 侧动作行为不变。
    """
    if seconds <= 0:
        return None
    return await conn.fetchval(
        "SELECT ended_at + ($3 || ' seconds')::interval "
        "FROM rag_remediation_actions "
        "WHERE action_key=$1 AND environment=$2 AND status='succeeded' "
        "AND ended_at IS NOT NULL "
        "AND ($4::text IS NULL OR target_ref=$4) "
        "AND now() < ended_at + ($3 || ' seconds')::interval "
        "ORDER BY ended_at DESC LIMIT 1",
        action_key, environment, str(seconds), target_ref,
    )


async def evaluate_action(conn, *, action_key: str, params: dict | None,
                          requested_by: str, environment: str | None = None) -> dict:
    """执行动作策略裁决，并写入不可变决策记录。"""
    from .action_registry import get_action_spec, spec_target_ref

    spec = get_action_spec(action_key)
    normalized = validate_action_params(action_key, params)
    target_ref = spec_target_ref(action_key, normalized)
    env = (environment or _environment()).strip().lower()
    row = await conn.fetchrow(
        "SELECT risk_level, enabled, allowed_environments, cooldown_seconds, "
        "config_version "
        "FROM rag_action_specs WHERE action_key=$1",
        action_key,
    )
    if row is None:
        raise PolicyRejectedError(f"动作未注册到策略表: {action_key}")
    if not row["enabled"]:
        raise PolicyRejectedError(f"动作已禁用: {action_key}")
    if env not in list(row["allowed_environments"] or []):
        raise PolicyRejectedError(f"动作不允许在环境 {env} 执行")
    if row["risk_level"] != spec.risk_level:
        raise PolicyRejectedError(
            f"动作风险等级配置不一致: code={spec.risk_level}, "
            f"db={row['risk_level']}")

    cooldown_until = await _cooldown_until(
        conn, action_key, env, int(row["cooldown_seconds"] or 0), target_ref)
    in_cooldown = bool(cooldown_until)
    if in_cooldown:
        raise PolicyRejectedError(
            f"动作 {action_key} 处于冷却期，最早可执行时间: {cooldown_until}")
    if action_key == "apply_knowledge_change":
        # 越过提议接口直接造动作时，也只允许指向待审批的变更集。
        from .rag_change_sets import is_pending_status

        cs_status = await conn.fetchval(
            "SELECT status FROM rag_change_sets WHERE change_set_id=$1",
            normalized["change_set_id"])
        if not is_pending_status(cs_status):
            raise PolicyRejectedError(
                f"变更集 {normalized['change_set_id']} 不存在"
                f"或状态为 {cs_status}，不是待审批变更集")
    if spec.risk_level == "L0":
        decision, status = "auto_allowed", "queued"
        reason = "L0 只读动作自动执行"
    elif spec.risk_level == "L1" and _auto_l1_enabled():
        decision, status = "auto_allowed", "queued"
        reason = "L1 自动执行已开启且未处于冷却期"
    elif spec.risk_level == "L1":
        decision, status = "approval_required", "waiting_approval"
        reason = "L1 自动执行未开启，需要人工审批"
    elif spec.risk_level == "L2":
        decision, status = "approval_required", "waiting_approval"
        reason = "L2 配置或数据变更需要人工审批"
    else:
        decision, status = "proposal_only", "proposed"
        reason = "L3 只生成提案，必须走工程评审"

    decision_id = await conn.fetchval(
        "INSERT INTO rag_policy_decisions "
        "(action_key, environment, decision, reason, policy_version, "
        " requested_params, requested_by, cooldown_until) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8) RETURNING decision_id",
        action_key, env, decision, reason, POLICY_VERSION,
        json.dumps(normalized, ensure_ascii=False),
        requested_by[:64] if requested_by else None, cooldown_until,
    )
    return {
        "decision_id": int(decision_id),
        "decision": decision,
        "status": status,
        "reason": reason,
        "environment": env,
        "policy_version": POLICY_VERSION,
        "config_version": row["config_version"],
        "cooldown_until": cooldown_until,
        "target_ref": target_ref,
        "params": normalized,
    }
