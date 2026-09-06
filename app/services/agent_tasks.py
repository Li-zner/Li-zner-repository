"""Agent 任务治理：创建/等待/取消/恢复 + 后台并发槽位上限

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
含 P1 #42：幂等改为 claim_idempotency 原子 SET NX 占位，杜绝 GET 后再创建的竞态。
"""
import asyncio
import json
import os
import time

from fastapi import HTTPException

from ..models.schemas import CreateTaskRequest
from ..core.task_manager import (
    create_task, get_task, update_status, set_cancelled,
    claim_idempotency, release_idempotency_claim, save_idempotent, read_accumulated_result,
)
from ..agents.runner import run_agent_task
from ..core.redis import get_redis
from ..core.quota import is_quota_exhausted
from ..core.logging import setup_logging

logger = setup_logging()

# ---------- 后台任务并发上限（P1 #18：防恶意用户无限创建任务拖垮 LLM/DB）----------
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "50"))
_task_active_count = 0
_task_count_lock = asyncio.Lock()


async def _try_reserve_task_slot() -> bool:
    """尝试预留一个后台任务并发槽位；已满返回 False（调用方返回 429）"""
    global _task_active_count
    async with _task_count_lock:
        if _task_active_count >= MAX_CONCURRENT_TASKS:
            return False
        _task_active_count += 1
        return True


def _task_conversation_id(task: dict) -> str:
    """从任务存储读取会话ID（兼容旧任务仍存 session_id 字段的情况）"""
    return task.get("conversation_id", "") or task.get("session_id", "")


def task_user_perms(current_user: dict) -> list | None:
    """知识库检索权限（P0 修复）：admin 不过滤（None），普通用户按权限组、缺省仅公开。

    与 SSE 路径 chat_stream_ctx 同规则。任务路径曾对 search_knowledge 硬编码
    permissions=None（不过滤语义），普通用户经任务端点可越权检索 VIP 知识块。
    """
    if current_user.get("role") == "admin":
        return None
    return current_user.get("permissions") or []


def ensure_task_access(task: dict, username: str, role: str) -> None:
    """任务归属校验：仅 owner 本人（或 admin）可读/取消/恢复。

    缺 owner 字段（部署切换瞬间的旧任务）一律拒绝非 admin 访问——
    任务 TTL 仅 1 小时，宁可误拒也不开跨用户泄露口子（P1）。
    用 404 而非 403，不向探测者泄露他人任务的存在性。
    """
    if role == "admin":
        return
    if task.get("owner") != username:
        raise HTTPException(status_code=404, detail="task not found")


async def _release_task_slot() -> None:
    """回收后台任务并发槽位（任务结束或启动失败时调用）"""
    global _task_active_count
    async with _task_count_lock:
        _task_active_count = max(0, _task_active_count - 1)


async def _launch_agent_task(**kwargs) -> None:
    """启动后台 Agent 任务，任务结束后自动释放并发槽位（防止计数泄漏）"""
    async def _wrapped() -> None:
        """任务协程包装：无论成败都释放并发槽位，防计数泄漏"""
        try:
            await run_agent_task(**kwargs)
        finally:
            await _release_task_slot()
    try:
        asyncio.create_task(_wrapped())
    except Exception:
        # 调度失败（事件循环关闭等极少数场景）：回收槽位避免泄漏
        await _release_task_slot()
        raise


async def create_agent_task(req: CreateTaskRequest, current_user: dict) -> dict:
    """创建 Agent 生成任务：幂等占位 → 并发槽位 → 建任务 → 后台启动

    幂等键含 username 防跨用户串号（P0 #5）；任务存储保留 persona_id/file_ids
    供 /resume 透传（P0 #6）。
    """
    username = current_user["username"]
    # GitHub 试用额度防御：任务端点也走 LLM，受限用户同样拦截（admin 豁免）
    if current_user.get("role") != "admin" and is_quota_exhausted(current_user):
        raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")

    # 幂等：同一 username+conversation + 相同消息 10秒内复用（P0 #5：键含 username 防跨用户串号；
    # P1 #42：SET NX 原子占位替代 GET 后再创建的竞态窗口）
    existing = await claim_idempotency(f"{username}:{req.conversation_id}", req.message)
    if existing:
        return {"task_id": existing, "idempotent": True}

    # 后台任务并发上限（P1 #18：防恶意用户无限创建任务）
    if not await _try_reserve_task_slot():
        # 槽位已满：释放刚才的幂等占位，避免悬空 __creating__ 让后续请求多等 1s
        await release_idempotency_claim(f"{username}:{req.conversation_id}", req.message)
        raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")

    try:
        task_id = await create_task(req.conversation_id, req.message, owner=username)
        # 在 task 存储中保留恢复所需字段（persona_id / file_ids），供 /resume 透传（P0 #6）
        _r = await get_redis()
        await _r.hset(f"task:{task_id}", mapping={
            "persona_id": req.persona_id or "",
            "file_ids": json.dumps(req.file_ids or []),
        })
        save_idempotent(f"{username}:{req.conversation_id}", req.message, task_id)

        # 启动后台任务（任务结束自动释放并发槽位）
        await _launch_agent_task(
            task_id=task_id, username=username, conversation_id=req.conversation_id,
            user_query=req.message,
            persona_id=req.persona_id, file_ids=req.file_ids, lang=req.lang,
            user_perms=task_user_perms(current_user),
        )
    except Exception:
        await release_idempotency_claim(f"{username}:{req.conversation_id}", req.message)
        await _release_task_slot()
        raise

    return {"task_id": task_id, "idempotent": False}


async def wait_task_result(task_id: str, task: dict, wait: int) -> dict:
    """查询/等待任务结果；wait=0 立即返回，wait>0 长轮询最多 60 秒（P0 #7）"""
    status = task["status"]
    # 已终结的状态直接返回（含 timeout，P2 C1）
    if status in ("completed", "cancelled", "error", "timeout"):
        result = await read_accumulated_result(task_id) or task.get("result", "")
        return {"status": status, "content": result, "conversation_id": _task_conversation_id(task)}

    max_wait = min(wait, 60) if wait and wait > 0 else 0
    if max_wait and status in ("pending", "generating"):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            task = await get_task(task_id)
            if not task:
                return {"status": "error", "content": "task not found"}
            st = task["status"]
            if st in ("completed", "cancelled", "error", "timeout"):  # P2 C1：timeout 视为终态
                result = await read_accumulated_result(task_id) or task.get("result", "")
                return {"status": st, "content": result, "conversation_id": _task_conversation_id(task)}
            # 还处于 generating，返回当前进度
            partial = await read_accumulated_result(task_id)
            if partial:
                return {"status": "generating", "content": partial, "conversation_id": _task_conversation_id(task)}

    # 超时或仍在生成中，返回进度
    partial = await read_accumulated_result(task_id) or ""
    return {"status": status, "content": partial, "conversation_id": _task_conversation_id(task)}


async def cancel_agent_task(task_id: str, task: dict) -> dict:
    """取消生成任务（置取消信号 + 标记 cancelled）"""
    if task["status"] in ("completed", "cancelled", "error"):
        return {"status": task["status"], "message": "任务已终结，无需取消"}
    set_cancelled(task_id)
    partial = await read_accumulated_result(task_id)
    await update_status(task_id, "cancelled", partial)
    return {"status": "cancelled", "message": "已取消"}


async def resume_agent_task(task_id: str, task: dict, username: str,
                            user_perms: list | None = []) -> dict:
    """重新生成（创建新任务，丢弃旧草稿）；仅已取消的任务可恢复

    user_perms 默认仅公开（fail-closed）；由路由层传 task_user_perms(current_user)。
    """
    if task["status"] != "cancelled":
        return {"error": "只有已取消的任务才能恢复", "status": task["status"]}

    # 后台任务并发上限（P1 #18）
    if not await _try_reserve_task_slot():
        raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")

    try:
        new_task_id = await create_task(_task_conversation_id(task), task["user_message"],
                                        owner=username)
        # 从原任务透传恢复字段（兼容旧任务未存字段的情况），并写入新任务 hash 供后续 resume 透传
        persona_id = task.get("persona_id") or ""
        file_ids_raw = task.get("file_ids") or ""
        file_ids = json.loads(file_ids_raw) if file_ids_raw else []
        _r = await get_redis()
        await _r.hset(f"task:{new_task_id}", mapping={
            "persona_id": persona_id,
            "file_ids": json.dumps(file_ids),
        })

        await _launch_agent_task(
            task_id=new_task_id, username=username, conversation_id=_task_conversation_id(task),
            user_query=task["user_message"],
            persona_id=persona_id, file_ids=file_ids, lang=task.get("lang", "zh"),
            user_perms=user_perms,
        )
    except Exception:
        await _release_task_slot()
        raise

    return {"task_id": new_task_id, "previous_task_id": task_id}
