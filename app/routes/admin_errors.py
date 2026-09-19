"""异常聚合管理端接口（仅管理员）"""
from fastapi import APIRouter, Depends, HTTPException

from ..middleware.auth import get_current_user
from ..core.error_aggregator import get_error_summary

admin_errors_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_errors_router.get("/errors")
async def api_error_summary(current_user: dict = Depends(get_current_user)):
    """异常聚合概览（按类型计数 + 最近样本）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.audit import audit
    await audit(current_user["username"], "admin_errors_view", {"scope": "recent"})
    return await get_error_summary()
