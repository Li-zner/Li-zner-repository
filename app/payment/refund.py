"""支付退款流程（2026-09-05 从 service.py 拆出：service.py 超 600 行硬限，行为等价移动）

流程：refund:{order_no} 分布式锁 → 读原订单 → 判定可退额度（支持部分退款，累计防超额）
→ 渠道退款（事务外 I/O）→ 同事务写退款单 + 原单状态联动 + 乐观锁退钱 + 流水。
"""
import asyncio
import json
from decimal import Decimal
from typing import Optional

from ..core.db import get_pool
from ..core.logging import setup_logging
from ._ledger import (
    _utcnow, _to_iso, _generate_order_no,
    _acquire_lock, _release_lock, _locked_wallet_change, _do_record_tx,
)

logger = setup_logging()


def _decide_refund(order: dict, refunded_sum: Decimal, requested: Optional[Decimal]) -> Decimal:
    """退款判定（纯函数）：状态门 + 剩余额度门，返回本次退款金额。

    P2 修复：支持部分退款——已退累计从退款单反查，剩余额度 = 原单金额 - 已退累计；
    退满后拒绝再退。不传金额时默认退剩余全部。
    """
    if order["status"] not in ("success", "partial_refunded"):
        raise ValueError("仅已成功的订单可退款")
    remaining = order["amount"] - refunded_sum
    if remaining <= 0:
        raise ValueError("订单已全额退款")
    # 注意 is None 判断：Decimal("0") 为 falsy，若用 truthiness 会把 0 元请求误当"退剩余全部"
    refund_amount = remaining if requested is None else requested
    if refund_amount <= 0:
        raise ValueError("退款金额必须大于 0")
    if refund_amount > remaining:
        raise ValueError("退款金额不能超过订单剩余可退金额")
    return refund_amount


async def _invoke_channel_refund(order: dict, amount: Decimal) -> None:
    """渠道退款（事务外 I/O，超时保护）。失败抛 ValueError，不产生任何账务变更。

    P2 修复：原先从不调用渠道退款。渠道已成功但本侧账务事务失败的缺口由对账兜底（模拟模式）。
    """
    from .channels import get_channel
    channel = get_channel(order["payment_method"])
    result = await asyncio.wait_for(channel.refund(order, amount), timeout=10.0)
    if not result.get("success"):
        raise ValueError(f"渠道退款失败: {result.get('message', '未知原因')}")


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
                         requested_amount: Decimal, reason: str):
    """事务内结算退款：退款单 + 原单状态联动 + 退钱 + 流水（同事务，任一步失败整体回滚）。

    2026-09-05 修复双重退款竞态（对齐充值路径 P0 #1 的锁行复验模式）：
    - 事务内 FOR UPDATE 锁原单并复验状态/重查已退累计——外层读、渠道调用（最长 10s）
      与分布式锁（TTL）之间的窗口内，并发退款可能已提交，外层快照不可信；
    - 请求金额超过剩余可退 → 抛异常回滚（此时渠道侧多退的缺口走对账，与充值路径同口径）；
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
        refund_amount = _decide_refund(dict(locked), fresh_sum, requested_amount)

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
        new_status = ("refunded" if fresh_sum + refund_amount >= locked["amount"]
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
    return refund_order_no, now


async def process_refund(
    order_no: str,
    user_id: str,
    amount: Optional[Decimal] = None,
    reason: str = "用户申请退款",
) -> dict:
    """处理退款（支持部分退款：多次退款累计防超额，退满后原单标 refunded）"""
    # TTL 30s 对齐充值路径：渠道退款超时 10s + 结算耗时，10s 默认值会在渠道变慢时过期，
    # 导致并发第二个退款请求拿新锁重复退款（2026-09-05）
    lock_key = f"refund:{order_no}"
    lock_token = await _acquire_lock(lock_key, ttl=30)
    if not lock_token:
        raise ValueError("退款正在处理中")

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 获取原订单
            row = await conn.fetchrow(
                "SELECT * FROM payment_orders WHERE order_no = $1 AND user_id = $2",
                order_no, user_id,
            )
            if not row:
                raise ValueError("订单不存在")
            order = dict(row)

            # 已退累计（从退款单反查，免加列；metadata->> 对 json/jsonb 均有效）
            refunded_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount), 0) AS refunded FROM payment_orders "
                "WHERE order_type = 'refund' AND status = 'refunded' "
                "AND metadata->>'original_order_no' = $1",
                order_no,
            )
            refunded_sum = refunded_row["refunded"] if refunded_row else Decimal("0")
            refund_amount = _decide_refund(order, refunded_sum, amount)

            # 渠道退款（事务外，P0 #33 同款约束：渠道 I/O 不持有 DB 连接）
            await _invoke_channel_refund(order, refund_amount)

            # 结算（同一事务：锁行复验 + 任一步失败整体回滚，防"订单标记退款但钱没退回"的不一致）
            refund_order_no, now = await _settle_refund(
                conn, order_no, user_id, refund_amount, reason)

            return _refund_result(order_no, refund_order_no, refund_amount, now)
    finally:
        await _release_lock(lock_key, lock_token)
