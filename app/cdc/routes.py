"""CDC 消费/查看 API

- GET /api/cdc/events?after=<id>&limit=N  按序号拉取事件（tail，断点续传）
- GET /api/cdc/journal                    查看落盘日志文件 + checkpoint
- GET /api/cdc/status                     查看 worker 处理进度
"""
from fastapi import APIRouter, Depends, Query, HTTPException

from ..core.db import get_pool
from ..core.redis import get_redis
from ..middleware.auth import get_current_user

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
        "table": r["table_name"],
        "op": r["op_type"],
        "pk": r["pk_value"],
        "before": r["row_before"],
        "after": r["row_after"],
        "ts": (r["created_at"].isoformat() + "Z") if r["created_at"] else None,  # P2 #15：UTC 标记
    }


@router.get("/events")
async def list_events(
    after: int = Query(0, ge=0, description="只返回 id 大于此值的事件"),
    limit: int = Query(100, ge=1, le=1000),
    user=Depends(get_current_user),
):
    """按序号拉取 CDC 事件（tail 消费，支持断点续传；仅 admin，P1 #11）"""
    _require_admin(user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, table_name, op_type, pk_value, row_before, row_after, created_at "
            "FROM cdc_events WHERE id > $1 ORDER BY id LIMIT $2",
            after, limit,
        )
    return {"total": len(rows), "events": [_fmt(r) for r in rows]}


@router.get("/journal")
async def journal_info(user=Depends(get_current_user)):
    """落盘日志文件与 checkpoint 信息（仅 admin，P1 #11）"""
    _require_admin(user)
    j = _get_journal()
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
    redis = await get_redis()
    leader = bool(await redis.exists("cdc:leader"))
    return {
        "leader_lock_held": leader,
        "last_processed_id": j.last_id,
        "journal_dir": j.journal_dir,
    }
