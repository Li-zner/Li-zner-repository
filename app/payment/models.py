"""支付模块 Pydantic 数据模型"""
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


# ============================================================
# 请求模型
# ============================================================

class RechargeRequest(BaseModel):
    """创建充值订单请求"""
    amount: Decimal = Field(..., ge=0.01, le=999999.00, description="充值金额（元）")
    payment_method: str = Field(default="simulated_alipay", description="支付渠道")
    subject: str = Field(default="账户余额充值", description="充值标题")


class PayRequest(BaseModel):
    """提交支付请求"""
    payment_method: Optional[str] = Field(default=None, description="支付渠道")


class RefundRequest(BaseModel):
    """申请退款请求"""
    order_no: str = Field(..., description="原订单号")
    amount: Optional[Decimal] = Field(default=None, description="退款金额（默认全额）")
    reason: str = Field(default="用户申请退款", description="退款原因")


class TransferRequest(BaseModel):
    """转账请求"""
    target_username: str = Field(..., description="目标用户名")
    amount: Decimal = Field(..., ge=0.01, le=999999.00, description="转账金额")
    remark: str = Field(default="", description="转账备注")


class InvoiceCreateRequest(BaseModel):
    """申请发票请求"""
    order_nos: List[str] = Field(..., min_length=1, description="关联订单号列表")
    invoice_type: str = Field(default="personal", description="发票类型: personal/company")
    company_name: str = Field(default="", description="公司名称")
    company_tax_id: str = Field(default="", description="税号")


class OrderQueryParams(BaseModel):
    """订单查询参数"""
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    status: Optional[str] = Field(default=None, description="筛选状态")
    order_type: Optional[str] = Field(default=None, description="筛选类型")


# ============================================================
# 响应模型
# ============================================================

class WalletResponse(BaseModel):
    """钱包信息"""
    user_id: str
    balance: float
    frozen_amount: float
    total_recharged: float
    total_spent: float
    total_refunded: float
    status: str


class OrderResponse(BaseModel):
    """订单详情"""
    order_no: str
    user_id: str
    order_type: str
    amount: float
    fee: float
    status: str
    subject: str
    body: Optional[str] = ""
    payment_method: str
    channel_order_no: Optional[str] = ""
    paid_at: Optional[str] = None
    refunded_at: Optional[str] = None
    expire_at: Optional[str] = None
    created_at: str
    updated_at: str


class RechargeResponse(BaseModel):
    """创建充值订单响应"""
    order_no: str
    amount: float
    fee: float
    status: str
    payment_method: str
    expire_at: Optional[str] = None
    created_at: str


class PayResponse(BaseModel):
    """支付结果响应"""
    order_no: str
    status: str
    paid_at: Optional[str] = None
    channel_order_no: Optional[str] = ""
    current_balance: Optional[float] = None
    message: str = ""


class RefundResponse(BaseModel):
    """退款结果响应"""
    order_no: str
    refund_order_no: str
    status: str
    refund_amount: float
    refunded_at: Optional[str] = None


class TransactionResponse(BaseModel):
    """交易流水"""
    id: int
    order_no: str
    tx_type: str
    amount: float
    before_balance: float
    after_balance: float
    remark: str
    created_at: str


class TransferResponse(BaseModel):
    """转账结果"""
    order_no: str
    status: str
    amount: float
    current_balance: float
    target_username: str
    remark: str


class PaginatedResponse(BaseModel):
    """分页响应包装"""
    items: list
    total: int
    page: int
    page_size: int
    total_pages: int


class ChannelResponse(BaseModel):
    """支付渠道信息"""
    channel_code: str
    channel_name: str
    icon: str
    is_active: bool
    fee_rate: float
    min_amount: float
    max_amount: float
    sort_order: int


class InvoiceResponse(BaseModel):
    """发票信息"""
    invoice_no: str
    user_id: str
    order_nos: List[str]
    total_amount: float
    invoice_type: str
    company_name: str
    status: str
    created_at: str
