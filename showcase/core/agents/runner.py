"""
Agent 后台任务运行器 — 从任务管理器获取控制信号，执行完整的 Agent 逻辑，
将结果逐步写入 Redis（而非流式推送）。

被 v2 路由中的 asyncio.create_task 启动，独立于 HTTP 请求生命周期。
"""

import asyncio
import json
import os
import time
from datetime import datetime
import httpx

from ..core.logging import setup_logging
from ..core.config import (
    DEEPSEEK_API_BASE, DEEPSEEK_API_TIMEOUT,
    SUMMARY_THRESHOLD, TOOL_TIMEOUT, DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL,
    HTTP_TIMEOUT_LONG
)
from ..core.redis import get_redis
from ..core.memory_manager import MemoryManager
from ..core.task_manager import (
    update_status, append_result, read_accumulated_result,
    is_cancelled, set_cancelled, cleanup_event,
    get_cancel_event
)
from ..core.semantic_cache import SemanticCache
from .tools import fetch_weather_async
from .sub_agents import call_sub_agent
from .router import classify_intent, handle_simple_task
from .memory import compress_message_history
from .orchestrator import build_shared_context

logger = setup_logging()

# 推理内容过滤模式（从 v2.py 复制）
_INTERNAL_PATTERNS = [
    r'工具定义.*?(?:query_weather|query_hotel|query_route|query_food)',
    r'(?:query_weather|query_hotel|query_route|query_food).*?(?:工具|函数|tool)',
    r'(?:parameters|type|description|required).*?(?:object|string)',
    r'你是旅行规划总控助手', r'判断用户意图.*?天气.*?酒店.*?路线.*?美食',
    r'根据意图调用对应的工具', r'如果用户问题涉及多个方面',
    r'收到工具返回的JSON数据后', r'你必须记住用户之前提到的',
    r'如果工具返回的JSON中包含', r'语气稍微热情即可',
    r'思考.*?过程请使用中文进行推理', r'保留专业术语的英文原名',
    r'当用户询问出行路线时', r'除非用户明确说.*?驾车.*?开车.*?自驾',
    r'根据两地距离智能选择', r'mode 字段.*?智能选择',
    r'当用户说.*?我在XX.*?或告知位置', r'这仅用于确定出发地和天气查询',
    r'不要擅自将此位置用于酒店推荐', r'如果用户问天气但没有说城市',
    r'用户告知位置后，不要反问用户位置', r'role.*?tool.*?tool_call_id',
    r'## 基本信息', r'## 角色定位', r'## 交通出行规则',
    r'## 定位与位置处理规则', r'【用户当前位置】', r'【用户画像】',
    r'用户上传了以下文件', r'【用户上传文件:', r'【文件结束】',
    # ── 人格系统元数据 ──
    r'你是\{name\}', r'你的专长是', r'## 你的角色',
    r'## 核心规则', r'### 规则A', r'### 规则B', r'### 规则C',
    r'禁止反问', r'禁止编造', r'有工具必须用工具',
    r'## 综合回答规则', r'## 情绪感知.*?旅游推荐映射',
    r'### 情绪.*?旅游风格映射', r'### 情绪推荐规则',
    r'## 禁止拒绝规则', r'## 多 Agent 协作', r'## 输出格式',
    r'## 🚨 核心规则', r'请参考这些专业意见来回答',
    r'禁止在回答中提及', r'### 你的任务', r'## 用户问题',
    r'各位专家的初步分析意见', r'只输出JSON', r'【约束】',
    r'## 定位与位置处理', r'## 多领域支持规则',
]

import re


def _clean_reasoning(text: str) -> str:
    if not text:
        return text
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        skip = False
        for pat in _INTERNAL_PATTERNS:
            if re.search(pat, stripped, re.IGNORECASE):
                skip = True
                break
        if skip:
            continue
        if re.match(r'^\s*[{}\[\],]\s*$', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\{', stripped):
            continue
        if re.match(r'^\s*"[^"]*"\s*:\s*\[', stripped):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


async def run_agent_task(
    task_id: str,
    username: str,
    session_id: str,
    user_query: str,
    user_location: str = "",
    persona_id: str = "",
    file_ids=None,
):
    """
    后台运行 Agent，逐步写入 Redis。
    前端通过轮询 /v2/chat/tasks/{task_id}/result 获取进度。
    """
    cancel_ev = get_cancel_event(task_id)
    full_content_text = ""
    conv_id = session_id or f"conv_{username}_{int(time.time())}"
    mm = MemoryManager(username, conv_id)

    try:
        # ===== 状态 → generating =====
        await update_status(task_id, "generating")

        # ===== 1. 语义缓存拦截 =====
        try:
            cached = await SemanticCache.get(user_query)
            if cached:
                from ..core.safety_filter import get_filter
                sf = get_filter()
                if sf.contains_sensitive(cached):
                    cached = sf.safe_message
                # 分块写入 Redis
                for i in range(0, len(cached), 80):
                    chunk = cached[i:i+80]
                    await append_result(task_id, chunk)
                    if is_cancelled(task_id):
                        await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                        return
                await update_status(task_id, "completed", cached)
                await mm.save_user_message({"role": "user", "content": user_query})
                await mm.save_messages(
                    {"role": "user", "content": user_query},
                    {"role": "assistant", "content": cached}
                )
                return
        except Exception:
            pass

        # ===== 1.5 意图路由：简单任务 vs 复杂任务 =====
        intent = None
        try:
            intent = await classify_intent(user_query, use_llm=False)
            logger.info(f"🔀 意图分类: agents={intent['agents']}, simple={intent['is_simple']}, method={intent.get('method','?')}")

            if intent["is_simple"] and intent["agents"]:
                # 简单任务 → 直接调用单个 Agent，不走四 Agent 圆桌
                agent_name = intent["agents"][0]
                logger.info(f"⚡ 路由为简单任务: agent={agent_name}, task_id={task_id}")
                await handle_simple_task(
                    task_id=task_id,
                    username=username,
                    session_id=session_id,
                    user_query=user_query,
                    agent_name=agent_name,
                    user_location=user_location,
                    persona_id=persona_id,
                    file_ids=file_ids,
                )
                return
            elif not intent["agents"]:
                # 未匹配到任何工具 → 走纯 LLM 回答（不加工具调用）
                logger.info(f"💬 未匹配工具领域，走纯 LLM 回答: task_id={task_id}")
                # 继续往下走，主循环会处理纯文本回答
            else:
                logger.info(f"🧠 路由为复杂任务: agents={intent['agents']}, task_id={task_id}")
        except Exception as route_err:
            logger.warning(f"意图路由异常（降级为复杂任务）: {route_err}")

        # ===== 2. 准备 prompt =====
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
        if not deepseek_api_key:
            await update_status(task_id, "error", "DEEPSEEK_API_KEY 未设置")
            return

        from ..core.persona_manager import get_persona_manager
        pm = get_persona_manager()
        persona = pm.current
        if persona_id and persona_id != pm.current_id:
            pm.switch(persona_id)
            persona = pm.current

        today_str = datetime.now().strftime("%Y年%m月%d日 %A")
        system_content = persona.system_prompt.format(today=today_str, name=persona.name) if persona else f"你是AI助手。今天是{today_str}。"

        history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
        user_profile = await mm.get_profile()
        system_content += f"\n{build_shared_context(user_query, user_location, user_profile or '')}"
        history_dicts = compress_message_history(history_dicts, max_messages=6)

        messages = [{"role": "system", "content": system_content}]

        # 上传文件注入
        if file_ids:
            r = await get_redis()
            file_contents = []
            for fid in file_ids:
                content = await r.get(f"file:{fid}:content")
                meta_raw = await r.get(f"file:{fid}:meta")
                if content and meta_raw:
                    meta = json.loads(meta_raw)
                    file_contents.append(f"【用户上传文件: {meta['filename']}】\n{content}\n【文件结束】")
            if file_contents:
                messages.append({"role": "system", "content": "用户上传了以下文件，请根据文件内容回答用户的问题：\n\n" + "\n\n".join(file_contents)})

        for msg in history_dicts:
            messages.append(msg)

        # 注入定位
        final_query = user_query
        if user_location:
            loc_name = user_location.replace("市", "")
            if loc_name not in user_query:
                intent_kw = ["天气", "酒店", "路线", "美食", "餐厅", "怎么去", "到", "旅游", "玩"]
                if any(kw in user_query for kw in intent_kw):
                    final_query = f"{user_query}（我在{user_location}）"
        messages.append({"role": "user", "content": final_query})

        # ===== 根据意图路由结果，只加载匹配的工具 =====
        from ..agents.router import get_tools_for_intent, get_agent_names_for_orchestrator
        try:
            if intent and intent.get("agents"):
                tools = get_tools_for_intent(intent["agents"])
                logger.info(f"🔧 使用匹配工具: {len(tools)}个, agents={intent['agents']}")
            else:
                tools = []
        except Exception:
            from ..agents.tool_definitions import ALL_TOOLS as tools

        # 立即保存用户消息
        await mm.save_user_message({"role": "user", "content": user_query})

        # ===== 3. 主循环 (max_steps) =====
        max_steps = 3
        from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total

        for step in range(max_steps):
            if is_cancelled(task_id):
                await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                return

            full_reasoning = ""
            full_content = ""
            has_tool_calls = False
            tool_calls_index = {}
            _stream_usage = {"prompt_tokens": 0, "completion_tokens": 0}

            try:
                async with httpx.AsyncClient(timeout=DEEPSEEK_API_TIMEOUT) as client:
                    async with client.stream(
                        "POST", f"{DEEPSEEK_API_BASE}/chat/completions",
                        headers={"Authorization": f"Bearer {deepseek_api_key}", "Content-Type": "application/json"},
                        json={"model": DEEPSEEK_MODEL, "messages": messages, "tools": tools, "tool_choice": "auto", "stream": True},
                    ) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if is_cancelled(task_id):
                                await _finish_cancelled(task_id, await read_accumulated_result(task_id) + full_content)
                                return

                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                if "usage" in data:
                                    u = data["usage"]
                                    _stream_usage["prompt_tokens"] = u.get("prompt_tokens", 0) or 0
                                    _stream_usage["completion_tokens"] = u.get("completion_tokens", 0) or 0

                                if delta.get("reasoning_content"):
                                    full_reasoning += delta["reasoning_content"]

                                if delta.get("content"):
                                    chunk = delta["content"]
                                    full_content += chunk
                                    full_content_text += chunk
                                    # 每块写入 Redis，前端可轮询获取进度
                                    await append_result(task_id, chunk)

                                if delta.get("tool_calls"):
                                    has_tool_calls = True
                                    for tc in delta["tool_calls"]:
                                        idx = tc.get("index")
                                        if idx is not None:
                                            if idx not in tool_calls_index:
                                                tool_calls_index[idx] = {"id": tc.get("id", ""), "type": tc.get("type", "function"), "function": {"name": "", "arguments": ""}}
                                            if tc.get("id"):
                                                tool_calls_index[idx]["id"] = tc["id"]
                                            if tc.get("function"):
                                                if tc["function"].get("name"):
                                                    tool_calls_index[idx]["function"]["name"] = tc["function"]["name"]
                                                if tc["function"].get("arguments"):
                                                    tool_calls_index[idx]["function"]["arguments"] += tc["function"]["arguments"]
                            except json.JSONDecodeError:
                                pass

            except Exception as e:
                logger.warning(f"DeepSeek API 失败 (step {step}): {e}")
                if full_content:
                    partial_answer = full_content
                # 降级走 fallback
                async for chunk in _fallback_chain(user_query, username):
                    if is_cancelled(task_id):
                        await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                        return
                    await append_result(task_id, chunk)
                full_content = await read_accumulated_result(task_id)
                # Token 计量
                llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', status='fallback').inc()
                break

            # ---- Token 计量 ----
            pt = _stream_usage.get("prompt_tokens", 0)
            ct = _stream_usage.get("completion_tokens", 0)
            if pt or ct:
                llm_tokens_total.labels(type='input').inc(pt)
                llm_tokens_total.labels(type='output').inc(ct)
                llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', type='input').inc(pt)
                llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', type='output').inc(ct)
            llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='v2_task', status='success').inc()

            # 检查是否有工具调用
            if has_tool_calls and tool_calls_index:
                tool_calls_list = [
                    {"id": v["id"], "type": v["type"],
                     "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]}}
                    for v in tool_calls_index.values()
                ]
                # 执行工具
                tasks = []
                for tc in tool_calls_list:
                    func_name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"])
                    if func_name == "query_weather":
                        tasks.append(fetch_weather_async(args.get("city")))
                    elif func_name in ["query_hotel", "query_route", "query_food"]:
                        tasks.append(call_sub_agent(func_name, args, user_query))
                    else:
                        tasks.append(asyncio.sleep(0, result={"error": f"未知工具: {func_name}"}))

                try:
                    tool_results = await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=TOOL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning("工具超时，降级")
                    async for chunk in _fallback_chain(user_query, username):
                        if is_cancelled(task_id):
                            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                            return
                        await append_result(task_id, chunk)
                    break

                # 多 Agent 讨论（只用匹配到的 Agent）
                try:
                    from .orchestrator import AgentOrchestrator
                    orch = AgentOrchestrator(user_query, user_location)
                    # 只用匹配到的 Agent，不浪费未涉及的 Agent
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
                        discussion_summary = await orch.run(enable_phase2=False)
                    else:
                        discussion_summary = ""
                except Exception as orch_err:
                    logger.warning(f"多Agent讨论异常: {orch_err}")
                    discussion_summary = ""

                # 构建 tool messages
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
                    "content": full_content,
                    "tool_calls": tool_calls_list
                })
                messages.extend(tool_msgs)

                if discussion_summary:
                    messages.append({
                        "role": "system",
                        "content": f"以下是各领域专家对结果的讨论分析，请参考这些专业意见来回答用户：\n\n{discussion_summary}"
                    })
            else:
                # 纯文本回答，完成
                # DFA 检查
                final_answer = full_content or "抱歉，我暂时无法回答。"
                from ..core.safety_filter import get_filter
                sf = get_filter()
                if sf.contains_sensitive(final_answer):
                    final_answer = sf.safe_message
                    await append_result(task_id, f"\n\n[内容已过滤]")

                # 保存到记忆
                await mm.save_messages(
                    {"role": "user", "content": user_query},
                    {"role": "assistant", "content": final_answer}
                )
                asyncio.create_task(SemanticCache.set(user_query, final_answer))

                await update_status(task_id, "completed", final_answer)
                logger.info(f"✅ 任务完成: task_id={task_id}")
                cleanup_event(task_id)
                return

        # max_steps 耗尽
        final_text = await read_accumulated_result(task_id) or "抱歉，我暂时无法回答。"
        await update_status(task_id, "completed", final_text)
        cleanup_event(task_id)

    except asyncio.CancelledError:
        await _finish_cancelled(task_id, await read_accumulated_result(task_id))
    except Exception as e:
        logger.error(f"Agent 任务异常: {e}", exc_info=True)
        await update_status(task_id, "error", str(e))
        cleanup_event(task_id)


async def _finish_cancelled(task_id: str, partial: str):
    """标记任务为已取消，保存草稿"""
    await update_status(task_id, "cancelled", partial)
    cleanup_event(task_id)
    logger.info(f"⏹ 任务已取消: task_id={task_id}")


async def _fallback_chain(user_query: str, username: str):
    """降级链：仅 Flash（Dify 已移除）"""
    try:
        async for chunk in _fallback_flash(user_query, username):
            yield chunk
    except Exception:
        return


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
                        pass
