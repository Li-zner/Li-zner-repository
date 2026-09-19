"""
用户端支付路由

提供充值、支付、退款、流水查询等功能。
所有接口均需要 JWT 认证。
"""
from decimal import Decimal
import time
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..middleware.auth import get_current_user
from ..core.logging import setup_logging
from . import router
from .models import RechargeRequest, RefundRequest
from .service import (
    get_wallet,
    create_recharge_order,
    process_recharge,
    get_order_list,
    get_order_detail,
    process_refund,
    get_transaction_logs,
    get_channels,
)

logger = setup_logging()

# 退款限频（2026-09-07 审查 P0 配套）：/refund 原先对所有登录用户开放且无频率限制
_REFUND_LIMIT = 5          # 每小时最多 5 次退款申请
_REFUND_WINDOW_S = 3600


async def _refund_rate_limit(user_id: str) -> None:
    """按用户固定窗口限频；超限 429（尽力而为，Redis 不可用不阻断）"""
    try:
        from ..core.redis import get_redis
        r = await get_redis()
        key = f"refund_rate:{user_id}:{int(time.time()) // _REFUND_WINDOW_S}"
        # Lua 原子 INCR+EXPIRE（2026-09-10 审查 P3：两步间崩溃留无 TTL 键，改一步原子）
        count = await r.eval(
            "local c = redis.call('INCR', KEYS[1]) "
            "if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end return c",
            1, key, _REFUND_WINDOW_S,
        )
        if count > _REFUND_LIMIT:
            raise HTTPException(status_code=429, detail="退款申请过于频繁，请稍后再试")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"退款限频检查失败（放行）: {type(e).__name__}")


@router.get("/wallet")
async def api_get_wallet(current_user: dict = Depends(get_current_user)):
    """获取当前用户钱包信息"""
    user_id = current_user["username"]
    wallet = await get_wallet(user_id)
    return wallet


@router.post("/recharge")
async def api_create_recharge(
    req: RechargeRequest,
    current_user: dict = Depends(get_current_user),
    x_idempotent_key: Optional[str] = Header(default=None, alias="X-Idempotent-Key"),
):
    """创建充值订单（生成订单，未实际扣款；支持 Idempotent-Key 幂等去重）"""
    user_id = current_user["username"]
    try:
        order = await create_recharge_order(
            user_id=user_id,
            amount=Decimal(str(req.amount)),
            payment_method=req.payment_method,
            subject=req.subject,
            idempotent_key=x_idempotent_key,
        )
        return order
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_no}/pay")
async def api_process_payment(
    order_no: str,
    current_user: dict = Depends(get_current_user),
):
    """提交支付（模拟支付确认）"""
    user_id = current_user["username"]
    try:
        result = await process_recharge(order_no, user_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders")
async def api_get_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: str = Query(default=None),
    order_type: str = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """获取当前用户订单列表"""
    user_id = current_user["username"]
    result = await get_order_list(
        user_id=user_id,
        page=page,
        page_size=page_size,
        status=status,
        order_type=order_type,
    )
    return result


@router.get("/orders/{order_no}")
async def api_get_order_detail(
    order_no: str,
    current_user: dict = Depends(get_current_user),
):
    """获取订单详情"""
    user_id = current_user["username"]
    order = await get_order_detail(order_no, user_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@router.post("/refund")
async def api_process_refund(
    req: RefundRequest,
    current_user: dict = Depends(get_current_user),
    idempotency_key: str = Header("", alias="X-Idempotent-Key"),
):
    """申请退款（按用户限频 5 次/小时，2026-09-07 审查 P0 配套）"""
    user_id = current_user["username"]
    await _refund_rate_limit(user_id)
    try:
        result = await process_refund(
            order_no=req.order_no,
            user_id=user_id,
            # 注意 is not None 判断：Decimal("0") 为 falsy，truthiness 会把 0 元退款误变全额退款
            amount=Decimal(str(req.amount)) if req.amount is not None else None,
            reason=req.reason,
            idempotency_key=idempotency_key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/transactions")
async def api_get_transactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tx_type: str = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """获取交易流水"""
    user_id = current_user["username"]
    result = await get_transaction_logs(
        user_id=user_id,
        page=page,
        page_size=page_size,
        tx_type=tx_type,
    )
    return result


@router.get("/channels")
async def api_get_channels(current_user: dict = Depends(get_current_user)):
    """获取可用支付渠道"""
    channels = await get_channels()
    return {"items": channels}
