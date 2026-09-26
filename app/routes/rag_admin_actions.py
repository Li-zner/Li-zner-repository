"""中控台动作提议端点（P2）：不挂事件的业务写动作入口。

只编排，不做业务判定：动作白名单、参数校验、分级裁决全在
action_registry / rag_policy，本文件只负责"能不能从这个门进"和响应形状。

挂在 rag_admin_router 之下，因此继承路由级 require_admin；端点内再显式判一次
role，沿用 2026-09-19 审查 F-P2-2 的双保险口径。

为什么不开放所有动作：既有 RAG 动作一直只能挂在事件下创建
（/incidents/{id}/actions），本端点放开的是注册表里标了 standalone 的业务动作，
其余动作保持原样，避免顺手扩大事件面动作的入口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.audit import ACT_RAG_ACTION_PROPOSE, audit
from ..middleware.auth import get_current_user
from ..services.action_registry import get_action_spec

actions_router = APIRouter(tags=["admin"])

REASON_MIN_CHARS = 4
REASON_MAX_CHARS = 500
PARAMS_MAX_BYTES = 8192


class ActionProposeBody(BaseModel):
    """无事件动作提议：动作键 + 参数 + 必填理由（理由进审计与动作行）。"""

    action_key: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)
    reason: str = Field(min_length=REASON_MIN_CHARS,
                        max_length=REASON_MAX_CHARS)


@actions_router.post("/actions")
async def propose_action(body: ActionProposeBody,
                         current_user: dict = Depends(get_current_user)) -> dict:
    """创建一个等待审批的业务动作；本端点只提议，不执行。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    if len(str(body.params)) > PARAMS_MAX_BYTES:
        raise HTTPException(status_code=413, detail="动作参数过大")
    try:
        spec = get_action_spec(body.action_key)
    except ValueError:
        raise HTTPException(status_code=422, detail="动作未注册")
    if not spec.standalone:
        raise HTTPException(
            status_code=422, detail="该动作只能挂在事件下创建")
    from ..core.db import get_pool
    from ..services.rag_actions import create_action

    actor = current_user.get("username", "")
    # 审计先落：失败也要留下"谁提议改什么"，成功再补动作编号与裁决状态
    await audit(actor, ACT_RAG_ACTION_PROPOSE,
                {"action_key": body.action_key, "reason_length": len(body.reason)})
    pool = await get_pool()
    try:
        async with pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                action = await create_action(
                    conn, incident_id=None, action_key=body.action_key,
                    params=body.params, reason=body.reason,
                    requested_by=actor)
    except ValueError as e:
        # PolicyRejectedError 与参数校验错误都是业务拒绝，不是服务故障
        await audit(actor, ACT_RAG_ACTION_PROPOSE,
                    {"outcome": "rejected", "action_key": body.action_key,
                     "reason": str(e)[:200]})
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"动作创建失败: {type(e).__name__}")
    await audit(actor, ACT_RAG_ACTION_PROPOSE,
                {"outcome": "ok", "action_key": body.action_key,
                 "action_id": action["action_id"], "status": action["status"],
                 "target_ref": action["target_ref"]})
    return action
