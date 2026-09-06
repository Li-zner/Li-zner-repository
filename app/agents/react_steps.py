"""
ReAct 步骤层 — 从 runner.py 拆出的单步执行原语（行为等价移动，2026-09-05）。

runner.run_agent_task 只做编排（缓存/锁/路由/收尾），本模块负责：
  - _consume_llm_stream : 单轮 LLM 流式消费（逐块 DFA 过滤 + 实时写 Redis）
  - _handle_tool_step   : 工具执行 + 多 Agent 讨论 + 工具消息回填
  - _stream_fallback    : 降级链输出
  - _run_discussion     : 圆桌讨论摘要
  - _finish_cancelled   : 取消收尾（runner 与本模块共用）
"""

import asyncio
import json
import os
import httpx

from ..core.logging import setup_logging
from ..core.config import (
    TOOL_TIMEOUT, DEEPSEEK_API_BASE, DEEPSEEK_MODEL,
    DEEPSEEK_FLASH_MODEL, HTTP_TIMEOUT_LONG,
)
from ..core.stream_utils import dispatch_tool, stream_llm
from ..core.task_manager import (
    update_status, append_result, read_accumulated_result,
    is_cancelled, cleanup_event,
)

logger = setup_logging()


async def _finish_cancelled(task_id: str, partial: str):
    """标记任务为已取消，保存草稿（runner 编排层与步骤层共用）"""
    await update_status(task_id, "cancelled", partial)
    cleanup_event(task_id)  # 2026-09-05 二次遍历修复：拆分时遗漏，取消路径事件对象滞留泄漏
    logger.info(f"任务已取消: task_id={task_id}")


async def _tool_arg_error(func_name: str) -> dict:
    """LLM 工具参数不是合法 JSON 时的占位结果：按工具失败处理，与真实工具错误走同一条兜底链。

    必须是协程：调用方把返回值直接 append 进 gather 的任务列表，
    同步 dict 会当场 TypeError 炸掉整个任务（2026-09-06 修复）。
    """
    return {"error": f"{func_name} 参数不是有效 JSON"}


async def _consume_llm_stream(task_id: str, api_key: str, messages: list, tools: list, sf) -> tuple:
    """消费一次 LLM 流式响应：内容逐块 DFA 过滤并实时写入 Redis（轮询端立即可见）。

    返回 (state, done)：state 含 content/tool_calls 聚合/usage/blocked；
    done=True 表示任务已因取消而终结，调用方直接返回。
    """
    state = {
        "content": "", "has_tool_calls": False,
        "tool_calls_index": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "blocked": False,
    }
    # 统一走 stream_llm（ReAct 路径保持模型默认 temperature）
    async for _ev in stream_llm(api_key, DEEPSEEK_MODEL, messages, tools=tools,
                                tool_choice="auto", temperature=None, max_tokens=None):
        if is_cancelled(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return state, True

        if _ev["type"] == "usage":
            state["usage"]["prompt_tokens"] = _ev["prompt_tokens"]
            state["usage"]["completion_tokens"] = _ev["completion_tokens"]
        elif _ev["type"] == "content":
            chunk = _ev["text"]
            # 逐块 DFA 过滤：内容块此刻已实时写入 Redis（轮询端立即可见），
            # 等最终检查再拦截已经晚了（对齐 _fallback_flash 的逐块检查行为）
            _chk = sf.check_stream(chunk)
            if not _chk["safe"]:
                state["content"] += sf.safe_message
                await append_result(task_id, sf.safe_message)
                state["blocked"] = True
                break
            state["content"] += chunk
            # 每块写入 Redis，前端可轮询获取进度
            await append_result(task_id, chunk)
        elif _ev["type"] == "tool_calls":
            state["has_tool_calls"] = True
            for tc in _ev["delta"]:
                idx = tc.get("index")
                if idx is None:
                    continue
                if idx not in state["tool_calls_index"]:
                    state["tool_calls_index"][idx] = {"id": tc.get("id", ""), "type": tc.get("type", "function"), "function": {"name": "", "arguments": ""}}
                if tc.get("id"):
                    state["tool_calls_index"][idx]["id"] = tc["id"]
                if tc.get("function"):
                    if tc["function"].get("name"):
                        state["tool_calls_index"][idx]["function"]["name"] = tc["function"]["name"]
                    if tc["function"].get("arguments"):
                        state["tool_calls_index"][idx]["function"]["arguments"] += tc["function"]["arguments"]
    return state, False


async def _run_discussion(user_query: str,
                          tool_calls_list: list, tool_results: list, intent) -> str:
    """多 Agent 圆桌讨论（只用匹配到的 Agent），返回讨论摘要；异常返回空串不中断任务。"""
    try:
        from .orchestrator import AgentOrchestrator
        from .router import get_agent_names_for_orchestrator
        orch = AgentOrchestrator(user_query)
        matched_agents = intent.get("agents", []) if intent else []
        agent_names = get_agent_names_for_orchestrator(matched_agents)
        if not agent_names:
            agent_names = ["query_weather", "query_hotel", "query_route", "query_food"]
        for idx, tc in enumerate(tool_calls_list):
            func_name = tc["function"]["name"]
            result = tool_results[idx] if idx < len(tool_results) else {"error": "missing"}
            if isinstance(result, Exception):
                result = {"error": str(result)}
            if func_name in agent_names:
                if isinstance(result, dict) and "error" in result:
                    orch.add_tool_result(func_name, {}, error=str(result["error"]))
                else:
                    orch.add_tool_result(func_name, result)
        if orch.get_involved_agents() and len(agent_names) > 1:
            return await orch.run(enable_phase2=False)
        return ""
    except Exception as orch_err:
        logger.warning(f"多Agent讨论异常: {orch_err}")
        return ""


async def _handle_tool_step(task_id: str, username: str, user_query: str,
                            assistant_content: str, messages: list, tool_calls_list: list,
                            intent, user_perms) -> str:
    """执行本轮工具调用并按结果续写对话（工具消息 + 多 Agent 讨论）。

    返回 "ok"（继续下一轮）/ "cancelled"（降级中任务已取消）/ "timeout"（工具超时已降级，
    调用方应结束循环走 max_steps 收尾）。
    """
    tasks = []
    for tc in tool_calls_list:
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            # LLM 偶发产出非法 JSON 参数：按工具失败处理，而不是炸掉整个任务
            tasks.append(_tool_arg_error(func_name))
        else:
            tasks.append(dispatch_tool(func_name, args, user_query, user_perms, username))

    try:
        tool_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TOOL_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning("工具超时，降级")
        if await _stream_fallback(task_id, user_query, username):
            return "cancelled"
        return "timeout"

    discussion_summary = await _run_discussion(
        user_query, tool_calls_list, tool_results, intent)

    # 工具结果喂回模型：错误工具换成友好兜底文案，不把原始报错透给用户
    tool_msgs = []
    for idx, result in enumerate(tool_results):
        if isinstance(result, Exception):
            result = {"error": str(result)}
        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            fb = "获取数据失败了，可能服务暂时不可用。"
            if "未找到" in err:
                fb = "暂时没找到这个城市的数据"
            elif "超时" in err:
                fb = "查询超时，请稍后再试"
            result = {"error": err, "fallback_message": fb, "success": False}
        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tool_calls_list[idx]["id"],
            "content": json.dumps(result, ensure_ascii=False)
        })

    messages.append({
        "role": "assistant",
        "content": assistant_content,
        "tool_calls": tool_calls_list
    })
    messages.extend(tool_msgs)
    if discussion_summary:
        messages.append({
            "role": "system",
            "content": f"以下是各领域专家对结果的讨论分析，请参考这些专业意见来回答用户：\n\n{discussion_summary}"
        })
    return "ok"


async def _stream_fallback(task_id: str, user_query: str, username: str) -> bool:
    """降级链输出到 Redis（主模型失败/工具超时时）。返回 True 表示降级中任务被取消（已终结）。"""
    async for chunk in _fallback_chain(user_query, username):
        if is_cancelled(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return True
        await append_result(task_id, chunk)
    return False


async def _fallback_chain(user_query: str, username: str):
    """降级链：仅 DeepSeek Flash；Flash 也失败则给友好兜底（P1 #37）"""
    yielded = False
    try:
        async for chunk in _fallback_flash(user_query, username):
            yielded = True
            yield chunk
    except Exception as e:
        # Flash 侧已带日志；链上剩余异常吞掉，交给下方 not-yielded 兜底文案（P2 修复：不再裸 pass）
        logger.debug(f"降级链异常: {e}")
    if not yielded:
        yield "服务繁忙，请稍后再试。"


async def _fallback_flash(user_query: str, username: str):
    """降级：DeepSeek Flash"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return
    from ..core.safety_filter import get_filter
    sf = get_filter()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_LONG) as client:
        async with client.stream(
            "POST", f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_FLASH_MODEL, "messages": [{"role": "user", "content": user_query}], "max_tokens": 1024},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        chunk = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if chunk:
                            result = sf.check_stream(chunk)
                            if not result['safe']:
                                yield sf.safe_message
                                return
                            yield chunk
                    except json.JSONDecodeError:
                        continue  # 非数据行/残包：跳过继续读流（P2 修复：不再裸 pass）
