"""流式编排核心：门禁组 + 意图路由 + 主流程 chat_generate

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
chat_generate 严格按原版顺序：
  civil_route_gate → serve_from_cache → rebuild_lock_gate → key → route_intent
  → law_mapping_gate → simple_fast_path → recommend_fast_path → react_loop
  → except 降级 fallback_chain；finally cleanup_stream。
"""
import asyncio
import json
import time
from typing import AsyncIterator

from ..models.schemas import ChatRequest
from ..core.config import DEEPSEEK_MODEL
from ..core.logging import setup_logging
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import sse
from ..middleware.rate_limit import release_concurrent
from .chat_stream_ctx import ChatStreamCtx, build_stream_ctx, finalize_answer
from .chat_support import get_deepseek_key
from .chat_fallback import fallback_chain
from .chat_fast_paths import simple_fast_path, recommend_fast_path
from .chat_react import react_loop

logger = setup_logging()


async def route_intent(ctx: ChatStreamCtx) -> None:
    """意图路由：先分类用户意图，只加载匹配工具；设置 ctx 的 agents/simple/recommend/tools"""
    from ..agents.router import classify_intent, get_tools_for_intent
    try:
        intent_result = await classify_intent(ctx.user_query, use_llm=False, username=ctx.username)
        matched_agents = intent_result.get("agents", [])
        is_simple = intent_result.get("is_simple", True)
        is_recommend = intent_result.get("is_recommend", False)
        logger.info(f"意图路由: agents={matched_agents}, simple={is_simple}, recommend={is_recommend}")
        if ctx.persona_id == "civil_code":
            matched_agents = ["search_knowledge"]
            is_simple = True
            is_recommend = False
            logger.info("民法典人格，强制意图=search_knowledge")
        if matched_agents:
            tools = get_tools_for_intent(matched_agents)
        else:
            tools = []
    except Exception as route_err:
        logger.warning(f"意图路由异常（降级为全部工具）: {route_err}")
        from ..agents.tool_definitions import ALL_TOOLS
        matched_agents, is_simple, is_recommend, tools = [], True, False, ALL_TOOLS
    ctx.matched_agents = matched_agents
    ctx.is_simple = is_simple
    ctx.is_recommend = is_recommend
    ctx.tools = tools


async def civil_route_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """前置路由裁决表（硬规则引擎）：civil 人格 adjudicate 领域，reject 直接返回"""
    if getattr(ctx.req, 'persona_id', None) != "civil_code":
        return
    try:
        from ..agents.routing_table import route_query
        _route = route_query(ctx.req.query)
        ctx.civil_route_match = _route.match_type
        if _route.action == "reject":
            logger.info(f"路由裁决: reject → {_route.domain} ({_route.match_type})")
            yield sse("answer_complete", _route.message) + "data: [DONE]\n\n"
            # 拒答也要收尾入历史：与 law_mapping_gate 同一标准，问答不丢上下文
            await finalize_answer(ctx, _route.message)
            ctx.saved_normally = True
            ctx.finished = True
            return
        if _route.action == "pass" and _route.mapped_query:
            ctx.civ_civil_mapped_query = _route.mapped_query
            logger.info(f"口语映射待用: {ctx.req.query[:30]}... → {_route.mapped_query[:60]}...")
    except Exception as _re:
        logger.warning(f"路由裁决异常（不影响正常流程）: {_re}")


async def serve_from_cache(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """语义缓存拦截（有文件上传时跳过）；命中/穿透占位直接返回"""
    if ctx.req.file_ids:
        return
    try:
        cached_response = await SemanticCache.get(ctx.req.query, cache_ctx=ctx.cache_ctx)
        if cached_response and await SemanticCache.is_empty(cached_response):
            logger.info("穿透占位命中，直接返回提示（不再打 LLM）")
            _busy_msg = "服务暂时繁忙，请稍后重试。" if getattr(ctx.req, 'lang', 'zh') != 'en' else "Service is busy, please try again later."
            yield sse("answer_chunk", _busy_msg)
            yield sse("answer_complete", _busy_msg) + "data: [DONE]\n\n"
            ctx.finished = True
            return
        if cached_response and not await SemanticCache.is_empty(cached_response):
            from ..core.safety_filter import get_filter
            sf = get_filter()
            if sf.contains_sensitive(cached_response):
                logger.warning("DFA 拦截语义缓存")
                cached_response = sf.safe_message
            logger.info("语义缓存命中")
            yield sse("answer_chunk", cached_response)
            yield sse("answer_complete", cached_response) + "data: [DONE]\n\n"
            ctx.finished = True
            return
    except Exception as cache_err:
        logger.warning(f"缓存查询失败，继续正常推理: {cache_err}")


async def rebuild_lock_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """防击穿（互斥重建）：同一 query 并发时只允许一个调用 LLM；无锁时等待后重读缓存"""
    if ctx.req.file_ids:
        return
    try:
        ctx.rebuild_lock = await SemanticCache.acquire_rebuild_lock(ctx.req.query, cache_ctx=ctx.cache_ctx)
        if ctx.rebuild_lock is None:
            await asyncio.sleep(0.3)
            _cached_retry = await SemanticCache.get(ctx.req.query, cache_ctx=ctx.cache_ctx)
            if _cached_retry and await SemanticCache.is_empty(_cached_retry):
                _busy_msg = "服务暂时繁忙，请稍后重试。" if getattr(ctx.req, 'lang', 'zh') != 'en' else "Service is busy, please try again later."
                yield sse("answer_chunk", _busy_msg)
                yield sse("answer_complete", _busy_msg) + "data: [DONE]\n\n"
                ctx.finished = True
                return
            if _cached_retry and not await SemanticCache.is_empty(_cached_retry):
                # 与 serve_from_cache 同一标准：缓存内容过 DFA 再返回
                # （修复前入库的旧缓存可能含未过滤内容）
                from ..core.safety_filter import get_filter
                _sf = get_filter()
                if _sf.contains_sensitive(_cached_retry):
                    logger.warning("DFA 拦截语义缓存（重读路径）")
                    _cached_retry = _sf.safe_message
                yield sse("answer_chunk", _cached_retry)
                yield sse("answer_complete", _cached_retry) + "data: [DONE]\n\n"
                ctx.finished = True
                return
    except Exception as _rebuild_err:
        logger.warning(f"防击穿锁获取失败（继续重建）: {_rebuild_err}")
        ctx.rebuild_lock = None

    if ctx.rebuild_lock:
        async def _renew_rebuild_loop() -> None:
            """后台续期防击穿锁：15s 一次；锁失效/被他人持有即停止"""
            while True:
                await asyncio.sleep(15)
                try:
                    if not await SemanticCache.renew_rebuild_lock(
                            ctx.req.query, ctx.rebuild_lock, cache_ctx=ctx.cache_ctx, ttl=45):
                        break
                except Exception:
                    break
        ctx.rebuild_renew_task = asyncio.create_task(_renew_rebuild_loop())


async def law_mapping_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """法律依据纠正映射表检查（优先于 LLM 调用；仅非强制白名单查询）"""
    if ctx.persona_id != "civil_code":
        return
    # route_query 已由 civil_route_gate 执行并存入 ctx.civil_route_match，不再重复跑一遍
    if ctx.civil_route_match == "forced_whitelist":
        logger.info("法律映射表跳过：强制白名单命中")
        return
    try:
        from ..agents.law_mapping import check_query as _check_law
        _law_match = _check_law(ctx.user_query)
        if _law_match:
            logger.info(f"法律映射表命中 #{_law_match['id']}: {_law_match['scenario']} → {_law_match['law']}")
            yield sse("answer_complete", _law_match['message']) + "data: [DONE]\n\n"
            await finalize_answer(ctx, _law_match['message'])
            ctx.saved_normally = True
            ctx.finished = True
            logger.info("法律映射表已拦截，返回引导信息")
            return
    except Exception as _le:
        logger.warning(f"法律映射表检查失败（不影响正常流程）: {_le}")


async def cleanup_stream(ctx: ChatStreamCtx) -> None:
    """finally 清理：停止锁续期任务 → 释放防击穿锁 → 释放并发槽位 → 断线保存"""
    if ctx.rebuild_renew_task:
        ctx.rebuild_renew_task.cancel()
    if ctx.rebuild_lock:
        try:
            await SemanticCache.release_rebuild_lock(ctx.req.query, ctx.rebuild_lock, cache_ctx=ctx.cache_ctx)
        except Exception as _rel_err:
            logger.warning(f"防击穿锁释放失败（TTL 自愈兜底）: {_rel_err}")
    await release_concurrent(ctx.username)
    if not ctx.saved_normally and ctx.mm and ctx.partial_answer and ctx.partial_answer != "抱歉，我暂时无法回答。":
        try:
            await ctx.mm.save_messages(
                {"role": "user", "content": ctx.req.query},
                {"role": "assistant", "content": ctx.partial_answer + "\n\n（内容不完整，连接已断开）"}
            )
            logger.info(f"断线保存: conv_id={ctx.conv_id}, 内容长度={len(ctx.partial_answer)}")
        except Exception as save_err:
            logger.warning(f"断线保存失败: {save_err}")


async def chat_generate(req: ChatRequest, current_user: dict, today: str) -> AsyncIterator[str]:
    """v2 流式对话主流程（SSE）；门禁/快速通道/ReAct 按原版顺序编排"""
    ctx = None
    try:
        # ctx 构建放入 try 内：若其抛异常（如 build_file_context / get_context 失败），
        # finally 仍需释放并发槽位/防击穿锁（与原版把整段置于 generate 的 try/finally 一致）。
        ctx = await build_stream_ctx(req, current_user, today)
        # 1. 前置路由裁决表
        async for ev in civil_route_gate(ctx):
            yield ev
            if ctx.finished:
                return
        # 2. 语义缓存拦截
        async for ev in serve_from_cache(ctx):
            yield ev
            if ctx.finished:
                return
        # 3. 防击穿锁
        async for ev in rebuild_lock_gate(ctx):
            yield ev
            if ctx.finished:
                return
        # 4. Key 检查
        ctx.api_key = await get_deepseek_key()
        if not ctx.api_key:
            logger.error("DeepSeek API Key 未设置，无法调用")
            yield sse("answer_complete", '服务配置不完整（API Key 缺失），请联系管理员。') + "data: [DONE]\n\n"
            return
        # 5. 意图路由（设置 ctx.matched_agents/is_simple/is_recommend/tools）
        await route_intent(ctx)
        # 6. 法律映射表检查
        async for ev in law_mapping_gate(ctx):
            yield ev
            if ctx.finished:
                return
        # 7. 简单任务快速通道
        if (ctx.is_simple and len(ctx.matched_agents) == 1) or (len(ctx.matched_agents) == 0):
            async for ev in simple_fast_path(ctx):
                yield ev
            if ctx.finished:
                return
        # 8. 推荐多 Agent 快速通道
        if ctx.is_recommend and len(ctx.matched_agents) >= 2:
            async for ev in recommend_fast_path(ctx):
                yield ev
            if ctx.finished:
                return
        # 9. ReAct 循环
        async for ev in react_loop(ctx):
            yield ev
    except Exception as e:
        logger.error(f"V2 未知异常，降级备胎: {e}", exc_info=True)
        try:
            async for chunk in fallback_chain(ctx.req, ctx.username, cache_ctx=ctx.cache_ctx):
                yield chunk
        except Exception as fb_err:
            logger.error(f"降级也失败: {fb_err}")
            yield sse("answer_complete", '服务暂时不可用，请稍后再试。') + "data: [DONE]\n\n"
    finally:
        if ctx is not None:
            await cleanup_stream(ctx)
        else:
            # 构建失败（ctx 未生成）：并发槽位仍须释放（ensure_chat_allowed 已在入口占用），
            # 与原版 generate 的 finally 保证一致，防 30s 槽位泄漏。
            await release_concurrent(current_user["username"])
