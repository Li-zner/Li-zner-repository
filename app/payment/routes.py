"""
用户端支付路由

提供充值、支付、退款、流水查询等功能。
所有接口均需要 JWT 认证。
"""
from decimal import Decimal
import time
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field

from ..middleware.auth import get_current_user, require_admin
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


# 幂等键长度上限：键本身只进 sha256 摘要（refund._refund_idem_key），限长是为了
# 不让异常串进 Redis 键、日志与 OpenAPI 示例里放大；正常客户端用的是 UUID（36 字符）
_IDEM_KEY_MAX_LEN = 128
_IDEM_KEY_MISSING_DETAIL = "资金操作必须携带幂等键（请求头 X-Idempotent-Key）"


def _require_idem_key(raw: Optional[str]) -> str:
    """P1-13（2026-09-22 拍板）：资金出口不再接受空幂等键。

    **为什么必须在路由层拦**：原先签名写 `Header("")`，把"没带 header"和"带了空串"
    合成同一个值，而 refund._refund_idem_key 对空串直接返回空键 → process_refund
    完全跳过 `_claim_refund_idempotency`，同一笔退款重复点击/网关重试就是二次出款，
    且资金链路上没有任何"这次是哪一笔"的可追溯标识。放在资金层里兜底也能修，但
    报错会变成 500 口径；路由层拦下来的结果是明确的 400 + 中文原因。
    """
    key = (raw or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail=_IDEM_KEY_MISSING_DETAIL)
    if len(key) > _IDEM_KEY_MAX_LEN:
        raise HTTPException(
            status_code=400, detail=f"幂等键过长（最多 {_IDEM_KEY_MAX_LEN} 字符）")
    return key


class AdminRefundRequest(RefundRequest):
    """管理员补偿退款入参（P1-12 拍板新增）。

    继承自助侧的 order_no/amount 约束（含 max_length 与金额上下限），只做两处收紧：
    归属人必须显式传（退款单要落在原单归属的钱包上），reason 不给默认值——
    补偿通道是真金白银出口，"谁为什么退" 是事后追责的唯一线索，留空等于自毁审计。
    """
    user_id: str = Field(
        ..., min_length=1, max_length=128,
        description="原订单归属用户（与登录侧 username 限长对称）",
    )
    reason: str = Field(
        ..., min_length=2, max_length=200, description="退款原因（审计必填）",
    )


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
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotent-Key"),
):
    """申请退款（按用户限频 5 次/小时，2026-09-07 审查 P0 配套）

    2026-09-22 审阅 P1-13：缺 X-Idempotent-Key 直接 400，空串不再允许进资金流程。
    本入口**不**放开消费单（allow_consumption_refund 默认 False）：已交付的模型服务
    自助退款会形成免费 token 漏洞，消费单争议一律走 /admin/refund 补偿通道。
    """
    user_id = current_user["username"]
    key = _require_idem_key(idempotency_key)
    await _refund_rate_limit(user_id)
    try:
        result = await process_refund(
            order_no=req.order_no,
            user_id=user_id,
            # 注意 is not None 判断：Decimal("0") 为 falsy，truthiness 会把 0 元退款误变全额退款
            amount=Decimal(str(req.amount)) if req.amount is not None else None,
            reason=req.reason,
            idempotency_key=key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# 与既有 router 同前缀（/api/payment），实际路径 POST /api/payment/admin/refund。
# 鉴权用 dependencies=[Depends(require_admin)]：FastAPI 会把 router/路由级依赖
# insert 到 dependant.dependencies 首位，先于函数签名里的 get_current_user 与函数体，
# 所以"非管理员"在任何资金动作之前就被拒（照抄 routes/rag_admin 的门禁写法）。
@router.post("/admin/refund", dependencies=[Depends(require_admin)])
async def api_admin_refund(
    req: AdminRefundRequest,
    current_user: dict = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotent-Key"),
):
    """管理员补偿退款（2026-09-22 审阅 P1-12 拍板）：消费单唯一的可退入口。

    背景：`_decide_refund` 的类型白名单只有 'payment'，而 'payment' 又被
    allow_consumption_refund 拦住，自助入口从来不会传这个 flag → 消费单在 HTTP 上
    恒拒（P1-12）。拍板是"入口不能死，但也不能对消费者开"：新增本通道，由管理员
    带 reason 代客发起。

    只放开消费单，**不放开充值单**：allow_consumption_refund=True 命中的是第二道
    状态门，refund._REFUNDABLE_ORDER_TYPES 白名单里没有 'recharge'，第一道类型门
    照旧拒（09-07 双倍入账 P0 口径不变，本通道也绕不过去）。

    与自助侧的差异只有两点：归属人由 req.user_id 显式指定（管理员代客），以及
    不吃按用户的 5 次/小时限频——那个阈值防的是消费者刷退款，补偿通道的节流由
    管理员鉴权 + 强制幂等键承担。
    """
    key = _require_idem_key(idempotency_key)
    # 审计落点：DB 侧退款单记的是原单归属 user_id，"哪个管理员发起的"只在这里留痕
    # （_settle_refund 的流水 operator 固定写 system，改它等于动资金层签名）
    logger.info(
        f"管理员补偿退款受理: admin={current_user.get('username')}, "
        f"target_user={req.user_id}, order={req.order_no}, reason={req.reason}")
    try:
        return await process_refund(
            order_no=req.order_no,
            user_id=req.user_id,
            amount=req.amount,
            reason=req.reason,
            idempotency_key=key,
            allow_consumption_refund=True,
        )
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
