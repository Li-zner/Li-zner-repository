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
    # 惰性超时：pending/generating 超时 → CAS 写回真实终态 timeout 并设取消
    # 标记（生成实例下一检查点退出）。2026-09-12 修复（外部复核 P1）：原实现只在
    # 返回对象上标记不回写——后台任务继续运行并可能晚到写 completed。
    # CORE-1（2026-09-20 审查）：本惰性检查只覆盖"有人在轮询"的路径，
    # 无人轮询时由生成侧 timeout_deadline 独立到点终结（同一 mark_task_timeout）。
    if result.get("status") in ("pending", "generating"):
        age = _task_age_seconds(result)
        if age is not None and age > TASK_TIMEOUT:
            # 终结动作本身会再走两次 Redis（hgetall + eval）。2026-09-22 审阅 P2：
            # 这块从 try 里移出来之后，抖动一次就让轮询接口 500——前端看到的不是
            # "还没好"而是报错。超时终结失败按"不终结"处理：返回已读到的旧快照，
            # 状态未知不等于状态错误，下一轮轮询与生成侧闹钟都会再试一次。
            try:
                if await mark_task_timeout(task_id):
                    result["status"] = "timeout"
                    return result
                # CAS 失败说明其他实例已写终态；必须重读真实状态，
                # 不能把返回对象强行标成 timeout 欺骗调用方。
                fresh = await r.hgetall(f"task:{task_id}")
            except Exception as e:
                logger.warning(f"任务超时终结失败（按未终结返回旧快照）: {e}")
                return result
            if fresh:
                result = {
                    k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in fresh.items()
                }
    return result


def _task_age_seconds(result: dict) -> Optional[float]:
    """任务创建至今秒数；created_at 缺失/不可解析返回 None（按不超时处理）。"""
    try:
        _dt = datetime.fromisoformat(result.get("created_at") or "")
    except (TypeError, ValueError) as e:
        logger.debug(f"任务超时检查时间解析失败: {e}")
        return None
    if _dt.tzinfo is None:
        _dt = _dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - _dt).total_seconds()


async def mark_task_timeout(task_id: str) -> bool:
    """仍活跃的任务 CAS 终结为 timeout 并设跨实例取消标记。

    get_task 惰性检查与 CORE-1 生成侧定时截止共用此入口；
    已终态/不存在/CAS 输都返回 False（无副作用，可安全重复调用）。
    """
    r = await _redis()
    data = await r.hgetall(f"task:{task_id}")
    if not data:
        return False
    result = {k.decode() if isinstance(k, bytes) else k:
              v.decode() if isinstance(v, bytes) else v
              for k, v in data.items()}
    if result.get("status") not in ("pending", "generating"):
        return False
    ok = await r.eval(
        _TIMEOUT_TASK_LUA, 1, f"task:{task_id}",
        "timeout", result.get("result") or "",
        datetime.now(timezone.utc).isoformat(), str(TASK_TTL),
    )
    if ok != 1:
        return False
    # 设跨实例取消标记：生成实例在下一检查点（usage/边界）退出
    await r.set(_cancel_key(task_id), "timeout", ex=TASK_TTL)
    logger.warning(f"任务超时已终结: task_id={task_id}")
    return True


async def timeout_deadline(task_id: str, delay_s: float) -> None:
    """CORE-1（2026-09-20 审查）：客户端断轮询后生成协程不再有截止点，
    僵尸生成继续吃 token 到自然结束。生成侧进 generating 后挂本协程：
    到点无条件走 mark_task_timeout（早已完成则 CAS no-op）。"""
    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:  # noqa: silent-except — 任务正常收尾即取消本闹钟
        return
    try:
        await mark_task_timeout(task_id)
    except Exception as e:
        logger.warning(f"任务超时闹钟执行失败（轮询路径兜底）: {e}")


_TIMEOUT_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'pending' and status ~= 'generating' then return 0 end
redis.call('HSET', KEYS[1],
    'status', 'timeout',
    'result', ARGV[1],
    'updated_at', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


_START_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'pending' and status ~= 'generating' then return 0 end
if status == 'generating' and ARGV[1] == 'pending' then return 0 end
redis.call('HSET', KEYS[1],
    'status', ARGV[1],
    'result', ARGV[2],
    'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


async def update_status(task_id: str, status: str, result: str = "") -> bool:
    """更新任务状态和结果（CAS：仅 pending→generating 合法迁移）。

    2026-09-12 修复（外部复核 P0）：原实现只校验目标状态不校验当前状态——
    已取消/已完成的任务可被写回 generating，取消失效且继续消耗 token。
    本函数是 finish/fail/cancel 三个终态 CAS 之外的旁路，现同样按当前态
    Lua CAS 执行：终态一律拒绝回到 active。
    """
    if status not in ("pending", "generating"):
        raise ValueError(
            f"update_status 仅限 pending/generating，终态请走 finish/fail/cancel: {status}")
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    ok = await r.eval(
        _START_TASK_LUA, 1, f"task:{task_id}",
        status, result if result is not None else "", now, TASK_TTL,
    )
    return ok == 1


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


async def reset_accumulated_result(task_id: str) -> None:
    """清空结果缓冲，供降级重写完整答案时避免半截内容与降级内容拼接。"""
    r = await _redis()
    await r.delete(f"task:{task_id}:result_buf")


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
    """标记取消（仅本进程 Event，供生成所在实例快速感知）"""
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


async def _persist_idempotent(key: str, task_id: str) -> None:
    """写幂等映射到 Redis（10 秒 TTL）；失败仅影响幂等复用，不影响主流程"""
    try:
        r = await _redis()
        await r.setex(f"{_IDEMPOTENT_PREFIX}{key}", 10, task_id)
    except Exception as e:
        logger.debug(f"幂等映射写入失败: {e}")


# 占位值：表示同键请求正在创建任务（TTL 10s 自愈，创建方崩溃后自动过期）
_CREATING = "__creating__"

# 等待超时返回的冲突标记（2026-09-14 审计 P1）：创建方 wait_s 内未写入真实
# task_id——可能仍在建（建任务链路慢）或已崩溃。调用方应拒绝请求（409），
# 而不是并发再建第二个任务（那正是幂等占位要防的事）；占位键 TTL 自愈。
CLAIM_IN_PROGRESS = "__claim_in_progress__"


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


async def claim_idempotency(session_id: str, msg: str, wait_s: float = 1.0):
    """原子占位幂等键（SET NX，P1 #42 修复原 GET→创建→SETEX 检查后行动竞态）

    返回：None=占位成功（调用方继续创建任务）；字符串 task_id=可复用的已有任务；
    CLAIM_IN_PROGRESS=占位等待超时，创建方仍未写入真实 task_id（2026-09-14 审计 P1：
    原实现按"未占位"放行会并发再建第二个任务，改返回冲突标记由调用方拒绝）。
    Redis 异常仍返回 None（尽力而为，幂等是优化不是正确性依赖）。
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
                # 旧任务已终结时用 Lua 原子把映射从旧 task_id 替换为创建占位；
                # 多个并发请求只有一个能取得创建权，其余继续等待或返回冲突。
                replaced = await r.eval(
                    _REPLACE_TERMINAL_CLAIM_LUA,
                    2, key, f"task:{v}",
                    v, _CREATING, 10,
                )
                if replaced == 1:
                    return None
                await asyncio.sleep(0.05)
                continue
            await asyncio.sleep(0.05)
        return CLAIM_IN_PROGRESS  # 创建方超时未写入：明确冲突，不并发放行
    except Exception:
        return None


# 释放占位（条件删除）：仅当仍是 __creating__ 才删，防止误删他人已写入的 task_id
_RELEASE_CLAIM_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# 幂等映射指向终态任务时，原子把映射替换为新的创建占位。
_REPLACE_TERMINAL_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local status = redis.call('HGET', KEYS[2], 'status')
if status == 'pending' or status == 'generating' then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


_FINISH_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'pending' and status ~= 'generating' then return 0 end
redis.call('HSET', KEYS[1],
    'status', ARGV[1],
    'result', ARGV[2],
    'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""

_CANCEL_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'pending' and status ~= 'generating' then return 0 end
redis.call('HSET', KEYS[1],
    'status', 'cancelled',
    'result', ARGV[1],
    'updated_at', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


async def finish_task(task_id: str, result: str) -> bool:
    """仅 active 任务可写 completed，禁止晚到结果覆盖 cancelled/error/timeout。"""
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    ok = await r.eval(
        _FINISH_TASK_LUA, 1, f"task:{task_id}",
        "completed", result if result is not None else "", now, TASK_TTL,
    )
    return ok == 1


async def fail_task(task_id: str, result: str) -> bool:
    """仅 active 任务可写 error，避免异常晚到覆盖取消终态。"""
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    ok = await r.eval(
        _FINISH_TASK_LUA, 1, f"task:{task_id}",
        "error", result if result is not None else "", now, TASK_TTL,
    )
    return ok == 1


async def cancel_task_if_active(task_id: str, partial: str) -> bool:
    """仅 pending/generating 可转 cancelled，避免取消覆盖刚完成的终态。"""
    r = await _redis()
    now = datetime.now(timezone.utc).isoformat()
    ok = await r.eval(
        _CANCEL_TASK_LUA, 1, f"task:{task_id}",
        partial if partial is not None else "", now, TASK_TTL,
    )
    return ok == 1


def _cancel_key(task_id: str) -> str:
    return f"task:{task_id}:cancel"


async def request_cancel(task_id: str) -> bool:
    """写跨实例取消标记，返回是否写入成功。

    2026-09-12 修复（外部复核 P1）：写失败原来只告警——取消接口被当作成功但
    任务继续执行。现返回 False 供路由明确报错。不再 set 本进程 Event（取消
    请求可能落在非生成实例，本地 Event 对生成实例不可见且条目永不清理）。
    """
    try:
        r = await _redis()
        await r.set(_cancel_key(task_id), "1", ex=TASK_TTL)
        return True
    except Exception as e:
        logger.warning(f"跨实例取消标记写入失败: {e}")
        return False


async def is_cancelled_remote(task_id: str) -> bool:
    """检查取消信号：本进程事件优先，再查 Redis 跨实例标记。"""
    if is_cancelled(task_id):
        return True
    try:
        r = await _redis()
        return bool(await r.get(_cancel_key(task_id)))
    except Exception as e:
        logger.debug(f"取消标记读取失败，按未取消处理: {e}")
        return False


async def clear_cancel_marker(task_id: str) -> None:
    """任务收尾清理跨实例取消标记。"""
    try:
        r = await _redis()
        await r.delete(_cancel_key(task_id))
    except Exception as e:
        logger.debug(f"取消标记清理失败（TTL 自愈）: {e}")


async def release_idempotency_claim(session_id: str, msg: str):
    """释放未成功创建任务的幂等占位（槽位满了/任务创建失败时恢复可重试）"""
    key = f"{_IDEMPOTENT_PREFIX}{_idempotent_key(session_id, msg)}"
    try:
        r = await _redis()
        await r.eval(_RELEASE_CLAIM_LUA, 1, key, _CREATING)
    except Exception as e:
        logger.debug(f"幂等占位释放失败（TTL 自愈）: {e}")


async def save_idempotent(session_id: str, msg: str, task_id: str) -> None:
    """等待幂等映射写入 Redis，关闭并发请求的调度窗口。"""
    key = _idempotent_key(session_id, msg)
    await _persist_idempotent(key, task_id)
