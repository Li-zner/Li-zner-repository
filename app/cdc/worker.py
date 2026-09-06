"""CDC Worker：单写者（Redis 锁）轮询 cdc_events -> JSONL 落盘 + 断点续传

多实例部署时只有一个实例成为 leader（Redis NX 锁 + TTL 自动续期），
leader 掉线后锁过期，其它实例自动接管，保证同一时刻只有一个写者。
"""
import asyncio
import os
import re
import time
import uuid
import signal
from datetime import datetime, timezone
from typing import Optional

from ..core.logging import setup_logging
from ..core.redis import get_redis
from ..core.db import init_pool
from ..core.metrics import (
    cdc_events_processed_total,
    cdc_errors_total,
    cdc_lag_seconds,
)
from .schema import ensure_schema
from .journal import CdcJournal, parse_jsonb, format_ts

logger = setup_logging()

POLL_INTERVAL = float(os.getenv("CDC_POLL_INTERVAL", "1"))   # 轮询间隔（秒）
BATCH_SIZE = int(os.getenv("CDC_BATCH_SIZE", "500"))          # 每批拉取条数
LOCK_KEY = "cdc:leader"
LOCK_TTL = 30                                                 # leader 锁 TTL（秒）
CHECKPOINT_EVERY = 10                                         # checkpoint 周期（秒）
SCHEMA_RETRY_INTERVAL = float(os.getenv("CDC_SCHEMA_RETRY_INTERVAL", "5"))  # schema 校验失败重试间隔（P1 #10）
CLEANUP_EVERY = int(os.getenv("CDC_CLEANUP_EVERY", "60"))     # 事件清理周期（秒；#15 控制表体积）
CLEANUP_BATCH_SIZE = int(os.getenv("CDC_CLEANUP_BATCH_SIZE", "5000"))   # 每批清理条数
CLEANUP_MAX_BATCHES = int(os.getenv("CDC_CLEANUP_MAX_BATCHES", "10"))    # 单轮最多清理批次数
PARTITION_CHECK_EVERY = int(os.getenv("CDC_PARTITION_CHECK_EVERY", "21600"))  # 分区维护周期（秒，默认6小时）
PARTITION_RETENTION_MONTHS = int(os.getenv("CDC_RETENTION_MONTHS", "3"))      # 分区保留月数（过期分区 DROP 阈值）
PARTITION_AHEAD_MONTHS = int(os.getenv("CDC_PARTITION_AHEAD", "2"))           # 预建未来分区月数

_PARTITION_NAME_RE = re.compile(r"^cdc_events_(\d{4})_(\d{2})$")  # 月分区命名白名单（防拼接注入）


def _month_start(dt: datetime) -> datetime:
    """取所在月第一天 0 点"""
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(month_start: datetime, months: int) -> datetime:
    """月初平移 N 个月（支持负数、跨年）"""
    total = month_start.year * 12 + (month_start.month - 1) + months
    return month_start.replace(year=total // 12, month=total % 12 + 1)


def _partition_name(month_start: datetime) -> str:
    """月分区命名：cdc_events_YYYY_MM"""
    return f"cdc_events_{month_start.year}_{month_start.month:02d}"


def _ensure_partition_statements(now: datetime) -> list:
    """生成补建分区 DDL：[当前-保留月 .. 当前+预建月] 每月一个（IF NOT EXISTS 幂等）+ DEFAULT 兜底。

    DEFAULT 兜底时钟漂移/漏建分区时的插入，避免直接报错阻塞支付主表的写入。
    """
    base = _month_start(now)
    stmts = []
    for offset in range(-PARTITION_RETENTION_MONTHS, PARTITION_AHEAD_MONTHS + 1):
        m = _shift_month(base, offset)
        nxt = _shift_month(m, 1)
        stmts.append(
            f"CREATE TABLE IF NOT EXISTS {_partition_name(m)} PARTITION OF cdc_events "
            f"FOR VALUES FROM ('{m.isoformat()}') TO ('{nxt.isoformat()}')"
        )
    stmts.append("CREATE TABLE IF NOT EXISTS cdc_events_default PARTITION OF cdc_events DEFAULT")
    return stmts


def _is_stale_month_partition(name: str, now: datetime) -> bool:
    """是否为超出保留期的月分区（整月都早于 当前月-保留月 才算过期；DEFAULT 分区永不命中）"""
    m = _PARTITION_NAME_RE.match(name)
    if not m:
        return False
    stale_start = _shift_month(_month_start(now), -PARTITION_RETENTION_MONTHS)
    return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc) < stale_start


class CdcWorker:
    """CDC 后台任务：leader 选举 + 事件消费 + 落盘"""

    def __init__(self):
        self.journal = CdcJournal(os.getenv("CDC_JOURNAL_DIR", "cdc_journal"))
        self.last_id = self.journal.last_id
        self.running = False
        self._lock_ok = False
        self._lock_token: Optional[str] = None
        self._last_renew = time.time()   # P2 #17：首轮不立即续期
        self._last_checkpoint = 0.0
        self._last_cleanup = 0.0         # 事件清理计时（#15）
        self._last_partition_check = 0.0  # 分区维护计时（安全网删区）

    async def _try_lock(self, redis) -> Optional[str]:
        """NX + EX 获取 leader 锁，返回 token；失败返回 None（P1 #5 校验持有者）"""
        token = uuid.uuid4().hex
        ok = await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL)
        return token if ok is True else None

    async def _renew_lock(self, redis, token: str) -> bool:
        """续期锁（Lua 校验 token：仅持有者能续，防误续他人锁，P1 #5）；返回是否仍持有（P1 #43）"""
        lua = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        ok = await redis.eval(lua, 1, LOCK_KEY, token, LOCK_TTL)
        return ok == 1

    async def _poll_once(self, pool) -> int:
        """拉取一批新事件并落盘，返回处理条数"""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, table_name, op_type, pk_value, row_before, row_after, created_at "
                "FROM cdc_events WHERE id > $1 ORDER BY id LIMIT $2",
                self.last_id, BATCH_SIZE,
            )
        if not rows:
            return 0
        # At-least-once（#13）：先全部落盘，全部成功后再一次性推进 last_id（P0 #1）。
        # 若中途失败，last_id 停在上一批，重放已写部分会重复，但绝不丢事件；消费端按事件 id 去重。
        # 因此删除已处理事件（cleanup）只删 id <= last_id，安全。
        for r in rows:
            event = {
                "id": r["id"],
                "table": r["table_name"],
                "op": r["op_type"],
                "pk": r["pk_value"],
                "before": parse_jsonb(r["row_before"]),
                "after": parse_jsonb(r["row_after"]),
                "ts": format_ts(r["created_at"]),
            }
            await self.journal.append(event, r["id"])
        self.last_id = rows[-1]["id"]
        cdc_events_processed_total.inc(len(rows))
        return len(rows)

    def _register_signal_handlers(self):
        """注册 SIGTERM/SIGINT：优雅退出确保 checkpoint 落盘（P1 #4）"""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, ValueError):  # noqa: silent-except 豁免：Windows/受限平台无信号处理器（平台惯例）
                pass

    def _request_stop(self):
        """信号回调：标记停止（同步，由 run() 的 finally 落盘）"""
        self.running = False

    async def _update_lag(self, pool):
        """更新滞后指标：仅当存在待处理事件时计算（P1 #10 防无事件时虚假告警）"""
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT MAX(created_at) AS max_ts, COUNT(*) AS pending "
                    "FROM cdc_events WHERE id > $1", self.last_id)
            if row:
                pending = row["pending"] or 0
                if pending > 0 and row["max_ts"]:
                    # TIMESTAMPTZ 返回 aware datetime；naive 视为 UTC（#8 后该列已带时区）
                    max_ts = row["max_ts"]
                    if max_ts.tzinfo is None:
                        max_ts = max_ts.replace(tzinfo=timezone.utc)
                    lag = (datetime.now(timezone.utc) - max_ts).total_seconds()
                    cdc_lag_seconds.set(max(lag, 0))
                else:
                    cdc_lag_seconds.set(0)  # 无待处理事件 → 滞后 0，防虚假告警
        except Exception as e:
            logger.debug(f"CDC 滞后指标更新失败（下轮再试）: {e}")

    async def _cleanup_events(self, pool) -> None:
        """周期删除已处理事件（id <= 处理水位 self.last_id），分批删，控制 cdc_events 体积（#15）。

        仅 leader（run 循环内）调用；id > self.last_id 的未处理/近段事件保留，保证消费与
        断点续传不受影响。删除不触发 CDC 触发器（触发器挂在支付表上，不挂在 cdc_events）。
        """
        if self.last_id <= 0:  # 尚未处理 → 无过期事件
            return
        try:
            async with pool.acquire() as conn:
                for _ in range(CLEANUP_MAX_BATCHES):
                    n = await conn.fetchval(
                        "WITH doomed AS ("
                        " SELECT id FROM cdc_events WHERE id <= $1 ORDER BY id LIMIT $2"
                        "), deleted AS ("
                        " DELETE FROM cdc_events USING doomed "
                        " WHERE cdc_events.id = doomed.id RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        self.last_id, CLEANUP_BATCH_SIZE,
                    )
                    if not n:
                        break
        except Exception as e:
            logger.warning(f"事件清理失败（下轮重试）: {e}")

    async def _maintain_partitions(self, pool) -> None:
        """分区维护（安全网，与 #15 互补）：补建未来月分区 + DROP 过期且已处理完的月分区。

        删除不变量与 #15 一致——只删已归档数据：月分区仅当 MAX(id) <= 处理水位
        （本区事件已全部落盘 JSONL）才允许 DROP；worker 停摆/积压时一个分区都不删，
        绝不丢未处理审计事件。DEFAULT 分区只告警不删（正常应为空，有行说明存在
        未覆盖时间段的漏建分区，需人工介入）。
        """
        try:
            async with pool.acquire() as conn:
                # 1) 补建缺失分区（IF NOT EXISTS 幂等；父表未分区时这里报错并整体跳过）
                for stmt in _ensure_partition_statements(datetime.now(timezone.utc)):
                    await conn.execute(stmt)

                # 2) 巡检现有分区（名字来自系统目录，白名单正则校验后才允许拼接进 SQL）
                rows = await conn.fetch(
                    "SELECT c.relname AS name FROM pg_class c "
                    "JOIN pg_inherits i ON i.inhrelid = c.oid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'cdc_events'"
                )
                now = datetime.now(timezone.utc)
                for r in rows:
                    name = r["name"]
                    if name == "cdc_events_default":
                        n = await conn.fetchval("SELECT count(*) FROM cdc_events_default")
                        if n:
                            logger.warning(f"DEFAULT 分区出现 {n} 行（本应落在月分区），请检查分区覆盖范围")
                        continue
                    if not _is_stale_month_partition(name, now):
                        continue
                    max_id = await conn.fetchval(f"SELECT COALESCE(MAX(id), 0) FROM {name}")
                    if max_id > self.last_id:
                        logger.info(f"分区 {name} 已过期但 max(id)={max_id} > 处理水位 {self.last_id}，跳过")
                        continue
                    await conn.execute(f"DROP TABLE {name}")
                    logger.info(f"已 DROP 过期分区 {name}（max_id={max_id} <= 水位 {self.last_id}）")
        except Exception as e:
            logger.warning(f"分区维护失败（下轮重试）: {e}")

    async def run(self):
        """主循环：先校验 schema（瞬时故障带退避重试，不杀 worker，P1 #10），
        再只在持有 leader 锁时消费事件；finally 落盘关闭。"""
        self.running = True
        self._register_signal_handlers()
        pool = None
        try:
            # schema 校验：失败重试而非永久退出，避免瞬时 DB 故障把 worker 停掉（#10）。
            # init_pool 幂等：已初始化则复用，否则重建；DB 恢复后可重新建池。
            while self.running:
                try:
                    _pool = await init_pool()
                    async with _pool.acquire() as conn:
                        await ensure_schema(conn)
                    pool = _pool          # 仅 schema 校验成功才赋值，供 finally 判断
                    logger.info("CDC 事件表与触发器已确保")
                    break
                except Exception as e:
                    cdc_errors_total.inc()
                    logger.warning(f"CDC schema 校验失败，{SCHEMA_RETRY_INTERVAL}s 后重试: {e}")
                    await asyncio.sleep(SCHEMA_RETRY_INTERVAL)

            while self.running:
                try:
                    redis = await get_redis()
                    if not self._lock_ok:
                        token = await self._try_lock(redis)
                        if token is not None:
                            self._lock_token = token
                            self._lock_ok = True
                            logger.info("CDC worker 成为 leader")
                    if not self._lock_ok:
                        await asyncio.sleep(POLL_INTERVAL)  # 非 leader，稍后重试
                        continue

                    now = time.time()
                    if now - self._last_renew > LOCK_TTL / 2:
                        if not await self._renew_lock(redis, self._lock_token):
                            # 锁已丢失（过期被其它实例接管）：立即停止消费退回竞选，防双写（P1 #43）
                            logger.warning("CDC leader 锁续期失败（已被接管或过期），停止消费并重新竞选")
                            self._lock_ok = False
                            self._lock_token = None
                            continue
                        self._last_renew = now

                    try:
                        await self._poll_once(pool)
                    except Exception as e:
                        cdc_errors_total.inc()
                        logger.warning(f"CDC 轮询失败: {e}")

                    if now - self._last_checkpoint >= CHECKPOINT_EVERY:
                        await self.journal.save_checkpoint(self.last_id)
                        self._last_checkpoint = now
                        await self._update_lag(pool)

                    # 周期清理已处理事件，控制 cdc_events 体积（#15）
                    if now - self._last_cleanup >= CLEANUP_EVERY:
                        await self._cleanup_events(pool)
                        self._last_cleanup = now

                    # 分区维护（安全网）：补建未来分区 + DROP 过期且已处理完的月分区
                    if now - self._last_partition_check >= PARTITION_CHECK_EVERY:
                        await self._maintain_partitions(pool)
                        self._last_partition_check = now
                except Exception as e:
                    cdc_errors_total.inc()
                    logger.warning(f"CDC worker 循环异常: {e}")
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            # 退出前落盘 + 关闭句柄（P1 #6：防句柄泄漏）；schema 未就绪(pool=None)无写入，跳过 checkpoint
            if pool is not None:
                try:
                    await self.journal.save_checkpoint(self.last_id)
                except Exception as e:
                    logger.warning(f"CDC checkpoint 保存失败: {e}")
            self.journal.close()
            logger.info("CDC worker 已停止")

    async def stop(self):
        """优雅停止：落 checkpoint 并关闭文件句柄"""
        self.running = False
        try:
            await self.journal.save_checkpoint(self.last_id)
        except Exception as e:
            logger.warning(f"CDC checkpoint 保存失败: {e}")
        self.journal.close()
