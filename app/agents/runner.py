"""
Agent 后台任务运行器 — 编排层（2026-09-05 重构：步骤原语拆至 react_steps.py，行为等价）。

职责：从任务管理器获取控制信号，串起 语义缓存/重建锁 → 意图路由 → ReAct 主循环，
将结果逐步写入 Redis（而非流式推送）。被 v2 路由中的 asyncio.create_task 启动，
独立于 HTTP 请求生命周期。
"""

import asyncio
import os
import time
import uuid

from ..core.logging import setup_logging
from ..core.quota import inc_used_questions
from ..core.config import SUMMARY_THRESHOLD, DEEPSEEK_MODEL
from ..core.memory_manager import MemoryManager
from ..core.task_manager import (
    update_status, append_result, read_accumulated_result,
    is_cancelled, cleanup_event,
    get_cancel_event
)
from ..core.semantic_cache import SemanticCache, safe_set
from ..core.concurrency import spawn
from .router import classify_intent, handle_simple_task
from .react_steps import (
    _consume_llm_stream, _handle_tool_step, _stream_fallback, _finish_cancelled,
)
from .memory import compress_message_history
from .orchestrator import build_shared_context

logger = setup_logging()


async def _safe_inc_used_questions(username: str):
    """安全累加提问次数：失败记录日志不静默（P1 #35）"""
    try:
        await inc_used_questions(username)
    except Exception as e:
        logger.warning(f"提问次数累计失败: user={username}, err={e}")


async def _try_cache_hit(task_id: str, username: str, user_query: str, mm, cache_ctx: str = "") -> bool:
    """语义缓存拦截：命中则分块写入 Redis 并返回 True（调用方直接返回）"""
    try:
        cached = await SemanticCache.get(user_query, cache_ctx=cache_ctx)
        # 穿透占位（__EMPTY__）不是答案：SSE 路径写入的短 TTL 标记，
        # 任务路径必须放行走正常生成，否则字面 "__EMPTY__" 会发给用户
        if not cached or await SemanticCache.is_empty(cached):
            return False
        from ..core.safety_filter import get_filter
        sf = get_filter()
        if sf.contains_sensitive(cached):
            cached = sf.safe_message
        # 分块写入 Redis
        for i in range(0, len(cached), 80):
            chunk = cached[i:i + 80]
            await append_result(task_id, chunk)
            if is_cancelled(task_id):
                await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                return True
        await update_status(task_id, "completed", cached)
        # 与正常完成路径一致：命中即任务终结，清理取消事件防泄漏（P2 修复）
        cleanup_event(task_id)
        # 用户消息由 save_messages 统一保存（含去重），不再先 save_user_message 一遍（P2 修复冗余）
        await mm.save_messages(
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": cached}
        )
        return True
    except Exception:
        # 缓存查询失败不影响正常推理（放行走正常生成）
        return False


async def _wait_cache_or_busy(task_id: str, username: str, user_query: str, mm,
                              cache_ctx: str) -> tuple:
    """语义缓存拦截 + 防击穿重建锁（P0 #1/#32）。

    未命中时获取重建锁，仅一个请求重建；拿不到锁则等待重读缓存（最多 15 秒），
    仍拿不到说明别家持有锁且重建慢——不能在未持锁状态下重复调用 LLM 并写缓存
    （多个超时任务同时重建会击穿防击穿机制，缓存雪崩），返回降级完成（P1）。

    返回 (rebuild_lock_token, served)：served=True 表示任务已终结，调用方直接返回；
    token 需由调用方在 finally 释放（仅持有者释放，失败由 TTL 自愈）。
    """
    if await _try_cache_hit(task_id, username, user_query, mm, cache_ctx=cache_ctx):
        return None, True

    token = await SemanticCache.acquire_rebuild_lock(user_query, cache_ctx=cache_ctx, ttl=45)
    if token is not None:
        return token, False

    # 已有请求在重建：等待后重读缓存（最多 15 秒）
    for _ in range(30):
        await asyncio.sleep(0.5)
        if is_cancelled(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return None, True
        if await _try_cache_hit(task_id, username, user_query, mm, cache_ctx=cache_ctx):
            return None, True
    # 15 秒仍拿不到锁：降级完成而非并发重建（P1）
    token = await SemanticCache.acquire_rebuild_lock(user_query, cache_ctx=cache_ctx, ttl=45)
    if token is None:
        await update_status(task_id, "completed", "服务繁忙，请稍后再试。")
        cleanup_event(task_id)
        return None, True
    return token, False


async def _renew_until_done(user_query: str, token: str, cache_ctx: str):
    """周期续期重建锁（仅持有者可续，P0 #50 落地）：长任务重建期间锁不过期，防并发重复重建。

    对端返回 False（已不持有，如 TTL 曾过期被夺）即停止；由调用方 finally 取消本协程。
    """
    while True:
        await asyncio.sleep(15)  # TTL 45s，15s 续一次留足裕量
        try:
            if not await SemanticCache.renew_rebuild_lock(user_query, token, cache_ctx=cache_ctx):
                return
        except Exception as e:
            # 单次续期失败（网络抖动）不放弃持有权，下轮重试；最终由持有者释放/TTL 兜底
            logger.warning(f"重建锁续期失败（下轮重试）: {e}")


async def _maybe_simple_task(task_id: str, username: str, conversation_id: str,
                             user_query: str, persona_id: str,
                             file_ids, cache_ctx: str, lang: str, user_perms) -> tuple:
    """意图路由（步骤 1.5）：简单任务直接执行单个 Agent 并终结任务。

    返回 (handled, intent)：handled=True 调用方直接返回；复杂任务/纯 LLM 返回
    (False, intent) 继续主流程；路由异常降级为复杂任务不中断（intent=None）。
    """
    try:
        intent = await classify_intent(user_query, use_llm=False, username=username)
        logger.info(f"意图分类: agents={intent['agents']}, simple={intent['is_simple']}, method={intent.get('method','?')}")

        if intent["is_simple"] and intent["agents"]:
            # 简单任务 → 直接调用单个 Agent，不走四 Agent 圆桌
            agent_name = intent["agents"][0]
            logger.info(f"路由为简单任务: agent={agent_name}, task_id={task_id}")
            await handle_simple_task(
                task_id=task_id, username=username, conversation_id=conversation_id,
                user_query=user_query, agent_name=agent_name,
                persona_id=persona_id, file_ids=file_ids, cache_ctx=cache_ctx,
                lang=lang, user_perms=user_perms,
            )
            return True, intent
        elif not intent["agents"]:
            # 未匹配到任何工具 → 走纯 LLM 回答（不加工具调用），继续主流程
            logger.info(f"未匹配工具领域，走纯 LLM 回答: task_id={task_id}")
        else:
            logger.info(f"路由为复杂任务: agents={intent['agents']}, task_id={task_id}")
    except Exception as route_err:
        logger.warning(f"意图路由异常（降级为复杂任务）: {route_err}")
        return False, None
    return False, intent


async def _finish_text_answer(task_id: str, user_query: str, mm, full_content: str,
                              cache_ctx: str, sf) -> None:
    """纯文本回答收尾：跨块敏感词终检 → 保存记忆 → 写语义缓存 → 标记完成。"""
    # DFA 终检兜底：check_stream 逐块检查拦不住跨块拼接的敏感词，只有这里能拦
    # （ponytail: 跨块词此时已进轮询缓冲只能补记不能撤回；彻底解决需在 append 侧做跨块
    #  滑动窗口，敏感词表目前是整词命中，跨块概率极低，暂不值得）
    final_answer = full_content or "抱歉，我暂时无法回答。"
    if sf.contains_sensitive(final_answer):
        final_answer = sf.safe_message
        await append_result(task_id, "\n\n[内容已过滤]")

    # 保存到记忆
    await mm.save_messages(
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": final_answer}
    )
    asyncio.create_task(safe_set(user_query, final_answer, cache_ctx=cache_ctx))

    await update_status(task_id, "completed", final_answer)
    logger.info(f"任务完成: task_id={task_id}")
    cleanup_event(task_id)


async def _run_react_loop(task_id: str, username: str, user_query: str,
                          mm, messages: list, tools: list, intent, cache_ctx: str,
                          user_perms, api_key: str) -> None:
    """ReAct 主循环（步骤 3，最多 max_steps 轮）：LLM 流式 → 工具 → 再 LLM，直到纯文本回答。

    步骤原语（流式消费/工具执行/降级）在 react_steps.py；本函数只做轮次编排与计量。
    """
    from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
    from ..core.safety_filter import get_filter
    max_steps = 3

    for step in range(max_steps):
        if is_cancelled(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return

        # 上下文截断：防 messages 无限累积撑爆上下文窗口（P0 #60）
        messages = compress_message_history(messages, max_messages=10)
        sf = get_filter()

        try:
            state, done = await _consume_llm_stream(task_id, api_key, messages, tools, sf)
            if done:
                return
        except Exception as e:
            logger.warning(f"DeepSeek API 失败 (step {step}): {e}")
            # 降级走 fallback（Flash）；降级中被取消则任务已终结
            if await _stream_fallback(task_id, user_query, username):
                return
            llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', status='fallback').inc()
            break

        # ---- Token 计量 ----
        pt = state["usage"]["prompt_tokens"]
        ct = state["usage"]["completion_tokens"]
        if pt or ct:
            llm_tokens_total.labels(type='input').inc(pt)
            llm_tokens_total.labels(type='output').inc(ct)
            llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', type='input').inc(pt)
            llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', type='output').inc(ct)
        llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', status='success').inc()
        # 2026-09-09 审查 P0：任务路径原先只打指标，从不扣费也不计日 token
        # （"旁路漏扣费"历史教训同类）；指标已由上方 v2_task 打点，复用不含指标的
        # bill_token_usage 补齐钱包扣费与日 token 计数，避免双计
        from ..services.llm_streaming import bill_token_usage
        bill_token_usage(state["usage"], username, task_id,
                         remark=f"Agent任务消耗 {pt + ct} tokens（输入 {pt} + 输出 {ct}）")

        # 检查是否有工具调用（内容被 DFA 拦截时不进入工具分支，直接走完成路径）
        if state["has_tool_calls"] and state["tool_calls_index"] and not state["blocked"]:
            tool_calls_list = [
                {"id": v["id"], "type": v["type"],
                 "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]}}
                for v in state["tool_calls_index"].values()
            ]
            branch = await _handle_tool_step(
                task_id, username, user_query, state["content"],
                messages, tool_calls_list, intent, user_perms)
            if branch == "cancelled":
                return
            if branch == "timeout":
                break
        else:
            # 纯文本回答，完成
            await _finish_text_answer(task_id, user_query, mm, state["content"], cache_ctx, sf)
            return

    # max_steps 耗尽
    final_text = await read_accumulated_result(task_id) or "抱歉，我暂时无法回答。"
    await update_status(task_id, "completed", final_text)
    cleanup_event(task_id)


async def _build_cache_ctx(mm, persona_id: str) -> str:
    """语义缓存上下文（人格|位置|画像指纹），防个性化回答跨用户泄漏（Bug #1）"""
    try:
        _task_profile = await mm.get_profile()
    except Exception as _tp_err:
        logger.warning(f"任务画像加载失败（按无画像处理）: {_tp_err}")
        _task_profile = ""
    return SemanticCache.build_cache_ctx(persona_id, _task_profile)


async def run_agent_task(
    task_id: str,
    username: str,
    conversation_id: str,
    user_query: str,
    persona_id: str = "",
    file_ids=None,
    lang: str = "zh",
    # 知识库检索权限：默认仅公开（fail-closed）；None=不过滤只能由 admin 显式传入（P0 修复）。
    # 默认用不可变 () 而非 None/[]：[] 可变默认参是隐患，None 在 search_knowledge
    # 语义是"不过滤"（fail-open），省略调用会变成宽权限（2026-09-07 审查 P2，按语义修正）
    user_perms: list | tuple | None = (),
):
    """后台运行 Agent，逐步写入 Redis；前端轮询 /v2/chat/tasks/{task_id}/result 获取进度。
    编排：语义缓存/重建锁 → 意图路由 → 构建消息 → ReAct 主循环；步骤原语见 react_steps.py。"""
    cancel_ev = get_cancel_event(task_id)
    # uuid 后缀（与 chat_stream_ctx 同一修复）：秒级时间戳同秒并发任务共用会话
    conv_id = conversation_id or f"conv_{username}_{uuid.uuid4().hex[:12]}"
    mm = MemoryManager(username, conv_id)
    # 任务也算一次提问（GitHub 试用额度全局共享，所有助手）；spawn 持引用+异常记日志
    spawn(_safe_inc_used_questions(username), name=f"inc-q:{task_id}")
    _cache_ctx = await _build_cache_ctx(mm, persona_id)

    rebuild_lock_token, renew_task = None, None
    try:
        await update_status(task_id, "generating")

        # ===== 1. 语义缓存拦截 + 防击穿重建锁 =====
        rebuild_lock_token, served = await _wait_cache_or_busy(
            task_id, username, user_query, mm, _cache_ctx)
        if served:
            return

        # 持锁期间周期续期（P0 #50）：长任务重建中锁不过期，防并发重复重建
        renew_task = asyncio.create_task(
            _renew_until_done(user_query, rebuild_lock_token, _cache_ctx))

        # ===== 1.5 意图路由：简单任务直接执行后返回 =====
        handled, intent = await _maybe_simple_task(
            task_id, username, conversation_id, user_query,
            persona_id, file_ids, _cache_ctx, lang, user_perms)
        if handled:
            return

        # ===== 2. 准备 prompt =====
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
        if not deepseek_api_key:
            # 环境变量名不进任务状态（前端会原样展示）
            logger.error("DEEPSEEK_API_KEY 未设置")
            await update_status(task_id, "error", "服务配置不完整，请联系管理员")
            # 早退也清理取消事件，与异常路径一致（P2 修复：原先漏清理泄漏事件对象）
            cleanup_event(task_id)
            return

        messages, final_query, tools = await _build_task_messages(
            mm, username, user_query, persona_id, file_ids, intent, lang)

        # 立即保存用户消息
        await mm.save_user_message({"role": "user", "content": user_query})

        # ===== 3. ReAct 主循环 =====
        await _run_react_loop(task_id, username, user_query,
                              mm, messages, tools, intent, _cache_ctx,
                              user_perms, deepseek_api_key)

    except asyncio.CancelledError:
        await _finish_cancelled(task_id, await read_accumulated_result(task_id))
    except Exception as e:
        logger.error(f"Agent 任务异常: {e}", exc_info=True)
        from ..core.safety_filter import sanitize_error_text
        # 异常 str 可能带完整 URL/Key，剥指纹后再进任务状态（前端原样展示）
        await update_status(task_id, "error", sanitize_error_text(str(e)))
    finally:
        await _release_task_resources(task_id, renew_task, rebuild_lock_token, user_query, _cache_ctx)


async def _release_task_resources(task_id: str, renew_task, rebuild_lock_token,
                                  user_query: str, cache_ctx: str) -> None:
    """任务收尾统一清理：取消事件 + 续期协程 + 重建锁。

    取消事件统一在此清理（2026-09-07 审查 P2，对应 core/task_manager _cancel_events
    慢性泄漏项）：此前散落在各路径，异常/早退路径漏清会慢性泄漏（task_id 为 uuid
    不复用）；pop 幂等，与散落调用并存无害。
    """
    cleanup_event(task_id)
    # 取消续期协程；释放重建锁（仅持有者释放；失败由 TTL 自愈，P0 #1）
    if renew_task:
        renew_task.cancel()
    if rebuild_lock_token:
        try:
            await SemanticCache.release_rebuild_lock(user_query, rebuild_lock_token, cache_ctx=cache_ctx)
        except Exception as _rel_err:
            logger.warning(f"重建锁释放失败（由 TTL 自愈，不影响任务结果）: {_rel_err}")


async def _build_task_messages(mm, username: str, user_query: str,
                               persona_id: str, file_ids, intent, lang: str = "zh") -> tuple:
    """准备任务消息：人格 → 历史/画像 → 文件注入 → 工具加载 + 多语言指令。

    返回 (messages, final_query, tools)。
    """
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    # 按请求解析人格，不改全局 current（并发任务各用各的，互不覆盖）
    persona = pm.get_effective(persona_id)

    from ..core.constants import today_cn
    today_str = today_cn()
    system_content = persona.system_prompt.format(today=today_str, name=persona.name) if persona else f"你是AI助手。今天是{today_str}。"
    # 多语言指令：与流式路径 chat_stream_ctx 一致（P2：任务路径此前漏掉 lang）
    from ..services.chat_support import lang_instruction
    system_content += lang_instruction(lang)

    history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
    user_profile = await mm.get_profile()
    system_content += f"\n{build_shared_context(user_query, user_profile or '')}"
    history_dicts = compress_message_history(history_dicts, max_messages=6)

    messages = [{"role": "system", "content": system_content}]

    # 上传文件注入：统一走 build_file_context（含提示词注入消毒 + uploaded_by 属主校验，
    # 与流式路径同一标准；原内联实现绕过这两道防线，P1 修复）
    if file_ids:
        from types import SimpleNamespace
        from ..core.stream_utils import build_file_context
        file_ctx = await build_file_context(SimpleNamespace(file_ids=file_ids), username)
        if file_ctx:
            messages.append({"role": "system", "content": file_ctx})

    for msg in history_dicts:
        messages.append(msg)

    # 出发地功能已移除：不再往原话附"（我在X）"，final_query 恒为原话
    final_query = user_query
    messages.append({"role": "user", "content": final_query})
    # 根据意图路由结果，只加载匹配的工具
    from ..agents.router import get_tools_for_intent
    try:
        if intent and intent.get("agents"):
            tools = get_tools_for_intent(intent["agents"])
            logger.info(f"使用匹配工具: {len(tools)}个, agents={intent['agents']}")
        else:
            tools = []
    except Exception:
        from ..agents.tool_definitions import ALL_TOOLS as tools

    return messages, final_query, tools
