"""流式对话共享上下文与入口门禁

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
ChatStreamCtx 承载一次 /v2/chat/stream 请求的全程可变状态；各编排模块
（fast_paths / react / core）通过它共享上下文与改写字段。
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import HTTPException

from ..models.schemas import ChatRequest
from ..core.config import (
    DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL, SUMMARY_THRESHOLD,
    DAILY_REQUEST_LIMIT, DAILY_TOKEN_LIMIT,
)
from ..core.logging import setup_logging
from ..core.metrics import gateway_requests_total
from ..core.memory_manager import MemoryManager
from ..core.persona_manager import get_persona_manager
from ..core.quota import is_quota_exhausted
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import build_file_context
from ..core.redis import get_redis
from ..middleware.rate_limit import check_qps, check_concurrent, get_daily_usage, release_concurrent
from ..agents.memory import compress_message_history
from ..agents.orchestrator import build_shared_context
from .chat_support import lang_instruction, safe_format_prompt

logger = setup_logging()

# 历史压缩时保留的最近消息条数（与 SUMMARY_THRESHOLD 的 token 阈值构成双保险，P2 #23）
_HISTORY_COMPACT_MSGS = 6


@dataclass
class ChatStreamCtx:
    """一次 /v2/chat/stream 请求的共享可变状态"""
    req: ChatRequest
    username: str
    user_role: str
    user_perms: Optional[List[str]]
    today: str
    conv_id: str
    mm: MemoryManager
    user_profile: str
    cache_ctx: str
    lang_instr: str
    current_user: dict     # 原始 user 字典（供透传 / 权限判断）
    # ---- 以下由 build_stream_ctx 填充 ----
    persona: object = None
    persona_id: str = ""
    selected_model: str = DEEPSEEK_MODEL
    model_try_list: List[str] = field(default_factory=lambda: [DEEPSEEK_MODEL])
    messages: List[Dict] = field(default_factory=list)
    user_query: str = ""
    tools: List = field(default_factory=list)
    matched_agents: List[str] = field(default_factory=list)
    is_simple: bool = True
    is_recommend: bool = False
    api_key: str = ""
    today_str: str = ""
    # ---- 运行时可变状态 ----
    civ_civil_mapped_query: Optional[str] = None
    civil_route_match: Optional[str] = None
    rebuild_lock: Optional[str] = None
    rebuild_renew_task: Optional[asyncio.Task] = None
    partial_answer: str = ""
    saved_normally: bool = False
    finished: bool = False


async def ensure_chat_allowed(current_user: dict) -> str:
    """角色分级限流 + 并发控制 + 日配额 + 试用额度门禁；返回 today 日期串

    任一门槛不过抛 HTTPException（由路由层转响应）；通过后调用方负责
    在 finally 释放并发槽位（在 chat_generate 的 cleanup 中完成）。
    """
    username = current_user["username"]
    user_role = current_user.get("role", "user")
    today = datetime.now().strftime("%Y-%m-%d")

    if not await check_qps(username, user_role):
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    if not await check_concurrent(username, user_role):
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="并发请求过多，请稍后再试")

    usage = await get_daily_usage(username, today)
    limits = {"daily_req": DAILY_REQUEST_LIMIT, "daily_token": DAILY_TOKEN_LIMIT}
    if user_role == "admin":
        from ..middleware.rate_limit import _ROLE_LIMITS
        limits = _ROLE_LIMITS["admin"]
    if usage["request_count"] >= limits["daily_req"]:
        await release_concurrent(username)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日请求次数已达上限")
    if usage["token_sum"] >= limits["daily_token"]:
        await release_concurrent(username)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日 Token 消耗已达上限")
    if user_role != "admin" and is_quota_exhausted(current_user):
        await release_concurrent(username)
        raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")
    return today


def _build_system_prompt(req: ChatRequest, today_str: str):
    """解析人格与模型链，返回 (persona, persona_id, selected_model, model_try_list, system_content)"""
    pm = get_persona_manager()
    persona = pm.current
    persona_id = req.persona_id or pm.current_id
    if req.persona_id and req.persona_id != pm.current_id:
        pm.switch(req.persona_id)
        persona = pm.current
        persona_id = req.persona_id
    if persona:
        system_content = safe_format_prompt(persona.system_prompt, today=today_str, name=persona.name)
        selected_model = persona.model or DEEPSEEK_MODEL
    else:
        system_content = f"你是AI助手。今天是{today_str}。"
        selected_model = DEEPSEEK_MODEL
    model_try_list = [selected_model]
    if DEEPSEEK_FLASH_MODEL != selected_model:
        model_try_list.append(DEEPSEEK_FLASH_MODEL)
    return persona, persona_id, selected_model, model_try_list, system_content


async def _assemble_messages(req: ChatRequest, mm: MemoryManager, system_content: str) -> list:
    """组装 system/user 消息：人格 + 共享上下文 + 文件上下文 + 历史压缩 + 最新消息提示"""
    messages = [{"role": "system", "content": system_content}]
    file_context_str = await build_file_context(req)
    if file_context_str:
        messages.append({"role": "system", "content": file_context_str})
    history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
    history_dicts = compress_message_history(history_dicts, max_messages=_HISTORY_COMPACT_MSGS)
    for msg in history_dicts:
        messages.append(msg)
    messages.append({
        "role": "system",
        "content": "注意：请以用户最新的消息为准。如果用户改变了目的地、预算、人数等计划，立即按新信息回答，不要沿用旧信息。"
    })
    return messages


def _rewrite_user_query(req: ChatRequest, user_query: str, persona_id: str) -> str:
    """用户在提问中附城市语境 + 民法典非民事领域提示"""
    if req.user_location:
        loc_name = req.user_location.replace("市", "")
        if loc_name not in user_query:
            intent_keywords = ["天气", "酒店", "路线", "美食", "餐厅", "怎么去", "旅游"]
            if any(kw in user_query for kw in intent_keywords):
                user_query = f"{user_query}（我在{req.user_location}）"
    if persona_id == "civil_code":
        hints = {
            "七天无理由退货": "【提示：此问题受《消费者权益保护法》调整，不属于民法典。请引用《消费者权益保护法》回答，不要引用民法典。】",
            "退货": "【提示：退货问题受《消费者权益保护法》调整，请考虑适用该法。】",
            "假货": "【提示：假货问题受《消费者权益保护法》调整（假一赔三），请考虑适用该法。】",
            "被公司辞退": "【提示：劳动纠纷受《劳动合同法》调整，不属于民法典。】",
            "工伤": "【提示：工伤问题受《工伤保险条例》调整，不属于民法典。】",
        }
        for kw, hint in hints.items():
            if kw in user_query:
                user_query = hint + "\n" + user_query
                break
    return user_query


async def build_stream_ctx(req: ChatRequest, current_user: dict, today: str) -> ChatStreamCtx:
    """构建共享上下文：会话/画像/缓存上下文/人格/消息/用户查询改写"""
    username = current_user["username"]
    user_role = current_user.get("role", "user")
    user_perms = None if user_role == "admin" else (current_user.get("permissions") or [])

    lang_instr = lang_instruction(req)
    today_str = datetime.now().strftime("%Y年%m月%d日 %A")
    conv_id = req.conversation_id or f"conv_{username}_{int(time.time())}"
    mm = MemoryManager(username, conv_id)
    try:
        user_profile = await mm.get_profile()
    except Exception as _pf_err:
        logger.warning(f"画像加载失败（按无画像处理）: {_pf_err}")
        user_profile = ""
    _cache_ctx = SemanticCache.build_cache_ctx(
        getattr(req, "persona_id", "") or "", req.user_location or "", user_profile
    )

    ctx = ChatStreamCtx(
        req=req, username=username, user_role=user_role, user_perms=user_perms,
        today=today, conv_id=conv_id, mm=mm, user_profile=user_profile,
        cache_ctx=_cache_ctx, lang_instr=lang_instr, current_user=current_user,
        today_str=today_str,
    )

    persona, persona_id, selected_model, model_try_list, system_content = _build_system_prompt(req, today_str)
    ctx.persona, ctx.persona_id = persona, persona_id
    ctx.selected_model, ctx.model_try_list = selected_model, model_try_list

    if req.user_location:
        asyncio.create_task(mm.save_user_location(req.user_location))

    shared_context = build_shared_context(req.query, req.user_location or "", user_profile or "")
    system_content += f"\n{shared_context}"
    system_content += lang_instr

    ctx.messages = await _assemble_messages(req, mm, system_content)
    ctx.user_query = _rewrite_user_query(req, req.query, persona_id)
    return ctx


async def finalize_answer(ctx: ChatStreamCtx, answer: str, write_cache: bool = True) -> None:
    """保存对话并累计用量（generate 内多处 finalize 尾的统一封装）

    行为等价：save_messages + 可选写语义缓存 + update_daily_usage + inc_used_questions。
    write_cache=False 用于 max_steps 截断的不完整回答（Bug #2：残缺答案禁止入缓存）。
    """
    from ..middleware.rate_limit import update_daily_usage
    from ..core.quota import inc_used_questions
    await ctx.mm.save_messages(
        {"role": "user", "content": ctx.req.query},
        {"role": "assistant", "content": answer},
    )
    if write_cache:
        asyncio.create_task(SemanticCache.set(ctx.req.query, answer, cache_ctx=ctx.cache_ctx))
    await update_daily_usage(ctx.username, ctx.today, inc_request=1, inc_token=0)
    asyncio.create_task(inc_used_questions(ctx.username))