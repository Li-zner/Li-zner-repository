"""Token 扣费（10元/万token）：计费判定 → 钱包分布式锁 → 事务内扣费与流水。

2026-09-19 从 service.py 拆出（kefa 文件 ≤600 行硬门禁），行为等价移动；
service.py re-export 保留对外公开符号（refund.py 同款先例）。
依赖方向 service → deduction → deferred/_ledger 保持外层依赖内层，不成环
（deferred 对 service 的引用是函数内延迟导入）。
"""
import asyncio
import json
from decimal import Decimal
from typing import Optional

from prometheus_client import Counter

from ..core.db import get_pool
from ..core.config import (
    DEFAULT_WALLET_BALANCE,
    TOKEN_COST_RATE,  # 每万token价格（元）
)
from ..core.logging import setup_logging
from ._ledger import (
    _acquire_lock, _release_lock, _apply_balance_change, _do_record_tx,
    _generate_order_no,
)
from .deferred import defer_deduction

logger = setup_logging()

# A20：Token 扣费指标（原定义在 service.py，随扣费逻辑一起搬入）
payment_deduct_total = Counter("payment_deduct_total", "Token 扣费请求数（按结果）", ["status"])


def _compute_token_cost(token_count: int) -> Optional[Decimal]:
    """按 10元/万token 计算扣费金额（保留2位小数）；token<=0 或金额过小返回 None（不扣费）

    为独立纯函数，方便单测与复用；扣费判定在此完成，主流程只判断 None 分支。
    """
    if token_count <= 0:
        return None
    cost_amount = Decimal(str(token_count)) / Decimal("10000") * Decimal(str(TOKEN_COST_RATE))
    cost_amount = cost_amount.quantize(Decimal("0.01"))
    if cost_amount <= 0:
        return None
    return cost_amount


async def _apply_token_deduction(
    conn, user_id: str, cost_amount: Decimal, token_count: int,
    order_no: str, session_id: str, remark: str,
) -> tuple:
    """事务内扣费核心：写扣费订单 → 乐观锁更新余额 → 记流水（原子，任一失败整体回滚）。

    余额读取与"不足按余额扣"的判定都在事务内完成（P1 修复：快照原先在事务外读，
    充值路径不持钱包锁，并发时流水 before/after 不衔接、实扣金额基于过期余额）；
    FOR UPDATE 行锁使充值/退款/扣费在本行上串行（单行锁无死锁环）。
    返回 (变更前余额, 实际扣费, 变更后余额)；无钱包则并发安全创建（P0 #34）。
    """
    async with conn.transaction():
        # 事务内读钱包并锁行；无钱包则原子创建后重读（P0 #34：防并发主键冲突）
        wallet_row = await conn.fetchrow(
            "SELECT balance FROM user_wallets WHERE user_id = $1 FOR UPDATE",
            user_id,
        )
        if not wallet_row:
            await conn.execute(
                "INSERT INTO user_wallets (user_id, balance, status) "
                "VALUES ($1, $2, 'active') ON CONFLICT (user_id) DO NOTHING",
                user_id, DEFAULT_WALLET_BALANCE,
            )
            wallet_row = await conn.fetchrow(
                "SELECT balance FROM user_wallets WHERE user_id = $1 FOR UPDATE",
                user_id,
            )

        before_balance = wallet_row["balance"]

        # 余额已 ≤0：模拟模式不拦截对话，但金钱不得为负——本次不扣。
        # （2026-09-12 主人验收发现：原守卫 before_balance > 0 时才走"按剩余扣"，
        # 负余额反而全额照扣越扣越负，实测 -4.67 → -5.09）
        if before_balance <= 0:
            return before_balance, Decimal("0"), before_balance

        actual_deduct = cost_amount

        # 余额不足时按剩余余额扣（扣到 0 为止，不穿透）
        if before_balance < cost_amount:
            actual_deduct = before_balance
            logger.info(f"余额不足，按剩余余额扣费: user={user_id}, "
                        f"应扣={cost_amount}, 实扣={actual_deduct}")

        # 写入扣费订单
        subject = f"AI对话消耗 {token_count} tokens"
        await conn.execute(
            """INSERT INTO payment_orders
               (order_no, user_id, order_type, amount, status, subject,
                payment_method, paid_at, callback_status, metadata)
               VALUES ($1, $2, 'payment', $3, 'success', $4,
                       'balance', CURRENT_TIMESTAMP, 'not_needed', $5)""",
            # 2026-09-12：金钱记录去负号——扣费单按正数金额入库（资金方向由
            # order_type/tx_type 表达），钱包余额运算仍为减法
            order_no, user_id, actual_deduct, subject,
            json.dumps({
                "token_count": token_count,
                "rate": f"{TOKEN_COST_RATE}元/万token",
                "session_id": session_id,
                "deduct_type": "full" if actual_deduct == cost_amount else "partial",
            }),
        )

        # 更新余额（乐观锁 + 冲突自动重试；行锁已持有，余额可以为负，模拟模式不阻止对话）
        updated, tx_before, new_balance, _ = await _apply_balance_change(
            conn, user_id, -actual_deduct, tx_type="consume"
        )
        if not updated:
            # 版本冲突：抛异常强制回滚（P0 #2），避免"扣费订单已入但余额未减"的账实不符
            raise ValueError("钱包版本冲突，请重试")

        # 记录流水（before 用事务内快照，保证与实际变更衔接、对账平滑）
        await _do_record_tx(
            conn, order_no, user_id, "consume", actual_deduct,
            tx_before, new_balance,
            remark or f"AI对话消耗 {token_count} tokens，费用 {actual_deduct} 元",
            "system",
        )
    return tx_before, actual_deduct, new_balance


async def _acquire_wallet_lock(user_id: str) -> tuple:
    """抢 wallet:{user_id} 分布式锁（同用户钱包变更串行化，防并发扣费版本冲突）。

    竞争时指数退避重试 3 次；仍失败返回 (lock_key, None)，调用方转「待结算占位单」
    由维护任务补偿结算——原实现直接跳过 = 高并发下漏计费且无痕
    （2026-09-05 修复，占位见 deferred.py）。
    """
    lock_key = f"wallet:{user_id}"
    for attempt in range(3):
        lock_token = await _acquire_lock(lock_key)
        if lock_token:
            return lock_key, lock_token
        await asyncio.sleep(0.05 * (2 ** attempt))
    return lock_key, None


async def deduct_token_cost(
    user_id: str,
    token_count: int,
    session_id: str = "",
    remark: str = "",
) -> dict:
    """
    根据 token 使用量扣费
    规则: 每 10000 token 扣 10 元
    注意: 余额不足仍可继续对话（模拟模式）
    """
    cost_amount = _compute_token_cost(token_count)
    if cost_amount is None:
        return {"deducted": False, "amount": 0,
                "reason": "no tokens" if token_count <= 0 else "amount too small"}

    # 生成扣费订单号
    order_no = await _generate_order_no()

    # 钱包锁策略与"抢不到就转占位单"的理由见 _acquire_wallet_lock
    lock_key, lock_token = await _acquire_wallet_lock(user_id)
    if not lock_token:
        await defer_deduction(order_no, user_id, cost_amount, token_count, session_id, remark)
        payment_deduct_total.labels(status="deferred").inc()  # 2026-09-10 审查 P2：延后可观测
        return {"deducted": False, "amount": 0, "reason": "wallet busy, deferred",
                "order_no": order_no, "pending_settlement": True}

    try:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            before_balance, actual_deduct, new_balance = await _apply_token_deduction(
                conn, user_id, cost_amount, token_count, order_no, session_id, remark,
            )
    except ValueError as e:
        # 业务失败（版本冲突等）：事务已回滚，返回未扣费（P0 #2）
        logger.warning(f"扣费业务失败，跳过: user={user_id}, reason={e}")
        payment_deduct_total.labels(status="failed").inc()  # 2026-09-10 审查 P2：失败可观测
        return {"deducted": False, "amount": 0, "reason": str(e)}
    except Exception as e:
        # 系统故障：记录 Error 日志后返回未扣费（模拟模式尽力而为；真实环境应接入重试/熔断）
        logger.error(f"扣费系统异常: user={user_id}, err={e}", exc_info=True)
        payment_deduct_total.labels(status="failed").inc()
        return {"deducted": False, "amount": 0, "reason": "system error"}
    finally:
        # 2026-09-22 审阅 P2：扣费已在事务里提交，裸调 Redis 释放锁一旦抖动就会把成功
        # 谎报成失败（上层按未扣费处理会二次扣费）。锁随 TTL(10s) 自动过期，只记日志。
        try:
            await _release_lock(lock_key, lock_token)
        except Exception as release_err:
            logger.exception(f"扣费锁释放失败（锁将随 TTL 过期，扣费结果不受影响）: {release_err}")

    logger.info(f"Token扣费: user={user_id}, tokens={token_count}, "
                f"amount={actual_deduct}, balance_before={before_balance}, balance_after={new_balance}")
    payment_deduct_total.labels(status="success").inc()  # A20 埋点

    if actual_deduct == 0:
        # 余额 ≤0 的不扣费对话（模拟模式）：如实回报未扣费
        return {
            "deducted": False,
            "order_no": order_no,
            "amount": 0,
            "token_count": token_count,
            "balance_before": float(before_balance),
            "balance_after": float(new_balance),
            "reason": "余额为零，本次未扣费（模拟模式不拦截对话）",
        }

    return {
        "deducted": True,
        "order_no": order_no,
        "amount": float(actual_deduct),
        "token_count": token_count,
        "balance_before": float(before_balance),
        "balance_after": float(new_balance),
        "remark": remark,
    }


