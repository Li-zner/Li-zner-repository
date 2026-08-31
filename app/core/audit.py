"""
审计日志 — 记录关键操作（登录/注册/检索/支付/admin），供追溯与合规
"""
import asyncio
import json

from ..core.db import get_pool
from ..core.logging import setup_logging

logger = setup_logging()

# 审计动作常量
ACT_LOGIN = "login"
ACT_REGISTER = "register"
ACT_SEARCH = "kb_search"
ACT_CHAT = "chat"
ACT_PAYMENT = "payment"
ACT_ADMIN = "admin_op"
ACT_PHONE_BIND = "phone_bind"


async def audit(user_id: str, action: str, detail: dict | None = None) -> None:
    """写入审计日志（异步，失败不影响主流程；detail 自动脱敏敏感键）"""
    detail = dict(detail or {})
    # 脱敏：审计中不落明文敏感字段
    for key in ("password", "token", "code", "secret", "api_key"):
        if key in detail:
            detail[key] = "***"

    async def _write():
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO audit_logs (user_id, action, detail) VALUES ($1, $2, $3)",
                    user_id, action, json.dumps(detail, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning(f"审计写入失败（不影响主流程）: {e}")

    asyncio.create_task(_write())


async def query_audit(user_id: str = "", action: str = "", limit: int = 100) -> list:
    """查询审计日志（admin 用）"""
    pool = await get_pool()
    clauses, args = [], []
    if user_id:
        clauses.append("user_id = $1")
        args.append(user_id)
    if action:
        clauses.append(f"action = ${len(args) + 1}")
        args.append(action)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, user_id, action, detail, created_at FROM audit_logs "
            f"{where} ORDER BY id DESC LIMIT ${len(args)}",
            *args,
        )
        return [
            {"id": r["id"], "user_id": r["user_id"], "action": r["action"],
             "detail": r["detail"], "created_at": str(r["created_at"])}
            for r in rows
        ]
