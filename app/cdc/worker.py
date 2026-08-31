"""CDC Worker：单写者（Redis 锁）轮询 cdc_events -> JSONL 落盘 + 断点续传

多实例部署时只有一个实例成为 leader（Redis NX 锁 + TTL 自动续期），
leader 掉线后锁过期，其它实例自动接管，保证同一时刻只有一个写者。
"""
import asyncio
import os
import time
import uuid
import signal
from datetime import datetime, timezone
from typing import Optional

from ..core.logging import setup_logging
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.metrics import (
    cdc_events_processed_total,
    cdc_errors_total,
    cdc_lag_seconds,
)
from .schema import ensure_schema
from .journal import CdcJournal

logger = setup_logging()

POLL_INTERVAL = float(os.getenv("CDC_POLL_INTERVAL", "1"))   # 轮询间隔（秒）
BATCH_SIZE = int(os.getenv("CDC_BATCH_SIZE", "500"))          # 每批拉取条数
LOCK_KEY = "cdc:leader"
LOCK_TTL = 30                                                 # leader 锁 TTL（秒）
CHECKPOINT_EVERY = 10                                         # checkpoint 周期（秒）


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

    async def _try_lock(self, redis) -> Optional[str]:
        """NX + EX 获取 leader 锁，返回 token；失败返回 None（P1 #5 校验持有者）"""
        token = uuid.uuid4().hex
        ok = await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL)
        return token if ok is True else None

    async def _renew_lock(self, redis, token: str):
        """续期锁（Lua 校验 token：仅持有者能续，防误续他人锁，P1 #5）"""
        lua = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        await redis.eval(lua, 1, LOCK_KEY, token, LOCK_TTL)

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
        # 先全部落盘，全部成功后再一次性推进 last_id（P0 #1：防批量中断导致后半批事件丢失）
        for r in rows:
            event = {
                "id": r["id"],
                "table": r["table_name"],
                "op": r["op_type"],
                "pk": r["pk_value"],
                "before": r["row_before"],
                "after": r["row_after"],
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
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
            except NotImplementedError:
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
                    lag = (datetime.now(timezone.utc) - row["max_ts"].replace(tzinfo=timezone.utc)).total_seconds()
                    cdc_lag_seconds.set(max(lag, 0))
                else:
                    cdc_lag_seconds.set(0)  # 无待处理事件 → 滞后 0，防虚假告警
        except Exception:
            pass

    async def run(self):
        """主循环：只在持有 leader 锁时消费事件（P0 #2/#20：schema 失败不进循环；finally 落盘关闭）"""
        self.running = True
        self._register_signal_handlers()
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await ensure_schema(conn)
            logger.info("CDC 事件表与触发器已确保")
        except Exception as e:
            # schema 初始化失败：不进入主消费循环（P1 #13/#20，防 pool 未定义 NameError / 触发器缺失消费）
            logger.warning(f"CDC schema 初始化失败（本轮跳过消费）: {e}")
            await asyncio.sleep(POLL_INTERVAL)
            self.running = False
            return
        try:
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
                        await self._renew_lock(redis, self._lock_token)
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
                except Exception as e:
                    cdc_errors_total.inc()
                    logger.warning(f"CDC worker 循环异常: {e}")
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            # 退出前落盘 + 关闭句柄（P1 #6：防句柄泄漏）
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
            logger.warning(f"⚠️ CDC checkpoint 保存失败: {e}")
        self.journal.close()
