"""支付读/查询层（只读，无事务/锁/写）

2026-08-31 从 app/payment/service.py 纯移动拆出（行为等价）：
订单/流水/渠道/统计的 SELECT 查询 + 系列化。与「账务写/流」(service.py) 分离，
避免读逻辑与资金事务耦合；service.py 经 re-export 保留原公开符号。
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from ..core.db import get_pool
from ..core.logging import setup_logging

logger = setup_logging()


def _utcnow():
    """返回 offset-naive 的 UTC 时间（兼容 asyncpg；避免弃用的 datetime.utcnow）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_iso(dt):
    """返回带 Z 后缀的 ISO 时间字符串（兼容 JavaScript 解析）"""
    if dt is None:
        return None
    return dt.isoformat() + "Z"


def _order_to_dict(row) -> dict:
    return {
        "order_no": row["order_no"],
        "user_id": row["user_id"],
        "order_type": row["order_type"],
        "amount": float(row["amount"]),
        "fee": float(row["fee"]),
        "status": row["status"],
        "subject": row["subject"] or "",
        "body": row["body"] or "",
        "payment_method": row["payment_method"],
        "channel_order_no": row["channel_order_no"] or "",
        "paid_at": _to_iso(row["paid_at"]) if row["paid_at"] else None,
        "refunded_at": _to_iso(row["refunded_at"]) if row["refunded_at"] else None,
        "expire_at": _to_iso(row["expire_at"]) if row["expire_at"] else None,
        "callback_status": row["callback_status"],
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {},
        "created_at": _to_iso(row["created_at"]) if row["created_at"] else "",
        "updated_at": _to_iso(row["updated_at"]) if row["updated_at"] else "",
    }


def _order_to_list_dict(row) -> dict:
    """列表场景精简序列化（不含 body/metadata 大字段，P2 #37）"""
    return {
        "order_no": row["order_no"],
        "order_type": row["order_type"],
        "amount": float(row["amount"]),
        "fee": float(row["fee"]),
        "status": row["status"],
        "subject": row["subject"] or "",
        "payment_method": row["payment_method"],
        "paid_at": _to_iso(row["paid_at"]) if row["paid_at"] else None,
        "expire_at": _to_iso(row["expire_at"]) if row["expire_at"] else None,
        "created_at": _to_iso(row["created_at"]) if row["created_at"] else "",
    }


async def get_transaction_logs(
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    tx_type: Optional[str] = None,
) -> dict:
    """获取交易流水（分页）"""
    pool = await get_pool()
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        if tx_type:
            rows = await conn.fetch(
                "SELECT id, order_no, user_id, tx_type, amount, before_balance, after_balance, "
                "remark, status, created_at FROM transaction_logs "
                "WHERE user_id = $1 AND tx_type = $2 "
                "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                user_id, tx_type, page_size, offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM transaction_logs WHERE user_id = $1 AND tx_type = $2",
                user_id, tx_type,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, order_no, user_id, tx_type, amount, before_balance, after_balance, "
                "remark, status, created_at FROM transaction_logs "
                "WHERE user_id = $1 "
                "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                user_id, page_size, offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM transaction_logs WHERE user_id = $1",
                user_id,
            )
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "order_no": r["order_no"],
            "tx_type": r["tx_type"],
            "amount": float(r["amount"]),
            "before_balance": float(r["before_balance"]),
            "after_balance": float(r["after_balance"]),
            "remark": r["remark"] or "",
            "status": r["status"],
            "created_at": _to_iso(r["created_at"]) if r["created_at"] else "",
        })
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": 0 if total == 0 else (total + page_size - 1) // page_size,  # P2 #36：空数据返回 0 页
    }


async def get_order_list(
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    order_type: Optional[str] = None,
) -> dict:
    """获取用户订单列表"""
    pool = await get_pool()
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        conditions = ["user_id = $1"]
        params = [user_id]
        param_idx = 2

        if status:
            conditions.append(f"status = ${param_idx}")
            params.append(status)
            param_idx += 1
        if order_type:
            conditions.append(f"order_type = ${param_idx}")
            params.append(order_type)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        rows = await conn.fetch(
            # 列表场景精简 SELECT（不含 body/metadata 大字段，P2 #37）
            f"SELECT order_no, order_type, amount, fee, status, subject, payment_method, "
            f"paid_at, expire_at, created_at FROM payment_orders WHERE {where_clause} "
            f"ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}",
            *params, page_size, offset,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM payment_orders WHERE {where_clause}",
            *params,
        )

    items = [_order_to_list_dict(r) for r in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": 0 if total == 0 else (total + page_size - 1) // page_size,  # P2 #36：空数据返回 0 页
    }


async def get_order_detail(order_no: str, user_id: str) -> Optional[dict]:
    """获取订单详情"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payment_orders WHERE order_no = $1 AND user_id = $2",
            order_no, user_id,
        )
        if row:
            return _order_to_dict(row)
    return None


async def get_channels() -> list:
    """获取可用支付渠道"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_code, channel_name, icon, is_active, "
            "fee_rate, min_amount, max_amount, sort_order "
            "FROM payment_channels WHERE is_active = true "
            "ORDER BY sort_order ASC"
        )
        if rows:
            return [
                {
                    "channel_code": r["channel_code"],
                    "channel_name": r["channel_name"],
                    "icon": r["icon"] or "",
                    "is_active": r["is_active"],
                    "fee_rate": float(r["fee_rate"]),
                    "min_amount": float(r["min_amount"]),
                    "max_amount": float(r["max_amount"]),
                    "sort_order": r["sort_order"],
                }
                for r in rows
            ]
    # DB 无渠道配置时回退默认（记录日志便于排查，P2 #39 防静默降级掩盖异常）
    logger.warning("payment_channels 表无数据，回退到硬编码默认渠道")
    return [
        {"channel_code": "balance", "channel_name": "余额支付", "icon": "💰",
         "is_active": True, "fee_rate": 0, "min_amount": 0.01, "max_amount": 999999, "sort_order": 0},
        {"channel_code": "simulated_alipay", "channel_name": "模拟支付宝", "icon": "💳",
         "is_active": True, "fee_rate": 0, "min_amount": 0.01, "max_amount": 999999, "sort_order": 1},
        {"channel_code": "simulated_wxpay", "channel_name": "模拟微信支付", "icon": "📱",
         "is_active": True, "fee_rate": 0, "min_amount": 0.01, "max_amount": 999999, "sort_order": 2},
    ]


async def get_admin_stats() -> dict:
    """获取支付统计概览"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        _now = _utcnow()
        _start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
        _end = _start + timedelta(days=1)
        today_row = await conn.fetchrow(
            "SELECT COUNT(*) as total, COALESCE(SUM(amount), 0) as total_amount "
            "FROM payment_orders WHERE created_at >= $1 AND created_at < $2",
            _start, _end,
        )
        # 总统计
        total_row = await conn.fetchrow(
            "SELECT COUNT(*) as total, COALESCE(SUM(amount), 0) as total_amount "
            "FROM payment_orders WHERE status = 'success'",
        )
        # 钱包总数
        wallet_count = await conn.fetchval("SELECT COUNT(*) FROM user_wallets")
        # 总充值
        recharge_total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM payment_orders "
            "WHERE order_type = 'recharge' AND status = 'success'",
        )
        # 总消费
        consume_total = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM payment_orders "
            "WHERE order_type = 'payment' AND status = 'success'",
        )

    return {
        "today_orders": today_row["total"] if today_row else 0,
        "today_amount": float(today_row["total_amount"]) if today_row else 0,
        "total_orders": total_row["total"] if total_row else 0,
        "total_amount": float(total_row["total_amount"]) if total_row else 0,
        "wallet_count": wallet_count or 0,
        "total_recharge": float(recharge_total or 0),
        "total_consume": float(consume_total or 0),
    }


async def get_admin_order_list(
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    order_type: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """管理端获取所有订单"""
    pool = await get_pool()
    offset = (page - 1) * page_size
    conditions = ["1=1"]
    params = []
    param_idx = 1

    if status:
        conditions.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1
    if order_type:
        conditions.append(f"order_type = ${param_idx}")
        params.append(order_type)
        param_idx += 1
    if user_id:
        conditions.append(f"user_id = ${param_idx}")
        params.append(user_id)
        param_idx += 1

    where_clause = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # 列表场景精简 SELECT（不含 body/metadata 大字段，P2 #37）
            f"SELECT order_no, order_type, amount, fee, status, subject, payment_method, "
            f"paid_at, expire_at, created_at FROM payment_orders WHERE {where_clause} "
            f"ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}",
            *params, page_size, offset,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM payment_orders WHERE {where_clause}",
            *params,
        )

    items = [_order_to_list_dict(r) for r in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": 0 if total == 0 else (total + page_size - 1) // page_size,  # P2 #36：空数据返回 0 页
    }
