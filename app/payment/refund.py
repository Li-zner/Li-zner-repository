"""支付退款流程（2026-09-05 从 service.py 拆出：service.py 超 600 行硬限，行为等价移动）

流程：refund:{order_no} 分布式锁 → 读原订单 → 判定可退额度（支持部分退款，累计防超额）
→ 渠道退款（事务外 I/O）→ 同事务写退款单 + 原单状态联动 + 乐观锁退钱 + 流水。
"""
import asyncio
import hashlib
import json
from decimal import Decimal
from typing import Optional

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ._ledger import (
    _utcnow, _to_iso, _generate_order_no,
    _acquire_lock, _release_lock, _locked_wallet_change, _do_record_tx,
)
from .attempts import begin_channel_attempt, record_channel_result, mark_attempt_settled

logger = setup_logging()

# 仅允许代码内显式开启的人工补偿。普通用户接口默认不允许消费者自助退款，
# 避免已交付的模型服务被退款后形成免费 token 漏洞。
_REFUNDABLE_ORDER_TYPES = frozenset({"payment"})
# 渠道退款超时（秒）：与 refund:{order_no} 锁 TTL 30s 配套——渠道耗时 + 本地结算
# 必须落在锁有效期内，否则锁过期后并发第二笔退款会绕过串行保护。
_CHANNEL_REFUND_TIMEOUT_SECONDS = 10.0


def _decide_refund(
    order: dict,
    refunded_sum: Decimal,
    requested: Optional[Decimal],
    allow_consumption_refund: bool = False,
) -> Decimal:
    """退款判定（纯函数）：订单类型门 + 状态门 + 剩余额度门，返回本次退款金额。

    P0 修复（2026-09-07 审查）：充值单原先可被用户自退——充值结算已给钱包 +amount，
    退款路径又执行 _locked_wallet_change(+refund_amount) 再加一次，同一笔模拟充值
    双倍入账，且"充值→退款→再充值"可无限刷余额。取白名单语义：仅扣费单可退，
    充值款可能已被消费，扣回会造出负余额黑洞；争议走人工/管理员通道。
    """
    order_type = order.get("order_type")
    if order_type not in _REFUNDABLE_ORDER_TYPES:
        raise ValueError("该订单类型不支持自助退款，如有问题请联系管理员")
    if order_type == "payment" and not allow_consumption_refund:
        # P1-12（2026-09-22 拍板）：拒绝的同时给出路——消费单只能由
        # POST /api/payment/admin/refund（require_admin + 强制幂等键）代客发起
        raise ValueError("已交付的消费订单不支持自助退款，请提交人工审核（管理员补偿通道）")
    if order["status"] not in ("success", "partial_refunded"):
        raise ValueError("仅已成功的订单可退款")
    # 2026-09-12 修复（外部复核 P0）：消费订单 amount 为负数（扣费为负记录），
    # 直接相减恒为负 → 所有消费单被判"已全额退款"。可退基数取绝对额，
    # 负号仅表示资金方向；refunded_sum 来自退款单（正数）。
    remaining = abs(order["amount"]) - refunded_sum
    if remaining <= 0:
        raise ValueError("订单已全额退款")
    # 注意 is None 判断：Decimal("0") 为 falsy，若用 truthiness 会把 0 元请求误当"退剩余全部"
    refund_amount = remaining if requested is None else requested
    if refund_amount <= 0:
        raise ValueError("退款金额必须大于 0")
    if refund_amount > remaining:
        raise ValueError("退款金额不能超过订单剩余可退金额")
    return refund_amount


async def _call_channel_refund(order: dict, amount: Decimal) -> dict:
    """调用渠道退款并原样返回结果（保留失败详情供 attempt 落库）。

    P2 修复：原先从不调用渠道退款。渠道已成功但本侧账务事务失败的缺口由对账兜底（模拟模式）。

    2026-09-22 审阅 P1 修复：wait_for 抛的 TimeoutError 不是 ValueError，routes 的
    `except ValueError` 漏接 → 退款接口裸 500。这里收敛为与"渠道异常"同一业务口径（400），
    原单保持 success/partial_refunded 不动，用户可稍后重试。
    关键边界（**为什么不能记 channel_failed**）：超时只代表"没等到回复"，渠道侧结果未知，
    真渠道可能已经退款成功。调用 record_channel_result 置 channel_failed 会让下一次重试
    新建 attempt 再次打渠道 = 重复退款；置本地 success 更是无据入账。因此异常在落库前抛出，
    attempt 留在 processing，300s 后由 settle_recoverable_attempts 转 manual_review 兜底。
    """
    from .channels import get_channel
    channel = get_channel(order["payment_method"])
    try:
        return await asyncio.wait_for(
            channel.refund(order, amount), timeout=_CHANNEL_REFUND_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        logger.error(
            f"渠道退款超时（结果未知，attempt 留在 processing 待恢复/人工）: "
            f"order={order.get('order_no')}, amount={amount}")
        raise ValueError("渠道退款响应超时，结果未知，请稍后查询订单状态") from exc


async def _invoke_channel_refund(order: dict, amount: Decimal) -> dict:
    """兼容旧调用：失败抛 ValueError，不产生任何账务变更。"""
    result = await _call_channel_refund(order, amount)
    if not result.get("success"):
        raise ValueError(f"渠道退款失败: {result.get('message', '未知原因')}")
    return result


def _refund_result(order_no: str, refund_order_no: str, refund_amount: Decimal, now) -> dict:
    """组装退款结果（含成功日志）"""
    logger.info(f"退款成功: 原订单={order_no}, 退款单={refund_order_no}, amount={refund_amount}")
    return {
        "order_no": order_no,
        "refund_order_no": refund_order_no,
        "status": "refunded",
        "refund_amount": float(refund_amount),
        "refunded_at": _to_iso(now),
    }


async def _settle_refund(conn, order_no: str, user_id: str,
                         requested_amount: Decimal, reason: str,
                         attempt_id: Optional[str] = None,
                         allow_consumption_refund: bool = False):
    """事务内结算退款：退款单 + 原单状态联动 + 退钱 + 流水（同事务，任一步失败整体回滚）。

    2026-09-05 修复双重退款竞态（对齐充值路径 P0 #1 的锁行复验模式）：
    - 事务内 FOR UPDATE 锁原单并复验状态/重查已退累计——外层读、渠道调用（最长 10s）
      与分布式锁（TTL）之间的窗口内，并发退款可能已提交，外层快照不可信；
    - 请求金额超过剩余可退 → 抛异常回滚（F4 起为纵深防御，渠道额已在预留时钳制）；
    - 退钱改用行锁确定性更新（_locked_wallet_change），消除乐观锁重试耗尽回滚的缺口。
    返回 (退款单号, 支付时间)。部分退款累计未满额时原单标 partial_refunded。
    """
    async with conn.transaction():
        # 锁原单并复验状态 + 事务内重查已退累计（防锁过期窗口内的并发退款双花）
        locked = await conn.fetchrow(
            "SELECT * FROM payment_orders WHERE order_no = $1 FOR UPDATE",
            order_no,
        )
        if locked is None or locked["status"] not in ("success", "partial_refunded"):
            raise ValueError("订单状态不允许退款（可能已被并发退款）")
        fresh_sum_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS refunded FROM payment_orders "
            "WHERE order_type = 'refund' AND status = 'refunded' "
            "AND metadata->>'original_order_no' = $1",
            order_no,
        )
        fresh_sum = fresh_sum_row["refunded"] if fresh_sum_row else Decimal("0")
        refund_amount = _decide_refund(
            dict(locked), fresh_sum, requested_amount,
            allow_consumption_refund=allow_consumption_refund,
        )

        # 创建退款订单
        refund_order_no = await _generate_order_no()
        await conn.execute(
            """INSERT INTO payment_orders
               (order_no, user_id, order_type, amount, status, subject,
                payment_method, metadata, callback_status)
               VALUES ($1, $2, 'refund', $3, 'refunded', $4,
                       $5, $6, 'not_needed')""",
            refund_order_no, user_id, refund_amount,
            f"退款: {locked.get('subject', '')}",
            locked["payment_method"],
            json.dumps({"original_order_no": order_no, "reason": reason}),
        )

        # 更新原订单状态（P2：部分退款标 partial_refunded，退满才标 refunded）
        # 带状态前置条件：即使锁意外失效，状态不符的更新也影响 0 行 → 回滚
        now = _utcnow()
        # 与 _decide_refund 的 abs() 口径对称：历史负数金额消费单不可被
        # 部分退款误判为已退满（负数恒小于正退款额 → 剩余额度被锁死）
        new_status = ("refunded"
                      if fresh_sum + refund_amount >= abs(locked["amount"])
                      else "partial_refunded")
        result = await conn.execute(
            "UPDATE payment_orders SET status = $1, refunded_at = $2, "
            "updated_at = CURRENT_TIMESTAMP WHERE order_no = $3 "
            "AND status IN ('success', 'partial_refunded')",
            new_status, now, order_no,
        )
        if "UPDATE 1" not in result:
            raise ValueError("原订单状态已被并发变更，退款回滚")

        # 退钱回钱包（行锁确定性更新，无乐观锁重试耗尽缺口）
        updated, before_balance, after_balance, _ = await _locked_wallet_change(
            conn, user_id, refund_amount, tx_type="refund"  # P0 #15：退款累计到 total_refunded
        )
        if not updated:
            raise ValueError("钱包更新失败")

        # 流水
        await _do_record_tx(
            conn, refund_order_no, user_id, "refund", refund_amount,
            before_balance, after_balance,
            reason, "system",
        )
        if attempt_id:
            await mark_attempt_settled(conn, attempt_id)
    return refund_order_no, now


async def _load_refund_order(pool, order_no: str, user_id: str) -> tuple:
    """读订单与已退累计（短借还：归还连接后再做渠道 I/O，09-11 P1）。"""
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payment_orders WHERE order_no = $1 AND user_id = $2",
            order_no, user_id,
        )
        if not row:
            raise ValueError("订单不存在")
        # 已退累计（从退款单反查，免加列；metadata->> 对 json/jsonb 均有效）
        refunded_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS refunded FROM payment_orders "
            "WHERE order_type = 'refund' AND status = 'refunded' "
            "AND metadata->>'original_order_no' = $1",
            order_no,
        )
        refunded_sum = refunded_row["refunded"] if refunded_row else Decimal("0")
        return dict(row), refunded_sum


async def _start_refund_channel_attempt(
    order: dict,
    order_no: str,
    user_id: str,
    refund_amount: Decimal,
    idem_key: str,
    allow_consumption_refund: bool,
) -> tuple:
    """新建或复用渠道退款 attempt；已有成功结果时不再调用渠道。"""
    attempt = await begin_channel_attempt(
        order_no, user_id, "refund", refund_amount,
        order["payment_method"], idem_key, allow_consumption_refund,
    )
    if attempt["status"] == "channel_succeeded":
        return attempt, True
    if not attempt.get("is_new"):
        raise ValueError("退款尝试仍在处理中，请稍后查询订单状态")
    # F4：渠道实退金额取 attempt 的预留额（begin 时已在锁内钳制到剩余可退额度），
    # 外层快照金额只用于决定"要不要退"，不再决定"退多少"
    attempt_amount = Decimal(str(attempt["amount"]))
    channel_result = await _call_channel_refund(order, attempt_amount)
    channel_done = bool(channel_result.get("success"))
    try:
        await record_channel_result(attempt["attempt_id"], channel_result)
    except Exception as exc:
        # 渠道已经成功但结果落库失败时，必须让调用方保留幂等键并转恢复流程。
        setattr(exc, "_channel_done", channel_done)
        raise
    if not channel_result.get("success"):
        raise ValueError(
            f"渠道退款失败: {channel_result.get('message', '未知原因')}"
        )
    return attempt, True


async def _claim_refund_idempotency(redis, idem_key: str) -> dict | None:
    """占用退款幂等键；已完成的同键请求直接返回原结果。"""
    claimed = await redis.set(idem_key, "processing", nx=True, ex=86400)
    if claimed:
        return None
    stored = await redis.get(idem_key)
    if stored and stored != "processing":
        return json.loads(stored)
    raise ValueError("退款处理中，请勿重复提交")


def _follow_attempt_amount(attempt: dict, snapshot_amount: Decimal, order_no: str) -> Decimal:
    """F4（2026-09-20 拍板"缺口自动冲正"）：结算以 attempt 预留额为唯一事实源。

    渠道实退恰为预留额，本地照它入账即完成自动冲正；新 attempt 的额已在
    预留时钳制（attempts._clamp_refund_quota），此处只会与"外层快照"不一致，
    不会超额度。恢复路径更不能跟随后来一次不同金额的请求结算。
    """
    attempt_amount = Decimal(str(attempt["amount"]))
    if attempt_amount != snapshot_amount:
        logger.info(
            f"退款结算跟随 attempt 预留额: 请求={snapshot_amount}, "
            f"结算={attempt_amount}, order={order_no}")
    return attempt_amount


def _refund_idem_key(user_id: str, idempotency_key: str) -> str:
    """PAY-2（09-20 审查）：仅显式 Idempotency-Key 建键；原派生键会吞掉
    同单同额的合法二次部分退款（Stripe 语义无键不去重）。

    2026-09-22 审阅 P1-13 之后：HTTP 侧（自助 /refund 与管理员 /admin/refund）已在
    路由层强制要求 X-Idempotent-Key，缺键根本走不到这里。空串分支保留为纵深防御，
    服务对象是资金层内部/恢复/脚本调用方（显式传空表示"我知道我在做什么"），
    不得被理解成"资金出口允许不去重"。
    """
    if not idempotency_key:  # 路由层已强制，此处仅兜底（见上方 P1-13 说明）
        return ""
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return f"payment:idem:refund:{user_id}:{digest}"


async def process_refund(
    order_no: str, user_id: str, amount: Optional[Decimal] = None,
    reason: str = "用户申请退款", idempotency_key: str = "",
    allow_consumption_refund: bool = False,
) -> dict:
    """处理退款（支持部分退款：多次退款累计防超额，退满后原单标 refunded）"""
    redis = await get_redis()
    idem_key = _refund_idem_key(user_id, idempotency_key)
    if idem_key:
        replayed = await _claim_refund_idempotency(redis, idem_key)
        if replayed is not None:
            return replayed

    channel_done = False  # 渠道已成功后本地异常不得删幂等键（2026-09-12 修复）
    # TTL 30s 对齐充值路径：渠道退款超时 10s + 结算耗时，10s 默认值会在渠道变慢时过期，
    # 导致并发第二个退款请求拿新锁重复退款（2026-09-05）
    lock_key = f"refund:{order_no}"
    lock_token = await _acquire_lock(lock_key, ttl=30)
    if not lock_token:
        if idem_key:
            await redis.delete(idem_key)
        raise ValueError("退款正在处理中")

    try:
        pool = await get_pool()
        # 1) 读订单与已退累计（短借还：渠道 I/O 前释放连接——2026-09-14 修复
        #    09-11 P1：原先整个流程持连接，最长 10s 的慢渠道可占满连接池）
        order, refunded_sum = await _load_refund_order(pool, order_no, user_id)
        refund_amount = _decide_refund(
            order, refunded_sum, amount,
            allow_consumption_refund=allow_consumption_refund,
        )

        # 2) 渠道退款（不持任何 DB 连接；TOCTOU 由 attempt 预留兜住（F4）：
        #    begin_channel_attempt 在锁原单的事务内把金额钳制到剩余可退额度，
        #    且同一原单同时只有一条活跃退款 attempt——渠道实退不会超过预留额，
        #    第 3 步事务内复验降级为纵深防御）
        attempt, channel_done = await _start_refund_channel_attempt(
            order, order_no, user_id, refund_amount, idem_key,
            allow_consumption_refund,
        )
        # 以 attempt 持久化的金额为唯一事实源（F4 自动冲正，见 _follow_attempt_amount）
        refund_amount = _follow_attempt_amount(attempt, refund_amount, order_no)

        # 3) 结算（独立短借还 + 自管事务：退款单/原单状态/钱包/流水同事务）
        async with pool.acquire(timeout=5) as conn:
            refund_order_no, now = await _settle_refund(
                conn, order_no, user_id, refund_amount, reason,
                attempt["attempt_id"], allow_consumption_refund,
            )

        result = _refund_result(order_no, refund_order_no, refund_amount, now)
        if idem_key:
            try:
                await redis.set(idem_key, json.dumps(result), ex=86400)
            except Exception as idem_err:
                # 2026-09-12 清欠 P1（09-11 审查 B-F11）：退款已入账，幂等键必须
                # 保留（24h TTL 兜底不可依赖时人工处理），删除会让重试再次退钱
                logger.error(f"退款结果写幂等键失败（键保留防重放）: {idem_err}")
        return result
    except Exception as exc:
        # 2026-09-12 修复（外部复核 P2→实际资金语义）：渠道已成功而本地结算异常时，
        # 删除幂等键会让重试再次打渠道（真渠道=重复退款）。此时保留键并转人工；
        # 仅渠道调用前/事务整体回滚（退款未发生）才安全删除
        channel_done = bool(getattr(exc, "_channel_done", channel_done))
        if channel_done:
            logger.error(
                f"渠道退款已成功但本地结算异常，幂等键保留防重放: order={order_no}")
        elif idem_key:
            await redis.delete(idem_key)
        raise
    finally:
        # 2026-09-22 审阅 P2（与 deduction 同一失效模式）：退款已在事务里提交，裸调 Redis
        # 释放锁一旦抖动就会把"已退成功"变成 500。锁有 TTL 30s 自愈，故只记日志不上抛。
        try:
            await _release_lock(lock_key, lock_token)
        except Exception as release_err:
            logger.exception(f"退款锁释放失败（锁将随 TTL 过期，退款结果不受影响）: {release_err}")
