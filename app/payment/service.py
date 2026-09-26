"""
支付核心业务逻辑：钱包/订单管理、充值支付（含分布式锁+乐观锁）、Token 扣费（10元/万token）、退款、流水。
账本/锁/余额原语见 _ledger.py；只读查询见 query.py。
"""
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
    ORDER_EXPIRE_SECONDS,
)
from ..core.logging import setup_logging
from .channels import get_channel, recharge_allowed_channels
# 账本/锁/余额原语（2026-08-31 从本模块拆出，见 _ledger.py）：re-export 保留对外公开符号
from ._ledger import (
    _utcnow, _to_iso, _generate_order_no,
    _acquire_lock, _release_lock, _apply_balance_change, _do_update_balance,
    _do_record_tx, ensure_wallet_exists, get_wallet,
    _update_wallet_balance, _locked_wallet_change, _LOCK_PREFIX,
)
# 只读查询层（2026-08-31 从本模块拆出，见 query.py）：re-export 保留对外公开符号
from .query import (
    get_transaction_logs, get_order_list, get_order_detail, get_channels,
    get_admin_stats, get_admin_order_list,
)
# 退款流程（2026-09-05 拆出，见 refund.py）：re-export 保留对外公开符号
from .refund import process_refund  # noqa: F401
# Token 扣费（2026-09-19 拆出，见 deduction.py）：re-export 保留对外公开符号
from .deduction import deduct_token_cost, _compute_token_cost  # noqa: F401
# 待结算扣费（2026-09-05 新增，见 deferred.py）：钱包忙时的占位与补偿结算
from .deferred import defer_deduction  # noqa: F401
from .attempts import begin_channel_attempt, record_channel_result, mark_attempt_settled
from prometheus_client import Counter

logger = setup_logging()

# A20：支付关键指标（成功率 / 扣费）
payment_recharge_total = Counter("payment_recharge_total", "充值请求数（按结果）", ["status"])
# payment_orders_total 已在 core/metrics 定义（告警规则引用），此处导入复用，
# 避免同名 Counter 重复注册（2026-09-11 审查 P1 告警接线）
from ..core.metrics import payment_orders_total

# 渠道支付超时（秒）：与 order:{order_no} 锁 TTL 30s 配套（refund 侧同名常量
# _CHANNEL_REFUND_TIMEOUT_SECONDS 同口径）——渠道耗时 + 本地结算须落在锁有效期内
_CHANNEL_PAY_TIMEOUT_SECONDS = 10.0


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
    # 幂等键含用户维度（2026-09-10 审查 P2 对齐清单）：跨用户撞同一
    # X-Idempotent-Key 会误报"订单创建中"（资金不串户，仅撞键干扰）
    idem_key = f"payment:idem:recharge:{user_id}:{idempotent_key}"
    existing = await r.get(idem_key)
    if existing:
        existing = existing.decode() if isinstance(existing, bytes) else existing
        if existing != "processing":
            # 已有完成订单 → 幂等返回
            pool = await get_pool()
            async with pool.acquire(timeout=5) as conn:
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
        # PAY-2（2026-09-19）：改走环境门控函数，非 dev/test 环境白名单为空 → 模拟充值直接拒单
        if payment_method not in recharge_allowed_channels():
            raise ValueError(f"充值不支持该支付渠道: {payment_method}")

        # 校验金额
        _validate_recharge_amount(amount)

        order_no = await _generate_order_no()
        now = _utcnow()
        expire_at = now + timedelta(seconds=ORDER_EXPIRE_SECONDS)

        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
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
    """读取订单（短连接，读完即释放，避免持有连接等待渠道）；不存在抛 ValueError

    PAY-1（2026-09-19 审查）：必须是 recharge 单 —— 待结算扣费占位单同为
    pending 态但 order_type='payment'，若被本充值路径结算，会误清欠费并给钱包反向加钱。
    """
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payment_orders "
            "WHERE order_no = $1 AND user_id = $2 AND order_type = 'recharge'",
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

    2026-09-22 审阅 P1 的同类漏口（与 refund._call_channel_refund 一起修）：
    wait_for 抛的 TimeoutError 不是 ValueError，routes 的 `except ValueError` 漏接
    → 充值接口裸 500。这里收敛为同一业务口径（400，"稍后查询订单状态"）。
    超时只代表没等到回复、**渠道侧结果未知**：异常在 record_channel_result 之前抛出，
    attempt 留在 processing（300s 后转 manual_review）。绝不能记 channel_failed——
    那会让下一次重试新建 attempt 再次打渠道，真渠道下等于重复扣款。
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
    try:
        return await asyncio.wait_for(
            channel.pay(order), timeout=_CHANNEL_PAY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        logger.error(
            f"渠道支付超时（结果未知，attempt 留在 processing 待恢复/人工）: "
            f"order={order.get('order_no')}, amount={order.get('amount')}")
        raise ValueError("支付渠道响应超时，结果未知，请稍后查询订单状态") from exc


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


async def _settle_recharge(
    conn,
    order_no: str,
    user_id: str,
    channel_result: dict,
    attempt_id: Optional[str] = None,
) -> tuple:
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
        if locked["order_type"] != "recharge":
            # PAY-1 原子兜底：短连接预读到 FOR UPDATE 之间状态可能变化，
            # 非充值单绝不允许走本结算路径（防误清欠费 + 防铸币）
            raise ValueError("订单类型不允许支付")
        if locked["status"] == "success":
            # 渠道成功后的恢复任务可能再次进入；订单已成功时只补记 attempt 终态。
            wallet_row = await conn.fetchrow(
                "SELECT balance FROM user_wallets WHERE user_id = $1",
                user_id,
            )
            if attempt_id:
                await mark_attempt_settled(conn, attempt_id)
            return locked.get("paid_at"), wallet_row["balance"] if wallet_row else None
        channel_succeeded = bool(channel_result.get("success"))
        # failed 允许重试；expired 仅在渠道已经成功时允许补结算，覆盖
        # “用户已付款但本地刚过期”的窗口。
        allowed = ("pending", "failed")
        if channel_succeeded:
            allowed = allowed + ("expired",)
        if locked["status"] not in allowed:
            raise ValueError(f"订单状态不允许支付: {locked['status']}")
        if (not channel_succeeded and locked["expire_at"]
                and locked["expire_at"] < _utcnow()):
            # 纵深防御（2026-09-22 审阅 P2）：状态迁移必须自带前置条件。
            # 现在靠同一事务的 FOR UPDATE + 上面的 allowed 判定保证安全，但一旦有人
            # 去掉行锁或新增调用方，`WHERE order_no = $1` 会把已 success 的订单改写成
            # expired（等于抹掉一笔已入账的充值）。前置集合与 allowed 完全一致。
            await conn.execute(
                "UPDATE payment_orders SET status = 'expired', updated_at = CURRENT_TIMESTAMP "
                "WHERE order_no = $1 AND status IN ('pending', 'failed')", order_no,
            )
            raise ValueError("订单已过期")

        if not channel_succeeded:
            # 渠道失败：标记 failed（无 paid_at / 无余额变动），返回 None 占位
            # 同上：带状态前置，绝不允许把 success/partial_refunded 改写成 failed
            await conn.execute(
                "UPDATE payment_orders SET status = 'failed', updated_at = CURRENT_TIMESTAMP "
                "WHERE order_no = $1 AND status IN ('pending', 'failed')", order_no,
            )
            payment_orders_total.labels(
                order_type='recharge', status='failed',
                payment_method=locked["payment_method"]).inc()  # 2026-09-11 审查 P1：告警接线
            logger.warning(f"充值失败: order_no={order_no}, reason={channel_result.get('message')}")
            return None, None

        return await _apply_recharge_success(
            conn, locked, user_id, order_no, channel_result, attempt_id
        )


async def _apply_recharge_success(
    conn,
    locked: dict,
    user_id: str,
    order_no: str,
    channel_result: dict,
    attempt_id: Optional[str],
) -> tuple:
    """在订单行锁内完成成功充值、钱包入账、流水与 attempt 终态。"""
    now = _utcnow()
    amount = locked["amount"]
    payment_method = locked["payment_method"]
    # 纵深防御（2026-09-22 审阅 P2）：状态前置与 _settle_recharge 的 allowed 集合一致
    # （success 已在前面提前返回，partial_refunded/refunded 一律不许被充值改写）。
    # 影响行数必须为 1：本语句之后就是钱包入账，更新不到行说明订单状态已被并发变更，
    # 抛错让整笔事务回滚，杜绝"钱进了钱包、订单却没转 success"的账实不符。
    result = await conn.execute(
        """UPDATE payment_orders SET
           status = 'success', paid_at = $1,
           channel_order_no = $2,
           callback_status = 'not_needed',
           updated_at = CURRENT_TIMESTAMP
           WHERE order_no = $3 AND status IN ('pending', 'failed', 'expired')""",
        now, channel_result.get("channel_order_no", ""), order_no,
    )
    if "UPDATE 1" not in result:
        raise ValueError("订单状态已被并发变更，充值结算回滚")
    payment_orders_total.labels(
        order_type='recharge', status='success',
        payment_method=payment_method).inc()
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
    if attempt_id:
        await mark_attempt_settled(conn, attempt_id)
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
    # 1. 抢分布式锁（等待期间不占用数据库连接）——只是"同一订单并发快速失败"的用户侧
    #    去重，**不是**资金防线：锁 TTL 30s 且无续期，慢渠道下第二个请求照样能进来
    #    再调一次渠道。防双花靠第 4 步事务内的 FOR UPDATE + 状态复验（2026-09-19 审查 F3）
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

        # 5. 渠道调用前先持久化 attempt；渠道结果先落库，本地账务随后同事务结算。
        attempt = await begin_channel_attempt(
            order_no, user_id, "recharge", order["amount"],
            order["payment_method"],
        )
        if attempt["status"] == "channel_succeeded":
            channel_result = attempt["channel_result"]
        else:
            if not attempt.get("is_new"):
                # 上次渠道调用结果未落库时不能盲目重调，避免真实渠道重复扣款。
                raise ValueError("支付尝试仍在处理中，请稍后查询订单状态")
            channel_result = await _invoke_channel(order)
            await record_channel_result(attempt["attempt_id"], channel_result)

        # 6. 事务内结算（FOR UPDATE 锁行 → 状态/过期最终校验 → 更新订单 + 钱包 + 流水，原子 P0 #1）
        async with pool.acquire(timeout=5) as conn:
            now, after_balance = await _settle_recharge(
                conn, order_no, user_id, channel_result, attempt["attempt_id"],
            )

        # 7. 返回结果（A20 埋点）
        return _recharge_response(channel_result, order_no, now, after_balance)
    finally:
        # 2026-09-22 审阅 P2（与扣费/退款路径同一失效模式）：钱已在事务里入账后，
        # finally 里裸调 Redis 释放锁一旦抖动就会把成功充值谎报成 500。
        # 锁有 TTL 30s 会自动过期，泄漏只延后同单重试，故只记日志不再上抛。
        try:
            await _release_lock(lock_key, lock_token)
        except Exception as release_err:
            logger.exception(f"充值锁释放失败（锁将随 TTL 过期，充值结果不受影响）: {release_err}")


# ============================================================
# 退款流程已拆至 refund.py（2026-09-05，行为等价移动）；
# 支付 schema DDL 由 Alembic 迁移管理（A19/A23）。
# recover_stale_processing 已删除（2026-09-07 审查 P2）：全仓无任何代码把订单写成
# processing（该词只用于 Redis 幂等占位），其 docstring 描述的恢复场景不存在。
# ============================================================
