"""
支付核心业务逻辑：钱包/订单管理、充值支付（含分布式锁+乐观锁）、Token 扣费（10元/万token）、退款、流水。
账本/锁/余额原语见 _ledger.py；只读查询见 query.py。
"""
import json
import asyncio
from decimal import Decimal
from datetime import timedelta

# 兼容性：asyncpg 对 offset-aware datetime 有 bug，统一用 naive UTC
from typing import Optional

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.config import (
    RECHARGE_MIN_AMOUNT,
    RECHARGE_MAX_AMOUNT,
    DEFAULT_WALLET_BALANCE,
    ORDER_EXPIRE_SECONDS,
    TOKEN_COST_RATE,  # 每万token价格（元）
)
from ..core.logging import setup_logging
from .channels import get_channel, RECHARGE_ALLOWED_CHANNELS
# 账本/锁/余额原语（2026-08-31 从本模块拆出，见 _ledger.py）：re-export 保留对外公开符号
from ._ledger import (
    _utcnow, _to_iso, _generate_order_no,
    _acquire_lock, _release_lock, _apply_balance_change, _do_update_balance,
    _do_record_tx, _record_transaction, ensure_wallet_exists, get_wallet,
    _update_wallet_balance, _locked_wallet_change, _LOCK_PREFIX,
)
# 只读查询层（2026-08-31 从本模块拆出，见 query.py）：re-export 保留对外公开符号
from .query import (
    get_transaction_logs, get_order_list, get_order_detail, get_channels,
    get_admin_stats, get_admin_order_list,
)
# 退款流程（2026-09-05 拆出，见 refund.py）：re-export 保留对外公开符号
from .refund import process_refund  # noqa: F401
# 待结算扣费（2026-09-05 新增，见 deferred.py）：钱包忙时的占位与补偿结算
from .deferred import defer_deduction  # noqa: F401
from prometheus_client import Counter

logger = setup_logging()

# A20：支付关键指标（成功率 / 扣费）
payment_recharge_total = Counter("payment_recharge_total", "充值请求数（按结果）", ["status"])
payment_deduct_total = Counter("payment_deduct_total", "Token 扣费请求数（按结果）", ["status"])


# ============================================================
# 充值流程
# ============================================================
async def _probe_idempotent_recharge(idempotent_key: str, user_id: str) -> tuple:
    """幂等探测（A15）：已命中返回 (已有订单dict, r, key, 占位bool)；占位未完成则抛 ValueError。

    命中已完成订单：返回 (order_dict, r, key, False)，调用方据此幂等返回；
    无幂等键：返回 (None, None, None, False)；SETNX 占位成功：返回 (None, r, key, True)。
    占位先于订单（Redis 失败不会造成重复订单），与主流程无事务耦合，可安全独立。
    """
    if not idempotent_key:
        return None, None, None, False
    r = await get_redis()
    idem_key = f"payment:idem:recharge:{idempotent_key}"
    existing = await r.get(idem_key)
    if existing:
        existing = existing.decode() if isinstance(existing, bytes) else existing
        if existing != "processing":
            # 已有完成订单 → 幂等返回
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM payment_orders WHERE order_no = $1 AND user_id = $2",
                    existing, user_id,
                )
            if row:
                o = dict(row)
                logger.info(f"幂等键命中，返回已有订单: order_no={existing}")
                return o, r, idem_key, False
        # 占位未完成（processing）：并发在建，拒绝重复
        raise ValueError("订单创建中，请稍后再试")
    # SETNX 占位（A15：防并发重复建单；占位先于订单，Redis 失败不会造成重复订单）
    ok = await r.set(idem_key, "processing", nx=True, ex=ORDER_EXPIRE_SECONDS)
    if ok is not True:
        raise ValueError("订单创建中，请稍后再试")
    return None, r, idem_key, True


def _validate_recharge_amount(amount: Decimal):
    """校验充值金额上下限（纯校验，越界抛 ValueError）"""
    if amount < Decimal(str(RECHARGE_MIN_AMOUNT)):
        raise ValueError(f"充值金额不能低于 {RECHARGE_MIN_AMOUNT} 元")
    if amount > Decimal(str(RECHARGE_MAX_AMOUNT)):
        raise ValueError(f"充值金额不能超过 {RECHARGE_MAX_AMOUNT} 元")


async def create_recharge_order(
    user_id: str,
    amount: Decimal,
    payment_method: str = "simulated_alipay",
    subject: str = "账户余额充值",
    idempotent_key: Optional[str] = None,
) -> dict:
    """创建充值订单（支持 Idempotent-Key 幂等去重）"""
    existing, r, _idem_key, _idem_occupied = await _probe_idempotent_recharge(idempotent_key, user_id)
    if existing is not None:
        # 幂等命中：返回已有订单
        return {
            "order_no": existing["order_no"],
            "amount": float(existing["amount"]),
            "fee": 0.0,
            "status": existing["status"],
            "payment_method": existing["payment_method"],
            "expire_at": _to_iso(existing.get("expire_at")),
            "created_at": _to_iso(existing.get("created_at")),
        }

    try:
        # 充值渠道白名单（P0 修复）：balance 是"用余额支付"，用于充值=无资金动作直接加余额
        if payment_method not in RECHARGE_ALLOWED_CHANNELS:
            raise ValueError(f"充值不支持该支付渠道: {payment_method}")

        # 校验金额
        _validate_recharge_amount(amount)

        order_no = await _generate_order_no()
        now = _utcnow()
        expire_at = now + timedelta(seconds=ORDER_EXPIRE_SECONDS)

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO payment_orders
                   (order_no, user_id, order_type, amount, status, subject,
                    payment_method, expire_at, callback_status)
                   VALUES ($1, $2, 'recharge', $3, 'pending', $4, $5, $6, 'not_needed')""",
                order_no, user_id, amount, subject, payment_method, expire_at,
            )

        # 订单创建成功 → 占位更新为订单号（TTL 与订单有效期一致，覆盖重试窗口）
        if _idem_occupied and r is not None:
            try:
                await r.set(_idem_key, order_no, ex=ORDER_EXPIRE_SECONDS)
            except Exception as _e:
                logger.warning(f"幂等键写入失败: {_e}")
    except Exception:
        # 建单/校验失败：回滚占位，允许后续重试
        if _idem_occupied and r is not None:
            try:
                await r.delete(_idem_key)
            except Exception as _e:
                # 占位回滚失败只影响"失败后能否立即重试"，幂等键自带 TTL 自愈，不影响资金
                logger.warning(f"幂等占位回滚失败（TTL 自愈）: {_e}")
        raise

    logger.info(f"创建充值订单: order_no={order_no}, user={user_id}, amount={amount}")
    return {
        "order_no": order_no,
        "amount": float(amount),
        "fee": 0.0,
        "status": "pending",
        "payment_method": payment_method,
        "expire_at": _to_iso(expire_at),
        "created_at": _to_iso(now),
    }


async def _load_order(pool, order_no: str, user_id: str) -> dict:
    """读取订单（短连接，读完即释放，避免持有连接等待渠道）；不存在抛 ValueError"""
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payment_orders WHERE order_no = $1 AND user_id = $2",
            order_no, user_id,
        )
    if not row:
        raise ValueError("订单不存在")
    return dict(row)


async def _precheck_terminal_status(order: dict, pool, user_id: str) -> Optional[dict]:
    """状态前置判断：已成功 → 返回幂等成功结果 dict（调用方直接返回）；终态 → 抛 ValueError；可继续 → None

    P2 修复：failed 不再视为终态，允许同单重试支付（渠道失败后无需重建订单）。
    非事务读（单独短连接）；终态判断与状态更新原子性交由事务内 FOR UPDATE 兜底（P0 #1）。
    """
    if order["status"] in ("pending", "failed"):
        return None
    if order["status"] == "success":
        async with pool.acquire(timeout=5) as conn:
            wallet_row = await conn.fetchrow(
                "SELECT balance FROM user_wallets WHERE user_id = $1", user_id
            )
        return {
            "order_no": order["order_no"],
            "status": "success",
            "paid_at": _to_iso(order.get("paid_at")),
            "channel_order_no": order.get("channel_order_no", ""),
            "current_balance": float(wallet_row["balance"]) if wallet_row else None,
            "message": "订单已支付成功（幂等返回）",
        }
    if order["status"] == "expired":
        raise ValueError("订单已过期")
    raise ValueError(f"订单状态不允许支付: {order['status']}")


async def _invoke_channel(order: dict) -> dict:
    """调用支付渠道（事务外，P0 #33：渠道路径 I/O 不持有 DB 连接；超时保护 P1 #20）

    A17：admin 停用渠道后立即生效（实时查 is_active）。返回渠道结果 dict。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        _active = await conn.fetchval(
            "SELECT is_active FROM payment_channels WHERE channel_code=$1",
            order["payment_method"],
        )
    if _active is False:
        return {"success": False, "message": "支付渠道已停用"}
    channel = get_channel(order["payment_method"])
    return await asyncio.wait_for(channel.pay(order), timeout=10.0)


def _recharge_response(channel_result: dict, order_no: str, now, after_balance) -> dict:
    """组装充值结果响应（A20 埋点：计数后按成功/失败返回）"""
    payment_recharge_total.labels(status="success" if channel_result["success"] else "failed").inc()
    if channel_result["success"]:
        return {
            "order_no": order_no,
            "status": "success",
            "paid_at": _to_iso(now),
            "channel_order_no": channel_result.get("channel_order_no", ""),
            "current_balance": float(after_balance),
            "message": channel_result.get("message", "充值成功"),
        }
    return {
        "order_no": order_no,
        "status": "failed",
        "paid_at": None,
        "channel_order_no": "",
        "current_balance": None,
        "message": channel_result.get("message", "支付失败"),
    }


async def _settle_recharge(conn, order_no: str, user_id: str, channel_result: dict) -> tuple:
    """事务内结算充值：FOR UPDATE 锁行 → 状态/过期最终校验 → 更新订单 + 钱包 + 流水（原子，P0 #1）。

    返回 (支付时间 now, 变更后余额)；渠道失败返回 (None, None)（fix：避免 else 分支末尾引用
    未定义 now/after_balance 引发 UnboundLocalError 500）。
    """
    async with conn.transaction():
        locked = await conn.fetchrow(
            "SELECT * FROM payment_orders WHERE order_no = $1 FOR UPDATE",
            order_no,
        )
        if not locked:
            raise ValueError("订单不存在")
        # P2 修复：failed 允许重试（同单重新支付），其余非 pending 状态拒绝
        if locked["status"] not in ("pending", "failed"):
            raise ValueError(f"订单状态不允许支付: {locked['status']}")
        if locked["expire_at"] and locked["expire_at"] < _utcnow():
            await conn.execute(
                "UPDATE payment_orders SET status = 'expired', updated_at = CURRENT_TIMESTAMP "
                "WHERE order_no = $1", order_no,
            )
            raise ValueError("订单已过期")

        if not channel_result["success"]:
            # 渠道失败：标记 failed（无 paid_at / 无余额变动），返回 None 占位
            await conn.execute(
                "UPDATE payment_orders SET status = 'failed', updated_at = CURRENT_TIMESTAMP "
                "WHERE order_no = $1", order_no,
            )
            logger.warning(f"充值失败: order_no={order_no}, reason={channel_result.get('message')}")
            return None, None

        now = _utcnow()
        amount = locked["amount"]
        payment_method = locked["payment_method"]
        await conn.execute(
            """UPDATE payment_orders SET
               status = 'success', paid_at = $1,
               channel_order_no = $2,
               callback_status = 'not_needed',
               updated_at = CURRENT_TIMESTAMP
               WHERE order_no = $3""",
            now, channel_result.get("channel_order_no", ""), order_no,
        )
        # 行锁确定性更新余额（2026-09-05 由乐观锁重试改为 FOR UPDATE）：
        # 渠道调用已生效后再回滚的代价是"用户已付款、余额未入账"，
        # 乐观锁重试耗尽正是唯一的常态回滚源；行锁让并发写者排队而非冲突。
        # tx_type 区分累计字段（P0 #15）
        updated, before_balance, after_balance, _ = await _locked_wallet_change(
            conn, user_id, amount, tx_type="recharge"
        )
        if not updated:
            raise ValueError("钱包更新失败，请重试")
        await _do_record_tx(
            conn, order_no, user_id, "recharge", amount,
            before_balance, after_balance,
            f"充值 {amount} 元（{payment_method}）", "system",
        )
        logger.info(f"充值成功: order_no={order_no}, user={user_id}, amount={amount}")
        return now, after_balance


async def process_recharge(order_no: str, user_id: str) -> dict:
    """
    处理充值支付（模拟支付确认）

    结构（2026-08-18 重构，修复资金链路硬伤）：
    - 渠道调用（外部 I/O，1-3s）移出事务且不持有 DB 连接（P0 #33）：先短连接读订单、释放连接、
      渠道调用、再开事务写库，避免慢渠道独占连接导致池耗尽。
    - 连接获取统一带 5s 超时（P1 #18）；分布式锁 TTL 30s（P1 #21）；渠道调用 10s 超时（P1 #20）。
    - 订单过期检查与状态更新同事务 + FOR UPDATE 锁行（P0 #1：防状态与资金不一致）。
    - 先抢锁（等待期零连接占用）再取连接（带超时），缓解锁-连接死锁（P0 #41）。
    """
    # 1. 抢分布式锁（等待期间不占用数据库连接；TTL 30s 覆盖渠道耗时，P1 #21）
    lock_key = f"order:{order_no}"
    lock_token = await _acquire_lock(lock_key, ttl=30)
    if not lock_token:
        raise ValueError("订单正在处理中，请勿重复提交")

    try:
        pool = await get_pool()
        # 2. 读取订单（短连接，读完即释放，避免持有连接等待渠道）
        order = await _load_order(pool, order_no, user_id)

        # 3. 状态前置判断（幂等：已成功直接返回；终态明确报错）
        idem_result = await _precheck_terminal_status(order, pool, user_id)
        if idem_result is not None:
            return idem_result

        # 4. 过期前置检查（只读判断；状态标记留待事务内原子处理，P0 #1）
        if order["expire_at"] and order["expire_at"] < _utcnow():
            raise ValueError("订单已过期")

        # 5. 渠道 active 检查（A17：admin 停用渠道后立即生效）+ 调用（事务外，P0 #33；超时保护 P1 #20）
        channel_result = await _invoke_channel(order)

        # 6. 事务内结算（FOR UPDATE 锁行 → 状态/过期最终校验 → 更新订单 + 钱包 + 流水，原子 P0 #1）
        async with pool.acquire(timeout=5) as conn:
            now, after_balance = await _settle_recharge(conn, order_no, user_id, channel_result)

        # 7. 返回结果（A20 埋点）
        return _recharge_response(channel_result, order_no, now, after_balance)
    finally:
        await _release_lock(lock_key, lock_token)


# ============================================================
# Token 扣费逻辑（10元/万token）
# ============================================================
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
        actual_deduct = cost_amount

        # 余额不足时按剩余余额扣（模拟模式不拦截）
        if before_balance < cost_amount and before_balance > 0:
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
            order_no, user_id, -actual_deduct, subject,
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

    # 分布式锁：同一用户钱包的并发变更串行化（防止并发扣费版本冲突）。
    # 竞争时退避重试 3 次；仍失败转「待结算占位单」由维护任务补偿结算——
    # 原实现直接跳过 = 高并发下漏计费且无痕（2026-09-05 修复，占位见 deferred.py）。
    lock_key = f"wallet:{user_id}"
    lock_token = None
    for attempt in range(3):
        lock_token = await _acquire_lock(lock_key)
        if lock_token:
            break
        await asyncio.sleep(0.05 * (2 ** attempt))
    if not lock_token:
        await defer_deduction(order_no, user_id, cost_amount, token_count, session_id, remark)
        return {"deducted": False, "amount": 0, "reason": "wallet busy, deferred",
                "order_no": order_no, "pending_settlement": True}

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            before_balance, actual_deduct, new_balance = await _apply_token_deduction(
                conn, user_id, cost_amount, token_count, order_no, session_id, remark,
            )
    except ValueError as e:
        # 业务失败（版本冲突等）：事务已回滚，返回未扣费（P0 #2）
        logger.warning(f"扣费业务失败，跳过: user={user_id}, reason={e}")
        return {"deducted": False, "amount": 0, "reason": str(e)}
    except Exception as e:
        # 系统故障：记录 Error 日志后返回未扣费（模拟模式尽力而为；真实环境应接入重试/熔断）
        logger.error(f"扣费系统异常: user={user_id}, err={e}", exc_info=True)
        return {"deducted": False, "amount": 0, "reason": "system error"}
    finally:
        await _release_lock(lock_key, lock_token)

    logger.info(f"Token扣费: user={user_id}, tokens={token_count}, "
                f"amount={actual_deduct}, balance_before={before_balance}, balance_after={new_balance}")
    payment_deduct_total.labels(status="success").inc()  # A20 埋点

    return {
        "deducted": True,
        "order_no": order_no,
        "amount": float(actual_deduct),
        "token_count": token_count,
        "balance_before": float(before_balance),
        "balance_after": float(new_balance),
        "remark": remark,
    }


# ============================================================
# 退款流程已拆至 refund.py（2026-09-05，行为等价移动）；
# 支付 schema DDL 由 Alembic 迁移管理（A19/A23）。
# ============================================================
async def recover_stale_processing(age_seconds: int = 300) -> int:
    """恢复超时的 processing 订单（P2 C10：模拟支付期间进程崩溃后订单卡死）

    将超过 age_seconds 未更新的 processing 订单回退为 failed，防永久卡死。
    由外部维护循环定期调用（建议接入 app/core/db_maintenance.py 的 maintenance_loop）。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        result = await conn.execute(
            "UPDATE payment_orders SET status = 'failed', updated_at = CURRENT_TIMESTAMP "
            "WHERE status = 'processing' AND updated_at < NOW() - ($1 * INTERVAL '1 second')",
            age_seconds,
        )
    affected = int(result.split()[-1]) if result else 0
    if affected:
        logger.warning(f"恢复 {affected} 个超时 processing 订单为 failed")
    return affected
