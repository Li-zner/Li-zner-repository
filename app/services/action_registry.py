"""动作注册表：控制面认账的唯一动作白名单（代码面）。

2026-09-24 从 rag_actions.py 拆出，两个原因：
1) rag_actions.py 已接近 600 行上限，P2 的业务动作再往里加就破线；
2) 控制面要从"只治 RAG"扩到"治全业务"，注册表是策略层(rag_policy)、执行器
   (rag_actions) 和业务处理器(biz_action_handlers) 共用的词汇表，不该带 rag 前缀。

白名单是双写的，两处都必须改：
- 本文件的 _REGISTRY 决定"存在哪些动作、怎么执行、怎么回滚"；
- 数据库表 rag_action_specs 决定"该动作在哪个环境可用、冷却多久、是否停用"
  （新增行走 alembic 迁移 seed，禁手工 INSERT）。
risk_level 两边不一致时 rag_policy 会直接拒绝执行（宁可停住，不静默降级）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .biz_action_handlers import (
    rollback_channel_active as _rollback_channel_active,
    rollback_user_active as _rollback_user_active,
    set_channel_active as _set_channel_active,
    set_user_active as _set_user_active,
)
from .rag_action_handlers import (
    _clear_exact_cache,
    _probe_health,
    _rerun_diagnosis,
)
from .rag_knowledge_remediation import (
    apply_knowledge_change as _apply_knowledge_change,
    rollback_knowledge_change as _rollback_knowledge_change,
)
from .rag_rerank_remediation import (
    adjust_rerank_top as _adjust_rerank_top,
    rollback_rerank_top as _rollback_rerank_top,
)


@dataclass(frozen=True)
class ActionSpec:
    """动作定义：风险等级、执行/回滚函数，以及目标怎么定位。

    target_type/target_ref_key 供 create_action 落到动作行的 target_ref 列，
    策略层的冷却期据此决定是"全局冷却"还是"按目标各冷却一份"。
    standalone=True 表示允许无事件（incident）单独提议，业务动作用它，
    RAG 侧动作仍只能挂在事件下创建。
    """

    key: str
    risk_level: str
    handler: Callable[[dict], Awaitable[dict]] | None
    rollback_plan: str = ""
    rollback: Callable[[dict, dict], Awaitable[dict]] | None = None
    target_type: str = "rag_service"
    target_ref_key: str = ""
    standalone: bool = False


_REGISTRY = {
    "probe_health": ActionSpec("probe_health", "L0", _probe_health),
    "rerun_diagnosis": ActionSpec("rerun_diagnosis", "L0", _rerun_diagnosis),
    "clear_exact_cache": ActionSpec(
        "clear_exact_cache", "L1", _clear_exact_cache,
        rollback_plan="缓存未删除时的重复执行幂等；删除后无需回滚数据。",
    ),
    "adjust_rerank_top": ActionSpec(
        "adjust_rerank_top", "L1", _adjust_rerank_top,
        rollback_plan="恢复动作执行前的 RERANK_TOP，并验证运行期配置已恢复。",
        rollback=_rollback_rerank_top,
    ),
    "apply_knowledge_change": ActionSpec(
        "apply_knowledge_change", "L2", _apply_knowledge_change,
        rollback_plan="按变更集快照回滚：删除本次新增 chunk 或恢复失效标记原值。",
        rollback=_rollback_knowledge_change,
    ),
    # ---- P2 业务写动作（2026-09-24）----
    "set_user_active": ActionSpec(
        "set_user_active", "L2", _set_user_active,
        rollback_plan="按快照恢复 users.is_active 原值，并失效该用户的鉴权缓存。",
        rollback=_rollback_user_active,
        target_type="user_account", target_ref_key="username", standalone=True,
    ),
    "set_channel_active": ActionSpec(
        "set_channel_active", "L2", _set_channel_active,
        rollback_plan="按快照恢复 payment_channels.is_active 原值。",
        rollback=_rollback_channel_active,
        target_type="payment_channel", target_ref_key="channel_code",
        standalone=True,
    ),
    # 资金面只出提案：worker 对 L3 硬拒绝执行（rag_actions 执行分支），
    # 真正的余额变更必须走 payment/_ledger.py 的原子增减，不由动作面绕开。
    "propose_wallet_adjustment": ActionSpec(
        "propose_wallet_adjustment", "L3", None,
        rollback_plan="L3 只生成提案，控制面不执行，无需回滚。",
        target_type="user_wallet", target_ref_key="username", standalone=True,
    ),
}


def get_action_spec(action_key: str) -> ActionSpec:
    """按白名单取动作，未知动作直接拒绝。"""
    spec = _REGISTRY.get(action_key)
    if spec is None:
        raise ValueError(f"未注册的动作: {action_key}")
    return spec


def spec_target_ref(action_key: str, params: dict) -> str | None:
    """按注册表声明的目标键取目标标识；RAG 动作没有目标键，返回 None。

    返回 None 在策略层意味着"该动作按全局冷却"，不是"冷却失效"。
    """
    key = get_action_spec(action_key).target_ref_key
    if not key:
        return None
    value = (params or {}).get(key)
    return str(value)[:256] if value is not None else None
