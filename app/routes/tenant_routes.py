"""
租户管理端点（admin 专属）— 多租户聚合报表与成员管理

聚合策略：业务表不加 tenant_id，通过 users JOIN 聚合（最小改动原则）
权限：仅 admin 角色可访问
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.audit import query_audit
from ..core.db import get_pool
from ..middleware.auth import get_current_user

router = APIRouter(prefix="/api/admin/tenants", tags=["tenant-admin"])


async def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


@router.get("/audit")
async def audit_logs(user_id: str = Query("", description="按用户过滤"),
                     action: str = Query("", description="按动作过滤：login/register/kb_search/payment/admin_op"),
                     limit: int = Query(100, ge=1, le=500),
                     _: dict = Depends(_require_admin)):
    """审计日志查询（登录/注册/检索/支付/admin 操作追溯）"""
    return {"logs": await query_audit(user_id=user_id, action=action, limit=limit)}


@router.get("")
async def list_tenants(_: dict = Depends(_require_admin)):
    """租户列表：用户数 / 对话数 / 消费额（按租户聚合）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT t.id, t.name, t.created_at,
                   COUNT(DISTINCT u.username) AS user_count,
                   COUNT(DISTINCT cm.id)      AS msg_count,
                   COALESCE(SUM(w.total_spent), 0) AS total_spent
            FROM tenants t
            LEFT JOIN users u ON u.tenant_id = t.id
            LEFT JOIN conversation_memories cm ON cm.user_id = u.username
            LEFT JOIN user_wallets w ON w.user_id = u.username
            GROUP BY t.id ORDER BY t.id
        """)
        return {"tenants": [
            {"id": r["id"], "name": r["name"], "created_at": str(r["created_at"]),
             "user_count": r["user_count"], "msg_count": r["msg_count"],
             "total_spent": float(r["total_spent"] or 0)}
            for r in rows
        ]}


@router.get("/{tenant_id}/members")
async def tenant_members(tenant_id: int, _: dict = Depends(_require_admin)):
    """租户成员列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, phone, display_name, role, created_at
            FROM users WHERE tenant_id = $1 ORDER BY created_at
        """, tenant_id)
        return {"tenant_id": tenant_id, "members": [
            {"username": r["username"], "phone": r["phone"], "display_name": r["display_name"],
             "role": r["role"], "created_at": str(r["created_at"])}
            for r in rows
        ]}


@router.get("/{tenant_id}/stats")
async def tenant_stats(tenant_id: int, _: dict = Depends(_require_admin)):
    """租户聚合报表：对话量 / 消费 / 活跃度（按天）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        info = await conn.fetchrow("SELECT name FROM tenants WHERE id = $1", tenant_id)
        if not info:
            raise HTTPException(404, "租户不存在")
        daily = await conn.fetch("""
            SELECT DATE(created_at) AS day, COUNT(*) AS msgs
            FROM conversation_memories
            WHERE user_id IN (SELECT username FROM users WHERE tenant_id = $1)
            GROUP BY DATE(created_at) ORDER BY day DESC LIMIT 14
        """, tenant_id)
        wallet = await conn.fetchrow("""
            SELECT COUNT(*) AS users, COALESCE(SUM(balance), 0) AS balance,
                   COALESCE(SUM(total_spent), 0) AS spent,
                   COALESCE(SUM(total_recharged), 0) AS recharged
            FROM user_wallets
            WHERE user_id IN (SELECT username FROM users WHERE tenant_id = $1)
        """, tenant_id)
        return {
            "tenant": info["name"],
            "daily_messages": [{"day": str(r["day"]), "msgs": r["msgs"]} for r in daily],
            "wallet": {k: (float(v) if isinstance(v, (int, float)) and k != "users" else v)
                       for k, v in dict(wallet).items()},
        }
