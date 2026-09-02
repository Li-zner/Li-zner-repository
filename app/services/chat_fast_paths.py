"""快速通道：单 Agent 简单任务 + 推荐多 Agent（已知意图直接并行工具 + 单次 LLM 格式化）

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
含 as_completed 返回值回查修复（P0 #45）：原实现用 as_completed 产出的包装协程
回查 {Task: agent} 字典在 Python 3.11 必 KeyError，整条推荐通道从未真正跑通、
总静默降级到 ReAct；现改由每个子任务自行返回 (agent, result)。
"""
import asyncio
import json
from datetime import datetime
from typing import AsyncIterator, Dict, List, Tuple

from ..core.config import TOOL_TIMEOUT
from ..core.logging import setup_logging
from ..core.persona_manager import get_persona_manager
from ..core.stream_utils import dispatch_tool, build_file_context, sse
from .chat_stream_ctx import ChatStreamCtx, finalize_answer
from .chat_support import lang_instruction, safe_format_prompt
from .llm_streaming import answer_via_models, ensure_answer, apply_safety_filter

logger = setup_logging()

# 推荐通道仅旅游四工具参与（与原行为一致）
RECOMMEND_TOUR_TOOLS = ("query_weather", "query_hotel", "query_route", "query_food")


def build_simple_tool_args(agent_name: str, ctx: ChatStreamCtx) -> Tuple[Dict, str]:
    """构造简单任务工具参数，返回 (args, 状态提示文案)"""
    args: Dict = {}
    if agent_name == "query_weather":
        city = ""
        if not city:
            from ..core.constants import CITIES
            for c in CITIES:
                if c in ctx.user_query:
                    city = c
                    break
        if not city:
            city = ctx.req.user_location.replace("市", "") if ctx.req.user_location else ""
        if not city:
            city = ctx.user_query
        args["city"] = city
        return args, f"正在查询 {city} 天气..."
    if agent_name == "query_hotel":
        destination = ctx.req.user_location.replace("市", "") if ctx.req.user_location else ctx.user_query
        return {"destination": destination}, f"正在搜索 {destination} 酒店..."
    if agent_name == "query_route":
        departure = ctx.req.user_location or "当前位置"
        return {"departure": departure, "destination": ctx.user_query}, f"正在规划从 {departure} 到 {ctx.user_query} 的路线..."
    if agent_name == "query_food":
        destination = ctx.req.user_location.replace("市", "") if ctx.req.user_location else ctx.user_query
        return {"destination": destination}, f"正在搜索 {destination} 美食..."
    if agent_name == "search_knowledge":
        q = ctx.civ_civil_mapped_query or ctx.user_query
        logger.info(f"search_knowledge 查询: {q[:80]}... (原: {ctx.user_query[:30]}...)")
        return {"query": q}, "正在查询知识库..."
    if agent_name == "search_project_knowledge":
        logger.info(f"search_project_knowledge 查询: {ctx.user_query[:80]}...")
        return {"query": ctx.user_query}, "正在查询项目知识库..."
    return {}, "正在查询相关信息..."


def _fast_system_base(ctx: ChatStreamCtx) -> str:
    """快速通道共享的 system 前缀（人格 + 语言指令 + 城市 + civil 路由结论）"""
    _pm = get_persona_manager()
    _persona = _pm.current
    _today_str = datetime.now().strftime("%Y年%m月%d日 %A")
    _system = (
        safe_format_prompt(_persona.system_prompt, today=_today_str, name=_persona.name)
        if _persona else f"你是AI助手。今天是{_today_str}。"
    )
    _system += ctx.lang_instr
    if ctx.req.user_location:
        _system += f"\n用户当前所在城市：{ctx.req.user_location}。"
    if ctx.persona_id == "civil_code":
        _system += (
            f"\n\n### 路由裁决：此问题已被确认为民法典问题\n"
            f"即使该问题也涉及其他法律领域，你**必须**先回答《民法典》中的相关规定（引用具体法条），\n"
            f"然后才可简要提及也涉及其他法律。**不得**以「不属于民法典」为由拒绝回答。\n"
            f"这是最高优先级指令。"
        )
    return _system


async def _build_fast_system(ctx: ChatStreamCtx, agent_name: str, tool_result) -> str:
    """简单任务：在 system_base 上追加工具结果/无数据引导 + 上传文件上下文"""
    _system = _fast_system_base(ctx)
    _fc = await build_file_context(ctx.req)
    if _fc:
        _system += "\n\n" + _fc
    if tool_result is not None:
        _has_data = False
        if isinstance(tool_result, dict):
            _has_data = bool(tool_result.get('results')) or bool(tool_result.get('temperature')) or bool(tool_result.get('city'))
        elif isinstance(tool_result, str) and len(tool_result) > 10:
            _has_data = True
        if _has_data:
            _system += (
                f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
                f"## 核心规则\n"
                f"1. **禁止编造**：只能基于工具返回的数据回答，不能编造任何具体数据\n"
                f"2. **先推荐再追问**：如果用户是求推荐，第一句就直接给推荐方案\n"
                f"3. **交叉领域**（如果问题同时涉及民法典和其他法律）：先回答民法典部分，再简要提及其他法律\n"
                f"4. **禁止「让我先查询」**：第一句话必须是法条引用或直接答案\n\n"
                f"工具返回的数据：\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}"
            )
        else:
            _system += (
                f"\n\n工具 [{agent_name}] 未找到相关知识库中的对应内容。\n"
                f"## 规则（最高优先级）\n"
                f"1. **直接回答用户的问题**，基于你自己的法律知识\n"
                f"2. **第一句话就必须是法条引用或直接答案**，禁止任何铺垫\n"
                f"3. **绝对禁止**说「让我先查询」「我来查一下」「请稍等」「正在查询」等任何等待语\n"
                f"4. 引用法条时注明具体条、款、项，并说明这是基于你的法律知识\n"
                f"5. 如果不确定某一具体法条编号，可以说明「根据民法典相关规定」\n"
                f"6. 提示用户：回答仅供参考，不构成正式法律意见\n"
                f"7. **如果问题涉及多个法律领域**（如同时涉及民法典和治安管理处罚法），先回答民法典部分，再简要提及其他法律"
            )
    return _system


async def simple_fast_path(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """单 Agent 或纯 LLM：直接执行工具(如有) + 单次 LLM 格式化"""
    is_pure_chat = len(ctx.matched_agents) == 0
    agent_name = ctx.matched_agents[0] if not is_pure_chat else "chat"
    logger.info(f"简单任务快速通道: agent={agent_name}")
    yield sse("thought", '正在查询相关信息...' if not is_pure_chat else '正在思考...')
    try:
        tool_result = None
        args = {}
        if not is_pure_chat:
            args, _status = build_simple_tool_args(agent_name, ctx)
            yield sse("reasoning_chunk", _status)
            tool_result = await dispatch_tool(agent_name, args, ctx.user_query, ctx.req.user_location, ctx.user_perms)
            if isinstance(tool_result, Exception):
                tool_result = {"error": str(tool_result)}
            if agent_name == "search_knowledge":
                from ..core.audit import audit
                await audit(ctx.username, "kb_search", {"query": ctx.user_query[:200]})
            yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_call', 'name': agent_name, 'args': args})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': tool_result})}\n\n"

        _system_fast = await _build_fast_system(ctx, agent_name, tool_result)
        result_text = ""
        async for kind, text in answer_via_models(ctx, [{"role": "system", "content": _system_fast},
                                                         {"role": "user", "content": ctx.user_query}], "简单任务"):
            if kind == "reasoning":
                yield sse("reasoning_chunk", text)
            elif kind == "answer":
                result_text += text
                yield sse("answer_chunk", text)

        result_text = await ensure_answer(ctx, result_text,
            f"简单任务快速通道未返回有效回复: agent={agent_name}, models={ctx.model_try_list}")
        result_text = apply_safety_filter(result_text)
        yield sse("answer_complete", result_text) + "data: [DONE]\n\n"

        await finalize_answer(ctx, result_text)
        ctx.saved_normally = True
        ctx.finished = True
        logger.info(f"简单任务快速通道完成: agent={agent_name}")
        return
    except Exception as fast_err:
        logger.warning(f"简单任务快速通道失败，降级到流式模式: {fast_err}")
        # 降级：继续走流式 ReAct 循环（不置 finished，主编排接续）


async def _run_recommend_tools(ctx: ChatStreamCtx, tool_results: List[Tuple[str, dict]]) -> AsyncIterator[str]:
    """并行执行推荐通道的旅游四工具；as_completed 逐个完成立即输出事件"""
    async def _run_one(agent: str) -> tuple:
        """执行单个工具；异常归一为 {error} 并带回 agent 名（供结果归位）"""
        try:
            res = await dispatch_tool(agent, {}, ctx.user_query, ctx.req.user_location)
        except Exception as te:
            res = {"error": str(te)}
        return agent, res

    touring = [a for a in ctx.matched_agents if a in RECOMMEND_TOUR_TOOLS]
    tasks = [asyncio.create_task(_run_one(agent)) for agent in touring]
    try:
        for done in asyncio.as_completed(tasks, timeout=TOOL_TIMEOUT):
            name, res = await done
            tool_results.append((name, res))
            yield f"data: {json.dumps({'type': 'tool_call', 'name': name, 'args': {}})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': res})}\n\n"
    except TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        for name in touring:
            if name not in {n for n, _ in tool_results}:
                tool_results.append((name, {"error": "tool timeout"}))
                yield f"data: {json.dumps({'type': 'tool_call', 'name': name, 'args': {}})}\n\n"
                yield f"data: {json.dumps({'type': 'tool_result', 'index': 0, 'result': {'error': 'tool timeout'}})}\n\n"


async def recommend_fast_path(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """推荐/多 Agent：已知意图 → 直接并行执行所有工具 → 单次 LLM 格式化"""
    logger.info(f"推荐/多Agent快速通道: agents={ctx.matched_agents}")
    yield sse("thought", '正在查询多个信息，请稍候...')
    try:
        tool_results: List[Tuple[str, dict]] = []
        async for ev in _run_recommend_tools(ctx, tool_results):
            yield ev

        _sys = _fast_system_base(ctx)
        _fc = await build_file_context(ctx.req)
        if _fc:
            _sys += "\n\n" + _fc
        _sys += "\n\n你查询到了以下信息，请整合成一份完整、自然、连贯的回答给用户。不要提及「专家」「查询工具」等词。\n\n"
        for name, res in tool_results:
            if isinstance(res, Exception):
                res = {"error": str(res)}
            _sys += f"--- {name} ---\n{json.dumps(res, ensure_ascii=False, indent=2)}\n\n"

        result_text = ""
        async for kind, text in answer_via_models(ctx, [{"role": "system", "content": _sys},
                                                         {"role": "user", "content": ctx.user_query}], "多Agent"):
            if kind == "reasoning":
                yield sse("reasoning_chunk", text)
            elif kind == "answer":
                result_text += text
                yield sse("answer_chunk", text)

        result_text = await ensure_answer(ctx, result_text,
            f"多Agent快速通道未返回有效回复: agents={ctx.matched_agents}, models={ctx.model_try_list}")
        result_text = apply_safety_filter(result_text)
        yield sse("answer_complete", result_text) + "data: [DONE]\n\n"

        await finalize_answer(ctx, result_text)
        ctx.saved_normally = True
        ctx.finished = True
        logger.info(f"多Agent快速通道完成: {len(tool_results)}个工具")
        return
    except Exception as fast_err:
        logger.warning(f"多Agent快速通道失败，降级到流式模式: {fast_err}")
