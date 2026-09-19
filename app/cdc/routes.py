"""CDC 消费/查看 API

- GET /api/cdc/events?after_txid=&after_id=&limit=N  按 (txid,id) 游标拉取（推荐，
  与 worker 同款可见性条件，不漏晚提交的小 id 事件）
- GET /api/cdc/events?after=<id>&limit=N             旧 id 游标（兼容保留：消费顺序
  是 (txid,id) 而非 id，停拉超过 CLEANUP 窗口可能漏事件，仅供旧消费者过渡）
- GET /api/cdc/journal                    查看落盘日志文件 + checkpoint
- GET /api/cdc/status                     查看 worker 处理进度
"""
from fastapi import APIRouter, Depends, Query, HTTPException

from ..core.db import get_pool
from ..core.redis import get_redis
from ..middleware.auth import get_current_user
from .journal import parse_jsonb, format_ts

router = APIRouter(prefix="/api/cdc", tags=["cdc"])

# 共享 CdcJournal 实例（P1 #12：避免 API 每次新建实例并发读写 checkpoint）
_journal_instance = None


def _get_journal():
    global _journal_instance
    if _journal_instance is None:
        from .journal import CdcJournal
        import os
        _journal_instance = CdcJournal(os.getenv("CDC_JOURNAL_DIR", "cdc_journal"))
    return _journal_instance


def _require_admin(user: dict):
    """CDC 接口含 row_before/row_after 敏感数据，仅允许 admin（P1 #11）"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")


def _fmt(r):
    """asyncpg Record -> 可 JSON 序列化 dict"""
    return {
        "id": r["id"],
        "txid": r["txid"],
        "table": r["table_name"],
        "op": r["op_type"],
        "pk": r["pk_value"],
        "before": parse_jsonb(r["row_before"]),
        "after": parse_jsonb(r["row_after"]),
        "ts": format_ts(r["created_at"]),
    }


@router.get("/events")
async def list_events(
    after: int = Query(0, ge=0, description="[旧] 只返回 id 大于此值的事件（id 序，兼容保留）"),
    after_txid: int | None = Query(None, ge=0, description="[新] (txid,id) 复合游标的 txid 位"),
    after_id: int = Query(0, ge=0, description="[新] (txid,id) 复合游标的 id 位"),
    limit: int = Query(100, ge=1, le=1000),
    user=Depends(get_current_user),
):
    """拉取 CDC 事件（仅 admin，P1 #11）。

    2026-09-14 修复（cdc 日志 09-11 P1）：旧实现 `id > after` 游标按 id 序消费，
    而真实提交顺序是 (txid,id)——晚事务提交的小 id 事件会落在游标之后被外部
    消费者永久漏掉。新游标 (after_txid, after_id) 与 worker 同款：复合比较 +
    `txid < 当前快照 xmin` 可见性条件（未决事务不返回，提交后按序补上）。
    """
    _require_admin(user)
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        if after_txid is not None:
            # 新语义：(txid,id) 复合游标 + 可见性条件，与 app/cdc/worker.py 同款
            rows = await conn.fetch(
                "SELECT id, txid, table_name, op_type, pk_value, row_before, row_after, created_at "
                "FROM cdc_events WHERE (txid, id) > ($1, $2) "
                "AND (txid = 0 OR txid < "
                "txid_snapshot_xmin(txid_current_snapshot())::bigint) "
                "ORDER BY txid, id LIMIT $3",
                after_txid, after_id, limit,
            )
        else:
            # 旧语义：id 序 tail（文档已标明停拉超过清理窗口可能漏事件）
            rows = await conn.fetch(
                "SELECT id, txid, table_name, op_type, pk_value, row_before, row_after, created_at "
                "FROM cdc_events WHERE id > $1 ORDER BY id LIMIT $2",
                after, limit,
            )
    events = [_fmt(r) for r in rows]
    # 新游标消费者可直接续传：最后一条的 (txid,id)
    cursor = {"after_txid": after_txid or 0, "after_id": after_id}
    if events:
        cursor = {"after_txid": events[-1]["txid"], "after_id": events[-1]["id"]}
    return {"total": len(events), "cursor": cursor, "events": events}


@router.get("/journal")
async def journal_info(user=Depends(get_current_user)):
    """落盘日志文件与 checkpoint 信息（仅 admin，P1 #11）"""
    _require_admin(user)
    j = _get_journal()
    j.refresh_checkpoint()  # 重读 worker 最新 checkpoint，避免首次创建后 last_id 冻结（P1）
    return {
        "dir": j.journal_dir,
        "files": j.list_files(),
        "checkpoint": {"last_id": j.last_id, "path": j.checkpoint_path},
    }


@router.get("/status")
async def cdc_status(user=Depends(get_current_user)):
    """worker 处理进度（last_id 来自 checkpoint；仅 admin，P1 #11）"""
    _require_admin(user)
    j = _get_journal()
    j.refresh_checkpoint()  # 重读 worker 最新 checkpoint，避免 last_id 冻结（P1）
    redis = await get_redis()
    leader = bool(await redis.exists("cdc:leader"))
    return {
        "leader_lock_held": leader,
        "last_processed_id": j.last_id,
        "journal_dir": j.journal_dir,
    }
