"""
管理端支付路由

提供支付统计、全量订单管理、对账、渠道管理等功能。
仅 admin 角色可访问。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..middleware.auth import get_current_user
from ..core.logging import setup_logging
from . import admin_router
from .service import (
    get_admin_stats,
    get_admin_order_list,
    get_wallet,
    get_channels as get_channels_list,
)
from ..core.db import get_pool

logger = setup_logging()


class ChannelUpdateRequest(BaseModel):
    channel_code: str
    fee_rate: float = 0.0
    is_active: bool = True
    min_amount: float = 0.01
    max_amount: float = 999999.00


@admin_router.get("/stats")
async def api_admin_stats(current_user: dict = Depends(get_current_user)):
    """支付统计概览（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return await get_admin_stats()


@admin_router.get("/orders")
async def api_admin_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default=None),
    order_type: str = Query(default=None),
    user_id: str = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """全量订单管理（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return await get_admin_order_list(
        page=page,
        page_size=page_size,
        status=status,
        order_type=order_type,
        user_id=user_id,
    )


@admin_router.get("/wallet/{target_user}")
async def api_admin_wallet(
    target_user: str,
    current_user: dict = Depends(get_current_user),
):
    """查看任意用户钱包（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return await get_wallet(target_user)


@admin_router.post("/channels")
async def api_admin_update_channel(
    req: ChannelUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    """更新支付渠道配置（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE payment_channels SET fee_rate = $1, is_active = $2, "
            "min_amount = $3, max_amount = $4, updated_at = CURRENT_TIMESTAMP "
            "WHERE channel_code = $5",
            req.fee_rate, req.is_active, req.min_amount, req.max_amount,
            req.channel_code,
        )
    # P2 修复：渠道不存在时 UPDATE 零行，如实报 404 而非误报"已更新"
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"渠道不存在: {req.channel_code}")
    return {"message": "渠道配置已更新"}


@admin_router.get("/channels")
async def api_admin_channels(current_user: dict = Depends(get_current_user)):
    """获取支付渠道配置（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM payment_channels ORDER BY sort_order ASC"
        )
    return {
        "items": [
            {
                "id": r["id"],
                "channel_code": r["channel_code"],
                "channel_name": r["channel_name"],
                "icon": r["icon"] or "",
                "is_active": r["is_active"],
                "fee_rate": float(r["fee_rate"]),
                "min_amount": float(r["min_amount"]),
                "max_amount": float(r["max_amount"]),
                "sort_order": r["sort_order"],
                "config": r["config"] if isinstance(r["config"], dict) else {},
            }
            for r in rows
        ]
    }


@admin_router.post("/reconcile")
async def api_admin_reconcile(current_user: dict = Depends(get_current_user)):
    """执行日对账（仅管理员）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")

    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # 范围查询（P1 #45：DATE() 包裹会使 idx_po_created 索引失效，改用 [当日零点, 次日零点)）
    _now = datetime.now(timezone.utc).replace(tzinfo=None)
    _start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
    _end = _start + timedelta(days=1)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 统计今日订单（P2 口径修复：success 含部分退款单；金额排除退款单，退款单独行统计）
        row = await conn.fetchrow(
            "SELECT COUNT(*) as total_orders, "
            "COALESCE(SUM(CASE WHEN status IN ('success', 'partial_refunded') THEN 1 ELSE 0 END), 0) as success_orders, "
            "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed_orders, "
            "COALESCE(SUM(CASE WHEN order_type != 'refund' THEN amount ELSE 0 END), 0) as total_amount, "
            "COALESCE(SUM(CASE WHEN status IN ('success', 'partial_refunded') AND order_type != 'refund' "
            "THEN amount ELSE 0 END), 0) as success_amount "
            "FROM payment_orders WHERE created_at >= $1 AND created_at < $2",
            _start, _end,
        )
        # 退款统计
        refund_row = await conn.fetchrow(
            "SELECT COUNT(*) as refund_orders, COALESCE(SUM(amount), 0) as refund_amount "
            "FROM payment_orders WHERE order_type = 'refund' AND created_at >= $1 AND created_at < $2",
            _start, _end,
        )

        # 写入对账记录
        await conn.execute(
            """INSERT INTO reconciliation_records
               (reconcile_date, total_orders, total_amount, success_orders, success_amount,
                failed_orders, refund_orders, refund_amount, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'balanced')
               ON CONFLICT (reconcile_date) DO UPDATE SET
               total_orders = $2, total_amount = $3, success_orders = $4,
               success_amount = $5, failed_orders = $6, refund_orders = $7,
               refund_amount = $8, status = 'balanced', updated_at = CURRENT_TIMESTAMP""",
            today,
            row["total_orders"], row["total_amount"],
            row["success_orders"], row["success_amount"],
            row["failed_orders"],
            refund_row["refund_orders"] if refund_row else 0,
            refund_row["refund_amount"] if refund_row else 0,
        )

    return {
        "message": "对账完成",
        "date": today,
        "total_orders": row["total_orders"],
        "success_orders": row["success_orders"],
        "failed_orders": row["failed_orders"],
        "total_amount": float(row["total_amount"]),
        "success_amount": float(row["success_amount"]),
        "refund_orders": refund_row["refund_orders"] if refund_row else 0,
        "refund_amount": float(refund_row["refund_amount"]) if refund_row else 0,
    }
