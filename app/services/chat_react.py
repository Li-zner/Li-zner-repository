"""ReAct 循环：流式调用 / 工具并行执行 / 多 Agent 圆桌 / 消息回填 / max_steps 截断兜底

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import json
from typing import AsyncIterator, Dict, List, Optional

from ..core.config import DEEPSEEK_MODEL, TOOL_TIMEOUT
from ..core.logging import setup_logging
from ..core.stream_utils import dispatch_tool, sse
from ..core.safety_filter import get_filter
from .chat_stream_ctx import ChatStreamCtx, finalize_answer
from .chat_support import hide_reasoning, mark_key_result
from .reasoning_guard import sanitize_reasoning
from .llm_streaming import _spawn_drain_bill, record_token_usage, stream_llm_throttled
from .chat_fallback import fallback_chain

logger = setup_logging()
MAX_STEPS = 3


def _accumulate_tool_calls(tool_calls_index: Dict, delta: List[dict]) -> Dict:
    """合并同一 tool_call 的多个增量片段（id/name/arguments 按 index 拼装）"""
    for tc in delta:
        idx = tc.get("index")
        if idx is not None:
            if idx not in tool_calls_index:
                tool_calls_index[idx] = {
                    "id": tc.get("id", ""), "type": tc.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                }
            if tc.get("id"):
                tool_calls_index[idx]["id"] = tc["id"]
            if tc.get("function"):
                if tc["function"].get("name"):
                    tool_calls_index[idx]["function"]["name"] = tc["function"]["name"]
                if tc["function"].get("arguments"):
                    tool_calls_index[idx]["function"]["arguments"] += tc["function"]["arguments"]
    return tool_calls_index


def _frame_tool_calls(tool_calls_index: Dict) -> Optional[List[dict]]:
    """把 index 字典转成调用参数列表；无工具调用返回 None"""
    return [
        {
            "id": v["id"], "type": v["type"],
            "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]},
        }
        for v in tool_calls_index.values()
    ]


async def _stream_react(ctx: ChatStreamCtx, model_try: str, frame: Dict) -> AsyncIterator[str]:
    """单步流式消费；产出 SSE；frame 记录 {full_reasoning, full_content, tool_calls, usage, fallback, content_blocked}"""
    full_reasoning = ""
    full_content = ""
    has_tool_calls = False
    content_blocked = False
    sf = get_filter()
    tool_calls_index: Dict = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    _agen = stream_llm_throttled(
        ctx, ctx.api_key, model_try, ctx.messages,
        username=ctx.username, tools=ctx.tools, tool_choice="auto",
    )
    try:
        async for kind, payload in _agen:
            if kind == "usage":
                usage.update(payload)
            elif kind == "reasoning":
                if not hide_reasoning(ctx.persona_id, ctx.persona):
                    chunk = sanitize_reasoning(payload)
                    if chunk:
                        full_reasoning += chunk
                        yield sse("reasoning_chunk", chunk)
            elif kind == "answer":
                # 逐块 DFA 过滤：answer_chunk 此刻已实时发往前端，事后检查拦不回
                # （与 runner 任务路径同一标准；跨块词由收尾终检兜底）
                _chk = sf.check_stream(payload)
                if not _chk["safe"]:
                    full_content += sf.safe_message
                    yield sse("answer_chunk", sf.safe_message)
                    content_blocked = True
                    break
                full_content += payload
                yield sse("answer_chunk", payload)
            elif kind == "tool_calls":
                has_tool_calls = True
                _accumulate_tool_calls(tool_calls_index, payload["delta"])
        if full_reasoning:
            yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
    except Exception as e:
        logger.warning(f"DeepSeek API 调用失败 (流式): {e}")
        await mark_key_result(ctx.api_key, False)
        if full_content:
            ctx.partial_answer = full_content
        async for chunk in fallback_chain(ctx.req, ctx.username, cache_ctx=ctx.cache_ctx):
            yield chunk
        ctx.saved_normally = True
        ctx.finished = True
        frame["fallback"] = True
        return
    except (GeneratorExit, asyncio.CancelledError):
        # 客户端断连排空计费（2026-09-09 审查 P1 修复：断连漏扣费）：此刻上游流未关闭，
        # 排空任务接管消费拿真实 usage 补计费；不走上方 fallback（客户端已断开），
        # 原样上抛不吞取消
        _spawn_drain_bill(_agen, ctx.username, ctx.conv_id, "react-step",
                          fallback_text=full_content)
        raise
    frame["full_reasoning"] = full_reasoning
    frame["full_content"] = full_content
    frame["content_blocked"] = content_blocked
    frame["usage"] = usage
    frame["tool_calls"] = _frame_tool_calls(tool_calls_index) if (has_tool_calls and tool_calls_index) else None


async def _tool_arg_error(func_name: str) -> dict:
    """LLM 工具参数不是合法 JSON 时的占位结果（对齐 runner._tool_arg_error，P1 修复）"""
    return {"error": f"{func_name} 参数不是有效 JSON"}


async def _execute_react_tools(ctx: ChatStreamCtx, tool_calls: List[dict], frame: Dict) -> AsyncIterator[str]:
    """并行执行工具（wait_for 超时手动 cancel）；产出事件；frame 记录 {tool_results, law_mapping_hit}"""
    # P1 修复：arguments 的 json.loads 必须有防护——LLM 偶发产出非法 JSON 时，
    # 原实现异常会打穿 async generator，整条 SSE 流无 answer_complete 直接断流
    parsed: List[tuple] = []
    for tc in tool_calls:
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            # 非法 JSON 也必须占位进 parsed：tool_results 与 tool_calls 按下标对齐，
            # 跳过会让坏调用拿到别人的结果、末尾 tool_call 缺 tool 消息（下轮 LLM 请求 400）
            args = None
        parsed.append((func_name, args))
        yield f"data: {json.dumps({'type': 'tool_call', 'name': func_name, 'args': args if args is not None else {}})}\n\n"

    tasks = []
    for func_name, args in parsed:
        if args is None:
            tasks.append(_tool_arg_error(func_name))
        else:
            # 兜底查询统一用改写后的 user_query（与 chat_fast_paths:175 同一口径，
            # 2026-09-07 审查 P2：原先一个用 req.query 一个用 user_query，语义不一致）
            tasks.append(dispatch_tool(func_name, args, ctx.user_query, ctx.user_perms, ctx.username))
    _tool_task_list = [asyncio.create_task(coro) for coro in tasks]
    try:
        tool_results = await asyncio.wait_for(
            asyncio.gather(*_tool_task_list, return_exceptions=True), timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        for _t in _tool_task_list:
            if not _t.done():
                _t.cancel()
        logger.warning("工具并行调用超时，触发降级")
        yield sse("reasoning_chunk", '知识库查询超时，正在基于已有知识继续回答...')
        tool_results = [{"error": "工具查询超时", "fallback": True} for _ in tool_calls]

    for idx, result in enumerate(tool_results):
        if isinstance(result, Exception):
            result = {"error": str(result)}
        yield f"data: {json.dumps({'type': 'tool_result', 'index': idx, 'result': result})}\n\n"
    law_mapping_hit = None
    for _res in tool_results:
        if isinstance(_res, dict) and _res.get("mapping_hit"):
            law_mapping_hit = _res.get("message", "")
            break
    frame["tool_results"] = tool_results
    frame["law_mapping_hit"] = law_mapping_hit


def _build_tool_messages(tool_calls: List[dict], tool_results: List[dict]) -> List[dict]:
    """把工具结果拼装为 tool 角色消息（error → 友好降级文案）"""
    tool_messages = []
    for idx, result in enumerate(tool_results):
        if isinstance(result, Exception):
            result = {"error": str(result)}
        if isinstance(result, dict) and "error" in result:
            error_msg = result["error"]
            fallback_text = "获取数据失败了，可能服务暂时不可用。"
            if "未找到" in error_msg:
                fallback_text = "暂时没找到这个城市的数据，建议换个关键词试试"
            elif "超时" in error_msg:
                fallback_text = "查询有点慢，可能网络问题，请稍后再试"
            elif "Key" in error_msg or "授权" in error_msg:
                fallback_text = "服务配置正在更新，暂时无法使用，我试试其他方式帮你。"
            result = {"error": error_msg, "fallback_message": fallback_text, "success": False}
        tool_messages.append({
            "role": "tool", "tool_call_id": tool_calls[idx]["id"],
            "content": json.dumps(result, ensure_ascii=False),
        })
    return tool_messages


def _build_assistant_message(tool_calls: List[dict], full_content: str) -> dict:
    """构造 assistant 消息（含 tool_calls），供回填对话历史"""
    return {
        "role": "assistant", "content": full_content,
        "tool_calls": [
            {
                "id": v["id"], "type": v["type"],
                "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]},
            }
            for v in tool_calls
        ],
    }


async def _roundtable(ctx: ChatStreamCtx, tool_calls: List[dict], tool_results: List[dict], frame: Dict) -> AsyncIterator[str]:
    """多 Agent 圆桌讨论；产出 thought 事件；frame 记录 {discussion_summary}"""
    discussion_summary = ""
    try:
        from ..agents.orchestrator import AgentOrchestrator
        from ..agents.router import get_agent_names_for_orchestrator
        orch = AgentOrchestrator(ctx.req.query)
        agent_names = get_agent_names_for_orchestrator(ctx.matched_agents)
        if not agent_names:
            agent_names = ["query_weather", "query_hotel", "query_route", "query_food"]
        for idx, tc in enumerate(tool_calls):
            func_name = tc["function"]["name"]
            result = tool_results[idx] if idx < len(tool_results) else {"error": "missing result"}
            if isinstance(result, Exception):
                result = {"error": str(result)}
            if func_name in agent_names:
                if isinstance(result, dict) and "error" in result:
                    orch.add_tool_result(func_name, {}, error=str(result["error"]))
                else:
                    orch.add_tool_result(func_name, result)
        if orch.get_involved_agents() and len(agent_names) > 1:
            yield sse("thought", '专家们正在讨论分析...')
            discussion_summary = await orch.run(enable_phase2=False)
    except Exception as orch_err:
        logger.warning(f"多Agent讨论异常（降级为常规模式）: {orch_err}")
    frame["discussion_summary"] = discussion_summary


async def _inject_discussion(ctx: ChatStreamCtx, discussion_summary: str) -> AsyncIterator[str]:
    """把讨论摘要发到思考区并作为内部参考注入 system（禁止提及"专家/讨论"等词）"""
    if not discussion_summary:
        return
    for line in discussion_summary.split('\n'):
        if line.strip():
            yield sse("reasoning_chunk", line + "\n")
    yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
    ctx.messages.append({
        "role": "system",
        "content": (
            "以下是基于工具查询结果的专业分析，已作为内部参考提供给你。\n"
            "请**直接**使用这些信息来回答用户，形成一份完整、连贯、自然的回答。\n"
            "**禁止在回答中提及**「专家」「天气专家说」「据讨论」「根据分析」等词汇。\n"
            "不要引用任何专家意见，不要用「某某专家认为」的句式。\n"
            "用你自己的口吻，把这些信息整合成一段流畅的建议。\n\n"
            f"{discussion_summary}"
        ),
    })


async def _react_step(ctx: ChatStreamCtx, step: int) -> AsyncIterator[str]:
    """一步 ReAct：流式 → 若纯文本则收尾；否则工具执行/圆桌/回填后进入下一步"""
    frame: Dict = {}
    async for ev in _stream_react(ctx, DEEPSEEK_MODEL, frame):
        yield ev
    if frame.get("fallback"):
        return
    record_token_usage(frame["usage"], ctx.username, ctx.conv_id)

    # 流中被 DFA 拦截：直接以已发送的安全文本收尾（full_content = 安全前缀 + 安全文案），
    # 不再进入工具分支；被拦截内容不写缓存（避免把"安全文案"钉成该 query 的长期答案）
    if frame.get("content_blocked"):
        final_safe = frame["full_content"] or "抱歉，我暂时无法回答。"
        yield sse("answer_complete", final_safe) + "data: [DONE]\n\n"
        ctx.partial_answer = final_safe
        await finalize_answer(ctx, final_safe, write_cache=False)
        ctx.saved_normally = True
        ctx.finished = True
        return

    tool_calls = frame["tool_calls"]
    if not tool_calls:
        # 纯文本回答：内容已逐块流式发送，此处仅做 DFA 检查 + 完成标记
        final_safe = frame["full_content"] or "抱歉，我暂时无法回答。"
        sf = get_filter()
        if sf.contains_sensitive(final_safe):
            logger.warning(f"DFA 拦截响应")
            final_safe = sf.safe_message
            yield sse("answer_chunk", final_safe)
        yield sse("answer_complete", final_safe) + "data: [DONE]\n\n"
        ctx.partial_answer = final_safe
        await finalize_answer(ctx, final_safe)
        ctx.saved_normally = True
        ctx.finished = True
        return

    async for ev in _execute_react_tools(ctx, tool_calls, frame):
        yield ev
    tool_results, law_mapping_hit = frame.get("tool_results"), frame.get("law_mapping_hit")
    if law_mapping_hit:
        logger.info("法律映射表命中，跳过LLM生成，直接返回引导信息")
        yield sse("answer_complete", law_mapping_hit) + "data: [DONE]\n\n"
        await finalize_answer(ctx, law_mapping_hit)
        ctx.saved_normally = True
        ctx.finished = True
        return

    round_frame: Dict = {}
    async for ev in _roundtable(ctx, tool_calls, tool_results, round_frame):
        yield ev
    discussion_summary = round_frame.get("discussion_summary", "")

    ctx.messages.append(_build_assistant_message(tool_calls, frame["full_content"]))
    ctx.messages.extend(_build_tool_messages(tool_calls, tool_results))
    async for ev in _inject_discussion(ctx, discussion_summary):
        yield ev


async def react_loop(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """ReAct 主循环（max_steps）；耗尽后返回已有内容兜底"""
    yield sse("thought", '正在分析你的问题...')
    await ctx.mm.save_user_message({"role": "user", "content": ctx.req.query})
    for step in range(MAX_STEPS):
        async for ev in _react_step(ctx, step):
            yield ev
        if ctx.finished:
            return
    # 循环结束未返回（max_steps 耗尽）
    if not ctx.saved_normally:
        last_content = ""
        for m in reversed(ctx.messages):
            if m.get("role") == "assistant" and m.get("content"):
                last_content = m["content"]
                break
        if not last_content:
            last_content = "抱歉，我暂时无法完成完整的回答。"
        logger.warning(f"V2 循环达到最大步数，返回已有内容: {len(last_content)} chars")
        yield sse("answer_complete", last_content) + "data: [DONE]\n\n"
        ctx.partial_answer = last_content
        await finalize_answer(ctx, last_content, write_cache=False)
        ctx.saved_normally = True
        ctx.finished = True
