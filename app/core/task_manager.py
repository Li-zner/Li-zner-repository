"""
任务管理器 — 为多 Agent 提供"创建→轮询→取消→恢复"的生命周期管理。

状态机: pending → generating → completed
                                ↘ cancelled
                                ↘ error

Redis 存储:
  task:{task_id}  (Hash)
    - status: pending|generating|completed|cancelled|error
    - result: 最终/部分结果文本
    - conversation_id: 会话ID（与 models.ChatRequest.conversation_id 对齐）
    - user_message: 用户原始消息
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
from ..core.config import TASK_TTL_SECONDS, TASK_TIMEOUT
from ..core.concurrency import spawn

from ..core.logging import setup_logging

logger = setup_logging()

# ---- 全局取消信号表 ----
_cancel_events: Dict[str, asyncio.Event] = {}

# 任务在 Redis 中的保留期（秒）：调大默认值，防长任务处理中被中途清除（P0 #35）
TASK_TTL = TASK_TTL_SECONDS

# ============================================================
# Redis 读写
# ============================================================

async def _redis():
    return await _get_redis()


async def create_task(conversation_id: str, user_message: str,
                      owner: str = "") -> str:
    """创建任务，返回 task_id

    owner：创建者 username，写入任务 hash 供端点做归属校验（防任何登录用户
    凭 task_id 读取/取消/恢复他人任务——跨用户信息泄露，P1）。
    """
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    r = await _redis()
    await r.hset(f"task:{task_id}", mapping={
        "status": "pending",
        "result": "",
        "conversation_id": conversation_id,
        "user_message": user_message,
        "owner": owner,
        "created_at": now,
        "updated_at": now,
    })
    await r.expire(f"task:{task_id}", TASK_TTL)
    return task_id


async def get_task(task_id: str) -> Optional[dict]:
    """获取任务状态（惰性超时检查：超过 TASK_TIMEOUT 未完成的 task 标记 timeout，P2 C1）"""
    r = await _redis()
    data = await r.hgetall(f"task:{task_id}")
    if not data:
        return None
    result = {k.decode() if isinstance(k, bytes) else k:
              v.decode() if isinstance(v, bytes) else v
              for k, v in data.items()}
    # 惰性超时：pending/generating 超时 → 读侧标记 timeout（不写回，由外部定时任务清理）
    if result.get("status") in ("pending", "generating"):
        try:
            _dt = datetime.fromisoformat(result.get("created_at") or "")
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - _dt).total_seconds() > TASK_TIMEOUT:
                result["status"] = "timeout"
        except Exception as e:
            logger.debug(f"任务超时检查时间解析失败: {e}")
    return result


async def update_status(task_id: str, status: str, result: str = ""):
    """更新任务状态和结果"""
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    # 显式设置 result（None/空串都覆盖旧值，避免残留，P1 #37）
    mapping = {"status": status, "updated_at": now, "result": result if result is not None else ""}
    await r.hset(f"task:{task_id}", mapping=mapping)
    await r.expire(f"task:{task_id}", TASK_TTL)


async def append_result(task_id: str, chunk: str):
    """追加结果文本"""
    r = await _redis()
    # 用 append 到独立 key 避免 hset 覆盖（原 _result_len 计数器零消费方，2026-09-05 审查删除）
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
# 多实例共享：幂等映射存 Redis（10s TTL），避免单实例字典无法跨实例共享（P0 #34）
# ============================================================

# 幂等键前缀（Redis key），TTL 由 setex 控制，天然过期无需手动清理
_IDEMPOTENT_PREFIX = "idempotent:"


def _idempotent_key(session_id: str, msg: str) -> str:
    return f"{session_id}:{hashlib.sha256(msg.encode()).hexdigest()[:16]}"


async def _persist_idempotent(key: str, task_id: str):
    """写幂等映射到 Redis（10 秒 TTL）；失败仅影响幂等复用，不影响主流程"""
    try:
        r = await _redis()
        await r.setex(f"{_IDEMPOTENT_PREFIX}{key}", 10, task_id)
    except Exception as e:
        logger.debug(f"幂等映射写入失败: {e}")


# 占位值：表示同键请求正在创建任务（TTL 10s 自愈，创建方崩溃后自动过期）
_CREATING = "__creating__"


async def try_idempotent(session_id: str, msg: str) -> Optional[str]:
    """幂等检查：返回已有 task_id 或 None（保留供旧模块/单测使用；新代码用 claim_idempotency）"""
    key = _idempotent_key(session_id, msg)
    try:
        r = await _redis()
        task_id = await r.get(f"{_IDEMPOTENT_PREFIX}{key}")
    except Exception:
        return None
    if task_id:
        task = await get_task(task_id)
        if task and task["status"] in ("pending", "generating"):
            return task_id
    return None


async def claim_idempotency(session_id: str, msg: str, wait_s: float = 1.0) -> Optional[str]:
    """原子占位幂等键（SET NX，P1 #42 修复原 GET→创建→SETEX 检查后行动竞态）

    返回：None=占位成功（调用方继续创建任务）；其余=可复用的 task_id。
    旧实现两次请求都通过 GET 检查 → 各自建任务；现在首个请求原子占位，
    后续请求等待读取真实 task_id 复用。等待超时（创建方可能崩溃）按未占位
    处理退化为原并发语义，TTL 自动清理；Redis 异常同样返回 None（尽力而为）。
    """
    key = f"{_IDEMPOTENT_PREFIX}{_idempotent_key(session_id, msg)}"
    try:
        r = await _redis()
        claimed = await r.set(key, _CREATING, nx=True, ex=10)
        if claimed:
            return None
        deadline = time.time() + wait_s
        while time.time() < deadline:
            v = await r.get(key)
            v = v.decode() if isinstance(v, bytes) else v
            if v and v != _CREATING:
                task = await get_task(v)
                if task and task["status"] in ("pending", "generating"):
                    return v
                return None  # 旧任务已完结：放行走新建（save_idempotent 会覆盖映射）
            await asyncio.sleep(0.05)
        return None  # 创建方超时未写入：按未占位处理（TTL 自愈）
    except Exception:
        return None


# 释放占位（条件删除）：仅当仍是 __creating__ 才删，防止误删他人已写入的 task_id
_RELEASE_CLAIM_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


async def release_idempotency_claim(session_id: str, msg: str):
    """释放未成功创建任务的幂等占位（槽位满了/任务创建失败时恢复可重试）"""
    key = f"{_IDEMPOTENT_PREFIX}{_idempotent_key(session_id, msg)}"
    try:
        r = await _redis()
        await r.eval(_RELEASE_CLAIM_LUA, 1, key, _CREATING)
    except Exception as e:
        logger.debug(f"幂等占位释放失败（TTL 自愈）: {e}")


def save_idempotent(session_id: str, msg: str, task_id: str):
    """记录幂等映射（10 秒 TTL，Redis 存储；保持同步签名，调用方无需改）"""
    key = _idempotent_key(session_id, msg)
    try:
        # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
        spawn(_persist_idempotent(key, task_id), name="idempotent-persist")
    except RuntimeError:  # noqa: silent-except 豁免：无运行循环为预期路径
        pass  # 无运行循环（非 async 上下文）时静默跳过，幂等写入尽力而为
