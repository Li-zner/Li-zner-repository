"""
用户端支付路由

提供充值、支付、退款、流水查询等功能。
所有接口均需要 JWT 认证。
"""
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..middleware.auth import get_current_user
from ..core.logging import setup_logging
from . import router
from .models import (
    RechargeRequest, PayRequest, RefundRequest,
    TransferRequest, OrderQueryParams,
)
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
):
    """申请退款"""
    user_id = current_user["username"]
    try:
        result = await process_refund(
            order_no=req.order_no,
            user_id=user_id,
            amount=Decimal(str(req.amount)) if req.amount else None,
            reason=req.reason,
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
