"""支付账本/锁/余额原语层（低层，无业务流语义）

2026-08-31 从 app/payment/service.py 纯移动拆出（行为等价）：
订单号生成、Redis 分布式锁（带持有者校验）、钱包读写、乐观锁余额变更、交易流水 WAL。
service.py 负责业务流编排，本模块提供原子原语；经 re-export 保持对外公开符号不变。
"""
import uuid
import random
import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.config import DEFAULT_WALLET_BALANCE
from ..core.logging import setup_logging

logger = setup_logging()

# Redis 分布式锁前缀
_LOCK_PREFIX = "payment:lock:"


# 时间列已迁移为 timestamptz（o6e7f8a9b0c1），统一使用 aware UTC，避免
# aware/naive 比较抛 TypeError，并让 asyncpg 明确按 UTC 写入数据库。
def _utcnow() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    """返回 UTC ISO 时间字符串，并统一以 Z 后缀供前端解析。"""
    if dt is None:
        return None
    # 兼容迁移尚未应用时数据库返回的 naive datetime；naive 值按既有语义视为 UTC。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# ============================================================
# 订单号生成
# ============================================================
async def _generate_order_no() -> str:
    """
    生成订单号: PA + YYYYMMDD + 10位序列
    按日期分 Key 自增：同日序列唯一，跨日不重复，取模碰撞已消除（P0 #3）；
    日期 Key 带 7 天 TTL，防无限增长（P2 #38）。
    INCR 与 EXPIRE 原子化（2026-09-05）：两步写法在"创建后、EXPIRE 前"崩溃会留下
    无 TTL 的日期键，永不过期、逐年累积。
    """
    r = await get_redis()
    now = _utcnow()
    date_part = now.strftime("%Y%m%d")
    seq_key = f"payment:order_seq:{date_part}"
    seq = await r.eval(_ORDER_SEQ_LUA, 1, seq_key, 7 * 86400)
    seq_str = str(seq).zfill(10)
    return f"PA{date_part}{seq_str}"


# INCR + 首次 EXPIRE 原子脚本：seq==1 说明是本日首个请求，由本请求负责设置 TTL
_ORDER_SEQ_LUA = """
local seq = redis.call('INCR', KEYS[1])
if seq == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return seq
"""


# ============================================================
# 分布式锁（带持有者校验，防误删他人锁）
# ============================================================
async def _acquire_lock(key: str, ttl: int = 10) -> Optional[str]:
    """获取分布式锁，成功返回持有者令牌；失败返回 None

    令牌用于释放时校验持有者：防止"锁过期后他人持有，原持有者
    晚归释放误删他人锁"的问题。
    """
    r = await get_redis()
    token = uuid.uuid4().hex
    ok = await r.set(f"{_LOCK_PREFIX}{key}", token, nx=True, ex=ttl)
    return token if ok is True else None


# Lua：仅当值匹配时才删除（原子操作，防误删）
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def _release_lock(key: str, token: str):
    """释放分布式锁（仅持有者能释放）"""
    if not token:
        return
    r = await get_redis()
    await r.eval(_RELEASE_LUA, 1, f"{_LOCK_PREFIX}{key}", token)


# ============================================================
# 钱包管理
# ============================================================
async def ensure_wallet_exists(user_id: str) -> dict:
    """确保用户钱包存在，不存在则创建"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_wallets WHERE user_id = $1", user_id
        )
        if row:
            return dict(row)
        # 创建新钱包
        await conn.execute(
            "INSERT INTO user_wallets (user_id, balance, status) VALUES ($1, $2, 'active')",
            user_id, DEFAULT_WALLET_BALANCE,
        )
        logger.info(f"创建新钱包: user_id={user_id}, balance={DEFAULT_WALLET_BALANCE}")
        return {
            "user_id": user_id,
            "balance": DEFAULT_WALLET_BALANCE,
            "frozen_amount": 0,
            "total_recharged": 0,
            "total_spent": 0,
            "total_refunded": 0,
            "status": "active",
            "version": 0,
        }


async def get_wallet(user_id: str) -> dict:
    """获取钱包信息（只读，不自动创建；P1 #16 防 GET 请求产生写操作/建空钱包）"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT user_id, balance, frozen_amount, total_recharged, total_spent, "
            "total_refunded, status FROM user_wallets WHERE user_id = $1",
            user_id,
        )
    if not row:
        return {
            "user_id": user_id, "balance": 0.0, "frozen_amount": 0.0,
            "total_recharged": 0.0, "total_spent": 0.0, "total_refunded": 0.0,
            "status": "active",
        }
    return {
        "user_id": row["user_id"],
        "balance": float(row["balance"]),
        "frozen_amount": float(row["frozen_amount"]),
        "total_recharged": float(row["total_recharged"]),
        "total_spent": float(row["total_spent"]),
        "total_refunded": float(row["total_refunded"]),
        "status": row["status"],
    }


async def _update_wallet_balance(
    user_id: str,
    delta: Decimal,
    version: int,
    conn=None,
    tx_type: str = "recharge",
) -> bool:
    """原子更新钱包余额（乐观锁）；返回 True 成功 / False 版本冲突"""
    if conn is None:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as c:
            return await _do_update_balance(c, user_id, delta, version, tx_type)
    else:
        return await _do_update_balance(conn, user_id, delta, version, tx_type)


async def _do_update_balance(conn, user_id: str, delta: Decimal, version: int,
                             tx_type: str = "recharge") -> bool:
    """原子更新钱包余额（乐观锁）；tx_type 决定累计字段（P0 #15 修复退款错误累加充值额）"""
    if tx_type == "refund":
        # 退款：余额增加，累计 total_refunded（不改 total_recharged / total_spent）
        recharged_expr, spent_expr, refunded_expr = "total_recharged", "total_spent", "total_refunded + $1"
    elif tx_type == "consume":
        # 消费：余额减少，累计 total_spent
        recharged_expr, spent_expr, refunded_expr = "total_recharged", "total_spent + ABS($1)", "total_refunded"
    else:
        # 充值（默认）：余额增加，累计 total_recharged
        recharged_expr, spent_expr, refunded_expr = "total_recharged + $1", "total_spent", "total_refunded"
    result = await conn.execute(
        "UPDATE user_wallets SET balance = balance + $1, "
        f"total_recharged = {recharged_expr}, "
        f"total_spent = {spent_expr}, "
        f"total_refunded = {refunded_expr}, "
        "version = version + 1, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = $2 AND version = $3",
        delta, user_id, version,
    )
    return "UPDATE 1" in result


async def _apply_balance_change(
    conn, user_id: str, delta: Decimal, max_retries: int = 3, tx_type: str = "recharge"
) -> tuple:
    """乐观锁更新余额，版本冲突时重读重试（有上限，带随机退避）

    返回 (是否成功, 变更前余额, 变更后余额, 版本号)
    tx_type: 'recharge' | 'consume' | 'refund'，决定 total_* 累计字段（P0 #15）。
    """
    for attempt in range(max_retries + 1):
        row = await conn.fetchrow(
            "SELECT balance, version FROM user_wallets WHERE user_id = $1",
            user_id,
        )
        if not row:
            # 钱包不存在：并发安全创建后重试（P0 #34：新用户首次充值/扣费不失败）
            await conn.execute(
                "INSERT INTO user_wallets (user_id, balance, status) "
                "VALUES ($1, $2, 'active') ON CONFLICT (user_id) DO NOTHING",
                user_id, DEFAULT_WALLET_BALANCE,
            )
            continue
        before = row["balance"]
        ver = row["version"]
        updated = await _do_update_balance(conn, user_id, delta, ver, tx_type)
        if updated:
            return True, before, before + delta, ver
        # 版本冲突：随机退避后重读重试（P2 #30 防惊群）
        await asyncio.sleep(random.uniform(0.01, 0.05) * (2 ** attempt))
    return False, None, None, None


async def _locked_wallet_change(
    conn, user_id: str, delta: Decimal, tx_type: str = "recharge"
) -> tuple:
    """行锁方式变更余额（确定性，无乐观锁重试）：FOR UPDATE 锁行（缺失则原子创建）后直接更新。

    适用已开启事务的关键结算路径（充值/退款，2026-09-05 新增）：乐观锁重试在事务内
    耗尽会抛异常整体回滚，而渠道等外部动作可能已生效——行锁在手时更新必然成功，
    从根上消除「外部已扣款、本地回滚」的缺口。并发写者会在行锁上排队而非版本冲突。
    返回 (是否成功, before, after, version)，与 _apply_balance_change 同构。
    """
    row = await conn.fetchrow(
        "SELECT balance, version FROM user_wallets WHERE user_id = $1 FOR UPDATE",
        user_id,
    )
    if not row:
        # 钱包不存在：并发安全创建后重读（P0 #34 同款语义）
        await conn.execute(
            "INSERT INTO user_wallets (user_id, balance, status) "
            "VALUES ($1, $2, 'active') ON CONFLICT (user_id) DO NOTHING",
            user_id, DEFAULT_WALLET_BALANCE,
        )
        row = await conn.fetchrow(
            "SELECT balance, version FROM user_wallets WHERE user_id = $1 FOR UPDATE",
            user_id,
        )
    updated = await _do_update_balance(conn, user_id, delta, row["version"], tx_type)
    if not updated:
        # 行锁在手理论上不可能冲突；防御保留同构返回
        return False, None, None, None
    return True, row["balance"], row["balance"] + delta, row["version"]


# ============================================================
# 交易流水
# ============================================================
# _record_transaction 已删除（2026-09-07 审查 P2）：函数体引用未定义的 order_no
#（NameError 地雷）、全仓无调用方、却经 service re-export 为公开符号。

async def _do_record_tx(conn, order_no, user_id, tx_type, amount, before_balance, after_balance, remark, operator):
    await conn.execute(
        "INSERT INTO transaction_logs "
        "(order_no, user_id, tx_type, amount, before_balance, after_balance, remark, operator) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        order_no, user_id, tx_type, amount, before_balance, after_balance, remark, operator,
    )
