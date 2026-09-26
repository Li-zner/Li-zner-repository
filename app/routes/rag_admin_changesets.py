"""L2 知识库变更集接口（仅管理员）—— 2026-09-17 从 rag_admin.py 拆出。

拆分理由：`app/routes/rag_admin.py` 触及 600 行文件上限（code-rules 门禁），
变更集三件套（列表 / 详情 / 提案）内聚成组，整体迁移到本模块。

口径保证：
- 本模块自带 `changeset_router`，由 `rag_admin.py` 用
  `rag_admin_router.include_router(...)` 在原位置挂载 —— 前缀由父 router 提供，
  全部 URL、注册顺序、OpenAPI operationId 均不变；
- 权限校验（role != admin → 403）、审计动作、错误码与提示文案逐字保留；
- `_audit_admin` 通过函数内延迟导入取得（项目惯例，避免模块循环导入）。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..middleware.auth import get_current_user

changeset_router = APIRouter(tags=["admin"])


class RagChangeSetBody(BaseModel):
    """L2 知识库变更集提案：change_type 决定 payload 结构。"""

    change_type: Literal["knowledge_add", "knowledge_invalidate"]
    payload: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


@changeset_router.get("/changesets")
async def rag_change_sets(
        limit: int = Query(default=20, ge=1, le=100),
        status: str | None = Query(default=None, max_length=32),
        current_user: dict = Depends(get_current_user)):
    """变更集列表（管理员），默认最新在前。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_change_sets import list_change_sets
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        items = await list_change_sets(conn, status=status, limit=limit)
    return {"items": items}


@changeset_router.get("/changesets/{change_set_id}")
async def rag_change_set_detail(
        change_set_id: int,
        current_user: dict = Depends(get_current_user)):
    """变更集详情（管理员），含 diff 载荷与校验报告。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_change_sets import get_change_set
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await get_change_set(conn, change_set_id)
    if item is None:
        raise HTTPException(status_code=404, detail="变更集不存在")
    return item


@changeset_router.post("/incidents/{incident_id}/changesets")
async def rag_propose_change_set(
        incident_id: int,
        body: RagChangeSetBody,
        current_user: dict = Depends(get_current_user)):
    """提议 L2 知识库变更集并创建待审批动作（同一事务）。

    审批与驳回复用现有 /actions/{id}/approve 与 /actions/{id}/cancel，
    不另设审批面；应用只发生在动作 worker 内。
    """
    from .rag_admin import _audit_admin
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    if len(str(body.payload)) > 65536:
        raise HTTPException(status_code=413, detail="变更载荷过大")
    from ..core.db import get_pool
    from ..services.rag_actions import (
        IncidentNotActionableError,
        IncidentNotFoundError,
        create_action,
    )
    from ..services.rag_change_sets import (
        ChangeSetRejectedError,
        create_change_set,
    )
    pool = await get_pool()
    try:
        async with pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                change_set = await create_change_set(
                    conn, change_type=body.change_type,
                    payload=body.payload,
                    created_by=current_user.get("username", ""))
                action = await create_action(
                    conn, incident_id=incident_id,
                    action_key="apply_knowledge_change",
                    params={"change_set_id": change_set["change_set_id"]},
                    reason=body.reason,
                    requested_by=current_user.get("username", ""))
        await _audit_admin(
            current_user, "rag_change_set_create",
            incident_id=incident_id,
            change_set_id=change_set["change_set_id"],
            change_type=change_set["change_type"],
            action_id=action["action_id"], action_status=action["status"])
        return {"change_set": change_set, "action": action}
    except IncidentNotFoundError:
        raise HTTPException(status_code=404, detail="incident 不存在")
    except IncidentNotActionableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ChangeSetRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"变更集创建失败: {type(exc).__name__}")
