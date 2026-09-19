"""支付模块 Pydantic 请求模型

2026-09-05 清理：响应模型全家（WalletResponse…InvoiceResponse）与
TransferRequest/InvoiceCreateRequest/PayRequest/OrderQueryParams 为零引用死代码，已删除；
接口响应直接由 service 层 dict 序列化，不经响应模型。
"""
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class RechargeRequest(BaseModel):
    """创建充值订单请求"""
    amount: Decimal = Field(
        ..., ge=0.01, le=999999.00, decimal_places=2,
        description="充值金额（元，最多两位小数）",
    )
    payment_method: str = Field(default="simulated_alipay", max_length=32, description="支付渠道")
    # 2026-09-14（payment 日志 09-11 P2）：subject/order_no/reason 限长，
    # 防超长串膨胀 DB/内存/日志
    subject: str = Field(default="账户余额充值", max_length=128, description="充值标题")


class RefundRequest(BaseModel):
    """申请退款请求"""
    order_no: str = Field(..., max_length=64, description="原订单号")
    amount: Optional[Decimal] = Field(
        default=None, ge=0.01, le=999999.00, decimal_places=2,
        description="退款金额（不传默认退剩余全部；上限与充值侧一致，"
                    "实际额度仍由剩余可退门约束）",
    )
    reason: str = Field(default="用户申请退款", max_length=200, description="退款原因")
