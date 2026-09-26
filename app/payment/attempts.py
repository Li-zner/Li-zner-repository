"""支付渠道尝试状态机。

渠道调用的副作用不可回滚，因此必须在调用前持久化 attempt，在调用后先落渠道结果，
再由同一数据库事务完成本地账务和 attempt 终态。进程崩溃后由恢复循环继续本地结算，
不会再次调用渠道。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import asyncpg

from ..core.db import get_pool
from ..core.logging import setup_logging

logger = setup_logging()

_ACTIVE_STATUSES = ("processing", "channel_succeeded")
_PROCESSING_STALE_SECONDS = 300
_RECOVERY_BATCH_LIMIT = 50


def _decode_json(value) -> dict:
    """把 asyncpg 返回的 JSONB 文本统一解析为字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def begin_channel_attempt(
    order_no: str,
    user_id: str,
    operation: str,
    amount: Decimal,
    channel_code: str,
    idempotency_key: str = "",
    allow_consumption_refund: bool = False,
) -> dict:
    """返回可恢复的渠道 attempt；已有成功 attempt 时直接复用。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        try:
            return await _begin_attempt_locked(conn, order_no, user_id, operation,
                                               amount, channel_code,
                                               idempotency_key, allow_consumption_refund)
        except asyncpg.exceptions.UniqueViolationError as exc:
            # PAY-3（2026-09-20 审查）：上面 existing 查询与 INSERT 之间，并发请求可
            # 抢插同一 (order_no, operation) 的活跃 attempt，撞 uq_payment_attempts_active
            # 唯一索引。UniqueViolationError（IntegrityConstraintViolationError 子类）
            # 不是 ValueError，routes 层 except 漏接
            # 变裸 500——转成与"退款尝试仍在处理中"同一业务口径（400 可安全重试）。
            # 2026-09-22 审阅 P2：**只捕 UniqueViolationError，不捕父类**。
            # 撞 NOT NULL / CHECK / FK 都是代码写错（字段漏传、状态值拼错），不是并发
            # 竞争；捕成父类会给用户回"正在处理中，请稍后重试"——一个永远重试不成功的
            # 假象，还会把真实缺陷从错误日志里抹掉。收窄后编码错误原样上抛为 500。
            logger.warning(
                f"attempt 活跃唯一索引竞态: order={order_no}, op={operation}, "
                f"err_type={type(exc).__name__}")
            raise ValueError("支付尝试正在处理中，请稍后查询订单状态") from exc


async def _begin_attempt_locked(
    conn, order_no: str, user_id: str, operation: str,
    amount: Decimal, channel_code: str, idempotency_key: str,
    allow_consumption_refund: bool,
) -> dict:
    """活跃检查 + 预留插入，必须在同一事务内（调用方已持 conn）。"""
    async with conn.transaction():
        existing = await conn.fetchrow(
            """
            SELECT * FROM payment_attempts
            WHERE order_no = $1 AND operation = $2
              AND status = ANY($3::text[])
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            order_no, operation, list(_ACTIVE_STATUSES),
        )
        if existing:
            result = dict(existing)
            result["channel_result"] = _decode_json(result.get("channel_result"))
            result["is_new"] = False
            return result
        attempt_id = f"att_{uuid.uuid4().hex[:24]}"
        if operation == "refund":
            # F4（2026-09-20 拍板）：渠道实退金额在预留时刻钳制到剩余可退额度。
            # 本事务对原单 FOR UPDATE，且同一原单同时只允许一条活跃退款 attempt
            # （上面的 existing 分支挡住并发新建），"渠道调用与本地结算之间
            # 并发退款已提交"因此不可能发生——超退缺口从源头消灭，而非事后对账。
            amount = await _clamp_refund_quota(conn, order_no, amount)
        row = await conn.fetchrow(
            """
            INSERT INTO payment_attempts
            (attempt_id, order_no, user_id, operation, status, amount,
             channel_code, idempotency_key, allow_consumption_refund)
            VALUES ($1, $2, $3, $4, 'processing', $5, $6, $7, $8)
            RETURNING *
            """,
            attempt_id, order_no, user_id, operation, amount,
            channel_code, idempotency_key, allow_consumption_refund,
        )
        result = dict(row)
        result["channel_result"] = {}
        result["is_new"] = True
        return result


async def _clamp_refund_quota(conn, order_no: str, requested: Decimal) -> Decimal:
    """退款额度预留（须在 begin_channel_attempt 的事务内调用，随 attempt 落库原子生效）。

    可退基数口径与 refund._decide_refund 对称：abs(原单金额) - 已退累计。
    剩余额度不足请求量时按剩余钳制（宁可少退给渠道，绝不超退）；退尽则拒绝。
    下面这条 SUM 在持原单 FOR UPDATE 的事务里执行，扫描时间直接就是行锁持有时间，
    故 metadata->>'original_order_no' 由迁移 z6b7c8d9e0f1 建部分表达式索引覆盖。
    """
    order = await conn.fetchrow(
        "SELECT status, amount FROM payment_orders WHERE order_no = $1 FOR UPDATE",
        order_no,
    )
    if order is None:
        raise ValueError("订单不存在")
    if order["status"] not in ("success", "partial_refunded"):
        raise ValueError("订单状态不允许退款（可能已被并发退款）")
    refunded_row = await conn.fetchrow(
        "SELECT COALESCE(SUM(amount), 0) AS refunded FROM payment_orders "
        "WHERE order_type = 'refund' AND status = 'refunded' "
        "AND metadata->>'original_order_no' = $1",
        order_no,
    )
    remaining = abs(order["amount"]) - refunded_row["refunded"]
    if remaining <= 0:
        raise ValueError("订单已全额退款")
    return min(requested, remaining)


async def record_channel_result(attempt_id: str, result: dict) -> None:
    """在调用渠道后立即持久化结果；失败时保留 processing 供人工排查。"""
    status = "channel_succeeded" if result.get("success") else "channel_failed"
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        updated = await conn.execute(
            """
            UPDATE payment_attempts
            SET status = $1, channel_order_no = $2, channel_result = $3::jsonb,
                error = $4, updated_at = CURRENT_TIMESTAMP
            WHERE attempt_id = $5 AND status = 'processing'
            """,
            status,
            result.get("channel_order_no", ""),
            json.dumps(result, ensure_ascii=False),
            result.get("message", "") if not result.get("success") else "",
            attempt_id,
        )
        if "UPDATE 1" not in updated:
            raise ValueError("支付尝试状态已变化，拒绝覆盖渠道结果")


async def mark_attempt_settled(conn, attempt_id: str) -> None:
    """在本地账务事务内把 attempt 标记为已结算。"""
    result = await conn.execute(
        """
        UPDATE payment_attempts
        SET status = 'settled', updated_at = CURRENT_TIMESTAMP
        WHERE attempt_id = $1 AND status IN ('channel_succeeded', 'processing')
        """,
        attempt_id,
    )
    if "UPDATE 1" not in result:
        raise ValueError("支付尝试状态已变化，拒绝重复结算")


async def mark_attempt_manual_review(attempt_id: str, reason: str) -> None:
    """把无法安全自动恢复的 attempt 转人工处理。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            """
            UPDATE payment_attempts
            SET status = 'manual_review', error = $1, updated_at = CURRENT_TIMESTAMP
            WHERE attempt_id = $2 AND status = 'processing'
            """,
            reason[:500], attempt_id,
        )


async def settle_recoverable_attempts(limit: int = _RECOVERY_BATCH_LIMIT) -> int:
    """恢复渠道已成功但本地账务未完成的 attempt。"""
    pool = await get_pool()
    stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=_PROCESSING_STALE_SECONDS
    )
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            """
            UPDATE payment_attempts
            SET status = 'manual_review',
                error = 'processing timeout before channel result persistence',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing' AND updated_at < $1
            """,
            stale_before,
        )
        rows = await conn.fetch(
            """
            SELECT * FROM payment_attempts
            WHERE status = 'channel_succeeded'
            ORDER BY updated_at
            LIMIT $1
            """,
            limit,
        )
    settled = 0
    for row in rows:
        try:
            if await _settle_attempt(dict(row)):
                settled += 1
        except Exception as exc:
            logger.error(
                f"支付尝试恢复失败: attempt={row['attempt_id']}, "
                f"order={row['order_no']}, err={exc}",
                exc_info=True,
            )
    return settled


async def _settle_attempt(row: dict) -> bool:
    """按 operation 调用本地结算函数，渠道结果不再次请求外部。"""
    operation = row["operation"]
    channel_result = _decode_json(row.get("channel_result"))
    pool = await get_pool()
    if operation == "recharge":
        from .service import _settle_recharge
        async with pool.acquire(timeout=5) as conn:
            now, balance = await _settle_recharge(
                conn, row["order_no"], row["user_id"],
                channel_result, row["attempt_id"],
            )
        return now is not None and balance is not None
    if operation == "refund":
        from .refund import _settle_refund, _refund_result
        async with pool.acquire(timeout=5) as conn:
            refund_order_no, now = await _settle_refund(
                conn, row["order_no"], row["user_id"],
                Decimal(str(row["amount"])), row.get("error") or "恢复退款",
                row["attempt_id"],
                bool(row.get("allow_consumption_refund")),
            )
        idem_key = row.get("idempotency_key") or ""
        if idem_key:
            try:
                from ..core.redis import get_redis
                redis = await get_redis()
                result = _refund_result(
                    row["order_no"], refund_order_no,
                    Decimal(str(row["amount"])), now,
                )
                await redis.set(idem_key, json.dumps(result), ex=86400)
            except Exception as exc:
                logger.warning(f"恢复退款后写幂等结果失败: {exc}")
        return True
    await mark_attempt_manual_review(row["attempt_id"], f"unknown operation: {operation}")
    return False


# 2026-09-22 审阅 P2：此处原有 `payment_attempt_recovery_loop` + 专用 `sleep_seconds`
# 已删除——全仓（app/ scripts/ tests/ .github 工作流）零调用方。恢复由
# `deferred.settlement_loop` 每轮内联调用 `settle_recoverable_attempts` 驱动
# （main.py 只启动 settlement_loop）。留第二条循环入口会误导排障：改 interval 不生效、
# 或以为有两个循环在跑从而重复扫描。
