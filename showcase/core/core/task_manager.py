"""
任务管理器 — 为多 Agent 提供"创建→轮询→取消→恢复"的生命周期管理。

状态机: pending → generating → completed
                                ↘ cancelled
                                ↘ error

Redis 存储:
  task:{task_id}  (Hash)
    - status: pending|generating|completed|cancelled|error
    - result: 最终/部分结果文本
    - session_id: 会话ID
    - user_message: 用户原始消息
    - user_location: 用户定位（可选）
    - created_at: ISO时间
    - updated_at: ISO时间

取消信号:
  全局 _cancel_events[task_id] = asyncio.Event()
"""

import json
import hashlib
import asyncio
import uuid
import time
from datetime import datetime, timezone
from typing import Optional, Dict
from ..core.redis import get_redis as _get_redis

# ---- 全局取消信号表 ----
_cancel_events: Dict[str, asyncio.Event] = {}

TASK_TTL = 600  # 10 分钟过期

# ============================================================
# Redis 读写
# ============================================================

async def _redis():
    return await _get_redis()


async def create_task(session_id: str, user_message: str, user_location: str = "") -> str:
    """创建任务，返回 task_id"""
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    r = await _redis()
    await r.hset(f"task:{task_id}", mapping={
        "status": "pending",
        "result": "",
        "session_id": session_id,
        "user_message": user_message,
        "user_location": user_location,
        "created_at": now,
        "updated_at": now,
    })
    await r.expire(f"task:{task_id}", TASK_TTL)
    return task_id


async def get_task(task_id: str) -> Optional[dict]:
    """获取任务状态"""
    r = await _redis()
    data = await r.hgetall(f"task:{task_id}")
    if not data:
        return None
    # bytes → str
    return {k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in data.items()}


async def update_status(task_id: str, status: str, result: str = ""):
    """更新任务状态和结果"""
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    mapping = {"status": status, "updated_at": now}
    if result:
        mapping["result"] = result
    await r.hset(f"task:{task_id}", mapping=mapping)
    await r.expire(f"task:{task_id}", TASK_TTL)


async def append_result(task_id: str, chunk: str):
    """追加结果文本"""
    r = await _redis()
    await r.hincrbyfloat(f"task:{task_id}", "_result_len", len(chunk))
    # 用 append 到独立 key 避免 hset 覆盖
    await r.append(f"task:{task_id}:result_buf", chunk)
    await r.expire(f"task:{task_id}:result_buf", TASK_TTL)


async def read_accumulated_result(task_id: str) -> str:
    """读取累积的结果文本"""
    r = await _redis()
    raw = await r.get(f"task:{task_id}:result_buf")
    return raw if raw else ""


# ============================================================
# 取消信号
# ============================================================

def get_cancel_event(task_id: str) -> asyncio.Event:
    """获取/创建取消事件"""
    if task_id not in _cancel_events:
        _cancel_events[task_id] = asyncio.Event()
    return _cancel_events[task_id]


def is_cancelled(task_id: str) -> bool:
    """检查是否已取消"""
    ev = _cancel_events.get(task_id)
    return ev is not None and ev.is_set()


def set_cancelled(task_id: str):
    """标记取消"""
    ev = get_cancel_event(task_id)
    ev.set()


def cleanup_event(task_id: str):
    """清理取消事件"""
    _cancel_events.pop(task_id, None)


# ============================================================
# 幂等保护: 同一 session + 相同 message 10秒内复用
# ============================================================

_idempotent_map: Dict[str, str] = {}  # key=f"{session_id}:{msg_hash}" → task_id


def _idempotent_key(session_id: str, msg: str) -> str:
    return f"{session_id}:{hashlib.sha256(msg.encode()).hexdigest()[:16]}"


async def try_idempotent(session_id: str, msg: str) -> Optional[str]:
    """幂等检查：返回已有 task_id 或 None"""
    key = _idempotent_key(session_id, msg)
    task_id = _idempotent_map.get(key)
    if task_id:
        task = await get_task(task_id)
        if task and task["status"] in ("pending", "generating"):
            return task_id
    return None


def save_idempotent(session_id: str, msg: str, task_id: str):
    """记录幂等映射（10秒后自动清除）"""
    key = _idempotent_key(session_id, msg)
    _idempotent_map[key] = task_id
    # 10秒后清理
    def _clean():
        if _idempotent_map.get(key) == task_id:
            _idempotent_map.pop(key, None)
    asyncio.get_running_loop().call_later(10, _clean)
