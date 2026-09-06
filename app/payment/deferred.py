"""待结算扣费：钱包锁竞争导致扣费无法即时完成时的占位与补偿结算（2026-09-05 新增）

背景：deduct_token_cost 原实现拿不到钱包锁就直接跳过扣费——高并发下漏计费且无痕。
方案：拿锁退避重试仍失败 → 落一张 pending 扣费单（占位，不动余额，"存一个空值"）→
维护循环（db_maintenance.maintenance_loop）周期调用 settle_pending_deductions 补偿结算：
锁钱包 → 按占位单记录的 token_count 重算费用 → 扣款 + 订单转 success + 记流水，同一事务原子完成。
幂等：结算 UPDATE 带 status='pending' 前置条件，多实例并发结算只有一个生效（其余整体回滚）。
"""
import json
from decimal import Decimal

from ..core.db import get_pool
from ..core.logging import setup_logging
from ._ledger import (
    _acquire_lock, _release_lock, _do_record_tx,
    _locked_wallet_change,
)

logger = setup_logging()

# 待结算单最小账龄（秒）：避开"锁刚释放、原请求还在收尾"的短窗口，给正常路径留重试余地
PENDING_MIN_AGE_SECONDS = 60
# 单轮最多结算张数：控制维护任务单次耗时
SETTLE_BATCH_LIMIT = 50


async def defer_deduction(order_no: str, user_id: str, cost_amount: Decimal,
                          token_count: int, session_id: str, remark: str = "") -> None:
    """落待结算占位单（status=pending，不动余额）。

    占位而非跳过：跳过 = 漏计费且无痕；占位 = 费用可见、可结算、可对账。
    入库失败打 error 级别——占位丢失就退化回旧行为，必须可告警。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            """INSERT INTO payment_orders
               (order_no, user_id, order_type, amount, status, subject,
                payment_method, metadata, callback_status)
               VALUES ($1, $2, 'payment', $3, 'pending', $4,
                       'balance', $5, 'not_needed')""",
            order_no, user_id, -cost_amount,
            f"AI对话消耗 {token_count} tokens（待结算）",
            json.dumps({
                "token_count": token_count,
                "session_id": session_id,
                "remark": remark,
                "deduct_type": "deferred",
            }),
        )
    logger.warning(f"扣费转待结算占位: user={user_id}, order={order_no}, 应扣={cost_amount}")


def _compute_cost(token_count: int):
    """与 service._compute_token_cost 同一公式；函数内延迟导入避免模块循环依赖"""
    from .service import _compute_token_cost  # noqa: PLC0415
    return _compute_token_cost(token_count)


async def settle_pending_deductions(min_age_seconds: int = PENDING_MIN_AGE_SECONDS,
                                    limit: int = SETTLE_BATCH_LIMIT) -> int:
    """补偿结算待结算扣费单；返回本轮成功结算张数。由维护循环周期调用。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            """SELECT order_no, user_id, metadata FROM payment_orders
               WHERE order_type = 'payment' AND status = 'pending'
                 AND payment_method = 'balance'
                 AND updated_at < CURRENT_TIMESTAMP - ($1 * INTERVAL '1 second')
               ORDER BY updated_at
               LIMIT $2""",
            min_age_seconds, limit,
        )
    settled = 0
    for row in rows:
        try:
            if await _settle_one(row["order_no"], row["user_id"], row["metadata"]):
                settled += 1
        except Exception as e:
            logger.error(f"待结算扣费单处理失败: order={row['order_no']}, err={e}", exc_info=True)
    if rows:
        logger.info(f"待结算扣费单本轮处理 {len(rows)} 张, 成功 {settled} 张")
    return settled


async def _settle_one(order_no: str, user_id: str, metadata) -> bool:
    """结算单张待结算扣费单（钱包锁 + 事务原子）；钱包仍忙返回 False 留待下轮"""
    meta = {}
    if metadata:
        meta = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
    token_count = int(meta.get("token_count") or 0)
    cost_amount = _compute_cost(token_count)
    if cost_amount is None or cost_amount <= 0:
        # 无法计价的异常占位单：作废（failed 为既有合法状态值），防止永久滞留
        await _void_order(order_no)
        return False

    lock_key = f"wallet:{user_id}"
    lock_token = await _acquire_lock(lock_key)
    if not lock_token:
        return False  # 钱包仍繁忙：留待下轮
    try:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                # FOR UPDATE 锁钱包行（与即时扣费路径同款）；缺失则占位单无从产生，留待人工
                wallet_row = await conn.fetchrow(
                    "SELECT balance FROM user_wallets WHERE user_id = $1 FOR UPDATE",
                    user_id,
                )
                if not wallet_row:
                    logger.error(f"待结算扣费单无对应钱包: order={order_no}, user={user_id}")
                    return False
                before_balance = wallet_row["balance"]
                actual_deduct = cost_amount
                if before_balance < cost_amount and before_balance > 0:
                    # 与即时扣费同语义：余额不足按剩余余额扣
                    actual_deduct = before_balance
                # 订单转 success 带 pending 前置：多实例并发结算只有一个生效，其余整体回滚
                result = await conn.execute(
                    """UPDATE payment_orders SET amount = $1, status = 'success',
                       paid_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                       metadata = $2
                       WHERE order_no = $3 AND status = 'pending'""",
                    -actual_deduct,
                    json.dumps({**meta, "deduct_type": "deferred_settled"}),
                    order_no,
                )
                if "UPDATE 1" not in result:
                    raise ValueError("订单已被并发结算，回滚本次钱包变更（防双扣）")
                updated, before, after, _ = await _locked_wallet_change(
                    conn, user_id, -actual_deduct, tx_type="consume"
                )
                if not updated:
                    raise ValueError("钱包更新失败，回滚订单状态")
                await _do_record_tx(
                    conn, order_no, user_id, "consume", actual_deduct,
                    before, after,
                    meta.get("remark") or f"AI对话消耗 {token_count} tokens（延后结算）",
                    "system",
                )
        logger.info(f"待结算扣费单已结算: order={order_no}, user={user_id}, 实扣={actual_deduct}")
        return True
    finally:
        await _release_lock(lock_key, lock_token)


async def _void_order(order_no: str) -> None:
    """作废异常占位单（failed 为既有合法状态值，不引入新状态规避 CHECK 约束风险）"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            "UPDATE payment_orders SET status = 'failed', updated_at = CURRENT_TIMESTAMP "
            "WHERE order_no = $1 AND status = 'pending'",
            order_no,
        )
    logger.warning(f"待结算占位单已作废（无法计价）: order={order_no}")
