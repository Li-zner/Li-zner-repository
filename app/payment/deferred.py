"""待结算扣费：钱包锁竞争导致扣费无法即时完成时的占位与补偿结算（2026-09-05 新增）

背景：deduct_token_cost 原实现拿不到钱包锁就直接跳过扣费——高并发下漏计费且无痕。
方案：拿锁退避重试仍失败 → 落一张 pending 扣费单（占位，不动余额，"存一个空值"）→
维护循环（db_maintenance.maintenance_loop）周期调用 settle_pending_deductions 补偿结算：
锁钱包 → **按占位单已入库的 amount 结算**（不再按当前费率重算，见 _settle_one 注释）→
扣款 + 订单转 success + 记流水，同一事务原子完成。
幂等：结算 UPDATE 带 status='pending' 前置条件，多实例并发结算只有一个生效（其余整体回滚）。
"""
import asyncio
import json
from decimal import Decimal

from ..core.db import get_pool
from ..core.redis import get_redis
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
# 无钱包连续失败轮数上限（2026-09-07 审查 P2 毒丸单防护）：达到即作废
SETTLE_NO_WALLET_MAX_FAILS = 5


async def _bump_settle_fail(order_no: str) -> int:
    """毒丸单失败计数（Redis INCR，7 天窗口自愈）；Redis 不可用按 0 处理不误杀"""
    try:
        r = await get_redis()
        key = f"deferred_no_wallet_fails:{order_no}"
        count = await r.incr(key)
        await r.expire(key, 7 * 86400)
        return int(count)
    except Exception as e:
        logger.warning(f"待结算失败计数不可用（本轮跳过作废判定）: {type(e).__name__}")
        return 0


async def defer_deduction(order_no: str, user_id: str, cost_amount: Decimal,
                          token_count: int, session_id: str, remark: str = "") -> None:
    """落待结算占位单（status=pending，不动余额）。

    占位而非跳过：跳过 = 漏计费且无痕；占位 = 费用可见、可结算、可对账。
    入库失败不在这里吞：异常向上抛给唯一的计费收口
    `llm_streaming._log_deduct_task_error`，那里按 error 级认领（2026-09-19 审查
    06-payment F7 提级），可告警；但这笔费用当场只能靠日志找回，无占位行可对账。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            """INSERT INTO payment_orders
               (order_no, user_id, order_type, amount, status, subject,
                payment_method, metadata, callback_status)
               VALUES ($1, $2, 'payment', $3, 'pending', $4,
                       'balance', $5, 'not_needed')""",
            order_no, user_id, cost_amount,
            f"AI对话消耗 {token_count} tokens（待结算）",
            json.dumps({
                "token_count": token_count,
                "session_id": session_id,
                "remark": remark,
                "deduct_type": "deferred",
            }),
        )
    logger.warning(f"扣费转待结算占位: user={user_id}, order={order_no}, 应扣={cost_amount}")


async def settle_pending_deductions(min_age_seconds: int = PENDING_MIN_AGE_SECONDS,
                                    limit: int = SETTLE_BATCH_LIMIT) -> int:
    """补偿结算待结算扣费单；返回本轮成功结算张数。由维护循环周期调用。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            """SELECT order_no, user_id, amount, metadata FROM payment_orders
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
            if await _settle_one(
                    row["order_no"], row["user_id"], row["amount"], row["metadata"]):
                settled += 1
        except Exception as e:
            logger.error(f"待结算扣费单处理失败: order={row['order_no']}, err={e}", exc_info=True)
    if rows:
        logger.info(f"待结算扣费单本轮处理 {len(rows)} 张, 成功 {settled} 张")
    return settled


async def _settle_one(order_no: str, user_id: str, stored_amount, metadata) -> bool:
    """结算单张待结算扣费单（钱包锁 + 事务原子）；钱包仍忙返回 False 留待下轮

    2026-09-22 审阅 P2：结算基数取**占位单已入库的 amount**，不再拿 token_count 按当前
    TOKEN_COST_RATE 重算——占位单落库那一刻价格即已定，重算等于把费率调整**追溯**应用到
    历史欠费，且同一张单在费率前后会结算出两个金额（口径漂移 + 破坏补偿结算的幂等）。
    """
    meta = {}
    if metadata:
        meta = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
    token_count = int(meta.get("token_count") or 0)  # 仅供流水摘要文案，不参与计价
    cost_amount = Decimal(str(stored_amount if stored_amount is not None else 0))
    if cost_amount <= 0:
        # 金额缺失/非正的异常占位单：作废（failed 为既有合法状态值），防止永久滞留
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
                    # 毒丸单防护（2026-09-07 审查 P2）：钱包不存在原先永久重试，
                    # 每维护轮刷一条 error。连续 5 轮仍无钱包则作废止血留痕
                    #（正常占位单产生于钱包锁竞争，必有对应钱包）。
                    fails = await _bump_settle_fail(order_no)
                    if fails >= SETTLE_NO_WALLET_MAX_FAILS:
                        logger.error(
                            f"待结算扣费单连续 {fails} 轮无对应钱包，作废: "
                            f"order={order_no}, user={user_id}")
                        await _void_order(order_no)
                    return False
                before_balance = wallet_row["balance"]
                actual_deduct = cost_amount
                # 与即时扣费路径保持同一资金语义：余额非正不扣，余额不足只扣剩余，
                # 且订单金额统一存正数，避免待结算路径制造负余额和反向账务。
                if before_balance <= 0:
                    actual_deduct = Decimal("0")
                elif before_balance < cost_amount:
                    actual_deduct = before_balance
                # SET amount 写回的是"实扣额"（余额不足时被上面钳低），不是重算的价格
                # 订单转 success 带 pending 前置：多实例并发结算只有一个生效，其余整体回滚
                result = await conn.execute(
                    """UPDATE payment_orders SET amount = $1, status = 'success',
                       paid_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                       metadata = $2
                       WHERE order_no = $3 AND status = 'pending'""",
                    actual_deduct,
                    json.dumps({**meta, "deduct_type": "deferred_settled"}),
                    order_no,
                )
                if "UPDATE 1" not in result:
                    raise ValueError("订单已被并发结算，回滚本次钱包变更（防双扣）")
                if actual_deduct == 0:
                    await _finish_zero_balance(conn, order_no, user_id, meta)
                    return True
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


async def _finish_zero_balance(conn, order_no: str, user_id: str, meta: dict) -> None:
    """零余额仍保留订单终态和审计元数据，但不改钱包、不写零金额流水。"""
    await conn.execute(
        "UPDATE payment_orders SET metadata = $1 "
        "WHERE order_no = $2 AND status = 'success'",
        json.dumps({**meta, "deduct_type": "zero_balance"}),
        order_no,
    )
    logger.info(
        f"待结算扣费单余额非正，按 0 元结算: order={order_no}, user={user_id}"
    )


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


async def settlement_loop(interval_seconds: int = 300):
    """待结算补偿短周期循环（2026-09-10 审查 P2）：原随 24h 维护循环结算，
    占位扣费单最长滞后 24h 才入流水。拆独立 5 分钟循环尽快入账；
    与维护循环内的调用并存无害（结算带 status='pending' 前置，幂等）。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            settled = await settle_pending_deductions()
            if settled:
                logger.info(f"待结算补偿循环结算 {settled} 张")
            from .attempts import settle_recoverable_attempts
            recovered = await settle_recoverable_attempts()
            if recovered:
                logger.info(f"支付尝试恢复循环结算 {recovered} 张")
        except Exception as e:
            logger.warning(f"待结算补偿循环异常（下轮重试）: {e}")
